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
from .models import Live, Vendeur

logger = logging.getLogger(__name__)

TIKTOOL_WS_BASE = 'wss://api.tik.tools'
TIKTOOL_CHECK_ALIVE_URL = 'https://api.tik.tools/webcast/check_alive'
TIKTOOL_LIVE_STATUS_URL = 'https://api.tik.tools/webcast/live_status'
TIKTOOL_ROOM_ID_URL = 'https://api.tik.tools/webcast/room_id'

_listeners: dict[int, '_TikToolLiveListener'] = {}
_scouts: dict[str, '_TikToolLiveListener'] = {}
_listeners_lock = threading.Lock()
_last_tiktok_sync_at: datetime | None = None
_tiktok_sync_lock = threading.Lock()
_rate_limited_until: datetime | None = None
_rate_limit_lock = threading.Lock()


def tiktool_configured() -> bool:
    return bool(getattr(settings, 'TIKTOOL_API_KEY', ''))


def _mark_rate_limited(seconds: float = 45.0) -> None:
    """Courte pause après un 429 TikTools (évite de marteler l'API)."""
    global _rate_limited_until
    until = timezone.now() + timedelta(seconds=max(seconds, 15.0))
    with _rate_limit_lock:
        if _rate_limited_until is None or until > _rate_limited_until:
            _rate_limited_until = until
            logger.warning(
                'TikTools rate-limit 429 : pause API jusqu’à %s',
                _rate_limited_until.isoformat(),
            )


def _tiktool_is_rate_limited() -> bool:
    with _rate_limit_lock:
        if _rate_limited_until is None:
            return False
        if timezone.now() >= _rate_limited_until:
            return False
        return True


def _is_valid_unique_id(unique_id: str) -> bool:
    return bool(re.fullmatch(r'[a-z0-9._-]+', unique_id or ''))


def _tiktool_get(url: str, params: dict[str, str]) -> dict[str, Any] | None:
    if _tiktool_is_rate_limited():
        return None
    query = dict(params)
    query['apiKey'] = settings.TIKTOOL_API_KEY
    request = urllib.request.Request(
        f'{url}?{urllib.parse.urlencode(query)}',
        headers={'User-Agent': 'AZLive/1.0'},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode('utf-8', errors='replace'))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            _mark_rate_limited(45.0)
            return None
        logger.warning('TikTools GET %s failed: %s', url, exc)
        return None
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        logger.warning('TikTools GET %s failed: %s', url, exc)
        return None
    return payload if isinstance(payload, dict) else None


def _tiktool_post(url: str, body: dict[str, Any]) -> dict[str, Any] | None:
    if _tiktool_is_rate_limited():
        return None
    query = urllib.parse.urlencode({'apiKey': settings.TIKTOOL_API_KEY})
    request = urllib.request.Request(
        f'{url}?{query}',
        data=json.dumps(body).encode('utf-8'),
        method='POST',
        headers={
            'Content-Type': 'application/json',
            'User-Agent': 'AZLive/1.0',
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode('utf-8', errors='replace'))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            _mark_rate_limited(45.0)
            return None
        logger.warning('TikTools POST %s failed: %s', url, exc)
        return None
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        logger.warning('TikTools POST %s failed: %s', url, exc)
        return None
    return payload if isinstance(payload, dict) else None


def _request_check_alive(*, unique_id: str | None = None, room_id: str | None = None) -> dict[str, Any] | None:
    params: dict[str, str] = {}
    if room_id:
        params['room_id'] = str(room_id)
    elif unique_id:
        params['unique_id'] = normalize_tiktok_username(unique_id)
    else:
        return None
    return _tiktool_get(TIKTOOL_CHECK_ALIVE_URL, params)


def _extract_room_id(payload: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get('data')
    if isinstance(data, dict) and data.get('room_id'):
        return str(data.get('room_id'))
    if payload.get('room_id'):
        return str(payload.get('room_id'))
    return None


def _check_live_via_live_status(unique_id: str) -> tuple[bool | None, str | None]:
    """Pré-check relay TikTools (cache ~90s).

    Retourne `(is_live, room_id)` :
    - True si `is_live` est True (assez fiable)
    - False si `is_live` est False (peut être un cache stale — à confirmer)
    - (None, …) si la requête a échoué ou le champ est absent
    """
    payload = _tiktool_get(TIKTOOL_LIVE_STATUS_URL, {'unique_id': unique_id})
    room_id = _extract_room_id(payload)
    if not payload:
        return None, room_id
    data = payload.get('data')
    if isinstance(data, dict) and 'is_live' in data:
        return bool(data.get('is_live')), room_id
    return _parse_live_state(payload), room_id


def _check_live_via_room_id(unique_id: str) -> tuple[bool | None, str | None]:
    """Fallback TikTools : POST /webcast/room_id → alive.

    Comme live_status, on ne fait confiance qu'à alive=True. Un False caché
    peut correspondre à l'ancien room_id d'un live déjà terminé.
    """
    payload = _tiktool_post(TIKTOOL_ROOM_ID_URL, {'unique_id': unique_id})
    room_id = _extract_room_id(payload)
    if not payload:
        return None, room_id
    data = payload.get('data')
    if isinstance(data, dict) and data.get('alive') is True:
        return True, room_id
    # room_id sans alive (souvent l'ancien room d'un live terminé) → indéterminé.
    state = _parse_live_state(payload)
    if state is True:
        return True, room_id
    return None, room_id


def _extract_room_id_from_resolve(payload: dict[str, Any]) -> tuple[str | None, str]:
    """Retourne (room_id, status) où status ∈ {ok, waf, empty, error, missing}.

    `empty` = page HTML récupérée mais sans roomId (typiquement offline).
    `waf` = page bloquée TikTok → indéterminée.
    """
    resolve_url = str(payload.get('resolve_url') or '')
    if not resolve_url:
        return None, 'missing'

    headers = payload.get('resolve_headers') or {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
        ),
    }
    request = urllib.request.Request(resolve_url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            html = response.read().decode('utf-8', errors='replace')
    except Exception as exc:  # noqa: BLE001
        logger.warning('TikTools resolve_url fetch failed (%s): %s', resolve_url, exc)
        return None, 'error'

    # TikTok renvoie parfois une page WAF (« Please wait ») sans roomId.
    if len(html) < 5000 and ('Please wait' in html or 'SlardarWAF' in html):
        logger.warning('TikTok live page bloquée par WAF (%s)', resolve_url)
        return None, 'waf'

    patterns = list(payload.get('room_id_patterns') or [])
    patterns.extend(
        [
            r'"roomId"\s*:\s*"(\d+)"',
            r'"roomId"\s*:\s*(\d+)',
            r'"room_id"\s*:\s*"?(\d+)"?',
            r'roomId\\":\\"(\d+)',
            r'"liveRoomId"\s*:\s*"?(\d+)"?',
        ]
    )
    for pattern in patterns:
        try:
            match = re.search(pattern, html)
        except re.error:
            continue
        if match and match.group(1):
            return match.group(1), 'ok'
    return None, 'empty'


def _parse_live_state(payload: dict[str, Any]) -> bool | None:
    if 'is_live' in payload:
        return bool(payload['is_live'])
    if 'alive' in payload:
        return bool(payload['alive'])
    if 'data' in payload and isinstance(payload['data'], dict):
        data = payload['data']
        if 'is_live' in data:
            return bool(data.get('is_live'))
        if 'alive' in data:
            return bool(data.get('alive'))
        if data.get('live') is not None:
            return bool(data.get('live'))
    if 'live' in payload:
        return bool(payload.get('live'))
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


def _check_alive_for_room(room_id: str) -> bool | None:
    """Vérifie définitivement un room_id via /webcast/check_alive (+ signed_url)."""
    payload = _request_check_alive(room_id=str(room_id))
    if not payload:
        return None
    state = _parse_live_state(payload)
    if state is not None:
        return state
    return _resolve_signed_live_state(payload)


def check_streamer_is_live(unique_id: str, *, deep: bool = False) -> bool | None:
    """Statut live TikTok avec un minimum d'appels REST.

    Mode normal (watchdog) :
    - 1× `live_status`
    - si True → 1× `check_alive` pour confirmer (anti faux positif)
    - si False → cache stale possible → None (ne ferme pas / ne crée pas ; les WS rattrapent)
    - si erreur / 429 → None

    Mode `deep=True` : ne fait pas confiance au False caché ; confirme via room_id / check_alive.
    """
    if not tiktool_configured() or _tiktool_is_rate_limited():
        return None
    normalized = normalize_tiktok_username(unique_id)
    if not _is_valid_unique_id(normalized):
        logger.warning(
            'TikTok unique_id invalide pour check_alive: %r (attendu ex: azplus.mg)',
            unique_id,
        )
        return None

    status_hint, room_id = _check_live_via_live_status(normalized)

    if status_hint is True:
        if room_id:
            confirmed = _check_alive_for_room(room_id)
            if confirmed is not None:
                return confirmed
            # check_alive indéterminé : True live_status reste un signal suffisant
            # pour démarrer (les scouts WS confirment ensuite).
            return True
        alive_payload = _request_check_alive(unique_id=normalized)
        if not alive_payload:
            return True  # live_status True sans REST dispo
        state = _parse_live_state(alive_payload)
        return state if state is not None else True

    # False en cache n'est PAS définitif (live qui vient de démarrer / hors relay).
    if status_hint is False and not deep:
        return None

    if status_hint is None and not deep:
        return None

    # Mode deep (ou False à confirmer) : fallbacks.
    definitive_false = False
    known_room_ids: list[str] = []
    if room_id:
        known_room_ids.append(room_id)
        room_state = _check_alive_for_room(room_id)
        if room_state is True:
            return True
        if room_state is False:
            definitive_false = True

    if not known_room_ids:
        _room_hint, new_room = _check_live_via_room_id(normalized)
        if new_room:
            known_room_ids.append(new_room)
            room_state = _check_alive_for_room(new_room)
            if room_state is True:
                return True
            if room_state is False:
                definitive_false = True

    alive_payload = _request_check_alive(unique_id=normalized)
    if alive_payload:
        state = _parse_live_state(alive_payload)
        if state is True:
            return True
        if state is False:
            definitive_false = True
        if alive_payload.get('action') == 'resolve_required':
            scraped_room, resolve_status = _extract_room_id_from_resolve(alive_payload)
            if scraped_room:
                state = _check_alive_for_room(scraped_room)
                if state is True:
                    return True
                if state is False:
                    definitive_false = True
            elif resolve_status in {'waf', 'error', 'missing'}:
                return None
            elif resolve_status == 'empty':
                definitive_false = True
        if alive_payload.get('signed_url'):
            state = _resolve_signed_live_state(alive_payload)
            if state is True:
                return True
            if state is False:
                definitive_false = True

    if definitive_false:
        return False
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


def build_tiktok_live_title(unique_id: str, when=None) -> str:
    """Nom auto : Live - TikTok - {compte} - {YYYY-MM-DD HH:mm:ss} (heure Madagascar)."""
    from zoneinfo import ZoneInfo

    moment = when or timezone.now()
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.utc)
    local = moment.astimezone(ZoneInfo('Indian/Antananarivo'))
    return f'Live - TikTok - {unique_id} - {local.strftime("%Y-%m-%d %H:%M:%S")}'


def ensure_tiktok_live_for_streamer(
    streamer_unique_id: str,
    *,
    already_verified: bool = False,
) -> Live | None:
    """Crée/active un Live AZLive quand TikTok est réellement en direct.

    `already_verified=True` : preuve WS (chat / streamStart) — pas de gate REST
    (indispensable quand TikTools est en 429).
    """
    vendeur = resolve_vendeur_from_tiktok_username(streamer_unique_id)
    if not vendeur:
        return None

    unique_id = normalize_tiktok_username(streamer_unique_id)
    if not already_verified:
        verified = check_streamer_is_live(unique_id, deep=True)
        if verified is not True:
            # Si REST bloqué (429) mais un live en_cours existe déjà, on le renvoie.
            existing = (
                Live.objects.filter(vendeur=vendeur, statut=Live.STATUT_EN_COURS)
                .order_by('-date_live')
                .first()
            )
            if existing is None:
                logger.info(
                    'Pas de création Live pour @%s : live TikTok non confirmé (%s)',
                    unique_id,
                    verified,
                )
                return None
            return existing

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
        # Listener d'abord : la génération du lien ne doit pas bloquer la capture JP.
        ensure_tiktool_listener(live)
        try:
            ensure_tiktok_confirmation_comment(live)
        except Exception:
            logger.exception('Confirmation link non généré pour live #%s', live.pk)
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
    auto_title = build_tiktok_live_title(unique_id, now)
    if live is None:
        live = Live.objects.create(
            titre=auto_title,
            vendeur=vendeur,
            statut=Live.STATUT_EN_COURS,
            date_live=now,
            date_debut=now,
        )
    else:
        live.titre = auto_title
        live.statut = Live.STATUT_EN_COURS
        live.date_debut = live.date_debut or now
        live.date_live = now
        live.date_fin = None
        live.save(update_fields=['titre', 'statut', 'date_debut', 'date_live', 'date_fin'])

    live = _upsert_tiktok_diffusion(
        live,
        unique_id=unique_id,
        username=vendeur.tiktok_username,
        status='LIVE',
        is_live=True,
    )
    ensure_tiktool_listener(live)
    try:
        ensure_tiktok_confirmation_comment(live, force=True)
    except Exception:
        logger.exception('Confirmation link non généré pour live #%s', live.pk)
    return live


def process_tiktool_chat_event(streamer_unique_id: str, event_data: dict[str, Any]) -> dict[str, Any]:
    user = event_data.get('user') or {}
    sender_id = str(user.get('uniqueId') or user.get('userId') or user.get('id') or '')
    sender_name = user.get('nickname') or user.get('uniqueId') or 'Client TikTok'
    comment_text = event_data.get('comment') or event_data.get('text') or ''

    vendeur = resolve_vendeur_from_tiktok_username(streamer_unique_id)
    # Un commentaire chat n'arrive que si le room est actif → preuve suffisante.
    live = (
        ensure_tiktok_live_for_streamer(streamer_unique_id, already_verified=True)
        if vendeur
        else None
    )
    if live is None and vendeur:
        live = resolve_active_live(vendeur)

    result = process_social_comment(
        sender_id=sender_id,
        sender_name=sender_name,
        comment_text=comment_text,
        channel='TikTok',
        vendeur=vendeur,
        live=live,
        id_field='tiktok_id',
    )
    if live is not None and 'live_id' not in result:
        result = {**result, 'live_id': live.id}
    return result


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

    def __init__(
        self,
        live_id: int | None,
        unique_id: str,
        stop_event: threading.Event,
        *,
        scout: bool = False,
    ):
        super().__init__(name=f'tiktool-{"scout" if scout else "live"}-{unique_id}')
        self.live_id = live_id
        self.unique_id = normalize_tiktok_username(unique_id)
        self.stop_event = stop_event
        self.scout = scout

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
                on_open=self._on_open,
            )
            ws_app.run_forever(ping_interval=30, ping_timeout=10)
            if self.stop_event.wait(3):
                break

    def _on_open(self, _ws):
        # Connexion scout seule ≠ live (évite les faux positifs). Pas de création ici.
        logger.info('TikTools WS connecté (@%s)', self.unique_id)

    def _ensure_live_from_ws_signal(self, reason: str) -> None:
        """streamStart / chat = preuve de room actif → créer sans gate REST."""
        try:
            live = ensure_tiktok_live_for_streamer(self.unique_id, already_verified=True)
            if live:
                self.live_id = live.pk
                logger.info(
                    'Live AZLive #%s créé/activé via WS %s (@%s)',
                    live.pk,
                    reason,
                    self.unique_id,
                )
        except Exception:
            logger.exception('ensure live via WS %s (@%s)', reason, self.unique_id)

    def _on_message(self, _ws, message: str):
        close_old_connections()
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return

        event = payload.get('event')
        # Signaux forts de live réel (pas connected / member seuls).
        if event in {'streamStart', 'stream_start', 'liveStart', 'live_start'}:
            self._ensure_live_from_ws_signal(event)
            return

        if event != 'chat':
            return

        # Premier chat = quelqu'un stream → créer le live avant de parser JP.
        if not self.live_id:
            self._ensure_live_from_ws_signal('chat')

        event_data = payload.get('data') or {}
        try:
            result = process_tiktool_chat_event(self.unique_id, event_data)
            if result.get('live_id'):
                self.live_id = result.get('live_id')
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
        logger.warning('TikTools WebSocket error (@%s / live #%s): %s', self.unique_id, self.live_id, error)

    def _on_close(self, _ws, close_status_code, close_msg):
        logger.info(
            'TikTools WebSocket fermé (@%s / live #%s): %s %s',
            self.unique_id,
            self.live_id,
            close_status_code,
            close_msg,
        )


def _start_listener_locked(unique_id: str, live_id: int | None = None, *, scout: bool = False) -> '_TikToolLiveListener':
    stop_event = threading.Event()
    listener = _TikToolLiveListener(live_id, unique_id, stop_event, scout=scout)
    if scout:
        old = _scouts.get(unique_id)
        if old and old is not listener:
            old.stop_event.set()
        _scouts[unique_id] = listener
    if live_id:
        old_live = _listeners.get(live_id)
        if old_live and old_live is not listener and not old_live.scout:
            old_live.stop_event.set()
        _listeners[live_id] = listener
    listener.start()
    return listener


def start_tiktool_listener(live: Live) -> bool:
    if not tiktool_configured() or live.vendeur.is_demo_mode:
        return False

    username = live.vendeur.tiktok_username
    if not username:
        return False

    unique_id = normalize_tiktok_username(username)
    with _listeners_lock:
        # Réutilise le scout déjà connecté pour cet unique_id (évite 2 WS).
        scout = _scouts.get(unique_id)
        if scout and scout.is_alive():
            scout.live_id = live.pk
            scout.scout = True
            _listeners[live.pk] = scout
            logger.info('TikTools scout réutilisé pour live #%s (@%s)', live.pk, unique_id)
            return True

        stop_tiktool_listener(live, lock_held=True)
        _start_listener_locked(unique_id, live.pk, scout=True)

    logger.info('TikTools listener démarré pour live #%s (@%s)', live.pk, unique_id)
    return True


def stop_tiktool_listener(live: Live, lock_held: bool = False) -> bool:
    live_id = live.pk

    def _stop():
        listener = _listeners.pop(live_id, None)
        if not listener:
            return False
        # Si c'est aussi le scout du compte, on le détache du live mais on le laisse tourner
        # pour redécouvrir le prochain direct TikTok.
        if _scouts.get(listener.unique_id) is listener:
            listener.live_id = None
            return True
        listener.stop_event.set()
        return True

    if lock_held:
        return _stop()

    with _listeners_lock:
        return _stop()


def ensure_tiktok_scouts() -> int:
    """Maintient un WebSocket TikTools par compte vendeur configuré.

    Indispensable : `live_status` ne voit un créateur que s'il est déjà sur le relay.
    Sans scout WS, un live fraîchement lancé (ex. @azplus.mg) reste invisible.
    """
    if not tiktool_configured():
        return 0
    started = 0
    vendors = (
        Vendeur.objects.exclude(tiktok_username__isnull=True)
        .exclude(tiktok_username='')
        .exclude(is_demo_mode=True)
        .order_by('id')
    )
    for vendeur in vendors:
        unique_id = normalize_tiktok_username(vendeur.tiktok_username)
        if not _is_valid_unique_id(unique_id):
            continue
        with _listeners_lock:
            existing = _scouts.get(unique_id)
            if existing and existing.is_alive():
                continue
            _start_listener_locked(unique_id, live_id=None, scout=True)
            started += 1
            logger.info('TikTools scout démarré pour @%s', unique_id)
    return started


def listener_status(live_id: int) -> dict[str, Any]:
    with _listeners_lock:
        listener = _listeners.get(live_id)
        if not listener:
            return {'running': False}
        return {
            'running': listener.is_alive(),
            'unique_id': listener.unique_id,
            'thread': listener.name,
            'scout': listener.scout,
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
        try:
            ensure_tiktok_confirmation_comment(live)
        except Exception:
            logger.exception(
                'Confirmation link non généré après démarrage listener live #%s',
                live.pk,
            )
    return started


def _facebook_still_live(live: Live) -> bool:
    broadcasts = list((live.diffusion_plateformes or {}).get('facebook') or [])
    for item in broadcasts:
        if str(item.get('status') or '').upper() in {'LIVE', 'LIVE_NOW'}:
            return True
    return False


def sync_external_tiktok_lives(*, min_interval_seconds: float = 45.0) -> dict[str, int]:
    """Détecte les lives TikTok externes et aligne la table Live + listeners.

    Appelé par le watchdog Django et la commande `sync_tiktok_lives`.
    Ne démarre un Live AZLive que si check_alive confirme un vrai direct.
    """
    global _last_tiktok_sync_at

    if not tiktool_configured():
        return {'started': 0, 'stopped': 0, 'skipped': 0}

    now = timezone.now()
    with _tiktok_sync_lock:
        if (
            _last_tiktok_sync_at is not None
            and (now - _last_tiktok_sync_at).total_seconds() < max(min_interval_seconds, 1.0)
        ):
            return {'started': 0, 'stopped': 0, 'skipped': 0, 'throttled': 1}
        _last_tiktok_sync_at = now

    started = 0
    stopped = 0
    skipped = 0

    # Scouts WS : détection temps réel (streamStart / chat) sans spam REST.
    try:
        ensure_tiktok_scouts()
    except Exception:
        logger.exception('ensure_tiktok_scouts a échoué')

    # Pendant un 429 : garder les WS, ne plus appeler l'API REST.
    if _tiktool_is_rate_limited():
        return {'started': 0, 'stopped': 0, 'skipped': 0, 'rate_limited': 1}

    vendors = (
        Vendeur.objects.exclude(tiktok_username__isnull=True)
        .exclude(tiktok_username='')
        .order_by('id')
    )
    for vendeur in vendors:
        if _tiktool_is_rate_limited():
            skipped += 1
            break

        unique_id = normalize_tiktok_username(vendeur.tiktok_username)
        if not _is_valid_unique_id(unique_id):
            skipped += 1
            continue

        is_live = check_streamer_is_live(unique_id)
        # Si un live AZLive est déjà en_cours, confirmer vraiment (éviter de rester
        # bloqué faute de False non-définitif en mode léger).
        if is_live is None:
            has_active = Live.objects.filter(
                vendeur=vendeur, statut=Live.STATUT_EN_COURS
            ).exists()
            if has_active:
                is_live = check_streamer_is_live(unique_id, deep=True)
        if is_live is None:
            skipped += 1
            continue

        if is_live:
            live = ensure_tiktok_live_for_streamer(unique_id, already_verified=True)
            if live:
                ensure_tiktok_confirmation_comment(live)
                ensure_tiktool_listener(live)
                started += 1
            continue

        # TikTok offline : clôturer les lives AZLive liés à ce live TikTok.
        active_lives = list(
            Live.objects.filter(vendeur=vendeur, statut=Live.STATUT_EN_COURS).order_by('-date_live')
        )
        for live in active_lives:
            tiktok_state = dict((live.diffusion_plateformes or {}).get('tiktok') or {})
            was_tiktok_tracked = bool(
                tiktok_state.get('unique_id')
                or tiktok_state.get('is_live_on_tiktok')
                or str(tiktok_state.get('status') or '').upper() in {'LIVE', 'PENDING_MANUAL', 'ENDED'}
            )
            if not was_tiktok_tracked:
                continue

            stop_tiktool_listener(live)
            diffusion = dict(live.diffusion_plateformes or {})
            tiktok_state = dict(diffusion.get('tiktok') or {})
            tiktok_state.update(
                {
                    'status': 'ENDED',
                    'is_live_on_tiktok': False,
                    'listener': 'stopped',
                    'updated_at': timezone.now().isoformat(),
                }
            )
            diffusion['tiktok'] = tiktok_state

            if not _facebook_still_live(live):
                live.statut = Live.STATUT_TERMINE
                live.date_fin = timezone.now()
                live.diffusion_plateformes = diffusion
                live.save(update_fields=['statut', 'date_fin', 'diffusion_plateformes'])
                stopped += 1
            else:
                live.diffusion_plateformes = diffusion
                live.save(update_fields=['diffusion_plateformes'])

    return {'started': started, 'stopped': stopped, 'skipped': skipped}


def recover_tiktool_listeners() -> int:
    """Relance les scouts TikTok + listeners des lives encore en cours après redémarrage Django."""
    restarted = 0
    try:
        restarted += ensure_tiktok_scouts()
    except Exception:
        logger.exception('recover: ensure_tiktok_scouts a échoué')

    lives = Live.objects.filter(statut=Live.STATUT_EN_COURS).select_related('vendeur')
    for live in lives:
        if not live.vendeur.tiktok_username:
            continue
        if ensure_tiktool_listener(live):
            restarted += 1
    return restarted
