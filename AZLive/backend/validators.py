from django.core.exceptions import ValidationError


def validate_variante_uniqueness(produit, taille, couleur, exclude_pk=None):
    """Une taille + couleur ne peut pas être dupliquée pour un même produit."""
    from .models import Variante

    qs = Variante.objects.filter(
        produit=produit,
        taille__iexact=taille.strip(),
        couleur__iexact=couleur.strip(),
    )
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    if qs.exists():
        raise ValidationError(
            f'La combinaison taille "{taille}" et couleur "{couleur}" existe déjà pour ce produit.'
        )


def validate_code_jp_uniqueness(code_jp, exclude_pk=None):
    """Un code_jp est unique dans le système."""
    from .models import Variante

    if not code_jp or not str(code_jp).strip():
        raise ValidationError('Le code JP est obligatoire pour chaque variante.')

    qs = Variante.objects.filter(code_jp__iexact=str(code_jp).strip())
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    if qs.exists():
        raise ValidationError(f'Le code JP "{code_jp}" est déjà utilisé par une autre variante.')
