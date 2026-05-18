from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from backend.models import Commande, Message
from backend.services import MessagingService


class Command(BaseCommand):
    help = "Exécute les relances automatiques pour les JPs capturés depuis plus de 30 minutes (max 3 relances)"

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force les relances immédiatement sans respecter le délai de 30 minutes',
        )

    def handle(self, *args, **options):
        force = options['force']
        now = timezone.now()
        max_relances = 3
        count = 0

        self.stdout.write(self.style.NOTICE("Démarrage du traitement des relances automatiques..."))

        # Look for commands captured but not yet confirmed
        commandes_a_relancer = Commande.objects.filter(statut=Commande.STATUT_JP_CAPTURE).prefetch_related('messages', 'client', 'produit')

        for commande in commandes_a_relancer:
            last_message = commande.messages.order_by('-date_envoi').first()
            if not last_message:
                self.stdout.write(self.style.WARNING(f"La commande #{commande.id} n'a aucun message initial enregistré. Ignorée."))
                continue

            if last_message.numero_relance >= max_relances:
                # Max followups reached
                continue

            # Check 30 minutes interval since the last message
            if not force:
                if last_message.date_envoi + timedelta(minutes=30) > now:
                    continue

            relance_num = last_message.numero_relance + 1
            contenu = (
                f"Bonjour {commande.client.nom}, ceci est votre relance n°{relance_num} "
                f"pour la commande '{commande.produit.nom}'. Merci de confirmer votre adresse et date de livraison."
            )

            # Record message in DB
            Message.objects.create(commande=commande, contenu=contenu, numero_relance=relance_num)

            # Trigger mock messaging pipeline
            MessagingService.send_relance_message(commande.client, commande.produit, relance_num)

            count += 1
            self.stdout.write(self.style.SUCCESS(
                f"✔ Relance #{relance_num} enregistrée et expédiée pour Commande #{commande.id} ({commande.client.nom})"
            ))

        self.stdout.write(self.style.SUCCESS(f"Traitement terminé. Total relances expédiées : {count}"))
