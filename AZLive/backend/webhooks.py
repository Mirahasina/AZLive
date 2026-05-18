from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from django.db.models import Max

from .models import Client, Commande, Produit, Message
from .serializers import CommandeSerializer
from .ai import JPCommentAnalyzer
from .services import MessagingService
from .views import _create_jp_commande


class FacebookWebhookView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        sender_facebook_id = request.data.get('sender_facebook_id')
        sender_name = request.data.get('sender_name', 'Client Facebook')
        comment_text = request.data.get('comment_text', '')

        if not sender_facebook_id or not comment_text:
            return Response(
                {'error': 'Les champs sender_facebook_id et comment_text sont obligatoires.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Step 1: Analyze comment
        analyzer = JPCommentAnalyzer()
        analysis = analyzer.analyze(comment_text)

        if analysis.get('intent') != 'achat':
            return Response(
                {'detail': 'Commentaire ignoré (intention d\'achat non détectée).', 'ai_analysis': analysis},
                status=status.HTTP_200_OK
            )

        # Step 2: Find the product matched
        produit_id = analysis.get('produit_id')
        if produit_id:
            produit = Produit.objects.filter(id=produit_id).first()
        else:
            produit = analyzer.find_best_produit(analysis.get('product_query'))

        if not produit:
            return Response(
                {'error': 'Produit introuvable pour ce commentaire.', 'ai_analysis': analysis},
                status=status.HTTP_404_NOT_FOUND
            )

        # Step 3: Find or create Client
        client, created = Client.objects.get_or_create(
            facebook_id=sender_facebook_id,
            defaults={
                'nom': sender_name,
                'telephone': '',
                'adresse': ''
            }
        )
        
        if not created and client.nom == 'Client Live' and sender_name != 'Client Facebook':
            client.nom = sender_name
            client.save()

        # Step 4: Create order atomically (Bug #3 fix — race condition protection)
        commande = _create_jp_commande(client, produit)

        # Step 5: Save message to DB and simulate dispatch
        message_content = (
            f"Bonjour {client.nom}, merci pour votre JP sur '{produit.nom}'. "
            f"Merci de confirmer votre commande en répondant avec : nom, téléphone, adresse et date préférée de livraison."
        )
        Message.objects.create(
            commande=commande,
            contenu=message_content,
            numero_relance=0
        )
        
        MessagingService.send_automatic_message(client, produit, commande.id)

        serializer = CommandeSerializer(commande)
        return Response({
            'status': 'JP capturé avec succès',
            'channel': 'Facebook',
            'client_cree': created,
            'commande': serializer.data,
            'ai_analysis': analysis
        }, status=status.HTTP_201_CREATED)


class TikTokWebhookView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        sender_tiktok_id = request.data.get('sender_tiktok_id')
        sender_name = request.data.get('sender_name', 'Client TikTok')
        comment_text = request.data.get('comment_text', '')

        if not sender_tiktok_id or not comment_text:
            return Response(
                {'error': 'Les champs sender_tiktok_id et comment_text sont obligatoires.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Step 1: Analyze comment
        analyzer = JPCommentAnalyzer()
        analysis = analyzer.analyze(comment_text)

        if analysis.get('intent') != 'achat':
            return Response(
                {'detail': 'Commentaire ignoré (intention d\'achat non détectée).', 'ai_analysis': analysis},
                status=status.HTTP_200_OK
            )

        # Step 2: Find the product matched
        produit_id = analysis.get('produit_id')
        if produit_id:
            produit = Produit.objects.filter(id=produit_id).first()
        else:
            produit = analyzer.find_best_produit(analysis.get('product_query'))

        if not produit:
            return Response(
                {'error': 'Produit introuvable pour ce commentaire.', 'ai_analysis': analysis},
                status=status.HTTP_404_NOT_FOUND
            )

        # Step 3: Find or create Client
        client, created = Client.objects.get_or_create(
            tiktok_id=sender_tiktok_id,
            defaults={
                'nom': sender_name,
                'telephone': '',
                'adresse': ''
            }
        )

        if not created and client.nom == 'Client Live' and sender_name != 'Client TikTok':
            client.nom = sender_name
            client.save()

        # Step 4: Create order atomically (Bug #3 fix — race condition protection)
        commande = _create_jp_commande(client, produit)

        # Step 5: Save message to DB and simulate dispatch
        message_content = (
            f"Bonjour {client.nom}, merci pour votre JP sur '{produit.nom}'. "
            f"Merci de confirmer votre commande en répondant avec : nom, téléphone, adresse et date préférée de livraison."
        )
        Message.objects.create(
            commande=commande,
            contenu=message_content,
            numero_relance=0
        )
        
        MessagingService.send_automatic_message(client, produit, commande.id)

        serializer = CommandeSerializer(commande)
        return Response({
            'status': 'JP capturé avec succès',
            'channel': 'TikTok',
            'client_cree': created,
            'commande': serializer.data,
            'ai_analysis': analysis
        }, status=status.HTTP_201_CREATED)
