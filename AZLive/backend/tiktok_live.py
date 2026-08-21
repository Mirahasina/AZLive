"""Gestion des lives TikTok AZLive et capture des commentaires via TikTokLive."""
import logging
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from .jp_capture import (
    normalize_tiktok_username,
    process_social_comment,
    resolve_active_live,
    resolve_vendeur_from_tiktok_username,
)
from .models import Live, Vendeur

logger = logging.getLogger(__name__)

_AUTO_LIVE_TITLE = re.compile(r'Live - TikTok - ([a-z0-9._-]+) - ', re.I)
_ensure_live_lock = threading.Lock()
_last_tiktok_sync_at: datetime | None = None
_tiktok_sync_lock = threading.Lock()
_last_vendeur_sync_at: dict[int, datetime] = {}
_last_end_reconcile_at: datetime | None = None


def tiktok_capture_configured() -> bool:
    from .tiktok_live_listener import tiktok_live_available

    return tiktok_live_available()


def _is_valid_unique_id(unique_id: str) -> bool:
    return bool(re.fullmatch(r'[a-z0-9._-]+', unique_id or ''))


def _try_fill_tiktok_username(vendeur: Vendeur) -> None:
    if vendeur.tiktok_username or not getattr(vendeur, 'tiktok_access_token', None):
        return
    from .tiktok_oauth import TikTokOAuthError, get_user_profile

    try:
        profile = get_user_profile(vendeur.tiktok_access_token)
    except TikTokOAuthError:
        return
    handle = normalize_tiktok_username(profile.get('username') or '')
    if not _is_valid_unique_id(handle):
        return
    vendeur.tiktok_username = f'@{handle}'
    vendeur.save(update_fields=['tiktok_username'])
    logger.info('Vendeur #%s : tiktok_username récupéré via OAuth → @%s', vendeur.pk, handle)


def _unique_ids_from_recent_lives(vendeur: Vendeur) -> list[str]:
    found: list[str] = []

    def _add(raw: str | None) -> None:
        handle = normalize_tiktok_username(raw)
        if _is_valid_unique_id(handle) and handle not in found:
            found.append(handle)

    recent = (
        Live.objects.filter(vendeur=vendeur)
        .order_by('-date_live', '-id')[:20]
    )
    for live in recent:
        match = _AUTO_LIVE_TITLE.search(live.titre or '')
        if match:
            _add(match.group(1))
        tiktok = dict((live.diffusion_plateformes or {}).get('tiktok') or {})
        _add(tiktok.get('unique_id'))
        _add(tiktok.get('username'))
    return found


def resolve_vendeur_tiktok_unique_id(vendeur: Vendeur) -> str | None:
    from_lives = _unique_ids_from_recent_lives(vendeur)
    if from_lives:
        return from_lives[0]
    candidate = normalize_tiktok_username(vendeur.tiktok_username)
    if _is_valid_unique_id(candidate):
        return candidate
    return None


def iter_connected_tiktok_vendeurs(*, vendeur_id: int | None = None):
    qs = (
        Vendeur.objects.exclude(tiktok_open_id__isnull=True)
        .exclude(tiktok_open_id='')
        .exclude(is_demo_mode=True)
        .order_by('id')
    )
    if vendeur_id is not None:
        qs = qs.filter(pk=vendeur_id)
    for vendeur in qs:
        unique_ids = _unique_ids_from_recent_lives(vendeur)
        if not unique_ids:
            oauth_handle = normalize_tiktok_username(vendeur.tiktok_username)
            if _is_valid_unique_id(oauth_handle):
                unique_ids.append(oauth_handle)
        if not unique_ids:
            _try_fill_tiktok_username(vendeur)
            unique_ids = _unique_ids_from_recent_lives(vendeur)
            oauth_handle = normalize_tiktok_username(vendeur.tiktok_username)
            if _is_valid_unique_id(oauth_handle) and oauth_handle not in unique_ids:
                unique_ids.append(oauth_handle)
        for unique_id in unique_ids[:1]:
            yield vendeur, unique_id


def _check_live_via_tiktok_page(unique_id: str) -> tuple[bool | None, str | None]:
    normalized = normalize_tiktok_username(unique_id)
    if not _is_valid_unique_id(normalized):
        return None, None

    url = f'https://www.tiktok.com/@{normalized}/live'
    request = urllib.request.Request(
        url,
        headers={
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/131.0.0.0 Safari/537.36'
            ),
            'Accept-Language': 'en-US,en;q=0.9',
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            html = response.read().decode('utf-8', errors='replace')
    except Exception as exc:  # noqa: BLE001
        logger.info('Page TikTok indisponible pour @%s: %s', normalized, exc)
        return None, None

    status_match = re.search(r'"status"\s*:\s*(\d+)', html)
    page_status = int(status_match.group(1)) if status_match else None

    room_patterns = [
        r'"roomId"\s*:\s*"(\d+)"',
        r'"room_id"\s*:\s*"?([0-9]+)"?',
        r"roomId[\"']?\s*[:=]\s*[\"']?(\d{15,})",
        r"liveRoomId[\"']?\s*[:=]\s*[\"']?(\d{15,})",
    ]
    room_id = None
    for pattern in room_patterns:
        match = re.search(pattern, html)
        if match:
            room_id = str(match.group(1))
            break

    if room_id is None:
        return None, None
    if page_status == 2:
        return True, room_id
    if page_status is not None and page_status != 2:
        return False, room_id
    return None, room_id


def check_streamer_is_live(unique_id: str, *, deep: bool = False) -> bool | None:
    """Vérifie si un compte TikTok est en direct (TikTokLive, puis page HTML)."""
    normalized = normalize_tiktok_username(unique_id)
    if not _is_valid_unique_id(normalized):
        logger.warning('TikTok unique_id invalide: %r', unique_id)
        return None

    from .tiktok_live_listener import check_streamer_is_live as _check_via_tiktoklive

    live_hint = _check_via_tiktoklive(normalized)
    if live_hint is not None:
        return live_hint

    if deep:
        page_hint, _room_id = _check_live_via_tiktok_page(normalized)
        return page_hint
    return None


def live_should_capture_tiktok_comments(live: Live) -> bool:
    """True si ce live AZLive en cours doit écouter le chat TikTok."""
    if live.statut != Live.STATUT_EN_COURS:
        return False
    if live.vendeur.is_demo_mode or not live.vendeur.tiktok_username:
        return False
    return bool((live.diffusion_plateformes or {}).get('tiktok'))


def build_tiktok_diffusion(live: Live) -> dict[str, Any] | None:
    username = live.vendeur.tiktok_username
    unique_id = normalize_tiktok_username(username)
    if not unique_id and not live.vendeur.tiktok_open_id:
        return None

    return {
        'username': username,
        'unique_id': unique_id or None,
        'status': 'PENDING_MANUAL',
        'is_live_on_tiktok': None,
        'comment_listener': tiktok_capture_configured(),
        'demo': False,
        'instructions': (
            'Lancez le live dans l’application TikTok. '
            'AZLive capture les commentaires JP automatiquement '
            'et n’envoie pas la caméra vers TikTok.'
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


def ensure_tiktok_confirmation_comment(live: Live, *, force: bool = False) -> dict[str, Any]:
    """Génère le lien/commentaire de confirmation à copier dans le chat TikTok."""
    del force  # conservé pour compatibilité des appels existants
    if live.statut != Live.STATUT_EN_COURS:
        return {'sent': False, 'detail': 'Live non actif.'}

    from .order_messaging import public_order_form_url

    diffusion = dict(live.diffusion_plateformes or {})
    tiktok_state = dict(diffusion.get('tiktok') or {})
    content = build_tiktok_confirmation_comment(live)
    link = public_order_form_url(live.id)
    now = timezone.now()

    tiktok_state.update(
        {
            'confirmation_link': link,
            'confirmation_comment': content,
            'pin_supported': False,
            'pin_mode': 'manual',
            'pin_note': (
                'Copiez le commentaire depuis AZLive, collez-le dans le chat TikTok '
                'puis épinglez-le manuellement.'
            ),
        }
    )
    delivery = {
        'sent': False,
        'mode': 'manual_copy',
        'confirmation_link': link,
        'confirmation_comment': content,
        'detail': 'Lien prêt à copier/épingler manuellement.',
    }
    tiktok_state['confirmation_comment_delivery'] = delivery
    tiktok_state['confirmation_link_generated_at'] = now.isoformat()
    diffusion['tiktok'] = tiktok_state
    live.diffusion_plateformes = diffusion
    live.save(update_fields=['diffusion_plateformes'])
    return delivery


def build_tiktok_live_title(unique_id: str, when=None) -> str:
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
    unique_id = normalize_tiktok_username(streamer_unique_id)
    vendeur = resolve_vendeur_from_tiktok_username(unique_id)
    if not vendeur:
        for candidate, uid in iter_connected_tiktok_vendeurs():
            if uid == unique_id:
                vendeur = candidate
                break
    if not vendeur:
        logger.warning(
            'Aucun vendeur AZLive pour @%s (tiktok_username ou compte connecté)',
            unique_id,
        )
        return None

    if not already_verified:
        verified = check_streamer_is_live(unique_id, deep=True)
        if verified is not True:
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
    with _ensure_live_lock:
        live = (
            Live.objects.filter(vendeur=vendeur, statut=Live.STATUT_EN_COURS)
            .order_by('-date_live')
            .first()
        )
        if live is None:
            recent_cutoff = now - timedelta(minutes=15)
            live = (
                Live.objects.filter(vendeur=vendeur, date_debut__gte=recent_cutoff)
                .order_by('-date_debut', '-id')
                .first()
            )
            if live and live.statut != Live.STATUT_EN_COURS:
                live.statut = Live.STATUT_EN_COURS
                live.date_fin = None
                live.date_live = now
                live.date_debut = live.date_debut or now
                live.save(update_fields=['statut', 'date_fin', 'date_live', 'date_debut'])

        if live:
            live = _upsert_tiktok_diffusion(
                live,
                unique_id=unique_id,
                username=vendeur.tiktok_username,
                status='LIVE',
                is_live=True,
            )
            ensure_tiktok_listener(live)
            try:
                ensure_tiktok_confirmation_comment(live)
            except Exception:
                logger.exception('Confirmation link non généré pour live #%s', live.pk)
            return live

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
        ensure_tiktok_listener(live)
        try:
            ensure_tiktok_confirmation_comment(live, force=True)
        except Exception:
            logger.exception('Confirmation link non généré pour live #%s', live.pk)
        return live


def process_tiktok_chat_event(streamer_unique_id: str, event_data: dict[str, Any]) -> dict[str, Any]:
    user = event_data.get('user') or {}
    sender_id = str(user.get('uniqueId') or user.get('userId') or user.get('id') or '')
    sender_name = user.get('nickname') or user.get('uniqueId') or 'Client TikTok'
    comment_text = event_data.get('comment') or event_data.get('text') or ''

    vendeur = resolve_vendeur_from_tiktok_username(streamer_unique_id)
    live = resolve_active_live(vendeur) if vendeur else None

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


def start_tiktok_listener(live: Live) -> bool:
    from .tiktok_live_listener import start_tiktok_live_listener

    return start_tiktok_live_listener(live)


def stop_tiktok_listener(live: Live, lock_held: bool = False) -> bool:
    from .tiktok_live_listener import stop_tiktok_live_listener

    return stop_tiktok_live_listener(live, lock_held=lock_held)


def listener_status(live_id: int) -> dict[str, Any]:
    from .tiktok_live_listener import listener_status as _listener_status

    return _listener_status(live_id)


def ensure_tiktok_listener(live: Live) -> bool:
    if not live_should_capture_tiktok_comments(live):
        return False
    status = listener_status(live.pk)
    if status.get('running'):
        return True
    started = start_tiktok_listener(live)
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


def find_active_tiktok_live_for_streamer(unique_id: str) -> Live | None:
    normalized = normalize_tiktok_username(unique_id)
    vendeur = resolve_vendeur_from_tiktok_username(normalized)
    if not vendeur:
        for candidate, uid in iter_connected_tiktok_vendeurs():
            if uid == normalized:
                vendeur = candidate
                break
    if not vendeur:
        return None

    for live in (
        Live.objects.filter(vendeur=vendeur, statut=Live.STATUT_EN_COURS)
        .select_related('vendeur')
        .order_by('-date_live')
    ):
        if _live_is_tiktok_tracked(live):
            return live
        titre = (live.titre or '').lower()
        if normalized in titre or 'tiktok' in titre:
            return live
    return None


def _live_is_tiktok_tracked(live: Live) -> bool:
    tiktok_state = dict((live.diffusion_plateformes or {}).get('tiktok') or {})
    return bool(tiktok_state)


def cloturer_tiktok_live(live: Live, *, reason: str = 'tiktok_stream_end') -> bool:
    if live.statut != Live.STATUT_EN_COURS:
        return False

    stop_tiktok_listener(live)

    diffusion = dict(live.diffusion_plateformes or {})
    tiktok_state = dict(diffusion.get('tiktok') or {})
    tiktok_state.update(
        {
            'status': 'ENDED',
            'is_live_on_tiktok': False,
            'listener': 'stopped',
            'ended_reason': reason,
            'updated_at': timezone.now().isoformat(),
        }
    )
    diffusion['tiktok'] = tiktok_state
    diffusion['stopped_at'] = timezone.now().isoformat()
    diffusion['stopped_reason'] = reason

    if _facebook_still_live(live):
        live.diffusion_plateformes = diffusion
        live.save(update_fields=['diffusion_plateformes'])
        logger.info(
            'TikTok terminé sur live #%s (%s) - Facebook encore live, statut AZLive inchangé',
            live.pk,
            reason,
        )
        return False

    try:
        from .facebook_live_comments import stop_facebook_comment_listener

        stop_facebook_comment_listener(live)
    except Exception:
        logger.exception('stop_facebook_comment_listener live #%s', live.pk)

    live.statut = Live.STATUT_TERMINE
    live.date_fin = timezone.now()
    live.diffusion_plateformes = diffusion
    live.save(update_fields=['statut', 'date_fin', 'diffusion_plateformes'])
    logger.info('Live #%s passé en terminé/archivé (%s)', live.pk, reason)
    return True


def cloturer_tiktok_lives_for_streamer(unique_id: str, *, reason: str = 'tiktok_stream_end') -> int:
    normalized = normalize_tiktok_username(unique_id)
    vendeur = resolve_vendeur_from_tiktok_username(normalized)
    if not vendeur:
        for candidate, uid in iter_connected_tiktok_vendeurs():
            if uid == normalized:
                vendeur = candidate
                break
    if not vendeur:
        return 0

    closed = 0
    active = list(
        Live.objects.filter(vendeur=vendeur, statut=Live.STATUT_EN_COURS)
        .select_related('vendeur')
        .order_by('-date_live')
    )
    for live in active:
        if not _live_is_tiktok_tracked(live):
            titre = (live.titre or '').lower()
            if 'tiktok' not in titre and normalized not in titre:
                continue
        before = live.statut
        cloturer_tiktok_live(live, reason=reason)
        live.refresh_from_db(fields=['statut'])
        if live.statut == Live.STATUT_TERMINE and before == Live.STATUT_EN_COURS:
            closed += 1
        elif before == Live.STATUT_EN_COURS and live.statut == Live.STATUT_EN_COURS:
            tiktok = dict((live.diffusion_plateformes or {}).get('tiktok') or {})
            if str(tiktok.get('status') or '').upper() == 'ENDED':
                closed += 1
    return closed


def reconcile_ended_tiktok_lives(
    *,
    min_interval_seconds: float = 60.0,
    vendeur_id: int | None = None,
) -> int:
    if not tiktok_capture_configured():
        return 0

    global _last_end_reconcile_at
    now = timezone.now()
    if (
        _last_end_reconcile_at is not None
        and (now - _last_end_reconcile_at).total_seconds() < max(min_interval_seconds, 25.0)
    ):
        return 0

    active_qs = Live.objects.filter(statut=Live.STATUT_EN_COURS)
    if vendeur_id is not None:
        active_qs = active_qs.filter(vendeur_id=vendeur_id)
    active = list(active_qs.select_related('vendeur').order_by('vendeur_id', '-date_live'))
    if not active:
        return 0

    _last_end_reconcile_at = now
    closed = 0
    seen_vendeurs: set[int] = set()

    for live in active:
        if live.vendeur_id in seen_vendeurs:
            continue
        if not _live_is_tiktok_tracked(live):
            titre = (live.titre or '').lower()
            if 'tiktok' not in titre:
                continue

        tiktok = dict((live.diffusion_plateformes or {}).get('tiktok') or {})
        unique_id = normalize_tiktok_username(
            str(tiktok.get('unique_id') or tiktok.get('username') or '')
        )
        if not unique_id:
            unique_id = resolve_vendeur_tiktok_unique_id(live.vendeur)
        if not unique_id:
            continue
        seen_vendeurs.add(live.vendeur_id)

        started_at = live.date_debut or live.date_live
        if started_at and (now - started_at).total_seconds() < 60:
            continue

        is_live = check_streamer_is_live(unique_id, deep=True)
        if is_live is True:
            continue
        if is_live is False:
            n = cloturer_tiktok_lives_for_streamer(unique_id, reason='tiktok_offline_reconcile')
            closed += n
            logger.info(
                'Reconcile TikTok @%s : offline → %s live(s) clôturé(s)',
                unique_id,
                n,
            )
    return closed


def sync_external_tiktok_lives(
    *,
    min_interval_seconds: float = 120.0,
    vendeur_id: int | None = None,
    rest: bool = True,
    wait_ws_seconds: float = 20.0,
    close_if_offline: bool = True,
) -> dict[str, int]:
    del rest, wait_ws_seconds, close_if_offline  # conservés pour compatibilité CLI
    global _last_tiktok_sync_at

    if not tiktok_capture_configured():
        return {'started': 0, 'stopped': 0, 'skipped': 0}

    now = timezone.now()
    with _tiktok_sync_lock:
        if vendeur_id is not None:
            last_v = _last_vendeur_sync_at.get(vendeur_id)
            if last_v is not None and (now - last_v).total_seconds() < max(min_interval_seconds, 1.0):
                return {'started': 0, 'stopped': 0, 'skipped': 0, 'throttled': 1}
            _last_vendeur_sync_at[vendeur_id] = now
        else:
            if (
                _last_tiktok_sync_at is not None
                and (now - _last_tiktok_sync_at).total_seconds() < max(min_interval_seconds, 1.0)
            ):
                return {'started': 0, 'stopped': 0, 'skipped': 0, 'throttled': 1}
            _last_tiktok_sync_at = now

    from .tiktok_live_listener import sync_tiktok_lives

    return sync_tiktok_lives(vendeur_id=vendeur_id)


def kick_tiktok_live_detection(*, vendeur_id: int | None = None) -> dict[str, int]:
    """Détection + clôture : crée un live AZLive si TikTok est actif, clôture sinon."""
    if not tiktok_capture_configured():
        return {'started': 0, 'stopped': 0, 'skipped': 0}

    started = 0
    skipped = 0

    for vendeur, unique_id in iter_connected_tiktok_vendeurs(vendeur_id=vendeur_id):
        already = (
            Live.objects.filter(vendeur=vendeur, statut=Live.STATUT_EN_COURS)
            .order_by('-date_live')
            .first()
        )
        if already is not None:
            ensure_tiktok_listener(already)
            started += 1
            continue

        is_live = check_streamer_is_live(unique_id, deep=True)
        if is_live is True:
            live = ensure_tiktok_live_for_streamer(unique_id, already_verified=True)
            if live:
                ensure_tiktok_listener(live)
                started += 1
        else:
            skipped += 1

    stopped = reconcile_ended_tiktok_lives(min_interval_seconds=30.0, vendeur_id=vendeur_id)
    return {'started': started, 'stopped': stopped, 'skipped': skipped}


def recover_tiktok_listeners() -> int:
    """Relance l'écoute uniquement pour les lives déjà en cours avec TikTok actif."""
    restarted = 0
    lives = Live.objects.filter(statut=Live.STATUT_EN_COURS).select_related('vendeur')
    for live in lives:
        if not live_should_capture_tiktok_comments(live):
            continue
        if ensure_tiktok_listener(live):
            restarted += 1
    return restarted
