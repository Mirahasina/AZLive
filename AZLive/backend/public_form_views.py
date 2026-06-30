"""Formulaire public de collecte d'informations client (live TikTok).

TikTok n'autorise pas l'automatisation des messages privés (DM). On ne peut donc pas
envoyer automatiquement la demande d'informations au client comme sur Facebook. À la
place, le vendeur partage un lien public par live : le client y saisit son @TikTok,
retrouve les commandes (JP) déjà capturées pendant ce live, puis complète ses
informations de livraison (nom, téléphone, adresse, date/heure) et la quantité.

La soumission réutilise la logique canonique de confirmation (`confirm_commande_from_message`),
exactement comme si le client avait répondu en message privé : même gestion de la file
d'attente, du stock et de la confirmation.

Endpoints (AllowAny — pensés pour un partage public) :
  GET  /api/public/lives/<live_id>/order-form/?handle=<@tiktok>
  POST /api/public/lives/<live_id>/order-form/
"""
from django.db import models
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .jp_capture import normalize_tiktok_username
from .models import Client, Commande, Live, LiveCodeJP
from .order_confirmation import OrderConfirmationError, confirm_commande_from_message

REQUIRED_CLIENT_FIELDS = ('nom', 'telephone', 'adresse', 'date_livraison', 'heure_livraison')


def _match_clients(handle: str):
    """Clients correspondant à un @TikTok (insensible à la casse, @ et espaces ignorés)."""
    normalized = normalize_tiktok_username(handle)
    if not normalized:
        return Client.objects.none()
    return Client.objects.filter(
        models.Q(tiktok_id__iexact=normalized) | models.Q(social_handle__iexact=normalized)
    )


def _live_code_map(live: Live, variante_ids) -> dict[int, str]:
    """Code JP propre au live pour chaque variante (repli sur le code catalogue)."""
    mapping = {}
    for entry in LiveCodeJP.objects.filter(live=live, variante_id__in=variante_ids):
        mapping[entry.variante_id] = entry.code
    return mapping


def _pending_commandes(live: Live, clients):
    return (
        Commande.objects.select_related('produit', 'variante')
        .filter(live=live, client__in=clients, statut=Commande.STATUT_JP_CAPTURE)
        .order_by('ordre_jp')
    )


def _serialize_commandes(live: Live, commandes) -> list[dict]:
    variante_ids = [c.variante_id for c in commandes if c.variante_id]
    code_map = _live_code_map(live, variante_ids)
    items = []
    for commande in commandes:
        variante = commande.variante
        code = code_map.get(commande.variante_id) or (variante.code_jp if variante else '')
        items.append(
            {
                'commande_id': commande.id,
                'produit': commande.produit.nom,
                'code_jp': code,
                'taille': variante.taille if variante else '',
                'couleur': variante.couleur if variante else '',
                'prix_unitaire': str(variante.prix_unitaire) if variante else None,
                'quantite': commande.quantite,
            }
        )
    return items


class PublicOrderFormAPIView(APIView):
    """Recherche (GET) et complétion (POST) des commandes d'un client pour un live."""

    permission_classes = [AllowAny]

    def get(self, request, live_id: int):
        live = get_object_or_404(Live, pk=live_id)
        handle = request.query_params.get('handle', '')

        base = {
            'live': {'id': live.id, 'titre': live.titre, 'statut': live.statut},
            'vendeur': live.vendeur.nom if live.vendeur_id else '',
        }

        if not handle.strip():
            return Response({**base, 'found': False, 'commandes': []}, status=status.HTTP_200_OK)

        clients = _match_clients(handle)
        if not clients.exists():
            return Response({**base, 'found': False, 'commandes': []}, status=status.HTTP_200_OK)

        commandes = list(_pending_commandes(live, clients))
        client = clients.first()
        return Response(
            {
                **base,
                'found': True,
                'client': {
                    'nom': client.nom,
                    'telephone': client.telephone,
                    'adresse': client.adresse,
                },
                'commandes': _serialize_commandes(live, commandes),
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request, live_id: int):
        live = get_object_or_404(Live, pk=live_id)
        data = request.data or {}

        handle = (data.get('handle') or '').strip()
        clients = _match_clients(handle)
        if not handle or not clients.exists():
            return Response(
                {'detail': 'Aucune commande trouvée pour ce compte TikTok. Vérifiez votre identifiant @.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Validation des champs client obligatoires.
        missing = [f for f in REQUIRED_CLIENT_FIELDS if not str(data.get(f, '')).strip()]
        if missing:
            return Response(
                {'detail': 'Champs obligatoires manquants.', 'champs_manquants': missing},
                status=status.HTTP_400_BAD_REQUEST,
            )

        items = data.get('items') or []
        if not isinstance(items, list) or not items:
            return Response(
                {'detail': 'Veuillez sélectionner au moins une commande à valider.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Commandes éligibles du client pour ce live (anti-falsification d'ID).
        allowed = {c.id: c for c in _pending_commandes(live, clients)}

        parsed_data = {
            'nom': str(data.get('nom')).strip(),
            'telephone': str(data.get('telephone')).strip(),
            'adresse': str(data.get('adresse')).strip(),
            'date_livraison': str(data.get('date_livraison')).strip(),
            'heure_livraison': str(data.get('heure_livraison')).strip(),
        }

        results = []
        errors = []
        for item in items:
            try:
                commande_id = int(item.get('commande_id'))
                quantite = int(item.get('quantite'))
            except (TypeError, ValueError):
                errors.append({'item': item, 'detail': 'commande_id et quantite doivent être des entiers.'})
                continue

            commande = allowed.get(commande_id)
            if commande is None:
                errors.append({'commande_id': commande_id, 'detail': 'Commande introuvable pour ce compte/live.'})
                continue
            if quantite <= 0:
                errors.append({'commande_id': commande_id, 'detail': 'La quantité doit être supérieure à 0.'})
                continue

            commande.quantite = quantite
            commande.save(update_fields=['quantite'])

            try:
                outcome = confirm_commande_from_message(
                    commande,
                    parsed_data,
                    inbound_text='Informations transmises via le formulaire de commande (TikTok).',
                    canal='TikTok',
                )
                results.append({'commande_id': commande_id, 'status': outcome.get('status'), 'complet': outcome.get('complet')})
            except OrderConfirmationError as exc:
                errors.append({'commande_id': commande_id, 'detail': exc.message})

        return Response(
            {
                'status': 'Informations enregistrées.',
                'traitees': results,
                'erreurs': errors,
            },
            status=status.HTTP_200_OK if results else status.HTTP_400_BAD_REQUEST,
        )
