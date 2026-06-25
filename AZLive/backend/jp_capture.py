from django.db import models, transaction
from django.db.models import Max

from .ai import JPCommentAnalyzer
from .models import Client, Commande, Live, PageFacebook, Produit, Vendeur
from .order_messaging import send_jp_confirmation_message
from .serializers import CommandeSerializer


class JPCaptureError(Exception):
    def __init__(self, message, status_code=400, payload=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}


def create_jp_commande(client, produit, live=None, canal='', comment_id=None, variante=None):
    """Crée une commande JP (ordre atomique) puis envoie le message au client.

    La quantité n'est PAS lue dans le commentaire : elle est demandée plus tard, pendant
    la collecte des informations (nom, finday, adiresy, daty, ora, isa). La commande est
    donc créée avec quantite = None (non encore renseignée).

    Le contenu (instructions si éligible, liste d'attente sinon) est construit et livré par
    send_jp_confirmation_message, qui enregistre aussi le message sortant. Pour un
    commentateur Facebook, comment_id permet la réponse privée (private_replies).
    La variante (déduite du code JP) est rattachée afin que le décrément de stock à la
    confirmation porte sur la bonne déclinaison. L'envoi (appel réseau) est fait hors
    transaction pour ne pas garder le verrou.

    Si le client a déjà une commande JP en attente pour la même déclinaison (même produit
    et même variante), on réutilise cette commande au lieu d'en créer un doublon — le
    client n'envoie pas plusieurs JP, c'est une re-publication accidentelle.
    """
    reused = False
    with transaction.atomic():
        existing = (
            Commande.objects.select_for_update()
            .filter(
                client=client,
                produit=produit,
                variante=variante,
                statut=Commande.STATUT_JP_CAPTURE,
            )
            .order_by('ordre_jp')
            .first()
        )
        if existing:
            commande = existing
            reused = True
        else:
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
                variante=variante,
                ordre_jp=ordre_jp,
                statut=Commande.STATUT_JP_CAPTURE,
                live=live,
            )

    if not reused:
        send_jp_confirmation_message(commande, comment_id=comment_id)
    return commande


def resolve_variante_for_analysis(produit, analysis):
    """Retrouve la variante du produit correspondant au code JP / variante détecté(e)."""
    code_jp = analysis.get('code_jp')
    if code_jp:
        variante = produit.variantes.filter(code_jp__iexact=code_jp).first()
        if variante:
            return variante
    variante_id = analysis.get('variante_id')
    if variante_id:
        return produit.variantes.filter(id=variante_id).first()
    return None


def normalize_tiktok_username(username: str | None) -> str:
    return (username or '').lstrip('@').strip().lower()


def resolve_vendeur_from_tiktok_username(unique_id: str | None):
    normalized = normalize_tiktok_username(unique_id)
    if not normalized:
        return None

    for vendeur in Vendeur.objects.exclude(tiktok_username__isnull=True).exclude(tiktok_username=''):
        if normalize_tiktok_username(vendeur.tiktok_username) == normalized:
            return vendeur
    return None


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
    streamer_unique_id: str | None = None,
    vendeur=None,
    live=None,
    id_field: str = 'facebook_id',
    comment_id: str | None = None,
):
    if not sender_id or not comment_text:
        raise JPCaptureError(
            'Les champs identifiant expéditeur et comment_text sont obligatoires.',
            status_code=400,
        )

    if vendeur is None and page_id:
        vendeur = resolve_vendeur_from_page(page_id)

    if vendeur is None and channel == 'TikTok' and streamer_unique_id:
        vendeur = resolve_vendeur_from_tiktok_username(streamer_unique_id)

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

    variante = resolve_variante_for_analysis(produit, analysis)
    commande = create_jp_commande(
        client,
        produit,
        live=live,
        canal=channel,
        comment_id=comment_id,
        variante=variante,
    )
    return {
        'status': 'JP capturé avec succès',
        'channel': channel,
        'client_cree': created,
        'commande': CommandeSerializer(commande).data,
        'ai_analysis': analysis,
        'live_id': live.id if live else None,
        'vendeur_id': vendeur.id if vendeur else None,
    }
