from django.db import models, transaction
from django.db.models import Max

from .ai import HybridCommentAnalyzer
from .jp_codes import normalize_jp_code
from .models import Client, Commande, Live, LiveCodeJP, PageFacebook, Produit, Variante, Vendeur
from .order_messaging import send_jp_confirmation_message
from .serializers import CommandeSerializer


# File d'attente JP : on capture jusqu'à 3 × stock (ex. stock 3 → 9 commandes max).
JP_CAPTURE_QUEUE_MULTIPLIER = 3


class JPCaptureError(Exception):
    def __init__(self, message, status_code=400, payload=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}


def _active_jp_demand_queryset(produit, variante=None, live=None):
    """Commandes encore actives pour la file (JP + confirmées + préparées).

    Si ``live`` est fourni, on ne compte que les commandes de CE live — les JP
    non confirmés d'un live précédent ne doivent pas bloquer la file du live actuel.
    """
    qs = Commande.objects.filter(
        produit=produit,
        variante=variante,
        statut__in=(
            Commande.STATUT_JP_CAPTURE,
            Commande.STATUT_CONFIRME,
            Commande.STATUT_PREPARE,
        ),
    )
    if live is not None:
        qs = qs.filter(live=live)
    return qs


def max_jp_captures_allowed(produit, variante=None) -> int:
    """Plafond de capture = 3 × stock de la variante (0 si stock épuisé / absent)."""
    stock = 0
    if variante is not None:
        stock = max(int(variante.stock or 0), 0)
    else:
        first = produit.variantes.order_by('id').first()
        stock = max(int(first.stock or 0), 0) if first else 0
    return JP_CAPTURE_QUEUE_MULTIPLIER * stock


def create_jp_commande(client, produit, live=None, canal='', comment_id=None, variante=None):
    """Crée une commande JP (ordre atomique) puis envoie le message au client.

    La quantité n'est PAS lue dans le commentaire : elle est demandée plus tard, pendant
    la collecte des informations (nom, finday, adiresy, daty, ora, isa). La commande est
    donc créée avec quantite = None (non encore renseignée).

    Plafond de capture : 3 × stock de la variante. Au-delà, la commande n'est pas créée
    (file pleine / stock insuffisant pour accepter de nouveaux JP).

    Le contenu (instructions si éligible, liste d'attente sinon) est construit et livré par
    send_jp_confirmation_message, qui enregistre aussi le message sortant. Pour un
    commentateur Facebook, comment_id permet la réponse privée (private_replies).
    La variante (déduite du code JP) est rattachée afin que le décrément de stock à la
    confirmation porte sur la bonne déclinaison. L'envoi (appel réseau) est fait hors
    transaction pour ne pas garder le verrou.

    Si le client a déjà une commande JP en attente pour la même déclinaison SUR LE MÊME
    LIVE, on réutilise cette commande (re-publication accidentelle). Un JP du même
    client sur un autre live crée bien une nouvelle commande.
    """
    reused = False
    with transaction.atomic():
        existing_qs = (
            Commande.objects.select_for_update()
            .filter(
                client=client,
                produit=produit,
                variante=variante,
                statut=Commande.STATUT_JP_CAPTURE,
            )
            .order_by('ordre_jp')
        )
        # Important : ne pas réutiliser un JP d'un live précédent.
        if live is not None:
            existing_qs = existing_qs.filter(live=live)
        else:
            existing_qs = existing_qs.filter(live__isnull=True)

        existing = existing_qs.first()
        if existing:
            commande = existing
            reused = True
        else:
            # Verrouille la file produit/variante (du live) pour respecter le plafond 3×stock.
            demand_qs = _active_jp_demand_queryset(produit, variante, live=live).select_for_update()
            active_count = demand_qs.count()
            max_allowed = max_jp_captures_allowed(produit, variante)
            if max_allowed <= 0 or active_count >= max_allowed:
                produit_nom = produit.nom if produit else 'ce produit'
                if variante is not None:
                    stock = int(variante.stock or 0)
                else:
                    first_var = produit.variantes.order_by('id').first()
                    stock = int(first_var.stock or 0) if first_var else 0
                raise JPCaptureError(
                    (
                        f"Produit en rupture de stock / file complète : « {produit_nom} ». "
                        f"Stock={stock}, plafond de capture={max_allowed} "
                        f"({JP_CAPTURE_QUEUE_MULTIPLIER}×stock), déjà {active_count} commande(s)."
                    ),
                    status_code=409,
                    payload={
                        'rupture_stock': True,
                        'stock': stock,
                        'max_captures': max_allowed,
                        'active_count': active_count,
                        'multiplier': JP_CAPTURE_QUEUE_MULTIPLIER,
                    },
                )

            # L'ordre suit le scope de la file d'attente / de l'éligibilité : (produit, variante).
            max_order = (
                Commande.objects.select_for_update()
                .filter(produit=produit, variante=variante)
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


def _candidate_code(analysis) -> str:
    """Code JP candidat (nu) déduit du commentaire.

    On privilégie ``code_jp`` puis le texte brut du commentaire : le LLM peut
    remplir ``product_query`` avec le nom produit (ex: "Tee-shirt") au lieu du
    code ("2"), ce qui casse la résolution par code.
    normalize_jp_code retire un éventuel préfixe « JP » résiduel.
    """
    for key in ('code_jp', 'raw_text', 'product_query'):
        candidate = normalize_jp_code(analysis.get(key))
        if candidate:
            return candidate
    return ''


def resolve_live_variante(live, analysis, vendeur=None):
    """Résout la variante via la correspondance code↔variante PROPRE au live.

    Prioritaire sur la détection par nom : si le code tapé correspond à un code
    attribué dans ce live, c'est cette variante (et donc ce produit) qui prime.
    """
    code = _candidate_code(analysis)
    if live is None or not code:
        return None
    queryset = LiveCodeJP.objects.filter(live=live, code__iexact=code).select_related(
        'variante', 'variante__produit'
    )
    if vendeur:
        queryset = queryset.filter(variante__produit__vendeur=vendeur)
    mapping = queryset.first()
    if mapping:
        return mapping.variante

    dressing_qs = live.produits_dressing.all()
    if vendeur:
        dressing_qs = dressing_qs.filter(vendeur=vendeur)
    return (
        Variante.objects.filter(produit__in=dressing_qs, code_jp__iexact=code)
        .select_related('produit')
        .first()
    )


def resolve_variante_for_analysis(produit, analysis, live=None):
    """Retrouve la variante du produit correspondant au code JP / variante détecté(e).

    Quand un live est connu, on tente d'abord la correspondance propre au live.
    """
    code = _candidate_code(analysis)
    if live is not None and code:
        mapping = (
            LiveCodeJP.objects.filter(
                live=live, variante__produit=produit, code__iexact=code
            )
            .select_related('variante')
            .first()
        )
        if mapping:
            return mapping.variante
    if code:
        variante = produit.variantes.filter(code_jp__iexact=code).first()
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
        | models.Q(variantes__couleur__icontains=query)
        | models.Q(variantes__taille__icontains=query)
        | models.Q(variantes__code_jp__icontains=query)
    ).distinct().first()
    if match:
        return match

    for token in [token for token in query.split() if len(token) > 1]:
        match = queryset.filter(
            models.Q(nom__icontains=token)
            | models.Q(variantes__couleur__icontains=token)
            | models.Q(variantes__taille__icontains=token)
            | models.Q(variantes__code_jp__icontains=token)
        ).distinct().first()
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

    analyzer = HybridCommentAnalyzer()
    analysis = analyzer.analyze(comment_text, vendeur=vendeur, live=live)

    if analysis.get('intent') != 'achat':
        return {
            'status': 'ignored',
            'detail': 'Commentaire ignoré (intention d\'achat non détectée).',
            'channel': channel,
            'ai_analysis': analysis,
        }

    # La correspondance code↔variante propre au live prime sur la détection par nom.
    variante = resolve_live_variante(live, analysis, vendeur=vendeur)
    if variante is not None:
        produit = variante.produit
    else:
        produit = find_produit_for_comment(analysis, vendeur=vendeur, live=live)
        if not produit:
            raise JPCaptureError(
                'Produit introuvable pour ce commentaire.',
                status_code=404,
                payload={'ai_analysis': analysis, 'channel': channel},
            )
        variante = resolve_variante_for_analysis(produit, analysis, live=live)

    lookup = {id_field: sender_id}
    defaults = {'nom': sender_name, 'telephone': '', 'adresse': ''}
    client, created = Client.objects.get_or_create(**lookup, defaults=defaults)

    placeholder_names = {'Client Live', 'Client Facebook', 'Client TikTok'}
    if not created and client.nom in placeholder_names and sender_name not in placeholder_names:
        client.nom = sender_name
        client.save(update_fields=['nom'])
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
