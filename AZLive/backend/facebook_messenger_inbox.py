from __future__ import annotations

import logging
import threading
from typing import Any

from django.db import close_old_connections

from .facebook_oauth import FacebookOAuthError, _graph_request
from .models import Commande, Message, PageFacebook
from .order_confirmation import (
    CANCELLABLE_STATUSES,
    OrderConfirmationError,
    _is_cancellation,
    process_inbound_private_message,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 8
CONVERSATION_LIMIT = 15
MESSAGE_LIMIT = 20

_seen_mids: set[str] = set()
_seen_lock = threading.Lock()
_MAX_SEEN = 5000

_inbox_listeners: dict[str, '_MessengerInboxListener'] = {}
_listeners_lock = threading.Lock()


def _try_claim_mid(mid: str) -> bool:
    """Réserve un mid pour traitement. False s'il est déjà réservé."""
    if not mid:
        return True
    with _seen_lock:
        if mid in _seen_mids:
            return False
        _seen_mids.add(mid)
        if len(_seen_mids) > _MAX_SEEN:
            for stale in list(_seen_mids)[: len(_seen_mids) // 2]:
                _seen_mids.discard(stale)
        return True


def _release_mid(mid: str) -> None:
    """Libère un mid après échec pour permettre un nouvel essai."""
    if not mid:
        return
    with _seen_lock:
        _seen_mids.discard(mid)


def _already_recorded_inbound(psid: str, text: str) -> bool:
    """Évite de retraiter un même texte déjà passé par le webhook."""
    return Message.objects.filter(
        direction=Message.DIRECTION_INBOUND,
        canal=Message.CANAL_FACEBOOK,
        contenu=text,
        commande__client__facebook_id=psid,
    ).exists()


def _should_skip_inbound(psid: str, text: str) -> bool:
    """Ne saute pas une annulation tant qu'une commande est encore annulable."""
    if _is_cancellation(text) and Commande.objects.filter(
        client__facebook_id=psid,
        statut__in=CANCELLABLE_STATUSES,
    ).exists():
        return False
    return _already_recorded_inbound(psid, text)


def _pending_facebook_pages() -> list[PageFacebook]:
    from datetime import timedelta

    from django.db.models import Q
    from django.utils import timezone

    recent = timezone.now() - timedelta(days=7)
    vendeur_ids = (
        Commande.objects.filter(
            client__facebook_id__isnull=False,
        )
        .filter(
            Q(statut__in=CANCELLABLE_STATUSES)
            | Q(statut=Commande.STATUT_ANNULE, date_creation__gte=recent)
        )
        .exclude(client__facebook_id='')
        .values_list('produit__vendeur_id', flat=True)
        .distinct()
    )
    return list(
        PageFacebook.objects.filter(vendeur_id__in=vendeur_ids)
        .exclude(access_token__isnull=True)
        .exclude(access_token='')
    )


def _fetch_recent_conversations(page: PageFacebook) -> list[dict[str, Any]]:
    payload = _graph_request(
        f'{page.page_id}/conversations',
        {
            'access_token': page.access_token,
            'platform': 'MESSENGER',
            'fields': (
                f'participants,updated_time,'
                f'messages.limit({MESSAGE_LIMIT}){{id,message,from,created_time}}'
            ),
            'limit': CONVERSATION_LIMIT,
        },
        method='GET',
    )
    data = (payload or {}).get('data')
    return data if isinstance(data, list) else []


def _participant_info(conversation: dict[str, Any], page_id: str) -> tuple[str | None, str | None]:
    """Retourne (psid, nom Facebook) du client dans la conversation."""
    for participant in ((conversation.get('participants') or {}).get('data') or []):
        pid = str(participant.get('id') or '')
        if pid and pid != str(page_id):
            name = (participant.get('name') or '').strip() or None
            return pid, name
    return None, None


def sync_page_messenger_inbox(page: PageFacebook) -> list[dict[str, Any]]:
    """Lit les derniers MP entrants et les traite (JP, confirmation, annulation…)."""
    if not page.access_token:
        return []

    try:
        conversations = _fetch_recent_conversations(page)
    except FacebookOAuthError as exc:
        logger.warning('Inbox Messenger page %s: %s', page.page_id, exc)
        return []

    results: list[dict[str, Any]] = []
    for conversation in conversations:
        psid, fb_name = _participant_info(conversation, page.page_id)
        if not psid or not psid.isdigit():
            continue
        from datetime import timedelta

        from django.db.models import Q
        from django.utils import timezone

        recent = timezone.now() - timedelta(days=7)
        if not Commande.objects.filter(client__facebook_id=psid).filter(
            Q(statut__in=CANCELLABLE_STATUSES)
            | Q(statut=Commande.STATUT_ANNULE, date_creation__gte=recent)
        ).exists():
            continue

        messages = ((conversation.get('messages') or {}).get('data') or [])
        # Graph renvoie souvent du plus récent au plus ancien : on traite chronologiquement.
        for item in reversed(messages):
            mid = str(item.get('id') or '')
            text = (item.get('message') or '').strip()
            sender = item.get('from') or {}
            sender_id = str(sender.get('id') or '')
            if not text or sender_id != psid:
                continue
            if not _try_claim_mid(mid):
                continue
            if _should_skip_inbound(psid, text):
                continue
            sender_name = (sender.get('name') or fb_name or '').strip()
            try:
                result = process_inbound_private_message(
                    sender_id=psid,
                    message_text=text,
                    channel='Facebook',
                    page_id=page.page_id,
                    id_field='facebook_id',
                    sender_name=sender_name,
                )
                results.append({'mid': mid, 'psid': psid, **result})
                logger.info(
                    'Inbox sync page %s PSID %s → %s',
                    page.page_id,
                    psid,
                    result.get('status'),
                )
            except OrderConfirmationError as exc:
                # Erreur métier : on garde le mid sauf si une annulation reste possible
                # (ex. premier crash technique qui a laissé la commande active).
                if _is_cancellation(text) and Commande.objects.filter(
                    client__facebook_id=psid,
                    statut__in=CANCELLABLE_STATUSES,
                ).exists():
                    _release_mid(mid)
                results.append({'mid': mid, 'psid': psid, 'status': 'error', 'detail': exc.message})
                logger.warning(
                    'Inbox sync page %s PSID %s erreur métier: %s',
                    page.page_id,
                    psid,
                    exc.message,
                )
            except Exception:  # noqa: BLE001
                _release_mid(mid)
                logger.exception(
                    'Inbox sync page %s PSID %s échec technique (mid relâché)',
                    page.page_id,
                    psid,
                )
    return results


def sync_pending_messenger_inboxes() -> list[dict[str, Any]]:
    """Parcourt les pages qui ont encore des JP Facebook ouverts."""
    close_old_connections()
    all_results: list[dict[str, Any]] = []
    for page in _pending_facebook_pages():
        all_results.extend(sync_page_messenger_inbox(page))
    return all_results


class _MessengerInboxListener(threading.Thread):
    daemon = True

    def __init__(self, page: PageFacebook):
        super().__init__(name=f'azlive-messenger-inbox-{page.page_id}')
        self.page_id = str(page.page_id)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        logger.info('Messenger inbox listener démarré pour page %s', self.page_id)
        while not self._stop.wait(POLL_INTERVAL_SECONDS):
            close_old_connections()
            try:
                page = PageFacebook.objects.filter(page_id=self.page_id).first()
                if not page or not page.access_token:
                    continue
                sync_page_messenger_inbox(page)
            except Exception:  # noqa: BLE001
                logger.exception('Erreur inbox listener page %s', self.page_id)
        logger.info('Messenger inbox listener arrêté pour page %s', self.page_id)


def start_messenger_inbox_listener(page: PageFacebook) -> None:
    page_id = str(page.page_id)
    with _listeners_lock:
        existing = _inbox_listeners.get(page_id)
        if existing and existing.is_alive():
            return
        listener = _MessengerInboxListener(page)
        _inbox_listeners[page_id] = listener
        listener.start()


def stop_messenger_inbox_listener(page_id: str | None = None) -> None:
    with _listeners_lock:
        if page_id:
            listener = _inbox_listeners.pop(str(page_id), None)
            if listener:
                listener.stop()
        else:
            targets = list(_inbox_listeners.values())
            _inbox_listeners.clear()
            for listener in targets:
                listener.stop()
