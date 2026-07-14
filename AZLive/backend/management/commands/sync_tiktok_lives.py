from django.core.management.base import BaseCommand

from backend.tiktool_live import sync_external_tiktok_lives, tiktool_configured


class Command(BaseCommand):
    help = "Synchronise automatiquement les lives TikTok externes vers la table Live."

    def handle(self, *args, **options):
        if not tiktool_configured():
            self.stdout.write(self.style.WARNING('TIKTOOL_API_KEY manquant : sync ignorée.'))
            return

        result = sync_external_tiktok_lives()
        self.stdout.write(
            self.style.SUCCESS(
                'Synchronisation terminée: '
                f"{result['started']} live(s) détecté(s), "
                f"{result['stopped']} live(s) clôturé(s), "
                f"{result['skipped']} vendeur(s) ignoré(s)."
            )
        )
