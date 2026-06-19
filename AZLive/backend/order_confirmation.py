import re
import unicodedata
from datetime import date, datetime, time
from typing import Any

from django.db import transaction
from django.utils import timezone

from .models import Client, Commande, Message, Paiement, PageFacebook, Vendeur
from .serializers import CommandeSerializer


class OrderConfirmationError(Exception):
    def __init__(self, message, status_code=400, payload=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}


FIELD_PATTERNS = {
    'nom': re.compile(r'(?:^|\n)\s*(?:nom|anarana)\s*[:\-]\s*(.+)', re.IGNORECASE),
    'telephone': re.compile(r'(?:^|\n)\s*(?:tel(?:éphone)?|finday|phone)\s*[:\-]\s*(.+)', re.IGNORECASE),
    'adresse': re.compile(r'(?:^|\n)\s*(?:adres(?:se)?|adiresy)\s*[:\-]\s*(.+)', re.IGNORECASE),
    'date_livraison': re.compile(
        r'(?:^|\n)\s*(?:date(?:\s+livraison)?|daty)\s*[:\-]\s*(.+)',
        re.IGNORECASE,
    ),
    'heure_livraison': re.compile(
        r'(?:^|\n)\s*(?:heure|ora|time)\s*[:\-]\s*(.+)',
        re.IGNORECASE,
    ),
}

PHONE_PATTERN = re.compile(
    r'^(?:\+261[\s.-]?|0)(3[0-9]{2})[\s.-]?(\d{2})[\s.-]?(\d{3})[\s.-]?(\d{2})$'
)
PHONE_LOOSE_PATTERN = re.compile(r'(?:\+261|0)?3[0-9]{8}')

TIME_PATTERN = re.compile(
    r'^(\d{1,2})\s*[hH:]\s*(\d{2})?(?:\s*(?:min|ora))?$|^\d{1,2}:\d{2}$',
)

FRENCH_MONTHS = {
    'janvier': 1,
    'fevrier': 2,
    'mars': 3,
    'avril': 4,
    'mai': 5,
    'juin': 6,
    'juillet': 7,
    'aout': 8,
    'septembre': 9,
    'octobre': 10,
    'novembre': 11,
    'decembre': 12,
}


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize('NFKD', value.lower())
    return ''.join(ch for ch in normalized if not unicodedata.combining(ch))


def _normalize_phone(value: str) -> str | None:
    digits = re.sub(r'\D', '', value or '')
    if digits.startswith('261') and len(digits) >= 12:
        digits = '0' + digits[3:]
    if len(digits) == 9 and digits.startswith('3'):
        digits = '0' + digits
    if len(digits) == 10 and digits.startswith('03'):
        return digits
    return None


def _looks_like_phone(value: str) -> bool:
    return _normalize_phone(value) is not None


def _parse_delivery_time(value: str | None) -> time | None:
    if not value:
        return None
    cleaned = value.strip().lower().replace('h30', 'h30').replace(' ', '')
    match = re.match(r'^(\d{1,2})[h:](\d{2})$', cleaned)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)
    match = re.match(r'^(\d{1,2})[hH]$', value.strip())
    if match:
        hour = int(match.group(1))
        if 0 <= hour <= 23:
            return time(hour, 0)
    try:
        return datetime.strptime(value.strip(), '%H:%M').time()
    except ValueError:
        return None


def _looks_like_time(value: str) -> bool:
    return _parse_delivery_time(value) is not None


def _parse_french_date(value: str, reference: date | None = None) -> date | None:
    reference = reference or timezone.localdate()
    cleaned = value.strip()
    normalized = _normalize_text(cleaned)

    for fmt in ('%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d', '%d/%m/%y'):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue

    match = re.match(r'^(\d{1,2})\s+([a-z]+)(?:\s+(\d{4}))?$', normalized)
    if match:
        day = int(match.group(1))
        month = FRENCH_MONTHS.get(match.group(2))
        year = int(match.group(3)) if match.group(3) else reference.year
        if month and 1 <= day <= 31:
            try:
                parsed = date(year, month, day)
                if not match.group(3) and parsed < reference:
                    parsed = date(year + 1, month, day)
                return parsed
            except ValueError:
                return None
    return None


def _looks_like_date(value: str) -> bool:
    return _parse_french_date(value) is not None


def _extract_inline_date_time(value: str) -> tuple[str | None, str | None]:
    """Extrait date/heure d'une ligne mixte, ex. '12 mai 14h'."""
    remaining = value.strip()
    date_part = None
    time_part = None

    time_match = re.search(r'(\d{1,2}\s*[hH:]\s*\d{0,2})', remaining)
    if time_match:
        time_part = time_match.group(1).strip()
        remaining = remaining.replace(time_match.group(0), ' ').strip()

    if remaining and _looks_like_date(remaining):
        date_part = remaining

    return date_part, time_part


def parse_confirmation_text(text: str) -> dict[str, str]:
    """
    Extrait nom, téléphone, adresse, date et heure depuis un message privé.
    Accepte les formats étiquetés ou libres, ex. :
      Lova
      Bypass
      12 mai
      14h
    """
    cleaned = (text or '').strip()
    if not cleaned:
        return {}

    parsed: dict[str, str] = {}
    for field, pattern in FIELD_PATTERNS.items():
        match = pattern.search(cleaned)
        if match:
            parsed[field] = match.group(1).strip().split('\n')[0].strip()

    if len(parsed) >= 3:
        return parsed

    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return parsed

    classified = {'phones': [], 'dates': [], 'times': [], 'others': []}
    for line in lines:
        inline_date, inline_time = _extract_inline_date_time(line)
        if inline_date:
            classified['dates'].append(inline_date)
            if inline_time:
                classified['times'].append(inline_time)
            continue
        if inline_time and not inline_date:
            classified['times'].append(inline_time)
            continue
        if _looks_like_phone(line):
            phone = _normalize_phone(line)
            if phone:
                classified['phones'].append(phone)
            continue
        if _looks_like_time(line):
            classified['times'].append(line)
            continue
        if _looks_like_date(line):
            classified['dates'].append(line)
            continue
        classified['others'].append(line)

    if classified['phones']:
        parsed.setdefault('telephone', classified['phones'][0])
    if classified['dates']:
        parsed.setdefault('date_livraison', classified['dates'][0])
    if classified['times']:
        parsed.setdefault('heure_livraison', classified['times'][0])

    others = classified['others']
    if others:
        parsed.setdefault('nom', others[0])
        if len(others) > 1:
            parsed.setdefault('adresse', ' '.join(others[1:]))
        elif len(others) == 1 and not parsed.get('adresse'):
            # Une seule ligne texte restante sans téléphone/date → probablement l'adresse/quartier
            if parsed.get('nom') and parsed.get('telephone') and parsed.get('date_livraison'):
                parsed.setdefault('adresse', others[0])
            elif parsed.get('nom') and (parsed.get('date_livraison') or parsed.get('telephone')):
                if not _looks_like_date(others[0]) and not _looks_like_phone(others[0]):
                    if parsed['nom'] == others[0] and len(lines) >= 2:
                        pass
                    else:
                        parsed.setdefault('adresse', others[0] if parsed.get('nom') != others[0] else '')

    # Cas typique Madagascar : Nom / Quartier / Date [/ Heure]
    if len(lines) >= 3 and not parsed.get('adresse'):
        if (
            parsed.get('nom')
            and parsed.get('date_livraison')
            and len(classified['others']) >= 2
        ):
            parsed['adresse'] = classified['others'][1]
        elif len(classified['others']) == 2 and parsed.get('date_livraison'):
            parsed.setdefault('nom', classified['others'][0])
            parsed.setdefault('adresse', classified['others'][1])
        elif len(classified['others']) == 1 and parsed.get('nom') and parsed.get('date_livraison'):
            # nom + date détectés, 1 ligne quartier restante
            for line in classified['others']:
                if line != parsed.get('nom'):
                    parsed.setdefault('adresse', line)

    # Reconstruction explicite 3 lignes : Nom / Adresse / Date
    if len(lines) == 3 and not parsed.get('telephone'):
        if _looks_like_date(lines[2]) and not _looks_like_phone(lines[1]):
            parsed['nom'] = lines[0]
            parsed['adresse'] = lines[1]
            parsed['date_livraison'] = lines[2]

    if len(lines) == 4 and not parsed.get('telephone'):
        if _looks_like_date(lines[2]) and _looks_like_time(lines[3]) and not _looks_like_phone(lines[1]):
            parsed['nom'] = lines[0]
            parsed['adresse'] = lines[1]
            parsed['date_livraison'] = lines[2]
            parsed['heure_livraison'] = lines[3]

    return parsed


def _parse_delivery_date(value: str | None):
    if not value:
        return None
    return _parse_french_date(value)


def detect_client_channel(client: Client) -> str:
    if client.facebook_id:
        return 'Facebook'
    if client.tiktok_id:
        return 'TikTok'
    return 'Inconnu'


def find_pending_commande(client: Client, vendeur: Vendeur | None = None) -> Commande | None:
    queryset = (
        Commande.objects.select_related('produit', 'produit__vendeur', 'client', 'variante', 'live')
        .filter(client=client, statut=Commande.STATUT_JP_CAPTURE)
        .order_by('ordre_jp', '-date_creation')
    )
    if vendeur:
        queryset = queryset.filter(produit__vendeur=vendeur)
    return queryset.first()


def resolve_page_for_commande(commande: Commande) -> PageFacebook | None:
    vendeur = commande.produit.vendeur
    if commande.live_id and commande.live.pages_facebook:
        for item in commande.live.pages_facebook:
            page = (
                PageFacebook.objects.filter(vendeur=vendeur, nom=item).first()
                or PageFacebook.objects.filter(vendeur=vendeur, page_id=str(item)).first()
            )
            if page and page.access_token:
                return page

    return (
        PageFacebook.objects.filter(vendeur=vendeur, statut=PageFacebook.STATUT_PRET)
        .exclude(access_token__isnull=True)
        .exclude(access_token='')
        .first()
    )


CANCELLATION_PATTERNS = [
    re.compile(r'\bannul', re.IGNORECASE),
    re.compile(r'\bne\s+(?:veux|prends?|prend)\s+plus\b', re.IGNORECASE),
    re.compile(r'\bplus\s+besoin\b', re.IGNORECASE),
    re.compile(r'\bnon\s+merci\b', re.IGNORECASE),
    re.compile(r'\btsy\s+(?:maka|haka|mila|te)\b', re.IGNORECASE),
    re.compile(r'^\s*tsia\s*$', re.IGNORECASE),
]


def _is_cancellation(text: str) -> bool:
    """Détecte une réponse de refus/annulation explicite (FR ou MG)."""
    cleaned = (text or '').strip()
    if not cleaned:
        return False
    return any(pattern.search(cleaned) for pattern in CANCELLATION_PATTERNS)


def _ensure_paiement(commande: Commande) -> Paiement:
    """Crée le règlement par défaut (paiement à la livraison, non payé) si absent."""
    paiement, _ = Paiement.objects.get_or_create(
        commande=commande,
        defaults={
            'methode': Paiement.METHODE_LIVRAISON,
            'statut': Paiement.STATUT_NON_PAYE,
        },
    )
    return paiement


def _missing_confirmation_fields(client: Client) -> list[str]:
    missing = []
    if not client.nom or client.nom in {'Client Live', 'Client Facebook', 'Client TikTok'}:
        missing.append('nom')
    if not client.telephone:
        missing.append('telephone')
    if not client.adresse:
        missing.append('adresse')
    if not client.date_livraison_preferee:
        missing.append('date_livraison')
    if not client.heure_livraison_preferee:
        missing.append('heure_livraison')
    return missing


def _client_snapshot(client: Client) -> dict[str, Any]:
    return {
        'nom': client.nom,
        'telephone': client.telephone,
        'adresse': client.adresse,
        'date_livraison_preferee': client.date_livraison_preferee,
        'heure_livraison_preferee': client.heure_livraison_preferee.strftime('%H:%M')
        if client.heure_livraison_preferee
        else None,
    }


def _apply_parsed_fields(client: Client, parsed_data: dict[str, str]) -> None:
    if parsed_data.get('nom'):
        client.nom = parsed_data['nom']
    if parsed_data.get('telephone'):
        client.telephone = _normalize_phone(parsed_data['telephone']) or parsed_data['telephone']
    if parsed_data.get('adresse'):
        client.adresse = parsed_data['adresse']
    delivery_date = _parse_delivery_date(parsed_data.get('date_livraison'))
    if delivery_date:
        client.date_livraison_preferee = delivery_date
    delivery_time = _parse_delivery_time(parsed_data.get('heure_livraison'))
    if delivery_time:
        client.heure_livraison_preferee = delivery_time


def analyze_confirmation_message(text: str, client: Client | None = None) -> dict[str, str]:
    from .ai import ConfirmationMessageAnalyzer

    return ConfirmationMessageAnalyzer().analyze(text, client=client)['fields']


@transaction.atomic
def handle_client_reply(
    commande: Commande,
    parsed_data: dict[str, str],
    *,
    inbound_text: str = '',
    canal: str | None = None,
) -> dict[str, Any]:
    """Enregistre ce que le client a envoyé ; confirme si complet, sinon demande le reste."""
    if commande.statut != Commande.STATUT_JP_CAPTURE:
        raise OrderConfirmationError(
            f'La commande #{commande.id} est déjà au statut {commande.get_statut_display()}.',
            status_code=409,
        )

    client = commande.client
    canal_message = canal or detect_client_channel(client)

    if inbound_text:
        Message.objects.create(
            commande=commande,
            contenu=inbound_text,
            numero_relance=0,
            direction=Message.DIRECTION_INBOUND,
            canal=canal_message,
        )

    # Réponse négative explicite : on annule la commande (le stock éventuellement
    # décrémenté est restauré et le suivant de la file est promu via Commande.save()).
    if _is_cancellation(inbound_text):
        commande.statut = Commande.STATUT_ANNULE
        commande.save(update_fields=['statut'])

        from .order_messaging import send_order_cancelled_message

        outbound = send_order_cancelled_message(commande)
        return {
            'status': 'Commande annulée',
            'annule': True,
            'complet': False,
            'commande': CommandeSerializer(commande).data,
            'client': _client_snapshot(client),
            'message_annulation': outbound.get('content'),
            'message_delivery': outbound.get('delivery'),
        }

    _apply_parsed_fields(client, parsed_data)

    client.save(
        update_fields=[
            'nom',
            'telephone',
            'adresse',
            'date_livraison_preferee',
            'heure_livraison_preferee',
        ],
    )

    missing = _missing_confirmation_fields(client)
    if missing:
        from .order_messaging import send_completion_request_message

        outbound = send_completion_request_message(commande, missing)
        return {
            'status': 'Informations partielles — complétez quand vous voulez',
            'complet': False,
            'champs_manquants': missing,
            'champs_recus': {k: v for k, v in _client_snapshot(client).items() if v},
            'parsed': parsed_data,
            'client': _client_snapshot(client),
            'message_relance': outbound.get('content'),
            'message_delivery': outbound.get('delivery'),
        }

    # Confirmation complète : statut + stock (décrément via Commande.save()) + règlement.
    commande.statut = Commande.STATUT_CONFIRME
    commande.save(update_fields=['statut'])
    paiement = _ensure_paiement(commande)

    from .order_messaging import send_order_confirmed_message

    outbound = send_order_confirmed_message(commande)

    return {
        'status': 'Commande confirmée',
        'complet': True,
        'commande': CommandeSerializer(commande).data,
        'reglement': {'methode': paiement.methode, 'statut': paiement.statut},
        'client': _client_snapshot(client),
        'parsed': parsed_data,
        'message_remerciement': outbound.get('content'),
        'message_delivery': outbound.get('delivery'),
        'facture_url': outbound.get('facture_url'),
        'etiquette_url': outbound.get('etiquette_url'),
    }


@transaction.atomic
def confirm_commande_from_message(
    commande: Commande,
    parsed_data: dict[str, str],
    *,
    inbound_text: str = '',
    canal: str | None = None,
) -> dict[str, Any]:
    return handle_client_reply(
        commande,
        parsed_data,
        inbound_text=inbound_text,
        canal=canal,
    )


def process_inbound_private_message(
    *,
    sender_id: str,
    message_text: str,
    channel: str,
    page_id: str | None = None,
    id_field: str = 'facebook_id',
) -> dict[str, Any]:
    if not sender_id or not message_text:
        raise OrderConfirmationError('Message privé vide ou expéditeur manquant.')

    lookup = {id_field: sender_id}
    client = Client.objects.filter(**lookup).first()
    if not client:
        raise OrderConfirmationError(
            'Aucun client trouvé pour cet identifiant. Postez d\'abord un JP pendant le live.',
            status_code=404,
        )

    vendeur = None
    if page_id:
        page = PageFacebook.objects.select_related('vendeur').filter(page_id=str(page_id)).first()
        vendeur = page.vendeur if page else None

    commande = find_pending_commande(client, vendeur=vendeur)
    if not commande:
        raise OrderConfirmationError(
            'Aucune commande JP en attente de confirmation pour ce client.',
            status_code=404,
        )

    parsed = analyze_confirmation_message(message_text, client=client)
    return handle_client_reply(
        commande,
        parsed,
        inbound_text=message_text,
        canal=channel,
    )
