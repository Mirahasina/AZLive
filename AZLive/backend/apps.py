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
            from .tiktool_live import recover_tiktool_listeners

            recover_facebook_comment_listeners()
            recover_tiktool_listeners()

        def _watchdog():
            interval = float(os.environ.get('AZLIVE_LISTENER_WATCHDOG_SECONDS', '15'))
            # Démarrage initial court puis boucle de réconciliation :
            # utile quand un live passe en_cours via une autre commande/process.
            time.sleep(1.0)
            while True:
                try:
                    _recover_listeners_once()
                except Exception:
                    # Évite que le watchdog s'arrête sur une erreur transitoire DB/réseau.
                    pass
                time.sleep(max(interval, 5.0))

        threading.Thread(target=_watchdog, name='azlive-listener-watchdog', daemon=True).start()
