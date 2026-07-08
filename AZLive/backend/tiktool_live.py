import json
import logging
import re
import threading
from datetime import datetime, timedelta
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from .jp_capture import (
    normalize_tiktok_username,
    process_social_comment,
    resolve_active_live,
    resolve_vendeur_from_tiktok_username,
)
from .models import Live

logger = logging.getLogger(__name__)

TIKTOOL_WS_BASE = 'wss://api.tik.tools'
TIKTOOL_CHECK_ALIVE_URL = 'https://api.tik.tools/webcast/check_alive'

_listeners: dict[int, '_TikToolLiveListener'] = {}
_listeners_lock = threading.Lock()


def tiktool_configured() -> bool:
    return bool(getattr(settings, 'TIKTOOL_API_KEY', ''))


def _is_valid_unique_id(unique_id: str) -> bool:
    return bool(re.fullmatch(r'[a-z0-9._-]+', unique_id or ''))


def _request_check_alive(*, unique_id: str | None = None, room_id: str | None = None) -> dict[str, Any] | None:
    params: dict[str, str] = {'apiKey': settings.TIKTOOL_API_KEY}
    if room_id:
        params['room_id'] = str(room_id)
    elif unique_id:
        params['unique_id'] = normalize_tiktok_username(unique_id)
    else:
        return None
    request = urllib.request.Request(
        f'{TIKTOOL_CHECK_ALIVE_URL}?{urllib.parse.urlencode(params)}',
        headers={'User-Agent': 'AZLive/1.0'},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode('utf-8', errors='replace'))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as exc:
        logger.warning('TikTools check_alive failed (%s/%s): %s', unique_id, room_id, exc)
        return None
    return payload if isinstance(payload, dict) else None


def _extract_room_id_from_resolve(payload: dict[str, Any]) -> str | None:
    resolve_url = str(payload.get('resolve_url') or '')
    if not resolve_url:
        return None

    headers = payload.get('resolve_headers') or {}
    request = urllib.request.Request(resolve_url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            html = response.read().decode('utf-8', errors='replace')
    except Exception as exc:  # noqa: BLE001
        logger.warning('TikTools resolve_url fetch failed (%s): %s', resolve_url, exc)
        return None

    patterns = payload.get('room_id_patterns') or []
    for pattern in patterns:
        try:
            match = re.search(pattern, html)
        except re.error:
            continue
        if match and match.group(1):
            return match.group(1)
    return None


def _parse_live_state(payload: dict[str, Any]) -> bool | None:
    if 'is_live' in payload:
        return bool(payload['is_live'])
    if 'data' in payload and isinstance(payload['data'], dict):
        return bool(payload['data'].get('is_live') or payload['data'].get('alive'))
    if 'alive' in payload or 'live' in payload:
        return bool(payload.get('alive') or payload.get('live'))
    return None


def _resolve_signed_live_state(payload: dict[str, Any]) -> bool | None:
    signed_url = str(payload.get('signed_url') or '')
    if not signed_url:
        return None
    headers = payload.get('headers') or {'User-Agent': 'AZLive/1.0'}
    request = urllib.request.Request(signed_url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            resolved = json.loads(response.read().decode('utf-8', errors='replace'))
    except Exception as exc:  # noqa: BLE001
        logger.warning('TikTools signed check fetch failed: %s', exc)
        return None

    if isinstance(resolved, dict):
        data = resolved.get('data')
        if isinstance(data, list) and data:
            alive = data[0].get('alive')
            if alive is not None:
                return bool(alive)
        return _parse_live_state(resolved)
    return None


def check_streamer_is_live(unique_id: str) -> bool | None:
    """Retourne True/False si TikTools répond, None si non configuré ou indéterminé."""
    if not tiktool_configured():
        return None
    normalized = normalize_tiktok_username(unique_id)
    if not _is_valid_unique_id(normalized):
        logger.warning(
            'TikTok unique_id invalide pour check_alive: %r (attendu ex: azplus.mg)',
            unique_id,
        )
        return None

    payload = _request_check_alive(unique_id=normalized)
    if not payload:
        return None

    state = _parse_live_state(payload)
    if state is not None:
        return state

    # Nouveau format TikTools: "resolve_required" => il faut résoudre room_id puis revérifier.
    if payload.get('action') == 'resolve_required':
        room_id = _extract_room_id_from_resolve(payload)
        if room_id:
            second = _request_check_alive(room_id=room_id)
            if second:
                state = _parse_live_state(second)
                if state is not None:
                    return state
                state = _resolve_signed_live_state(second)
                if state is not None:
                    return state
        return None

    return None


def build_tiktok_diffusion(live: Live) -> dict[str, Any] | None:
    username = live.vendeur.tiktok_username
    if not username:
        return None

    unique_id = normalize_tiktok_username(username)
    is_live = check_streamer_is_live(unique_id)

    return {
        'username': username,
        'unique_id': unique_id,
        'status': 'LIVE' if is_live else 'PENDING_MANUAL',
        'is_live_on_tiktok': is_live,
        'tiktool_listener': tiktool_configured(),
        'demo': False,
        'instructions': (
            'Lancez le live sur TikTok (app ou Live Center). '
            'Les commentaires JP seront capturés automatiquement via TikTools '
            'et une réponse avec le lien formulaire sera publiée dans le chat live.'
        ),
    }


def _upsert_tiktok_diffusion(
    live: Live,
    *,
    unique_id: str,
    username: str | None = None,
    status: str = 'LIVE',
    is_live: bool | None = True,
    listener: str | None = None,
) -> Live:
    diffusion = dict(live.diffusion_plateformes or {})
    current = dict(diffusion.get('tiktok') or {})
    merged = {
        **current,
        'status': status,
        'is_live_on_tiktok': is_live,
        'unique_id': unique_id,
        'username': username or current.get('username') or live.vendeur.tiktok_username,
        'demo': False,
        'updated_at': timezone.now().isoformat(),
    }
    if listener:
        merged['listener'] = listener
    diffusion['tiktok'] = merged
    live.diffusion_plateformes = diffusion
    live.save(update_fields=['diffusion_plateformes'])
    return live


def build_tiktok_confirmation_comment(live: Live) -> str:
    from .order_messaging import public_order_form_url

    return (
        "📦 Pour confirmer votre commande, cliquez ici :\n"
        f"{public_order_form_url(live.id)}"
    )


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def ensure_tiktok_confirmation_comment(live: Live, *, force: bool = False) -> dict[str, Any]:
    """Génère le lien/commentaire de confirmation dès détection du live.

    Flux principal (recommandé) :
    - le backend génère et stocke le lien + texte à coller ;
    - le vendeur copie depuis l'UI AZLive, colle et épingle manuellement sur TikTok.

    Option secondaire (si TIKTOK_SESSION_COOKIES est configuré) :
    - tentative d'envoi auto via TikTools chat-send (non officiel, fragile).
    """
    if live.statut != Live.STATUT_EN_COURS:
        return {'sent': False, 'detail': 'Live non actif.'}

    from .order_messaging import public_order_form_url
    from .tiktok_live_chat import send_tiktok_live_chat_message, tiktok_chat_send_configured

    diffusion = dict(live.diffusion_plateformes or {})
    tiktok_state = dict(diffusion.get('tiktok') or {})
    content = build_tiktok_confirmation_comment(live)
    link = public_order_form_url(live.id)
    now = timezone.now()

    # Toujours générer le lien (indépendamment des cookies).
    tiktok_state.update(
        {
            'confirmation_link': link,
            'confirmation_comment': content,
            'pin_supported': False,
            'pin_mode': 'manual',
            'pin_note': (
                'Copiez le commentaire depuis AZLive, collez-le dans le chat TikTok '
                'puis épinglez-le manuellement. Aucune API officielle ne permet le pin auto.'
            ),
        }
    )

    delivery: dict[str, Any] = {
        'sent': False,
        'mode': 'manual_copy',
        'confirmation_link': link,
        'confirmation_comment': content,
        'detail': 'Lien prêt à copier/épingler manuellement.',
    }

    # Envoi auto facultatif uniquement si cookies session configurés.
    if live.vendeur.tiktok_username and tiktok_chat_send_configured():
        cooldown_minutes = int(getattr(settings, 'TIKTOK_CONFIRMATION_COMMENT_REFRESH_MINUTES', 10))
        last_sent_at = _parse_iso_dt(tiktok_state.get('confirmation_comment_sent_at'))
        cooldown_ok = (
            force
            or last_sent_at is None
            or (now - last_sent_at) >= timedelta(minutes=max(cooldown_minutes, 1))
        )
        if cooldown_ok:
            delivery = send_tiktok_live_chat_message(live.vendeur.tiktok_username, content)
            delivery['mode'] = 'auto_chat_send'
            delivery['confirmation_link'] = link
            delivery['confirmation_comment'] = content
            if delivery.get('sent'):
                tiktok_state['confirmation_comment_sent_at'] = now.isoformat()
        else:
            delivery = {
                'sent': False,
                'skipped': True,
                'mode': 'auto_chat_send',
                'confirmation_link': link,
                'confirmation_comment': content,
                'detail': f'Cooldown actif ({cooldown_minutes} min).',
            }

    tiktok_state['confirmation_comment_delivery'] = delivery
    tiktok_state['confirmation_link_generated_at'] = now.isoformat()
    diffusion['tiktok'] = tiktok_state
    live.diffusion_plateformes = diffusion
    live.save(update_fields=['diffusion_plateformes'])
    return delivery


def ensure_tiktok_live_for_streamer(streamer_unique_id: str) -> Live | None:
    """Crée/active automatiquement un Live AZLive quand TikTok est détecté en direct."""
    vendeur = resolve_vendeur_from_tiktok_username(streamer_unique_id)
    if not vendeur:
        return None

    unique_id = normalize_tiktok_username(streamer_unique_id)
    now = timezone.now()

    live = (
        Live.objects.filter(vendeur=vendeur, statut=Live.STATUT_EN_COURS)
        .order_by('-date_live')
        .first()
    )
    if live:
        live = _upsert_tiktok_diffusion(
            live,
            unique_id=unique_id,
            username=vendeur.tiktok_username,
            status='LIVE',
            is_live=True,
        )
        ensure_tiktok_confirmation_comment(live)
        return live

    # Réutilise en priorité un live planifié récent du vendeur (dressing déjà préparé).
    window_start = now - timedelta(hours=24)
    live = (
        Live.objects.filter(
            vendeur=vendeur,
            statut=Live.STATUT_PLANIFIE,
            date_live__gte=window_start,
        )
        .order_by('date_live')
        .first()
    )
    if live is None:
        live = Live.objects.create(
            titre=f'Live TikTok @{unique_id}',
            vendeur=vendeur,
            statut=Live.STATUT_EN_COURS,
            date_live=now,
            date_debut=now,
        )
    else:
        live.statut = Live.STATUT_EN_COURS
        live.date_debut = live.date_debut or now
        live.date_live = now
        live.date_fin = None
        live.save(update_fields=['statut', 'date_debut', 'date_live', 'date_fin'])

    live = _upsert_tiktok_diffusion(
        live,
        unique_id=unique_id,
        username=vendeur.tiktok_username,
        status='LIVE',
        is_live=True,
    )
    ensure_tiktok_confirmation_comment(live, force=True)
    return live


def process_tiktool_chat_event(streamer_unique_id: str, event_data: dict[str, Any]) -> dict[str, Any]:
    user = event_data.get('user') or {}
    sender_id = str(user.get('uniqueId') or user.get('userId') or user.get('id') or '')
    sender_name = user.get('nickname') or user.get('uniqueId') or 'Client TikTok'
    comment_text = event_data.get('comment') or event_data.get('text') or ''

    vendeur = resolve_vendeur_from_tiktok_username(streamer_unique_id)
    live = ensure_tiktok_live_for_streamer(streamer_unique_id) if vendeur else None
    if live is None and vendeur:
        live = resolve_active_live(vendeur)

    return process_social_comment(
        sender_id=sender_id,
        sender_name=sender_name,
        comment_text=comment_text,
        channel='TikTok',
        vendeur=vendeur,
        live=live,
        id_field='tiktok_id',
    )


def _build_ws_url(unique_id: str) -> str:
    params = urllib.parse.urlencode(
        {
            'uniqueId': normalize_tiktok_username(unique_id),
            'apiKey': settings.TIKTOOL_API_KEY,
        }
    )
    return f'{TIKTOOL_WS_BASE}?{params}'


class _TikToolLiveListener(threading.Thread):
    daemon = True

    def __init__(self, live_id: int, unique_id: str, stop_event: threading.Event):
        super().__init__(name=f'tiktool-live-{live_id}')
        self.live_id = live_id
        self.unique_id = normalize_tiktok_username(unique_id)
        self.stop_event = stop_event

    def run(self):
        try:
            import websocket
        except ImportError:
            logger.error('websocket-client non installé: pip install websocket-client')
            return

        while not self.stop_event.is_set():
            ws_app = websocket.WebSocketApp(
                _build_ws_url(self.unique_id),
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            ws_app.run_forever(ping_interval=30, ping_timeout=10)
            if self.stop_event.wait(3):
                break

    def _on_message(self, _ws, message: str):
        close_old_connections()
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return

        if payload.get('event') != 'chat':
            return

        event_data = payload.get('data') or {}
        try:
            result = process_tiktool_chat_event(self.unique_id, event_data)
            if result.get('status') != 'ignored':
                logger.info(
                    'JP TikTok capturé (live #%s, streamer @%s): %s',
                    self.live_id,
                    self.unique_id,
                    result.get('status'),
                )
        except Exception as exc:
            logger.warning('Erreur capture JP TikTok (live #%s): %s', self.live_id, exc)

    def _on_error(self, _ws, error):
        logger.warning('TikTools WebSocket error (live #%s): %s', self.live_id, error)

    def _on_close(self, _ws, close_status_code, close_msg):
        logger.info(
            'TikTools WebSocket fermé (live #%s): %s %s',
            self.live_id,
            close_status_code,
            close_msg,
        )


def start_tiktool_listener(live: Live) -> bool:
    if not tiktool_configured() or live.vendeur.is_demo_mode:
        return False

    username = live.vendeur.tiktok_username
    if not username:
        return False

    unique_id = normalize_tiktok_username(username)
    stop_event = threading.Event()

    with _listeners_lock:
        stop_tiktool_listener(live, lock_held=True)
        listener = _TikToolLiveListener(live.pk, unique_id, stop_event)
        _listeners[live.pk] = listener
        listener.start()

    logger.info('TikTools listener démarré pour live #%s (@%s)', live.pk, unique_id)
    return True


def stop_tiktool_listener(live: Live, lock_held: bool = False) -> bool:
    live_id = live.pk

    def _stop():
        listener = _listeners.pop(live_id, None)
        if not listener:
            return False
        listener.stop_event.set()
        return True

    if lock_held:
        return _stop()

    with _listeners_lock:
        return _stop()


def listener_status(live_id: int) -> dict[str, Any]:
    with _listeners_lock:
        listener = _listeners.get(live_id)
        if not listener:
            return {'running': False}
        return {
            'running': listener.is_alive(),
            'unique_id': listener.unique_id,
            'thread': listener.name,
        }


def ensure_tiktool_listener(live: Live) -> bool:
    """Démarre/re-démarre le listener TikTok pour un live en cours."""
    if live.statut != Live.STATUT_EN_COURS or live.vendeur.is_demo_mode:
        return False
    status = listener_status(live.pk)
    if status.get('running'):
        return True
    started = start_tiktool_listener(live)
    if started and live.vendeur.tiktok_username:
        _upsert_tiktok_diffusion(
            live,
            unique_id=normalize_tiktok_username(live.vendeur.tiktok_username),
            username=live.vendeur.tiktok_username,
            status='LIVE',
            is_live=True,
            listener='running',
        )
        ensure_tiktok_confirmation_comment(live)
    return started


def recover_tiktool_listeners() -> int:
    """Relance les listeners TikTok pour les lives encore en cours après redémarrage Django."""
    restarted = 0
    lives = Live.objects.filter(statut=Live.STATUT_EN_COURS).select_related('vendeur')
    for live in lives:
        if not live.vendeur.tiktok_username:
            continue
        if ensure_tiktool_listener(live):
            restarted += 1
    return restarted
