import logging
import uuid
from django.utils import timezone

logger = logging.getLogger(__name__)


class MessagingService:
    @staticmethod
    def send_automatic_message(client, produit, order_id) -> bool:
        """
        Simulates sending the initial JP message via WhatsApp/Messenger.
        """
        message_content = (
            f"Bonjour {client.nom}, merci pour votre JP sur '{produit.nom}'. "
            f"Merci de confirmer votre commande en répondant avec : nom, téléphone, adresse et date préférée de livraison."
        )
        # Log to Django console
        logger.info(f"[SMS/MESSENGER MOCK] Envoyé à {client.telephone or 'Client ID ' + str(client.id)} (Commande #{order_id}) : '{message_content}'")
        print(f"\n [MESSAGING SERVICE] Message envoyé avec succès à {client.nom} ({client.telephone or 'Social Platform'}):")
        print(f"   > '{message_content}'\n")
        return True

    @staticmethod
    def send_relance_message(client, produit, numero_relance) -> bool:
        """
        Simulates sending a follow-up relance message via WhatsApp/Messenger.
        """
        message_content = (
            f"Bonjour {client.nom}, ceci est votre relance n°{numero_relance} "
            f"pour la commande '{produit.nom}'. Merci de confirmer votre adresse et date de livraison."
        )
        logger.info(f"[SMS/MESSENGER RELANCE MOCK] Relance #{numero_relance} envoyée à {client.telephone or 'Client ID ' + str(client.id)} : '{message_content}'")
        print(f"\n⏰ [MESSAGING SERVICE] Relance #{numero_relance} envoyée à {client.nom} ({client.telephone or 'Social Platform'}):")
        print(f"   > '{message_content}'\n")
        return True


class AZExpressService:
    @staticmethod
    def transmettre_colis(commande, livraison) -> dict:
        """
        Simulates transmitting package information to AZExpress shipping API.
        Returns mock tracking number and success payload.
        """
        tracking_number = f"AZX-{uuid.uuid4().hex[:8].upper()}"
        
        # Log the payload that would be sent to AZExpress API
        payload = {
            "commande_id": commande.id,
            "vendeur": commande.produit.vendeur.nom,
            "client_nom": commande.client.nom,
            "client_telephone": commande.client.telephone,
            "client_adresse": commande.client.adresse,
            "produit": f"{commande.produit.nom} ({commande.produit.couleur}, {commande.produit.taille})",
            "montant_a_percevoir": float(commande.produit.prix),
            "tracking_number": tracking_number
        }
        
        logger.info(f"[AZEXPRESS API MOCK] Colis transmis pour Commande #{commande.id}. Payload : {payload}")
        print(f"\n [AZEXPRESS SERVICE] Synchronisation réussie pour Commande #{commande.id} :")
        print(f"   > Code Tracking AZExpress généré : {tracking_number}")
        print(f"   > Livreur assigné par défaut : {livraison.livreur.nom if livraison.livreur else 'Aucun'}\n")
        
        return {
            "status": "success",
            "tracking_number": tracking_number,
            "assigned_carrier": "AZExpress Dispatcher",
            "estimated_delivery": (timezone.now() + timezone.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        }
