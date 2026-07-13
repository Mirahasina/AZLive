import os
import sys

from django.apps import AppConfig


class BackendConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'backend'

    def ready(self):
        # Sous runserver + autoreload : ne démarrer que dans le process enfant.
        if 'runserver' in sys.argv and os.environ.get('RUN_MAIN') != 'true':
            return
        # Pendant les tests : pas de thread background (évite du bruit / races).
        if 'test' in sys.argv:
            return
        try:
            from backend.jp_relances import start_jp_relance_scheduler

            start_jp_relance_scheduler()
        except Exception:  # noqa: BLE001
            import logging

            logging.getLogger(__name__).exception(
                'Impossible de démarrer le planificateur de relances JP'
            )
