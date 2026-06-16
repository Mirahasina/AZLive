import re
from datetime import timedelta

from django.db import models, transaction
from django.db.models import Max, Sum, Count
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.permissions import IsAuthenticated, AllowAny

from .ai import JPCommentAnalyzer
from .models import Client, Commande, Livraison, Livreur, Paiement, Produit, ProduitImage, Vendeur, Message, Collaborateur, Live, Variante, PageFacebook, ParametresPlateforme
from .serializers import (
    ClientSerializer,
    CommandeSerializer,
    LivraisonSerializer,
    LivreurSerializer,
    PaiementSerializer,
    ProduitImageSerializer,
    ProduitSerializer,
    VendeurSerializer,
    MessageSerializer,
    CollaborateurSerializer,
    LiveSerializer,
    VarianteSerializer,
    PageFacebookSerializer,
)
from .services import MessagingService, AZExpressService


def _commande_variante(commande):
    if commande.variante_id:
        return commande.variante
    return commande.produit.variantes.order_by('id').first()


def _commande_variante_payload(commande):
    variante = _commande_variante(commande)
    if not variante:
        return {
            'taille': '',
            'couleur': '',
            'prix': '0',
            'code_jp': '',
        }
    return {
        'taille': variante.taille,
        'couleur': variante.couleur,
        'prix': str(variante.prix_unitaire),
        'code_jp': variante.code_jp,
    }


class VendeurListCreateView(generics.ListCreateAPIView):
    queryset = Vendeur.objects.all()
    serializer_class = VendeurSerializer


class ProduitListCreateView(generics.ListCreateAPIView):
    queryset = Produit.objects.select_related('vendeur').prefetch_related('variantes', 'images').all().order_by('id')
    serializer_class = ProduitSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]


class ProduitDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Produit.objects.select_related('vendeur').prefetch_related('variantes', 'images').all()
    serializer_class = ProduitSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]


class ProduitImageDeleteView(generics.DestroyAPIView):
    queryset = ProduitImage.objects.select_related('produit').all()
    serializer_class = ProduitImageSerializer

    def perform_destroy(self, instance):
        produit = instance.produit
        instance.delete()
        first = produit.images.order_by('created_at', 'id').first()
        produit.photo = first.image if first else None
        produit.save(update_fields=['photo'])


class CommandeListCreateView(generics.ListCreateAPIView):
    serializer_class = CommandeSerializer

    def get_queryset(self):
        queryset = Commande.objects.select_related('client', 'produit', 'variante', 'paiement', 'livraison').all()
        live_id = self.request.query_params.get('live_id')
        client_id = self.request.query_params.get('client_id')
        produit_id = self.request.query_params.get('produit_id')
        vendeur_id = self.request.query_params.get('vendeur_id')

        if live_id:
            queryset = queryset.filter(live_id=live_id)
        if client_id:
            queryset = queryset.filter(client_id=client_id)
        if produit_id:
            queryset = queryset.filter(produit_id=produit_id)
        if vendeur_id:
            queryset = queryset.filter(produit__vendeur_id=vendeur_id)

        return queryset


class CommandeDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Commande.objects.select_related('client', 'produit', 'variante').all()
    serializer_class = CommandeSerializer


class JPCaptureAPIView(APIView):
    permission_classes = [AllowAny]  # MVP — accessible sans token

    def post(self, request):
        comment_text = request.data.get('comment_text', '')
        if not comment_text:
            return Response({'detail': 'Le champ comment_text est requis.'}, status=status.HTTP_400_BAD_REQUEST)

        parsed = JPCommentAnalyzer().analyze(comment_text)
        product_query = parsed.get('product_query') or self.extract_product_query(comment_text)
        match = self.find_best_match(product_query, parsed.get('couleur'), parsed.get('taille'))
        if match is None:
            return Response(
                {
                    'detail': "Produit introuvable pour ce JP.",
                    'product_query': product_query,
                    'ai_analysis': parsed,
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        produit, variante = match

        client, _created = Client.objects.get_or_create(
            telephone=request.data.get('telephone', ''),
            defaults={
                'nom': request.data.get('nom', 'Client Live'),
                'adresse': request.data.get('adresse', ''),
                'date_livraison_preferee': request.data.get('date_livraison_preferee', None),
            },
        )

        max_order = Commande.objects.filter(produit=produit).aggregate(max_ordre=Max('ordre_jp'))['max_ordre'] or 0
        ordre_jp = max_order + 1
        commande = Commande.objects.create(
            client=client,
            produit=produit,
            variante=variante,
            ordre_jp=ordre_jp,
        )

        if ordre_jp == 1:
            message_content = self.build_auto_message(client, produit)
        else:
            message_content = (
                f"Salama {client.nom}, tafiditra ao anatin'ny lisitra miandry (liste d'attente) ho an'ny '{produit.nom}' ianao (Laharana faha-{ordre_jp}). "
                f"Hampilazainay ianao raha misy fahafahana avy amin'ireo nialoha anao."
            )

        message = Message.objects.create(
            commande=commande,
            contenu=message_content,
            numero_relance=0,
        )

        if ordre_jp == 1:
            MessagingService.send_automatic_message(client, produit, commande.id)
        else:
            MessagingService.send_waiting_list_message(client, produit, ordre_jp, commande.id)

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

    def find_best_match(self, query, couleur=None, taille=None):
        if not query:
            return None

        variante = Variante.objects.filter(code_jp__iexact=query.strip()).select_related('produit').first()
        if variante:
            return variante.produit, variante

        variante_qs = Variante.objects.select_related('produit').filter(
            models.Q(code_jp__icontains=query)
            | models.Q(produit__nom__icontains=query)
            | models.Q(couleur__icontains=query)
            | models.Q(taille__icontains=query)
        )
        if couleur:
            variante_qs = variante_qs.filter(couleur__icontains=couleur)
        if taille:
            variante_qs = variante_qs.filter(taille__icontains=taille)

        variante = variante_qs.first()
        if variante:
            return variante.produit, variante

        produit = Produit.objects.filter(nom__icontains=query).prefetch_related('variantes').first()
        if produit:
            first_variante = produit.variantes.order_by('id').first()
            return produit, first_variante
        return None

    def build_auto_message(self, client, produit):
        return (
            f"Salama {client.nom}, nahazo ny JP-nao amin'ny '{produit.nom}' izahay. "
            "Mba hafahao ny baikonao amin'ny alalan'ny fandefasana ny: anarana feno, finday, adiresy ary ny daty tianao hanaterana azy."
        )


def _create_jp_commande(client, produit, variante=None):
    """Utilitaire partagé : crée une commande JP avec ordre atomique (protège contre les race conditions)."""
    if variante is None:
        variante = produit.variantes.order_by('id').first()

    with transaction.atomic():
        max_order = Commande.objects.select_for_update().filter(produit=produit).aggregate(max_ordre=Max('ordre_jp'))['max_ordre'] or 0
        ordre_jp = max_order + 1
        commande = Commande.objects.create(
            client=client,
            produit=produit,
            variante=variante,
            ordre_jp=ordre_jp,
            statut=Commande.STATUT_JP_CAPTURE
        )

        if ordre_jp == 1:
            message_content = (
                f"Salama {client.nom}, nahazo ny JP-nao amin'ny '{produit.nom}' izahay. "
                "Mba hafahao ny baikonao amin'ny alalan'ny fandefasana ny: anarana feno, finday, adiresy ary ny daty tianao hanaterana azy."
            )
            MessagingService.send_automatic_message(client, produit, commande.id)
        else:
            message_content = (
                f"Salama {client.nom}, tafiditra ao anatin'ny lisitra miandry (liste d'attente) ho an'ny '{produit.nom}' ianao (Laharana faha-{ordre_jp}). "
                "Hampilazainay ianao raha misy fahafahana avy amin'ireo nialoha anao."
            )
            MessagingService.send_waiting_list_message(client, produit, ordre_jp, commande.id)

        from .models import Message
        Message.objects.create(
            commande=commande,
            contenu=message_content,
            numero_relance=0
        )
        return commande


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
            Commande.objects.select_related('client', 'produit', 'variante').prefetch_related('paiement', 'livraison__livreur'),
            id=commande_id,
        )
        variante_info = _commande_variante_payload(commande)
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
                'taille': variante_info['taille'],
                'couleur': variante_info['couleur'],
                'prix': variante_info['prix'],
                'code_jp': variante_info['code_jp'],
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
                f"Produit: {commande.produit.nom} ({variante_info['couleur']}, {variante_info['taille']})\n"
                f"Prix: {variante_info['prix']} Ar\n"
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
        queryset = Commande.objects.select_related('client', 'produit', 'variante').all()
        if not query:
            return queryset.order_by('-date_creation')

        filters = (
            models.Q(client__nom__icontains=query)
            | models.Q(client__telephone__icontains=query)
            | models.Q(produit__nom__icontains=query)
            | models.Q(variante__couleur__icontains=query)
            | models.Q(variante__taille__icontains=query)
            | models.Q(variante__code_jp__icontains=query)
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

            if not force:
                now = timezone.now()
                if last_message.date_envoi + timedelta(minutes=30) > now:
                    continue

            relance_num = last_message.numero_relance + 1
            contenu = self.build_relance_message(commande, relance_num)
            
            Message.objects.create(commande=commande, contenu=contenu, numero_relance=relance_num)
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

        paiement, _created = Paiement.objects.get_or_create(
            commande=commande,
            defaults={
                'methode': Paiement.METHODE_MOBILE_MONEY,
                'statut': Paiement.STATUT_PAYE
            }
        )

        from django.core.files.storage import default_storage
        file_name = f"payments/receipt_{commande.id}_{file_obj.name}"
        saved_path = default_storage.save(file_name, file_obj)

        paiement.methode = Paiement.METHODE_MOBILE_MONEY
        paiement.statut = Paiement.STATUT_PAYE
        paiement.capture_mobile_money = default_storage.url(saved_path)
        paiement.save()

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
        commande = get_object_or_404(Commande.objects.select_related('produit', 'variante'), pk=pk)
        variante = _commande_variante(commande)
        if not variante:
            return Response({'detail': 'Aucune variante associée à cette commande.'}, status=status.HTTP_404_NOT_FOUND)

        label_text = (
            f"{variante.code_jp} {commande.produit.nom.upper()} - {int(variante.prix_unitaire):,} Ar\n"
            f"({variante.couleur.upper()}, {variante.taille.upper()})"
        )

        ticket_data = {
            'commande_id': commande.id,
            'produit_nom': commande.produit.nom,
            'prix': str(variante.prix_unitaire),
            'couleur': variante.couleur,
            'taille': variante.taille,
            'code_jp': variante.code_jp,
            'ordre_jp': commande.ordre_jp,
            'label_text': label_text,
            'html_print': (
                f"<div style='width: 58mm; font-family: monospace; text-align: center; border: 1px dashed black; padding: 10px; margin: 10px;'>"
                f"<h2>AZLIVE LABEL</h2>"
                f"<div style='font-size: 16px; font-weight: bold; margin: 10px 0;'>{variante.code_jp} {commande.produit.nom.upper()}</div>"
                f"<div style='font-size: 20px; font-weight: bold; margin: 5px 0;'>{int(variante.prix_unitaire):,} Ar</div>"
                f"<div style='font-size: 12px; margin: 5px 0;'>Taille: {variante.taille.upper()} | Couleur: {variante.couleur.upper()}</div>"
                f"<div style='font-size: 10px; color: gray; margin-top: 15px;'>Commande #{commande.id} | Ordre JP: #{commande.ordre_jp}</div>"
                f"</div>"
            )
        }
        return Response(ticket_data, status=status.HTTP_200_OK)


class CommandeLancerLivraisonAPIView(APIView):
    permission_classes = [AllowAny]  # MVP — accessible sans token

    def post(self, request, pk):
        commande = get_object_or_404(Commande.objects.select_related('client', 'produit__vendeur'), pk=pk)

        if commande.statut in (Commande.STATUT_EN_LIVRAISON, Commande.STATUT_LIVRE):
            return Response(
                {'detail': f"Impossible de lancer la livraison : la commande est déjà en statut '{commande.get_statut_display()}'."},
                status=status.HTTP_409_CONFLICT
            )

        if commande.statut == Commande.STATUT_JP_CAPTURE:
            commande.statut = Commande.STATUT_CONFIRME
            commande.save()

        livraison, created = Livraison.objects.get_or_create(
            commande=commande,
            defaults={
                'statut': Livraison.STATUT_PREPARATION,
                'localisation_actuelle': "Bureau Principal"
            }
        )

        if not livraison.livreur:
            livreur, _ = Livreur.objects.get_or_create(
                nom="Livreur AZExpress Standard",
                defaults={'telephone': '0340000000'}
            )
            livraison.livreur = livreur

        livraison.statut = Livraison.STATUT_EN_LIVRAISON
        livraison.localisation_actuelle = "En cours d'expédition avec AZExpress"
        livraison.date_assignation = timezone.now()
        livraison.save()

        commande.statut = Commande.STATUT_EN_LIVRAISON
        commande.save()

        az_response = AZExpressService.transmettre_colis(commande, livraison)

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
        commandes_query = Commande.objects.select_related('produit', 'variante').all()
        lives_query = Live.objects.all()
        products_query = Produit.objects.prefetch_related('variantes').all()

        if request.user.is_authenticated:
            try:
                vendeur = request.user.vendeur
                commandes_query = commandes_query.filter(produit__vendeur=vendeur)
                lives_query = lives_query.filter(vendeur=vendeur)
                products_query = products_query.filter(vendeur=vendeur)
            except Vendeur.DoesNotExist:
                if vendeur_id:
                    commandes_query = commandes_query.filter(produit__vendeur_id=vendeur_id)
                    lives_query = lives_query.filter(vendeur_id=vendeur_id)
                    products_query = products_query.filter(vendeur_id=vendeur_id)
        elif vendeur_id:
            commandes_query = commandes_query.filter(produit__vendeur_id=vendeur_id)
            lives_query = lives_query.filter(vendeur_id=vendeur_id)
            products_query = products_query.filter(vendeur_id=vendeur_id)
        else:
            return Response(
                {'detail': 'Authentification ou paramètre vendeur_id requis pour accéder aux statistiques.'},
                status=status.HTTP_403_FORBIDDEN
            )

        total_jps = commandes_query.count()

        confirmed_orders = commandes_query.filter(
            statut__in=[
                Commande.STATUT_CONFIRME,
                Commande.STATUT_PREPARE,
                Commande.STATUT_EN_LIVRAISON,
                Commande.STATUT_LIVRE
            ]
        )
        confirmed_count = confirmed_orders.count()

        taux_confirmation = (confirmed_count / total_jps * 100) if total_jps > 0 else 0

        chiffre_affaires = sum(float(cmd.get_prix_unitaire()) for cmd in confirmed_orders)

        commission_rate = float(ParametresPlateforme.get_current().taux_commission)
        montant_a_reverser = float(chiffre_affaires) * (1.0 - commission_rate)

        best_sellers = (
            commandes_query.values('produit__nom')
            .annotate(total_ventes=Count('id'))
            .order_by('-total_ventes')[:5]
        )
        best_sellers_list = [{'produit_nom': item['produit__nom'], 'ventes': item['total_ventes']} for item in best_sellers]

        lives_realises_count = lives_query.filter(statut=Live.STATUT_TERMINE).count()
        total_stock = sum(v.stock for p in products_query for v in p.variantes.all())

        months = {
            1: 'Janvier', 2: 'Février', 3: 'Mars', 4: 'Avril', 5: 'Mai', 6: 'Juin',
            7: 'Juillet', 8: 'Août', 9: 'Septembre', 10: 'Octobre', 11: 'Novembre', 12: 'Décembre'
        }
        monthly_chart_data = []
        for m_num, m_name in months.items():
            month_orders = confirmed_orders.filter(date_creation__month=m_num)
            revenue = sum(float(cmd.get_prix_unitaire()) for cmd in month_orders)
            monthly_chart_data.append({
                'mois': m_name,
                'chiffre_affaires': float(revenue)
            })

        best_sellers_ranking = []
        best_sellers_query = (
            confirmed_orders.values('variante_id', 'produit_id')
            .annotate(units_sold=Count('id'))
            .order_by('-units_sold')[:5]
        )
        for index, item in enumerate(best_sellers_query, start=1):
            variante = Variante.objects.filter(id=item['variante_id']).first() if item['variante_id'] else None
            prod = Produit.objects.filter(id=item['produit_id']).first()
            if prod:
                prix = float(variante.prix_unitaire) if variante else float(prod.variantes.order_by('id').first().prix_unitaire) if prod.variantes.exists() else 0
                stock = variante.stock if variante else prod.stock_total
                code_jp = variante.code_jp if variante else (prod.variantes.order_by('id').first().code_jp if prod.variantes.exists() else f'JP{prod.id}')
                best_sellers_ranking.append({
                    'rang': index,
                    'produit_nom': prod.nom,
                    'code_jp': code_jp,
                    'prix_unitaire': prix,
                    'unites_vendues': item['units_sold'],
                    'stock_restant': stock,
                    'revenus_cumules': float(prix * item['units_sold'])
                })

        return Response({
            'chiffre_affaires': float(chiffre_affaires),
            'articles_vendus': confirmed_count,
            'lives_realises': lives_realises_count,
            'articles_en_stock': total_stock,
            'monthly_chart_data': monthly_chart_data,
            'best_sellers_ranking': best_sellers_ranking,
            'nombre_jps': total_jps,
            'confirmes': confirmed_count,
            'taux_confirmation': round(taux_confirmation, 2),
            'montant_a_reverser': round(montant_a_reverser, 2),
            'commission_plateforme': round(float(chiffre_affaires) * commission_rate, 2),
            'produits_les_plus_vendus': best_sellers_list
        }, status=status.HTTP_200_OK)


class LiveListCreateView(generics.ListCreateAPIView):
    queryset = Live.objects.all().order_by('-date_live')
    serializer_class = LiveSerializer
    permission_classes = [AllowAny]


class LiveDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Live.objects.all()
    serializer_class = LiveSerializer
    permission_classes = [AllowAny]


class CollaborateurListCreateView(generics.ListCreateAPIView):
    queryset = Collaborateur.objects.all().order_by('nom')
    serializer_class = CollaborateurSerializer
    permission_classes = [AllowAny]


class CollaborateurDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Collaborateur.objects.all()
    serializer_class = CollaborateurSerializer
    permission_classes = [AllowAny]


class VarianteListCreateView(generics.ListCreateAPIView):
    queryset = Variante.objects.select_related('produit').all()
    serializer_class = VarianteSerializer
    permission_classes = [AllowAny]

    def perform_create(self, serializer):
        serializer.save()


class VarianteDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Variante.objects.select_related('produit').all()
    serializer_class = VarianteSerializer
    permission_classes = [AllowAny]


class ClientListCreateView(generics.ListCreateAPIView):
    queryset = Client.objects.all().order_by('nom')
    serializer_class = ClientSerializer
    permission_classes = [AllowAny]


class ClientDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Client.objects.all()
    serializer_class = ClientSerializer
    permission_classes = [AllowAny]


class ClientStatsAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        vendeur_id = request.query_params.get('vendeur_id')
        clients_query = Client.objects.all()
        commandes_query = Commande.objects.select_related('variante').filter(
            statut__in=[
                Commande.STATUT_CONFIRME,
                Commande.STATUT_PREPARE,
                Commande.STATUT_EN_LIVRAISON,
                Commande.STATUT_LIVRE
            ]
        )

        if request.user.is_authenticated:
            try:
                vendeur = request.user.vendeur
                commandes_query = commandes_query.filter(produit__vendeur=vendeur)
                clients_query = Client.objects.filter(commandes__produit__vendeur=vendeur).distinct()
            except Vendeur.DoesNotExist:
                if vendeur_id:
                    commandes_query = commandes_query.filter(produit__vendeur_id=vendeur_id)
                    clients_query = Client.objects.filter(commandes__produit__vendeur_id=vendeur_id).distinct()
        elif vendeur_id:
            commandes_query = commandes_query.filter(produit__vendeur_id=vendeur_id)
            clients_query = Client.objects.filter(commandes__produit__vendeur_id=vendeur_id).distinct()

        total_clients = clients_query.count()
        avg_order_price = (
            sum(float(cmd.get_prix_unitaire()) for cmd in commandes_query) / commandes_query.count()
            if commandes_query.count() else 0
        )

        client_order_counts = (
            commandes_query.values('client')
            .annotate(cnt=Count('id'))
            .filter(cnt__gte=2)
        )
        fideles_count = client_order_counts.count()
        taux_fidelite = (fideles_count / total_clients * 100) if total_clients > 0 else 0

        return Response({
            'nombre_clients': total_clients,
            'prix_moyen_commande': round(float(avg_order_price), 2),
            'taux_fidelite': round(taux_fidelite, 2),
            'clients_fideles_count': fideles_count
        }, status=status.HTTP_200_OK)


class SocialConnectAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        vendeur_id = request.data.get('vendeur_id')
        platform = request.data.get('platform')

        if not vendeur_id:
            return Response({'detail': 'Le champ vendeur_id est requis.'}, status=status.HTTP_400_BAD_REQUEST)

        vendeur = get_object_or_404(Vendeur, id=vendeur_id)

        if platform == 'facebook':
            vendeur.facebook_page_id = request.data.get('facebook_page_id', 'fb_page_123456789')
            vendeur.facebook_page_name = request.data.get('facebook_page_name', 'Ma Boutique Facebook Officielle')
            vendeur.is_demo_mode = False

            pages_to_create = [
                {'page_id': 'fb_page_123', 'nom': 'AZLive Fashion'},
                {'page_id': 'fb_page_456', 'nom': 'Boutique Chic Madagascar'},
                {'page_id': 'fb_page_789', 'nom': 'Tana Dressing Hub'},
                {'page_id': 'fb_page_999', 'nom': "L'armoire des Princesses"},
            ]
            for p in pages_to_create:
                PageFacebook.objects.get_or_create(
                    vendeur=vendeur,
                    page_id=p['page_id'],
                    defaults={'nom': p['nom'], 'statut': PageFacebook.STATUT_PRET}
                )
        elif platform == 'tiktok':
            vendeur.tiktok_username = request.data.get('tiktok_username', '@maboutique_tiktok')
            vendeur.is_demo_mode = False
        elif platform == 'demo':
            vendeur.is_demo_mode = True
            vendeur.facebook_page_id = None
            vendeur.facebook_page_name = None
            vendeur.tiktok_username = None
        else:
            return Response({'detail': 'Plateforme invalide.'}, status=status.HTTP_400_BAD_REQUEST)

        vendeur.save()
        return Response(VendeurSerializer(vendeur).data, status=status.HTTP_200_OK)


class SocialDisconnectAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        vendeur_id = request.data.get('vendeur_id')
        platform = request.data.get('platform')

        if not vendeur_id:
            return Response({'detail': 'Le champ vendeur_id est requis.'}, status=status.HTTP_400_BAD_REQUEST)

        vendeur = get_object_or_404(Vendeur, id=vendeur_id)

        if platform == 'facebook' or platform == 'all':
            vendeur.facebook_page_id = None
            vendeur.facebook_page_name = None
            vendeur.pages_facebook.all().delete()
        if platform == 'tiktok' or platform == 'all':
            vendeur.tiktok_username = None
        if platform == 'demo' or platform == 'all':
            vendeur.is_demo_mode = False

        vendeur.save()
        return Response(VendeurSerializer(vendeur).data, status=status.HTTP_200_OK)


class FacebookPagesAPIView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        vendeur_id = request.query_params.get('vendeur_id')
        if not vendeur_id and request.user.is_authenticated:
            try:
                vendeur_id = request.user.vendeur.id
            except Vendeur.DoesNotExist:
                pass

        if vendeur_id:
            pages = PageFacebook.objects.filter(vendeur_id=vendeur_id)
        else:
            pages = PageFacebook.objects.all()

        serializer = PageFacebookSerializer(pages, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)
