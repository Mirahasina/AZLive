from django.core.management.base import BaseCommand
from django.utils import timezone

from backend.models import Live, Vendeur
from backend.tiktool_live import (
    ensure_tiktok_confirmation_comment,
    check_streamer_is_live,
    ensure_tiktok_live_for_streamer,
    normalize_tiktok_username,
    stop_tiktool_listener,
    tiktool_configured,
)


def _facebook_still_live(live: Live) -> bool:
    broadcasts = list((live.diffusion_plateformes or {}).get('facebook') or [])
    for item in broadcasts:
        if str(item.get('status') or '').upper() in {'LIVE', 'LIVE_NOW'}:
            return True
    return False


class Command(BaseCommand):
    help = "Synchronise automatiquement les lives TikTok externes vers la table Live."

    def handle(self, *args, **options):
        if not tiktool_configured():
            self.stdout.write(self.style.WARNING('TIKTOOL_API_KEY manquant : sync ignorée.'))
            return

        vendors = (
            Vendeur.objects.exclude(tiktok_username__isnull=True)
            .exclude(tiktok_username='')
            .order_by('id')
        )
        started = 0
        stopped = 0

        for vendeur in vendors:
            unique_id = normalize_tiktok_username(vendeur.tiktok_username)
            is_live = check_streamer_is_live(unique_id)
            if is_live is None:
                continue

            if is_live:
                live = ensure_tiktok_live_for_streamer(unique_id)
                if live:
                    ensure_tiktok_confirmation_comment(live)
                    started += 1
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'TikTok LIVE détecté @{unique_id} -> live #{live.pk} (en_cours)'
                        )
                    )
                continue

            live = (
                Live.objects.filter(vendeur=vendeur, statut=Live.STATUT_EN_COURS)
                .order_by('-date_live')
                .first()
            )
            if not live:
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
            else:
                live.diffusion_plateformes = diffusion
                live.save(update_fields=['diffusion_plateformes'])

            stopped += 1
            self.stdout.write(
                self.style.WARNING(f'TikTok OFFLINE @{unique_id} -> live #{live.pk} mis à jour')
            )

        self.stdout.write(
            self.style.SUCCESS(
                f'Synchronisation terminée: {started} live(s) détecté(s), {stopped} live(s) clôturé(s).'
            )
        )
