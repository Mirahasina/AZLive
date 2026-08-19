from django.core.management.base import BaseCommand

from backend.tiktok_live import sync_external_tiktok_lives, tiktok_capture_configured


class Command(BaseCommand):
    help = (
        "Détecte les lives TikTok via TikTokLive (scouts + is_live). "
        "À lancer pendant qu'un live TikTok est réellement en cours."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--no-rest',
            action='store_true',
            help='Conservé pour compatibilité (ignoré avec TikTokLive).',
        )
        parser.add_argument(
            '--wait',
            type=float,
            default=20.0,
            help='Conservé pour compatibilité (ignoré avec TikTokLive).',
        )

    def handle(self, *args, **options):
        if not tiktok_capture_configured():
            self.stdout.write(
                self.style.WARNING(
                    'TikTokLive non disponible : pip install TikTokLive'
                )
            )
            return

        result = sync_external_tiktok_lives(
            min_interval_seconds=0,
            rest=not options['no_rest'],
            wait_ws_seconds=float(options['wait']),
        )
        if result.get('throttled'):
            self.stdout.write(self.style.WARNING('Sync ignorée (throttle).'))
            return

        self.stdout.write(
            self.style.SUCCESS(
                'Synchronisation terminée: '
                f"{result.get('started', 0)} live(s) détecté(s), "
                f"{result.get('stopped', 0)} live(s) clôturé(s), "
                f"{result.get('skipped', 0)} vendeur(s) sans preuve live."
            )
        )
        if result.get('started', 0) == 0:
            self.stdout.write(
                self.style.NOTICE(
                    'Astuce: 1) pip install TikTokLive  2) compte TikTok OAuth connecté  '
                    '3) live TikTok ON  4) python manage.py runserver'
                )
            )
