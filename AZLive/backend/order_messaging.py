import logging
from typing import Any

from django.conf import settings

from .facebook_messenger import send_facebook_private_message, send_facebook_private_reply
from .message_humanizer import emoji, first_name, greeting, pick, thanks
from .models import Commande, Message
from .tiktok_live_chat import send_tiktok_live_chat_message

logger = logging.getLogger(__name__)


def _public_base_url() -> str:
    return getattr(settings, 'AZLIVE_PUBLIC_BASE_URL', 'http://localhost:8000').rstrip('/')


def public_order_form_url(live_id: int) -> str:
    base = getattr(settings, 'AZLIVE_PUBLIC_ORDER_BASE_URL', 'http://localhost:3000').rstrip('/')
    return f'{base}/commander/{live_id}'


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


def _deliver_private_message(
    commande: Commande,
    content: str,
    comment_id: str | None = None,
) -> dict[str, Any]:
    canal = _detect_channel(commande)
    delivery = {'channel': canal, 'sent': False, 'mock': True}

    if canal == Message.CANAL_FACEBOOK and (commande.client.facebook_id or comment_id):
        from .order_confirmation import resolve_page_for_commande

        page = resolve_page_for_commande(commande)
        if page:
            if comment_id:
                # Réponse privée à un commentateur (live/post) : seul canal possible
                # car l'id du commentaire n'est pas un PSID Messenger.
                result = send_facebook_private_reply(page, comment_id, content)
            else:
                result = send_facebook_private_message(
                    page,
                    commande.client.facebook_id,
                    content,
                )
            delivery.update(result)
            delivery['mock'] = False

    elif canal == Message.CANAL_TIKTOK:
        streamer_channel = None
        if commande.live_id and commande.live and commande.live.vendeur_id:
            streamer_channel = commande.live.vendeur.tiktok_username
        if streamer_channel:
            result = send_tiktok_live_chat_message(streamer_channel, content)
            delivery.update(result)
            delivery['mock'] = not result.get('sent')
        else:
            logger.info(
                '[TIKTOK CHAT PENDING] commande #%s → @%s (pas de @ streamer): %s',
                commande.id,
                commande.client.tiktok_id,
                content[:120],
            )
            delivery['detail'] = (
                'Compte TikTok du vendeur non configuré — impossible de répondre dans le chat live.'
            )

    if delivery.get('mock', True):
        logger.info('[MESSAGING MOCK] commande #%s (%s): %s', commande.id, canal, content)
        safe_content = content.encode('ascii', 'replace').decode('ascii')
        print(f'\n [ORDER MESSAGING] Message privé ({canal}) commande #{commande.id}:')
        print(f'   > {safe_content}\n')

    _record_outbound(commande, content, canal)
    return delivery


def build_tiktok_jp_comment_reply(commande: Commande) -> str:
    """Réponse publique dans le chat live TikTok avec lien vers le formulaire."""
    handle = (commande.client.tiktok_id or '').strip()
    salutation = f'@{handle} ' if handle else ''
    if commande.live_id:
        link = public_order_form_url(commande.live_id)
        return (
            f'Bonjour {salutation}😊 merci pour votre intérêt ! '
            f'Complétez vos infos ici 👉 {link}'
        )
    return f'Bonjour {salutation}😊 merci pour votre intérêt ! Contactez le vendeur pour vos infos.'


def build_jp_confirmation_message(commande: Commande) -> str:
    if _detect_channel(commande) == Message.CANAL_TIKTOK:
        return build_tiktok_jp_comment_reply(commande)

    from .order_confirmation import _order_is_eligible

    client = commande.client
    produit = commande.produit
    hello = greeting(client.nom)

    if not _order_is_eligible(commande):
        intro = pick(
            [
                f"{hello} 😊 Voaray ny JP-nao ho an'ny '{produit.nom}'.",
                f"{hello}! Efa azonay ny JP-nao ho an'ny '{produit.nom}'.",
                f"{hello}! Tonga soa ny JP-nao ho an'ny '{produit.nom}'.",
            ]
        )
        attente = pick(
            [
                f"Fa efa misy nanao commande mialoha anao, ka ao amin'ny liste d'attente ianao aloha (numéro {commande.ordre_jp}).",
                f"Saingy mbola misy olona eo alohanao, ka miandry kely ianao izao (numéro {commande.ordre_jp} amin'ny liste d'attente).",
                f"Mbola eo am-piandrasana ny anjaranao ianao izao (numéro {commande.ordre_jp} amin'ny liste d'attente).",
            ]
        )
        rassurance = pick(
            [
                "Hilazanay anao raha vao misy toerana. Misaotra amin'ny faharetana!",
                "Raha vao misy malalaka dia tofandrenesinay anao. Misaotra e!",
                "Aza manahy, holazainay anao raha vao tonga ny anjaranao.",
            ]
        )
        return f'{intro} {attente} {rassurance}{emoji(prob=0.4)}'

    intro = pick(
        [
            f"{hello} 😊 Voaray ny JP-nao ho an'ny '{produit.nom}' (Commande #{commande.id}).",
            f"{hello}! Efa azonay ny JP-nao ho an'ny '{produit.nom}' (Commande #{commande.id}).",
            f"{hello}! Tonga soa ny JP-nao ho an'ny '{produit.nom}' (Commande #{commande.id}).",
        ]
    )
    demande = pick(
        [
            "Mba alefaso aminay azafady ny anaranao, numéro, adresse, daty sy ora "
            "hanaterana, ary firy no alainao.",
            "Mba hahavita ny commande, omeo anay ny anaranao, numéro, adresse, daty sy "
            "ora hanaterana, ary firy no alainao.",
            "Lazao anay azafady ny anaranao, numéro, adresse, daty sy ora hanaterana, "
            "ary firy no alainao.",
        ]
    )
    souplesse = pick(
        [
            "Afaka soratanao tsikelikely ihany, tsy maika, tsy misy modèle tsy maintsy arahina.",
            "Azonao zaraina amin'ny message maromaro, araka izay mora aminao.",
            "Ataovy mora fotsiny, tsy voatery atao indray miaraka.",
        ]
    )
    return f'{intro}\n\n{demande} {souplesse}{emoji(prob=0.3)}'


FIELD_COMPLETION_PROMPTS = {
    'nom': 'ny anaranao',
    'telephone': 'ny numéro-nao',
    'adresse': 'ny adresse-nao',
    'date_livraison': 'ny daty hanaterana',
    'heure_livraison': 'ny ora (ohatra 14h)',
    'quantite': 'firy no alainao (ohatra 2)',
}


def build_completion_request_message(commande: Commande, missing_fields: list[str]) -> str:
    client = commande.client
    received = []
    if client.nom and client.nom not in {'Client Live', 'Client Facebook', 'Client TikTok'}:
        received.append(f"anarana ({client.nom})")
    if client.telephone:
        received.append(f"numéro ({client.telephone})")
    if client.adresse:
        received.append(f"adresse ({client.adresse})")
    if client.date_livraison_preferee:
        received.append(f"daty ({client.date_livraison_preferee.strftime('%d/%m/%Y')})")
    if client.heure_livraison_preferee:
        received.append(f"ora ({client.heure_livraison_preferee.strftime('%H:%M')})")
    if commande.quantite:
        received.append(f"firy ({commande.quantite})")

    missing_labels = [FIELD_COMPLETION_PROMPTS[field] for field in missing_fields if field in FIELD_COMPLETION_PROMPTS]
    intro = f'{thanks()}!'
    if received:
        recu_label = pick(['Efa voaray', 'Efa azonay', 'Voaray tsara'])
        intro += f' {recu_label} : {", ".join(received)}.'
    if missing_labels:
        manque_label = pick(['Mbola mila', 'Ny sisa ilaina', 'Mbola ilaina'])
        intro += f'\n{manque_label} : {", ".join(missing_labels)}.'
    cloture = pick(
        [
            "Azonao alefa amin'ny message manaraka, tsy maika.",
            "Andrasanay rehefa vonona ianao, soraty fotsiny eto.",
            "Azonao soratana tsikelikely ihany, araka izay mora aminao.",
        ]
    )
    intro += f'\n{cloture}{emoji(prob=0.3)}'
    return intro


def send_completion_request_message(commande: Commande, missing_fields: list[str]) -> dict[str, Any]:
    content = build_completion_request_message(commande, missing_fields)
    delivery = _deliver_private_message(commande, content)
    return {'content': content, 'delivery': delivery}


def build_waiting_with_info_message(commande: Commande) -> str:
    """Le client a tout fourni mais reste en liste d'attente (stock pas encore dispo)."""
    client = commande.client
    produit = commande.produit
    fn = first_name(client.nom)
    nom_court = f' {fn}' if fn else ''
    intro = pick(
        [
            f"{thanks()}{nom_court}! Voaray daholo ny infos-nao ho an'ny '{produit.nom}'.",
            f"{greeting(client.nom)}! Azonay tsara ny infos rehetra momba ny '{produit.nom}'.",
            f"{greeting(client.nom)}! Feno daholo ny infos-nao ho an'ny '{produit.nom}'. {thanks()}!",
        ]
    )
    attente = pick(
        [
            f"Fa mbola misy olona eo alohanao izao (numéro {commande.ordre_jp} amin'ny liste d'attente).",
            f"Mbola miandry ny anjaranao ihany ianao (numéro {commande.ordre_jp}).",
        ]
    )
    rassurance = pick(
        [
            "Raha vao misy toerana dia confirmé-nay ny commande-nao ary lazainay aminao. Misaotra amin'ny faharetana!",
            "Hovitainay avy hatrany ny commande-nao raha vao tonga ny anjaranao. Misaotra amin'ny fandeferana!",
        ]
    )
    return f'{intro} {attente} {rassurance}{emoji(prob=0.4)}'


def send_waiting_with_info_message(commande: Commande) -> dict[str, Any]:
    content = build_waiting_with_info_message(commande)
    delivery = _deliver_private_message(commande, content)
    return {'content': content, 'delivery': delivery}


def build_promotion_completion_message(commande: Commande, missing_fields: list[str]) -> str:
    """Une place s'est libérée : le client est promu mais il manque encore des infos."""
    client = commande.client
    produit = commande.produit
    labels = [FIELD_COMPLETION_PROMPTS[field] for field in missing_fields if field in FIELD_COMPLETION_PROMPTS]
    # Ancres testées : « toerana malalaka » et « alefaso ».
    bonne_nouvelle = pick(['vaovao tsara', 'vaovao mahafaly', 'fa misy vaovao'])
    message = (
        f"{greeting(client.nom)}, {bonne_nouvelle}! Nisy toerana malalaka ho an'ny '{produit.nom}', "
        f"ka afaka manohy ny commande-nao ianao izao."
    )
    if labels:
        message += f"\nMba alefaso haingana azafady : {', '.join(labels)} mba hahavitanay azy.{emoji(prob=0.4)}"
    return message


def send_promotion_completion_message(commande: Commande, missing_fields: list[str]) -> dict[str, Any]:
    content = build_promotion_completion_message(commande, missing_fields)
    delivery = _deliver_private_message(commande, content)
    return {'content': content, 'delivery': delivery}


def build_thank_you_message(commande: Commande, *, promoted: bool = False) -> str:
    urls = _document_urls(commande.id)
    client = commande.client
    produit = commande.produit
    delivery_slot = ''
    if client.date_livraison_preferee:
        delivery_slot = client.date_livraison_preferee.strftime('%d/%m/%Y')
    if client.heure_livraison_preferee:
        hour_label = client.heure_livraison_preferee.strftime('%H:%M')
        delivery_slot = f'{delivery_slot} à {hour_label}'.strip()

    # Cas « promu » : le client était en liste d'attente, une place s'est libérée et
    # comme ses informations étaient déjà complètes, sa commande est prise en compte.
    # Ancre testée : « toerana malalaka ».
    if promoted:
        intro = pick(
            [
                f"{greeting(client.nom)}, vaovao tsara! Nisy toerana malalaka, ka vita sy "
                f"confirmé ny commande-nao '{produit.nom}' (#{commande.id}).",
                f"{greeting(client.nom)}, vaovao mahafaly! Nisy toerana malalaka, ka vita "
                f"ny commande-nao '{produit.nom}' (#{commande.id}).",
            ]
        )
    else:
        fn = first_name(client.nom)
        nom_court = f' {fn}' if fn else ' tompoko'
        intro = pick(
            [
                f"{greeting(client.nom)}! Vita ny commande-nao '{produit.nom}' (#{commande.id}). {thanks()}!",
                f"{thanks()}{nom_court}! Confirmé ny commande-nao '{produit.nom}' (#{commande.id}).",
                f"{thanks()} betsaka{nom_court}! Vita tsara ny commande-nao "
                f"'{produit.nom}' (#{commande.id}).",
            ]
        )

    livraison = pick(
        [
            f"Ho avy ny livraison{(' ' + delivery_slot) if delivery_slot else ''}.",
            f"Haterinay ny entana{(' ' + delivery_slot) if delivery_slot else ''}.",
        ]
    )
    return (
        f"{intro}{emoji(prob=0.5)}\n\n"
        f"Facture : {urls['facture_url']}\n"
        f"Etiquette livreur : {urls['etiquette_url']}\n\n"
        f"{livraison}"
    )


def send_jp_confirmation_message(
    commande: Commande,
    comment_id: str | None = None,
) -> dict[str, Any]:
    content = build_jp_confirmation_message(commande)
    delivery = _deliver_private_message(commande, content, comment_id=comment_id)
    return {'content': content, 'delivery': delivery}


def build_order_cancelled_message(commande: Commande) -> str:
    client = commande.client
    produit = commande.produit
    intro = pick(
        [
            f"Ekena {client.nom}, nofoanana ny commande-nao '{produit.nom}' (#{commande.id}).",
            f"Azo {client.nom}, nesorina ny commande-nao '{produit.nom}' (#{commande.id}).",
            f"Ekena tsara, voafoana ny commande-nao '{produit.nom}' (#{commande.id}).",
        ]
    )
    cloture = pick(
        [
            "Raha nisy diso na te-hanao commande vaovao ianao, valio fotsiny eto. Misaotra!",
            "Raha mbola mila zavatra ianao, soraty eto fotsiny dia eto izahay. Misaotra e!",
        ]
    )
    return f'{intro} {cloture}'


def send_order_cancelled_message(commande: Commande) -> dict[str, Any]:
    content = build_order_cancelled_message(commande)
    delivery = _deliver_private_message(commande, content)
    return {'content': content, 'delivery': delivery}


def build_order_expired_message(commande: Commande) -> str:
    client = commande.client
    produit = commande.produit
    intro = pick(
        [
            f"{greeting(client.nom)}, voafoana ny commande-nao '{produit.nom}' (#{commande.id}) "
            "satria tsy tonga tao anatin'ny fotoana ny infos ilaina, ka nomena ny manaraka ny toerana.",
            f"{greeting(client.nom)}, lany ny fotoana hamenoana ny infos ho an'ny commande "
            f"'{produit.nom}' (#{commande.id}), ka voatery nomena ny manaraka ny toerana.",
        ]
    )
    cloture = pick(
        [
            "Raha mbola liana ianao, valio fotsiny eto. Misaotra!",
            "Raha te-hanao commande indray ianao, soraty eto fotsiny dia hanampy anao izahay. Misaotra e!",
        ]
    )
    return f'{intro} {cloture}'


def send_order_expired_message(commande: Commande) -> dict[str, Any]:
    content = build_order_expired_message(commande)
    delivery = _deliver_private_message(commande, content)
    return {'content': content, 'delivery': delivery}


def send_order_confirmed_message(commande: Commande, *, promoted: bool = False) -> dict[str, Any]:
    content = build_thank_you_message(commande, promoted=promoted)
    delivery = _deliver_private_message(commande, content)
    urls = _document_urls(commande.id)
    return {
        'content': content,
        'delivery': delivery,
        **urls,
    }
