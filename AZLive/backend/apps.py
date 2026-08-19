import logging
import os
import sys
import threading
import time

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class BackendConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'backend'

    def ready(self):
        # Évite le double démarrage du parent runserver (autoreloader).
        if 'runserver' in sys.argv and os.environ.get('RUN_MAIN') != 'true':
            return

        # Threads background UNIQUEMENT pour un serveur HTTP long-vivant.
        # (évite de lancer watchdog/JP sur sync_tiktok_lives, etc.)
        argv_joined = ' '.join(sys.argv).lower()
        is_server = (
            'runserver' in sys.argv
            or 'gunicorn' in argv_joined
            or 'uvicorn' in argv_joined
            or 'daphne' in argv_joined
            or os.environ.get('AZLIVE_START_BACKGROUND') == '1'
        )
        if not is_server:
            return

        if any(cmd in sys.argv for cmd in ('test', 'migrate', 'makemigrations', 'collectstatic', 'shell')):
            return

        try:
            from backend.jp_relances import start_jp_relance_scheduler

            start_jp_relance_scheduler()
        except Exception:  # noqa: BLE001
            logger.exception('Impossible de démarrer le planificateur de relances JP')

        def _recover_listeners_once(*, recover_social: bool = True) -> None:
            if not recover_social:
                return

            from .facebook_live_comments import recover_facebook_comment_listeners
            from .tiktok_live import reconcile_ended_tiktok_lives, recover_tiktok_listeners

            try:
                n = recover_facebook_comment_listeners()
                if n:
                    logger.info('Watchdog: %s poller(s) Facebook relancé(s) (lives en cours)', n)
            except Exception:
                logger.exception('Watchdog: échec recover_facebook_comment_listeners')

            try:
                n = recover_tiktok_listeners()
                if n:
                    logger.info('Watchdog: %s listener(s) TikTok relancé(s) (lives en cours)', n)
            except Exception:
                logger.exception('Watchdog: échec recover_tiktok_listeners')

            try:
                ended = reconcile_ended_tiktok_lives()
                if ended:
                    logger.info('Watchdog: %s live(s) TikTok clôturé(s)/archivé(s)', ended)
            except Exception:
                logger.exception('Watchdog: échec reconcile_ended_tiktok_lives')

        def _watchdog():
            interval = float(os.environ.get('AZLIVE_LISTENER_WATCHDOG_SECONDS', '60'))
            time.sleep(1.0)
            # Au démarrage : aucune capture sociale (FB/TikTok). Uniquement au démarrage du live.
            while True:
                time.sleep(max(interval, 30.0))
                try:
                    _recover_listeners_once(recover_social=True)
                except Exception:
                    logger.exception('Watchdog listener: erreur inattendue')

        threading.Thread(target=_watchdog, name='azlive-listener-watchdog', daemon=True).start()
