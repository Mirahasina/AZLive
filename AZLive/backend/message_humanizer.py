"""Humanisation des messages sortants (ton malgache naturel, sans LLM).

Objectif : que les messages envoyés aux clients ne sonnent pas « robot ». On ne
génère rien avec une IA (risque d'inventer un prix/stock) ; on combine plutôt
des fragments rédigés à la main (salutations, remerciements, relances, emojis)
tirés au hasard, plus le prénom du client. Le client ne voit donc presque jamais
deux fois exactement la même tournure.

Les fonctions ici ne renvoient QUE des fragments réutilisables. La construction
complète des messages (avec produit, numéro de file, liens PDF…) reste dans
order_messaging.py, qui garde la maîtrise du contenu « métier ».
"""

import random

# Noms génériques posés par défaut tant que le client n'a pas donné son vrai nom.
PLACEHOLDER_NAMES = {'Client Live', 'Client Facebook', 'Client TikTok'}


def first_name(nom: str | None) -> str:
    """Prénom (premier mot) utilisable dans une salutation.

    Renvoie '' pour un nom vide ou un nom générique placeholder : l'appelant
    bascule alors sur une salutation sans prénom (« Salama tompoko »).
    """
    if not nom:
        return ''
    cleaned = nom.strip()
    if cleaned in PLACEHOLDER_NAMES:
        return ''
    return cleaned.split()[0]


def pick(options: list[str]) -> str:
    """Tire une variante au hasard (rotation des tournures)."""
    return random.choice(options)


def greeting(nom: str | None = None) -> str:
    """Salutation variée, avec prénom si on le connaît."""
    prenom = first_name(nom)
    if prenom:
        base = pick(['Salama', 'Manao ahoana', 'Miarahaba anao', 'Salama e'])
        return f'{base} {prenom}'
    return pick(['Salama tompoko', 'Manao ahoana tompoko', 'Miarahaba anao'])


def thanks() -> str:
    """Formule de remerciement variée."""
    return pick(['Misaotra', 'Misaotra betsaka', 'Misaotra indrindra', 'Misaotra tompoko'])


def emoji(prob: float = 0.5, choices: list[str] | None = None) -> str:
    """Renvoie « ' 😊' » (avec espace) avec une probabilité donnée, sinon ''.

    Usage parcimonieux : un emoji de temps en temps rend le ton chaleureux ;
    à chaque message ça ferait au contraire « bot ».
    """
    choices = choices or ['😊', '🙏', '❤️', '🥰']
    if random.random() < prob:
        return ' ' + random.choice(choices)
    return ''
