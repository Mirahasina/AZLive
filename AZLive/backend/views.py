import re

from django.db import models
from django.db.models import Max
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

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


class VendeurListCreateView(generics.ListCreateAPIView):
    queryset = Vendeur.objects.all()
    serializer_class = VendeurSerializer


class ProduitListCreateView(generics.ListCreateAPIView):
    queryset = Produit.objects.all()
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


class JPAnalyseAPIView(APIView):
    def post(self, request):
        comment_text = request.data.get('comment_text', '')
        if not comment_text:
            return Response({'detail': 'Le champ comment_text est requis.'}, status=status.HTTP_400_BAD_REQUEST)

        parsed = JPCommentAnalyzer().analyze(comment_text)
        return Response(parsed, status=status.HTTP_200_OK)


class LivraisonTrackingAPIView(APIView):
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

    def post(self, request):
        commandes_a_relancer = []
        for commande in Commande.objects.filter(statut=Commande.STATUT_JP_CAPTURE).prefetch_related('messages'):
            last_message = commande.messages.order_by('-date_envoi').first()
            if not last_message:
                continue
            if last_message.numero_relance >= self.MAX_RELANCES:
                continue

            relance_num = last_message.numero_relance + 1
            contenu = self.build_relance_message(commande, relance_num)
            Message.objects.create(commande=commande, contenu=contenu, numero_relance=relance_num)
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
