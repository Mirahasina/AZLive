import uuid
from typing import Any

from django.conf import settings
from django.utils import timezone

from .facebook_oauth import FacebookOAuthError, _graph_request, facebook_configured
from .models import Live, PageFacebook


class FacebookLiveError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def resolve_live_pages(live: Live):
    vendeur = live.vendeur
    selected = live.pages_facebook or []
    queryset = PageFacebook.objects.filter(vendeur=vendeur)

    if selected:
        pages = []
        for item in selected:
            page = queryset.filter(nom=item).first() or queryset.filter(page_id=str(item)).first()
            if page:
                pages.append(page)
        return pages

    return list(queryset.filter(statut=PageFacebook.STATUT_PRET))


def create_facebook_live_broadcast(page: PageFacebook, title: str, description: str = '') -> dict[str, Any]:
    if not page.access_token:
        raise FacebookLiveError(f"Aucun token pour la page {page.nom}.")

    payload = _graph_request(
        f'{page.page_id}/live_videos',
        {
            'title': title,
            'description': description or title,
            'status': settings.FACEBOOK_LIVE_STATUS,
            'access_token': page.access_token,
        },
        method='POST',
    )
    return {
        'page_id': page.page_id,
        'page_name': page.nom,
        'live_video_id': payload.get('id'),
        'status': 'LIVE',
        'stream_url': payload.get('stream_url'),
        'secure_stream_url': payload.get('secure_stream_url'),
        'embed_url': payload.get('embed_html'),
    }


def end_facebook_live_broadcast(live_video_id: str, page_access_token: str) -> dict[str, Any]:
    return _graph_request(
        live_video_id,
        {
            'end_live_video': 'true',
            'access_token': page_access_token,
        },
        method='POST',
    )


def create_demo_facebook_broadcasts(live: Live, pages: list[PageFacebook]) -> list[dict[str, Any]]:
    broadcasts = []
    for page in pages:
        broadcasts.append(
            {
                'page_id': page.page_id,
                'page_name': page.nom,
                'live_video_id': f'demo_live_{page.page_id}_{uuid.uuid4().hex[:8]}',
                'status': 'LIVE',
                'stream_url': f'rtmp://live.demo.azlive/{page.page_id}',
                'embed_url': f'https://facebook.com/{page.page_id}/live/demo',
                'demo': True,
            }
        )
    if not broadcasts and not pages:
        broadcasts.append(
            {
                'page_id': 'fb_page_demo',
                'page_name': live.vendeur.facebook_page_name or 'Page Demo AZLive',
                'live_video_id': f'demo_live_{uuid.uuid4().hex[:8]}',
                'status': 'LIVE',
                'demo': True,
            }
        )
    return broadcasts


def create_demo_tiktok_broadcast(vendeur) -> dict[str, Any] | None:
    username = vendeur.tiktok_username
    if not username:
        return None
    return {
        'live_id': f'demo_tt_{uuid.uuid4().hex[:8]}',
        'username': username,
        'status': 'LIVE',
        'stream_url': f'rtmp://live.demo.azlive/tiktok/{username.lstrip("@")}',
        'demo': True,
    }


def start_facebook_broadcasts(live: Live, pages: list[PageFacebook]) -> list[dict[str, Any]]:
    if not pages:
        return []

    use_demo = live.vendeur.is_demo_mode or not facebook_configured()
    if use_demo:
        return create_demo_facebook_broadcasts(live, pages)

    broadcasts = []
    errors = []
    for page in pages:
        try:
            broadcasts.append(create_facebook_live_broadcast(page, live.titre))
        except FacebookOAuthError as exc:
            errors.append(f'{page.nom}: {exc.message}')

    if errors and not broadcasts:
        raise FacebookLiveError('Impossible de démarrer le live Facebook: ' + '; '.join(errors))

    return broadcasts


def stop_facebook_broadcasts(broadcasts: list[dict[str, Any]], pages_by_id: dict[str, PageFacebook]):
    for broadcast in broadcasts:
        if broadcast.get('demo'):
            broadcast['status'] = 'ENDED'
            continue

        live_video_id = broadcast.get('live_video_id')
        page_id = str(broadcast.get('page_id', ''))
        page = pages_by_id.get(page_id)
        if not live_video_id or not page or not page.access_token:
            broadcast['status'] = 'ENDED'
            continue

        try:
            end_facebook_live_broadcast(live_video_id, page.access_token)
            broadcast['status'] = 'ENDED'
        except FacebookOAuthError:
            broadcast['status'] = 'ENDED'


_LIVE_VIDEO_STATUSES = frozenset({'LIVE', 'LIVE_NOW'})
_ENDED_VIDEO_STATUSES = frozenset({
    'VOD',
    'LIVE_STOPPED',
    'ENDED',
    'SCHEDULED_CANCELED',
    'SCHEDULED_EXPIRED',
})


def get_facebook_live_video_status(live_video_id: str, page_access_token: str) -> str | None:
    """Statut Graph de la live_video (LIVE, VOD, LIVE_STOPPED…). None si inconnu."""
    if not live_video_id or str(live_video_id).startswith('demo_'):
        return 'LIVE'
    try:
        payload = _graph_request(
            str(live_video_id),
            {'fields': 'status', 'access_token': page_access_token},
            method='GET',
        )
    except FacebookOAuthError:
        return None
    status = str((payload or {}).get('status') or '').upper().strip()
    return status or None


def facebook_broadcast_is_live_on_platform(broadcast: dict[str, Any], pages_by_id: dict[str, PageFacebook]) -> bool | None:
    """True = encore live sur FB, False = terminé, None = indéterminé."""
    if broadcast.get('demo'):
        return str(broadcast.get('status') or '').upper() in _LIVE_VIDEO_STATUSES
    local = str(broadcast.get('status') or '').upper()
    if local in _ENDED_VIDEO_STATUSES:
        return False
    video_id = broadcast.get('live_video_id')
    page = pages_by_id.get(str(broadcast.get('page_id') or ''))
    if not video_id or not page or not page.access_token:
        return local in _LIVE_VIDEO_STATUSES if local else None
    remote = get_facebook_live_video_status(str(video_id), page.access_token)
    if remote is None:
        return None
    if remote in _LIVE_VIDEO_STATUSES:
        return True
    if remote in _ENDED_VIDEO_STATUSES or remote not in _LIVE_VIDEO_STATUSES:
        return False
    return None


def any_facebook_broadcast_still_live(live: Live) -> bool | None:
    """True si au moins un broadcast FB est encore LIVE sur la plateforme."""
    broadcasts = list((live.diffusion_plateformes or {}).get('facebook') or [])
    if not broadcasts:
        return False
    pages_by_id = {str(p.page_id): p for p in resolve_live_pages(live)}
    saw_unknown = False
    for broadcast in broadcasts:
        state = facebook_broadcast_is_live_on_platform(broadcast, pages_by_id)
        if state is True:
            return True
        if state is None:
            saw_unknown = True
    if saw_unknown:
        return None
    return False


def cloturer_facebook_live(live: Live, *, reason: str = 'facebook_stream_end') -> bool:
    """Marque Facebook terminé ; clôture AZLive si TikTok n'est plus en cours non plus."""
    import logging

    logger = logging.getLogger(__name__)

    if live.statut != Live.STATUT_EN_COURS:
        return False

    diffusion = dict(live.diffusion_plateformes or {})
    broadcasts = list(diffusion.get('facebook') or [])
    for item in broadcasts:
        item['status'] = 'ENDED'
        item['ended_reason'] = reason
        item['ended_at'] = timezone.now().isoformat()
    diffusion['facebook'] = broadcasts
    diffusion['facebook_ended_at'] = timezone.now().isoformat()
    diffusion['facebook_ended_reason'] = reason

    try:
        from .facebook_live_comments import stop_facebook_comment_listener

        stop_facebook_comment_listener(live)
    except Exception:
        logger.exception('stop_facebook_comment_listener live #%s', live.pk)

    tiktok = dict(diffusion.get('tiktok') or {})
    tiktok_live = str(tiktok.get('status') or '').upper() in _LIVE_VIDEO_STATUSES or bool(
        tiktok.get('is_live_on_tiktok')
    )
    if tiktok_live:
        live.diffusion_plateformes = diffusion
        live.save(update_fields=['diffusion_plateformes'])
        logger.info(
            'Facebook terminé sur live #%s (%s) - TikTok encore live, statut AZLive inchangé',
            live.pk,
            reason,
        )
        return False

    live.diffusion_plateformes = diffusion
    live.save(update_fields=['diffusion_plateformes'])

    from .live_service import arreter_live

    arreter_live(live, auto=True)
    live.refresh_from_db()
    diffusion = dict(live.diffusion_plateformes or {})
    diffusion['stopped_reason'] = reason
    live.diffusion_plateformes = diffusion
    live.save(update_fields=['diffusion_plateformes'])
    logger.info('Live #%s passé en terminé (Facebook arrêté: %s)', live.pk, reason)
    return True


_last_fb_end_reconcile_at = None


def reconcile_ended_facebook_lives(*, min_interval_seconds: float = 45.0) -> int:
    """Si la live_video Facebook n'est plus LIVE, termine le live AZLive (comme TikTok)."""
    import logging

    global _last_fb_end_reconcile_at
    logger = logging.getLogger(__name__)

    if not facebook_configured():
        return 0

    now = timezone.now()
    if (
        _last_fb_end_reconcile_at is not None
        and (now - _last_fb_end_reconcile_at).total_seconds() < max(min_interval_seconds, 25.0)
    ):
        return 0

    active = list(
        Live.objects.filter(statut=Live.STATUT_EN_COURS)
        .select_related('vendeur')
        .order_by('-date_live')
    )
    if not active:
        return 0

    _last_fb_end_reconcile_at = now
    closed = 0
    for live in active:
        if live.vendeur.is_demo_mode:
            continue
        broadcasts = list((live.diffusion_plateformes or {}).get('facebook') or [])
        if not broadcasts:
            continue
        started_at = live.date_debut or live.date_live
        if started_at and (now - started_at).total_seconds() < 45:
            continue
        still = any_facebook_broadcast_still_live(live)
        if still is True or still is None:
            continue
        before = live.statut
        cloturer_facebook_live(live, reason='facebook_offline_reconcile')
        live.refresh_from_db(fields=['statut'])
        if live.statut == Live.STATUT_TERMINE and before == Live.STATUT_EN_COURS:
            closed += 1
            logger.info('Reconcile Facebook live #%s → terminé', live.pk)
    return closed
