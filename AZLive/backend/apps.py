from django.apps import AppConfig
import os
import sys
import threading
import time


class BackendConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'backend'

    def ready(self):
        if 'runserver' in sys.argv and os.environ.get('RUN_MAIN') != 'true':
            return
        if any(cmd in sys.argv for cmd in ('test', 'migrate', 'makemigrations', 'collectstatic', 'shell')):
            return

        def _recover_listeners_once():
            from .facebook_live_comments import recover_facebook_comment_listeners
            from .tiktool_live import recover_tiktool_listeners, sync_external_tiktok_lives
            import logging

            logger = logging.getLogger(__name__)

            # Sync TikTok et recovery des listeners sont indépendants :
            # une erreur réseau TikTools ne doit pas empêcher de relancer les WS.
            try:
                sync_external_tiktok_lives()
            except Exception:
                logger.exception('Watchdog: échec sync_external_tiktok_lives')

            try:
                recover_facebook_comment_listeners()
            except Exception:
                logger.exception('Watchdog: échec recover_facebook_comment_listeners')

            try:
                n = recover_tiktool_listeners()
                if n:
                    logger.info('Watchdog: %s listener(s) TikTok actifs/relancés', n)
            except Exception:
                logger.exception('Watchdog: échec recover_tiktool_listeners')

        def _watchdog():
            # Background seulement : detection REST espacée pour éviter les 429 TikTools.
            interval = float(os.environ.get('AZLIVE_LISTENER_WATCHDOG_SECONDS', '45'))
            time.sleep(2.0)
            while True:
                try:
                    _recover_listeners_once()
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception('Watchdog listener: erreur inattendue')
                time.sleep(max(interval, 30.0))

        threading.Thread(target=_watchdog, name='azlive-listener-watchdog', daemon=True).start()
