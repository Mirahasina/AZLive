import logging
from typing import Any

from django.conf import settings

from .facebook_messenger import send_facebook_private_message
from .models import Commande, Message

logger = logging.getLogger(__name__)


def _public_base_url() -> str:
    return getattr(settings, 'AZLIVE_PUBLIC_BASE_URL', 'http://localhost:8000').rstrip('/')


def _document_urls(commande_id: int) -> dict[str, str]:
    base = _public_base_url()
    return {
        'facture_url': f'{base}/api/commandes/{commande_id}/facture.pdf',
        'etiquette_url': f'{base}/api/commandes/{commande_id}/etiquette-livraison.pdf',
    }


def _detect_channel(commande: Commande) -> str:
    client = commande.client
    if client.facebook_id:
        return Message.CANAL_FACEBOOK
    if client.tiktok_id:
        return Message.CANAL_TIKTOK
    return Message.CANAL_MOCK


def _record_outbound(commande: Commande, content: str, canal: str) -> Message:
    return Message.objects.create(
        commande=commande,
        contenu=content,
        numero_relance=0,
        direction=Message.DIRECTION_OUTBOUND,
        canal=canal,
    )


def _deliver_private_message(commande: Commande, content: str) -> dict[str, Any]:
    canal = _detect_channel(commande)
    delivery = {'channel': canal, 'sent': False, 'mock': True}

    if canal == Message.CANAL_FACEBOOK and commande.client.facebook_id:
        page = None
        from .order_confirmation import resolve_page_for_commande

        page = resolve_page_for_commande(commande)
        if page:
            result = send_facebook_private_message(
                page,
                commande.client.facebook_id,
                content,
            )
            delivery.update(result)
            delivery['mock'] = False

    elif canal == Message.CANAL_TIKTOK:
        # TikTok DM officiel indisponible — journaliser pour envoi manuel / WhatsApp futur
        logger.info(
            '[TIKTOK DM PENDING] commande #%s → @%s: %s',
            commande.id,
            commande.client.tiktok_id,
            content[:120],
        )
        delivery['detail'] = (
            'TikTok ne permet pas l\'envoi automatique de DM. '
            'Copiez le message depuis la console ou utilisez WhatsApp si le client a laissé son numéro.'
        )

    if delivery.get('mock', True):
        logger.info('[MESSAGING MOCK] commande #%s (%s): %s', commande.id, canal, content)
        print(f'\n [ORDER MESSAGING] Message privé ({canal}) commande #{commande.id}:')
        print(f'   > {content}\n')

    _record_outbound(commande, content, canal)
    return delivery


def build_jp_confirmation_message(commande: Commande) -> str:
    client = commande.client
    produit = commande.produit
    if commande.ordre_jp > 1:
        return (
            f"Salama {client.nom}, tafiditra ao anatin'ny lisitra miandry ho an'ny '{produit.nom}' ianao "
            f"(Laharana faha-{commande.ordre_jp}). Hampilazainay ianao raha misy fahafahana."
        )

    return (
        f"Salama {client.nom}, nahazo ny JP-nao amin'ny '{produit.nom}' izahay (Commande #{commande.id}).\n\n"
        "Mba hafahana ny baikonao, alefaso anay ny anaranao, finday, adiresy ary omena daty/ora — "
        "afaka alefa amin'ny iray na maro message, araka izay mety aminao. Tsy mila manaraka modèle manokana."
    )


FIELD_COMPLETION_PROMPTS = {
    'nom': 'ny anaranao',
    'telephone': 'ny findainao',
    'adresse': 'ny adiresinao',
    'date_livraison': 'ny daty tianao halefa',
    'heure_livraison': 'ny ora tianao (ohatra 14h)',
}


def build_completion_request_message(commande: Commande, missing_fields: list[str]) -> str:
    client = commande.client
    received = []
    if client.nom and client.nom not in {'Client Live', 'Client Facebook', 'Client TikTok'}:
        received.append(f"anarana ({client.nom})")
    if client.telephone:
        received.append(f"finday ({client.telephone})")
    if client.adresse:
        received.append(f"adiresy ({client.adresse})")
    if client.date_livraison_preferee:
        received.append(f"daty ({client.date_livraison_preferee.strftime('%d/%m/%Y')})")
    if client.heure_livraison_preferee:
        received.append(f"ora ({client.heure_livraison_preferee.strftime('%H:%M')})")

    missing_labels = [FIELD_COMPLETION_PROMPTS[field] for field in missing_fields if field in FIELD_COMPLETION_PROMPTS]
    intro = "Misaotra!"
    if received:
        intro += f" Efa voaray : {', '.join(received)}."
    if missing_labels:
        intro += f"\nMbola ilaina : {', '.join(missing_labels)}."
    intro += "\nAlefaso amin'ny message manaraka — afaka misy fizarana, tsy mila modèle."
    return intro


def send_completion_request_message(commande: Commande, missing_fields: list[str]) -> dict[str, Any]:
    content = build_completion_request_message(commande, missing_fields)
    delivery = _deliver_private_message(commande, content)
    return {'content': content, 'delivery': delivery}


def build_thank_you_message(commande: Commande) -> str:
    urls = _document_urls(commande.id)
    client = commande.client
    produit = commande.produit
    delivery_slot = ''
    if client.date_livraison_preferee:
        delivery_slot = client.date_livraison_preferee.strftime('%d/%m/%Y')
    if client.heure_livraison_preferee:
        hour_label = client.heure_livraison_preferee.strftime('%H:%M')
        delivery_slot = f'{delivery_slot} à {hour_label}'.strip()

    return (
        f"Misaotra {client.nom} ! Ny baikonao '{produit.nom}' (#{commande.id}) voafahana.\n\n"
        f"Facture PDF : {urls['facture_url']}\n"
        f"Etiquette livreur : {urls['etiquette_url']}\n\n"
        f"Ho avy ny livraison{(' ' + delivery_slot) if delivery_slot else ''}."
    )


def send_jp_confirmation_message(commande: Commande) -> dict[str, Any]:
    content = build_jp_confirmation_message(commande)
    delivery = _deliver_private_message(commande, content)
    return {'content': content, 'delivery': delivery}


def send_order_confirmed_message(commande: Commande) -> dict[str, Any]:
    content = build_thank_you_message(commande)
    delivery = _deliver_private_message(commande, content)
    urls = _document_urls(commande.id)
    return {
        'content': content,
        'delivery': delivery,
        **urls,
    }
