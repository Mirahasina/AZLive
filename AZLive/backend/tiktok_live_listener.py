"""Capture des commentaires TikTok live via TikTokLive.

Connexion directe au webcast TikTok avec le @username du vendeur.
Nécessite ``pip install TikTokLive``.

Optionnel : ``SIGN_API_KEY`` (Euler Stream) améliore la fiabilité des signatures.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import threading
from typing import Any

from django.db import close_old_connections

from .jp_capture import normalize_tiktok_username
from .models import Live

logger = logging.getLogger(__name__)

_listeners: dict[int, '_TikTokLiveCommentListener'] = {}
_listeners_lock = threading.Lock()

_RECONNECT_BASE_SECONDS = 15.0
_RECONNECT_MAX_SECONDS = 120.0


def tiktok_live_available() -> bool:
    return importlib.util.find_spec('TikTokLive') is not None


def _import_tiktok_live():
    from TikTokLive import TikTokLiveClient
    from TikTokLive.client.errors import UserOfflineError
    from TikTokLive.events import CommentEvent, ConnectEvent, DisconnectEvent, LiveEndEvent

    return TikTokLiveClient, UserOfflineError, CommentEvent, ConnectEvent, DisconnectEvent, LiveEndEvent


async def _safe_disconnect(client) -> None:
    disconnect = getattr(client, 'disconnect', None)
    if disconnect is None:
        return
    try:
        result = disconnect(close_client=True)
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        pass


async def _check_is_live_async(unique_id: str) -> bool | None:
    if not tiktok_live_available():
        return None
    TikTokLiveClient, *_ = _import_tiktok_live()
    client = TikTokLiveClient(unique_id=unique_id)
    try:
        return bool(await client.is_live(unique_id))
    except Exception as exc:
        logger.info('TikTokLive is_live @%s indisponible: %s', unique_id, exc)
        return None
    finally:
        await _safe_disconnect(client)


def check_streamer_is_live(unique_id: str) -> bool | None:
    normalized = normalize_tiktok_username(unique_id)
    if not normalized:
        return None

    def _run() -> bool | None:
        return asyncio.run(_check_is_live_async(normalized))

    try:
        # asyncio.run() est interdit si un event loop tourne déjà (ex. handler TikTokLive).
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return _run()
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_run).result(timeout=45)
    except Exception as exc:
        logger.info('check_streamer_is_live @%s: %s', normalized, exc)
        return None


def _comment_event_payload(event) -> dict[str, Any]:
    user = getattr(event, 'user', None)
    unique_id = ''
    nickname = 'Client TikTok'
    user_id = ''
    if user is not None:
        unique_id = str(getattr(user, 'unique_id', None) or getattr(user, 'display_id', None) or '')
        nickname = (
            getattr(user, 'nickname', None)
            or getattr(user, 'nick_name', None)
            or unique_id
            or nickname
        )
        user_id = str(getattr(user, 'id', None) or getattr(user, 'user_id', None) or unique_id or '')
    comment = (getattr(event, 'comment', None) or getattr(event, 'content', None) or '').strip()
    return {
        'user': {
            'uniqueId': unique_id,
            'nickname': nickname,
            'userId': user_id,
            'id': user_id,
        },
        'comment': comment,
        'text': comment,
    }


class _TikTokLiveCommentListener(threading.Thread):
    daemon = True

    def __init__(
        self,
        live_id: int | None,
        unique_id: str,
        stop_event: threading.Event,
    ):
        super().__init__(name=f'tiktoklive-live-{unique_id}-{live_id or "pending"}')
        self.live_id = live_id
        self.unique_id = normalize_tiktok_username(unique_id)
        self.stop_event = stop_event
        self._reconnect_delay = _RECONNECT_BASE_SECONDS
        self._session_saw_live = False

    def run(self) -> None:
        if not tiktok_live_available():
            logger.error('TikTokLive non installé : pip install TikTokLive')
            return
        try:
            asyncio.run(self._async_main())
        except Exception:
            logger.exception('TikTokLive listener @%s terminé avec erreur', self.unique_id)

    async def _async_main(self) -> None:
        TikTokLiveClient, UserOfflineError, CommentEvent, ConnectEvent, DisconnectEvent, LiveEndEvent = (
            _import_tiktok_live()
        )

        while not self.stop_event.is_set():
            client = TikTokLiveClient(unique_id=self.unique_id)
            self._register_handlers(
                client,
                CommentEvent=CommentEvent,
                ConnectEvent=ConnectEvent,
                DisconnectEvent=DisconnectEvent,
                LiveEndEvent=LiveEndEvent,
            )

            offline = False
            try:
                await client.connect(
                    process_connect_events=True,
                    fetch_live_check=True,
                    fetch_room_info=False,
                    fetch_gift_info=False,
                )
            except UserOfflineError:
                offline = True
                logger.debug('TikTokLive @%s hors ligne', self.unique_id)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning('TikTokLive connexion @%s échouée: %s', self.unique_id, exc)
            finally:
                await _safe_disconnect(client)

            if self.stop_event.is_set():
                break

            if offline:
                delay = min(self._reconnect_delay, 60.0)
                self._reconnect_delay = min(self._reconnect_delay * 1.25, _RECONNECT_MAX_SECONDS)
            else:
                delay = self._reconnect_delay
                self._reconnect_delay = min(self._reconnect_delay * 1.5, _RECONNECT_MAX_SECONDS)
                if self._session_saw_live:
                    await asyncio.to_thread(self._handle_stream_end, 'tiktoklive_disconnect')

            logger.info(
                'TikTokLive reconnexion @%s dans %.0fs (offline=%s)',
                self.unique_id,
                delay,
                offline,
            )
            try:
                await asyncio.wait_for(self._wait_stop(delay), timeout=delay)
                break
            except asyncio.TimeoutError:
                continue

    async def _wait_stop(self, seconds: float) -> None:
        step = 0.5
        elapsed = 0.0
        while elapsed < seconds:
            if self.stop_event.is_set():
                return
            await asyncio.sleep(min(step, seconds - elapsed))
            elapsed += step

    def _register_handlers(self, client, *, CommentEvent, ConnectEvent, DisconnectEvent, LiveEndEvent) -> None:
        streamer = self.unique_id

        @client.on(ConnectEvent)
        async def on_connect(_event: ConnectEvent) -> None:
            self._session_saw_live = True
            self._reconnect_delay = _RECONNECT_BASE_SECONDS
            # ORM Django = sync → hors de l'event loop TikTokLive.
            await asyncio.to_thread(self._on_live_started, streamer)

        @client.on(CommentEvent)
        async def on_comment(event: CommentEvent) -> None:
            payload = _comment_event_payload(event)
            if not payload.get('comment'):
                return
            self._session_saw_live = True
            await asyncio.to_thread(self._process_comment, streamer, payload)

        @client.on(LiveEndEvent)
        async def on_live_end(_event: LiveEndEvent) -> None:
            await asyncio.to_thread(self._handle_stream_end, 'tiktoklive_live_end')

        @client.on(DisconnectEvent)
        async def on_disconnect(_event: DisconnectEvent) -> None:
            if self._session_saw_live:
                logger.info('TikTokLive déconnecté (@%s)', streamer)

    def _on_live_started(self, streamer_unique_id: str) -> None:
        if not self.live_id:
            return
        try:
            close_old_connections()
            from .tiktok_live import _upsert_tiktok_diffusion

            live = Live.objects.filter(pk=self.live_id, statut=Live.STATUT_EN_COURS).first()
            if live is None:
                return
            _upsert_tiktok_diffusion(
                live,
                unique_id=streamer_unique_id,
                username=live.vendeur.tiktok_username,
                status='LIVE',
                is_live=True,
                listener='running',
            )
            logger.info('TikTokLive connecté (@%s, live #%s)', streamer_unique_id, self.live_id)
        except Exception:
            logger.exception('TikTokLive : mise à jour diffusion (@%s)', streamer_unique_id)

    def _process_comment(self, streamer_unique_id: str, payload: dict[str, Any]) -> None:
        try:
            close_old_connections()
            from .tiktok_live import process_tiktok_chat_event

            result = process_tiktok_chat_event(streamer_unique_id, payload)
            if result.get('status') == 'JP capturé avec succès':
                logger.info(
                    'JP TikTok capturé (live #%s, @%s): %s',
                    result.get('live_id'),
                    streamer_unique_id,
                    (payload.get('comment') or '')[:80],
                )
        except Exception as exc:
            logger.warning(
                'Erreur capture JP TikTokLive (@%s / live #%s): %s',
                streamer_unique_id,
                self.live_id,
                exc,
            )

    def _handle_stream_end(self, reason: str) -> None:
        try:
            close_old_connections()
            from .tiktok_live import (
                cloturer_tiktok_live,
                find_active_tiktok_live_for_streamer,
            )

            live = None
            if self.live_id:
                live = Live.objects.filter(pk=self.live_id, statut=Live.STATUT_EN_COURS).first()
            if live is None:
                live = find_active_tiktok_live_for_streamer(self.unique_id)
            closed = 1 if live and cloturer_tiktok_live(live, reason=reason) else 0

            if closed:
                logger.info(
                    'Fin live TikTok (%s @%s) → %s session(s) clôturée(s)',
                    reason,
                    self.unique_id,
                    closed,
                )
            self._session_saw_live = False
            self.live_id = None
        except Exception:
            logger.exception('TikTokLive : échec clôture live (@%s)', self.unique_id)


def _start_listener_locked(unique_id: str, live_id: int) -> '_TikTokLiveCommentListener':
    stop_event = threading.Event()
    listener = _TikTokLiveCommentListener(live_id, unique_id, stop_event)
    old_live = _listeners.get(live_id)
    if old_live is not listener and old_live:
        old_live.stop_event.set()
    _listeners[live_id] = listener
    listener.start()
    return listener


def start_tiktok_live_listener(live: Live) -> bool:
    if not tiktok_live_available() or live.vendeur.is_demo_mode:
        return False
    if live.statut != Live.STATUT_EN_COURS:
        return False

    username = live.vendeur.tiktok_username
    if not username:
        return False

    unique_id = normalize_tiktok_username(username)
    with _listeners_lock:
        existing = _listeners.get(live.pk)
        if existing and existing.is_alive():
            return True

        stop_tiktok_live_listener(live, lock_held=True)
        _start_listener_locked(unique_id, live.pk)

    logger.info('TikTokLive listener démarré pour live #%s (@%s)', live.pk, unique_id)
    return True


def stop_tiktok_live_listener(live: Live, lock_held: bool = False) -> bool:
    live_id = live.pk

    def _stop() -> bool:
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
            return {'running': False, 'backend': 'tiktoklive'}
        return {
            'running': listener.is_alive(),
            'unique_id': listener.unique_id,
            'thread': listener.name,
            'backend': 'tiktoklive',
        }


def sync_tiktok_lives(*, vendeur_id: int | None = None) -> dict[str, int]:
    """Relance les listeners pour les lives AZLive déjà en cours (commande manuelle)."""
    if not tiktok_live_available():
        return {'started': 0, 'stopped': 0, 'skipped': 0}

    from .tiktok_live import (
        cloturer_tiktok_live,
        ensure_tiktok_listener,
        live_should_capture_tiktok_comments,
        resolve_vendeur_tiktok_unique_id,
    )

    started = 0
    stopped = 0
    skipped = 0

    lives = Live.objects.filter(statut=Live.STATUT_EN_COURS).select_related('vendeur')
    if vendeur_id is not None:
        lives = lives.filter(vendeur_id=vendeur_id)

    for live in lives:
        if not live_should_capture_tiktok_comments(live):
            skipped += 1
            continue

        unique_id = resolve_vendeur_tiktok_unique_id(live.vendeur)
        if not unique_id:
            skipped += 1
            continue

        is_live = check_streamer_is_live(unique_id)
        if is_live is True:
            if ensure_tiktok_listener(live):
                started += 1
        elif is_live is False:
            if cloturer_tiktok_live(live, reason='tiktoklive_offline'):
                stopped += 1
        else:
            skipped += 1

    return {'started': started, 'stopped': stopped, 'skipped': skipped}
