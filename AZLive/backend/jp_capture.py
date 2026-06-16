from django.db import models, transaction
from django.db.models import Max

from .ai import JPCommentAnalyzer
from .models import Client, Commande, Live, Message, PageFacebook, Produit, Vendeur
from .serializers import CommandeSerializer
from .services import MessagingService


class JPCaptureError(Exception):
    def __init__(self, message, status_code=400, payload=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}


def create_jp_commande(client, produit, live=None):
    """Crée une commande JP avec ordre atomique et notifications associées."""
    with transaction.atomic():
        max_order = (
            Commande.objects.select_for_update()
            .filter(produit=produit)
            .aggregate(max_ordre=Max('ordre_jp'))['max_ordre']
            or 0
        )
        ordre_jp = max_order + 1
        commande = Commande.objects.create(
            client=client,
            produit=produit,
            ordre_jp=ordre_jp,
            statut=Commande.STATUT_JP_CAPTURE,
            live=live,
        )

        if ordre_jp == 1:
            message_content = (
                f"Salama {client.nom}, nahazo ny JP-nao amin'ny '{produit.nom}' izahay. "
                "Mba hafahao ny baikonao amin'ny alalan'ny fandefasana ny: anarana feno, finday, adiresy ary ny daty tianao hanaterana azy."
            )
            MessagingService.send_automatic_message(client, produit, commande.id)
        else:
            message_content = (
                f"Salama {client.nom}, tafiditra ao anatin'ny lisitra miandry (liste d'attente) ho an'ny '{produit.nom}' ianao (Laharana faha-{ordre_jp}). "
                "Hampilazainay ianao raha misy fahafahana avy amin'ireo nialoha anao."
            )
            MessagingService.send_waiting_list_message(client, produit, ordre_jp, commande.id)

        Message.objects.create(
            commande=commande,
            contenu=message_content,
            numero_relance=0,
        )
        return commande


def resolve_vendeur_from_page(page_id: str | None):
    if not page_id:
        return None
    page = PageFacebook.objects.select_related('vendeur').filter(page_id=str(page_id)).first()
    return page.vendeur if page else None


def resolve_active_live(vendeur: Vendeur | None, page_id: str | None = None, page_name: str | None = None):
    if not vendeur:
        return None

    lives = Live.objects.filter(
        vendeur=vendeur,
        statut=Live.STATUT_EN_COURS,
    ).order_by('-date_live')

    if page_id or page_name:
        for live in lives:
            pages = live.pages_facebook or []
            if page_id and str(page_id) in [str(p) for p in pages]:
                return live
            if page_name and page_name in pages:
                return live

    return lives.first()


def find_produit_for_comment(analysis, vendeur=None, live=None):
    produit_id = analysis.get('produit_id')
    queryset = Produit.objects.all()

    if vendeur:
        queryset = queryset.filter(vendeur=vendeur)

    if live is not None and live.produits_dressing.exists():
        queryset = queryset.filter(id__in=live.produits_dressing.values_list('id', flat=True))

    if produit_id and queryset.filter(id=produit_id).exists():
        return queryset.filter(id=produit_id).first()

    query = analysis.get('product_query') or ''
    if not query:
        return None

    match = queryset.filter(
        models.Q(nom__icontains=query)
        | models.Q(couleur__icontains=query)
        | models.Q(taille__icontains=query)
    ).first()
    if match:
        return match

    for token in [token for token in query.split() if len(token) > 1]:
        match = queryset.filter(
            models.Q(nom__icontains=token)
            | models.Q(couleur__icontains=token)
            | models.Q(taille__icontains=token)
        ).first()
        if match:
            return match

    return None


def process_social_comment(
    *,
    sender_id: str,
    sender_name: str,
    comment_text: str,
    channel: str,
    page_id: str | None = None,
    vendeur=None,
    live=None,
    id_field: str = 'facebook_id',
):
    if not sender_id or not comment_text:
        raise JPCaptureError(
            'Les champs identifiant expéditeur et comment_text sont obligatoires.',
            status_code=400,
        )

    if vendeur is None and page_id:
        vendeur = resolve_vendeur_from_page(page_id)

    if live is None:
        page = PageFacebook.objects.filter(page_id=str(page_id)).first() if page_id else None
        live = resolve_active_live(vendeur, page_id=page_id, page_name=page.nom if page else None)

    analyzer = JPCommentAnalyzer()
    analysis = analyzer.analyze(comment_text)

    if analysis.get('intent') != 'achat':
        return {
            'status': 'ignored',
            'detail': 'Commentaire ignoré (intention d\'achat non détectée).',
            'channel': channel,
            'ai_analysis': analysis,
        }

    produit = find_produit_for_comment(analysis, vendeur=vendeur, live=live)
    if not produit:
        raise JPCaptureError(
            'Produit introuvable pour ce commentaire.',
            status_code=404,
            payload={'ai_analysis': analysis, 'channel': channel},
        )

    lookup = {id_field: sender_id}
    defaults = {'nom': sender_name, 'telephone': '', 'adresse': ''}
    client, created = Client.objects.get_or_create(**lookup, defaults=defaults)

    placeholder_names = {'Client Live', 'Client Facebook', 'Client TikTok'}
    if not created and client.nom in placeholder_names and sender_name not in placeholder_names:
        client.nom = sender_name
        client.save(update_fields=['nom'])

    commande = create_jp_commande(client, produit, live=live)
    return {
        'status': 'JP capturé avec succès',
        'channel': channel,
        'client_cree': created,
        'commande': CommandeSerializer(commande).data,
        'ai_analysis': analysis,
        'live_id': live.id if live else None,
        'vendeur_id': vendeur.id if vendeur else None,
    }
