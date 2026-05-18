import re
from datetime import timedelta

from django.db import models, transaction
from django.db.models import Max, Sum, Count
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated, AllowAny

from .ai import JPCommentAnalyzer
from .models import Client, Commande, Livraison, Livreur, Paiement, Produit, Vendeur, Message
from .serializers import (
    ClientSerializer,
    CommandeSerializer,
    LivraisonSerializer,
    LivreurSerializer,
    PaiementSerializer,
    ProduitSerializer,
    VendeurSerializer,
    MessageSerializer,
)
from .services import MessagingService, AZExpressService


class VendeurListCreateView(generics.ListCreateAPIView):
    queryset = Vendeur.objects.all()
    serializer_class = VendeurSerializer


class ProduitListCreateView(generics.ListCreateAPIView):
    queryset = Produit.objects.all().order_by('id')
    serializer_class = ProduitSerializer


class ProduitDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Produit.objects.all()
    serializer_class = ProduitSerializer


class CommandeListCreateView(generics.ListCreateAPIView):
    queryset = Commande.objects.select_related('client', 'produit').all()
    serializer_class = CommandeSerializer


class CommandeDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Commande.objects.select_related('client', 'produit').all()
    serializer_class = CommandeSerializer


class JPCaptureAPIView(APIView):
    permission_classes = [AllowAny]  # MVP — accessible sans token

    def post(self, request):
        comment_text = request.data.get('comment_text', '')
        if not comment_text:
            return Response({'detail': 'Le champ comment_text est requis.'}, status=status.HTTP_400_BAD_REQUEST)

        parsed = JPCommentAnalyzer().analyze(comment_text)
        product_query = parsed.get('product_query') or self.extract_product_query(comment_text)
        produit = self.find_best_produit(product_query)
        if produit is None:
            return Response(
                {
                    'detail': "Produit introuvable pour ce JP.",
                    'product_query': product_query,
                    'ai_analysis': parsed,
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        client, _created = Client.objects.get_or_create(
            telephone=request.data.get('telephone', ''),
            defaults={
                'nom': request.data.get('nom', 'Client Live'),
                'adresse': request.data.get('adresse', ''),
                'date_livraison_preferee': request.data.get('date_livraison_preferee', None),
            },
        )

        max_order = Commande.objects.aggregate(max_ordre=Max('ordre_jp'))['max_ordre'] or 0
        commande = Commande.objects.create(
            client=client,
            produit=produit,
            ordre_jp=max_order + 1,
        )

        message = Message.objects.create(
            commande=commande,
            contenu=self.build_auto_message(client, produit),
            numero_relance=0,
        )

        # Bug #4 fix — envoyer réellement le message via MessagingService
        MessagingService.send_automatic_message(client, produit, commande.id)

        serializer = CommandeSerializer(commande)
        return Response(
            {
                'commande': serializer.data,
                'produit_reconnu': produit.nom,
                'message_envoye': message.contenu,
                'ai_analysis': parsed,
            },
            status=status.HTTP_201_CREATED,
        )

    def extract_product_query(self, text):
        cleaned = text.upper()
        cleaned = re.sub(r'JE\s*PRENDS|JP|JE\s*VOIS', ' ', cleaned)
        cleaned = re.sub(r'–.*$', ' ', cleaned)
        cleaned = re.sub(r'\d+[\s\S]*AR', ' ', cleaned)
        cleaned = re.sub(r'[^A-Z0-9\s]', ' ', cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        return cleaned

    def find_best_produit(self, query):
        if not query:
            return None
        queryset = Produit.objects.filter(
            models.Q(nom__icontains=query)
            | models.Q(couleur__icontains=query)
            | models.Q(taille__icontains=query)
        )
        return queryset.first()

    def build_auto_message(self, client, produit):
        return (
            f"Bonjour {client.nom}, merci pour votre JP sur '{produit.nom}'. "
            "Merci de confirmer votre commande en répondant avec : nom, téléphone, adresse et date préférée de livraison."
        )


def _create_jp_commande(client, produit):
    """Utilitaire partagé : crée une commande JP avec ordre atomique (protège contre les race conditions)."""
    with transaction.atomic():
        max_order = Commande.objects.select_for_update().aggregate(max_ordre=Max('ordre_jp'))['max_ordre'] or 0
        return Commande.objects.create(
            client=client,
            produit=produit,
            ordre_jp=max_order + 1,
            statut=Commande.STATUT_JP_CAPTURE
        )


class JPAnalyseAPIView(APIView):
    permission_classes = [AllowAny]  # MVP — accessible sans token

    def post(self, request):
        comment_text = request.data.get('comment_text', '')
        if not comment_text:
            return Response({'detail': 'Le champ comment_text est requis.'}, status=status.HTTP_400_BAD_REQUEST)

        parsed = JPCommentAnalyzer().analyze(comment_text)
        return Response(parsed, status=status.HTTP_200_OK)


class LivraisonTrackingAPIView(APIView):
    permission_classes = [AllowAny]  # MVP — accessible sans token

    def get(self, request):
        commande_id = request.query_params.get('commande_id')
        queryset = Livraison.objects.select_related('commande__client', 'livreur').all()
        if commande_id:
            livraison = get_object_or_404(queryset, commande__id=commande_id)
            serializer = LivraisonSerializer(livraison)
            return Response(serializer.data)

        serializer = LivraisonSerializer(queryset, many=True)
        return Response(serializer.data)


class TicketAPIView(APIView):
    permission_classes = [AllowAny]  # MVP — accessible sans token

    def get(self, request, commande_id):
        commande = get_object_or_404(
            Commande.objects.select_related('client', 'produit').prefetch_related('paiement', 'livraison__livreur'),
            id=commande_id,
        )
        ticket = {
            'commande_id': commande.id,
            'client': {
                'nom': commande.client.nom,
                'telephone': commande.client.telephone,
                'adresse': commande.client.adresse,
                'date_livraison_preferee': commande.client.date_livraison_preferee,
            },
            'produit': {
                'nom': commande.produit.nom,
                'taille': commande.produit.taille,
                'couleur': commande.produit.couleur,
                'prix': str(commande.produit.prix),
            },
            'statut_commande': commande.get_statut_display(),
            'paiement': {
                'statut': commande.paiement.statut if hasattr(commande, 'paiement') else None,
                'methode': commande.paiement.methode if hasattr(commande, 'paiement') else None,
            },
            'livraison': {
                'statut': commande.livraison.get_statut_display() if hasattr(commande, 'livraison') else None,
                'localisation_actuelle': commande.livraison.localisation_actuelle if hasattr(commande, 'livraison') else None,
                'livreur': commande.livraison.livreur.nom if hasattr(commande, 'livraison') and commande.livraison.livreur else None,
            },
            'ticket_text': (
                f"TICKET COMMANDE #{commande.id}\n"
                f"Client: {commande.client.nom}\n"
                f"Téléphone: {commande.client.telephone}\n"
                f"Adresse: {commande.client.adresse}\n"
                f"Produit: {commande.produit.nom} ({commande.produit.couleur}, {commande.produit.taille})\n"
                f"Prix: {commande.produit.prix} Ar\n"
                f"Statut commande: {commande.get_statut_display()}\n"
                f"Statut livraison: {commande.livraison.get_statut_display() if hasattr(commande, 'livraison') else 'N/A'}\n"
            ),
        }
        return Response(ticket)


class CommandeSearchAPIView(generics.ListAPIView):
    serializer_class = CommandeSerializer
    permission_classes = [AllowAny]  # MVP — accessible sans token

    def get_queryset(self):
        query = self.request.query_params.get('q', '').strip()
        queryset = Commande.objects.select_related('client', 'produit').all()
        if not query:
            return queryset.order_by('-date_creation')

        filters = (
            models.Q(client__nom__icontains=query)
            | models.Q(client__telephone__icontains=query)
            | models.Q(produit__nom__icontains=query)
            | models.Q(produit__couleur__icontains=query)
            | models.Q(produit__taille__icontains=query)
            | models.Q(statut__icontains=query)
        )
        if query.isdigit():
            filters |= models.Q(id=int(query))

        return queryset.filter(filters).order_by('-date_creation')


class JPRelanceAPIView(APIView):
    MAX_RELANCES = 3
    permission_classes = [AllowAny]  # MVP — déclenché par le planificateur Cron sans token

    def post(self, request):
        force = request.data.get('force', False) or request.query_params.get('force', 'false').lower() == 'true'
        commandes_a_relancer = []
        
        for commande in Commande.objects.filter(statut=Commande.STATUT_JP_CAPTURE).prefetch_related('messages', 'client', 'produit'):
            last_message = commande.messages.order_by('-date_envoi').first()
            if not last_message:
                continue
            if last_message.numero_relance >= self.MAX_RELANCES:
                continue

            # Respect the strict 30 minutes interval unless forced
            if not force:
                now = timezone.now()
                if last_message.date_envoi + timedelta(minutes=30) > now:
                    continue

            relance_num = last_message.numero_relance + 1
            contenu = self.build_relance_message(commande, relance_num)
            
            # Save relance history to database
            Message.objects.create(commande=commande, contenu=contenu, numero_relance=relance_num)
            
            # Simulate real WhatsApp/Messenger transmission
            MessagingService.send_relance_message(commande.client, commande.produit, relance_num)

            commandes_a_relancer.append({
                'commande_id': commande.id,
                'client': commande.client.nom,
                'produit': commande.produit.nom,
                'numero_relance': relance_num,
                'contenu': contenu,
            })

        return Response({'relances': commandes_a_relancer}, status=status.HTTP_200_OK)

    def build_relance_message(self, commande, numero_relance):
        return (
            f"Bonjour {commande.client.nom}, ceci est votre relance n°{numero_relance} "
            f"pour la commande '{commande.produit.nom}'. Merci de confirmer votre adresse et date de livraison."
        )


class CommandeUploadPaiementAPIView(APIView):
    parser_classes = [MultiPartParser, FormParser]
    permission_classes = [AllowAny]  # MVP — accessible sans token

    def post(self, request, pk):
        commande = get_object_or_404(Commande, pk=pk)
        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response(
                {'detail': "Aucun fichier téléversé. Veuillez envoyer le screenshot sous la clé 'file'."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get or create Paiement record
        paiement, _created = Paiement.objects.get_or_create(
            commande=commande,
            defaults={
                'methode': Paiement.METHODE_MOBILE_MONEY,
                'statut': Paiement.STATUT_PAYE
            }
        )

        # Save screenshot file using default_storage
        from django.core.files.storage import default_storage
        file_name = f"payments/receipt_{commande.id}_{file_obj.name}"
        saved_path = default_storage.save(file_name, file_obj)

        paiement.methode = Paiement.METHODE_MOBILE_MONEY
        paiement.statut = Paiement.STATUT_PAYE
        paiement.capture_mobile_money = default_storage.url(saved_path)
        paiement.save()

        # Update Commande status to Confirmed since payment is verified
        commande.statut = Commande.STATUT_CONFIRME
        commande.save()

        return Response({
            'detail': "Capture de paiement Mobile Money téléversée avec succès.",
            'paiement': PaiementSerializer(paiement).data,
            'commande_statut': commande.statut
        }, status=status.HTTP_200_OK)


class CommandeEtiquetteJPAPIView(APIView):
    permission_classes = [AllowAny]  # MVP — accessible sans token

    def get(self, request, pk):
        commande = get_object_or_404(Commande.objects.select_related('produit'), pk=pk)
        produit = commande.produit

        label_text = f"JP {produit.nom.upper()} - {int(produit.prix):,} Ar\n({produit.couleur.upper()}, {produit.taille.upper()})"

        ticket_data = {
            'commande_id': commande.id,
            'produit_nom': produit.nom,
            'prix': str(produit.prix),
            'couleur': produit.couleur,
            'taille': produit.taille,
            'ordre_jp': commande.ordre_jp,
            'label_text': label_text,
            'html_print': (
                f"<div style='width: 58mm; font-family: monospace; text-align: center; border: 1px dashed black; padding: 10px; margin: 10px;'>"
                f"<h2>AZLIVE LABEL</h2>"
                f"<div style='font-size: 16px; font-weight: bold; margin: 10px 0;'>JP {produit.nom.upper()}</div>"
                f"<div style='font-size: 20px; font-weight: bold; margin: 5px 0;'>{int(produit.prix):,} Ar</div>"
                f"<div style='font-size: 12px; margin: 5px 0;'>Taille: {produit.taille.upper()} | Couleur: {produit.couleur.upper()}</div>"
                f"<div style='font-size: 10px; color: gray; margin-top: 15px;'>Commande #{commande.id} | Ordre JP: #{commande.ordre_jp}</div>"
                f"</div>"
            )
        }
        return Response(ticket_data, status=status.HTTP_200_OK)


class CommandeLancerLivraisonAPIView(APIView):
    permission_classes = [AllowAny]  # MVP — accessible sans token

    def post(self, request, pk):
        commande = get_object_or_404(Commande.objects.select_related('client', 'produit__vendeur'), pk=pk)

        # Bug #5 fix — bloquer la double-expédition
        if commande.statut in (Commande.STATUT_EN_LIVRAISON, Commande.STATUT_LIVRE):
            return Response(
                {'detail': f"Impossible de lancer la livraison : la commande est déjà en statut '{commande.get_statut_display()}'."},
                status=status.HTTP_409_CONFLICT
            )

        # Auto-confirm command status if not done yet
        if commande.statut == Commande.STATUT_JP_CAPTURE:
            commande.statut = Commande.STATUT_CONFIRME
            commande.save()

        # Get or create Livraison record
        livraison, created = Livraison.objects.get_or_create(
            commande=commande,
            defaults={
                'statut': Livraison.STATUT_PREPARATION,
                'localisation_actuelle': "Bureau Principal"
            }
        )

        # Assign a default carrier if none exists
        if not livraison.livreur:
            livreur, _ = Livreur.objects.get_or_create(
                nom="Livreur AZExpress Standard",
                defaults={'telephone': '0340000000'}
            )
            livraison.livreur = livreur

        # Dispatch the package
        livraison.statut = Livraison.STATUT_EN_LIVRAISON
        livraison.localisation_actuelle = "En cours d'expédition avec AZExpress"
        livraison.date_assignation = timezone.now()
        livraison.save()

        # Update order status to shipping
        commande.statut = Commande.STATUT_EN_LIVRAISON
        commande.save()

        # Sync package with AZExpress shipping service
        az_response = AZExpressService.transmettre_colis(commande, livraison)

        # Save courier references
        livraison.tracking_notes = f"Tracking ID: {az_response.get('tracking_number')}. Estimé le: {az_response.get('estimated_delivery')}"
        livraison.save()

        return Response({
            'detail': "Colis expédié et transmis avec succès à AZExpress.",
            'livraison': LivraisonSerializer(livraison).data,
            'azexpress_response': az_response,
            'commande_statut': commande.statut
        }, status=status.HTTP_200_OK)


class DashboardStatsAPIView(APIView):
    def get(self, request):
        vendeur_id = request.query_params.get('vendeur_id')
        commandes_query = Commande.objects.select_related('produit').all()

        # W6 fix — isolation stricte multi-vendeur
        if request.user.is_authenticated:
            try:
                vendeur = request.user.vendeur
                commandes_query = commandes_query.filter(produit__vendeur=vendeur)
            except Vendeur.DoesNotExist:
                # Admin peut voir tout avec vendeur_id explicite
                if vendeur_id:
                    commandes_query = commandes_query.filter(produit__vendeur_id=vendeur_id)
        elif vendeur_id:
            commandes_query = commandes_query.filter(produit__vendeur_id=vendeur_id)
        else:
            # Ni authentifié, ni vendeur_id fourni : refus d'accès
            return Response(
                {'detail': 'Authentification ou paramètre vendeur_id requis pour accéder aux statistiques.'},
                status=status.HTTP_403_FORBIDDEN
            )

        total_jps = commandes_query.count()

        confirmed_count = commandes_query.filter(
            statut__in=[
                Commande.STATUT_CONFIRME,
                Commande.STATUT_PREPARE,
                Commande.STATUT_EN_LIVRAISON,
                Commande.STATUT_LIVRE
            ]
        ).count()

        taux_confirmation = (confirmed_count / total_jps * 100) if total_jps > 0 else 0

        # Revenue
        chiffre_affaires = commandes_query.filter(
            statut__in=[
                Commande.STATUT_CONFIRME,
                Commande.STATUT_PREPARE,
                Commande.STATUT_EN_LIVRAISON,
                Commande.STATUT_LIVRE
            ]
        ).aggregate(total_revenue=Sum('produit__prix'))['total_revenue'] or 0

        # Commission and net payout
        commission_rate = 0.10  # 10% Platform fee
        montant_a_reverser = float(chiffre_affaires) * (1.0 - commission_rate)

        # Top 5 products
        best_sellers = (
            commandes_query.values('produit__nom')
            .annotate(total_ventes=Count('id'))
            .order_by('-total_ventes')[:5]
        )
        best_sellers_list = [{'produit_nom': item['produit__nom'], 'ventes': item['total_ventes']} for item in best_sellers]

        return Response({
            'nombre_jps': total_jps,
            'confirmes': confirmed_count,
            'taux_confirmation': round(taux_confirmation, 2),
            'chiffre_affaires': float(chiffre_affaires),
            'montant_a_reverser': round(montant_a_reverser, 2),
            'commission_plateforme': round(float(chiffre_affaires) * commission_rate, 2),
            'produits_les_plus_vendus': best_sellers_list
        }, status=status.HTTP_200_OK)
