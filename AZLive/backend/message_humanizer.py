import random

PLACEHOLDER_NAMES = {'Client Live', 'Client Facebook', 'Client TikTok'}


def is_placeholder_name(nom: str | None) -> bool:
    cleaned = (nom or '').strip()
    if not cleaned:
        return True
    if cleaned in PLACEHOLDER_NAMES:
        return True
    # Ex. « Client Facebook (auteur masqué) »
    return cleaned.startswith('Client Facebook') or cleaned.startswith('Client TikTok')


def first_name(nom: str | None) -> str:
    if is_placeholder_name(nom):
        return ''
    cleaned = nom.strip()
    return cleaned.split()[0]


def platform_display_name(nom: str | None) -> str:
    """Prénom Facebook/TikTok pour les messages ; vide si placeholder."""
    return first_name(nom)


def pick(options: list[str]) -> str:
    """Tire une variante au hasard (rotation des tournures)."""
    return random.choice(options)


def greeting(nom: str | None = None) -> str:
    prenom = first_name(nom)
    if prenom:
        base = pick(['Salama', 'Manao ahoana', 'Miarahaba anao', 'Salama e'])
        return f'{base} {prenom}'
    return pick(['Salama tompoko', 'Manao ahoana tompoko', 'Miarahaba anao'])


def thanks() -> str:
    return pick(['Misaotra', 'Misaotra betsaka', 'Misaotra indrindra', 'Misaotra tompoko'])


def thanks_with_name(nom: str | None = None) -> str:
    """Remerciement naturel, sans doubler « tompoko » / « betsaka » devant un prénom."""
    prenom = first_name(nom)
    if prenom and len(prenom) > 2:
        return pick([
            f'Misaotra betsaka {prenom}',
            f'Misaotra indrindra {prenom}',
            f'Misaotra {prenom}',
        ])
    return pick(['Misaotra betsaka', 'Misaotra indrindra', 'Misaotra tompoko'])


def emoji(prob: float = 0.5, choices: list[str] | None = None) -> str:
    choices = choices or ['😊', '🙏', '❤️', '🥰']
    if random.random() < prob:
        return ' ' + random.choice(choices)
    return ''


def apply_platform_display_name(client, platform_name: str | None) -> bool:
    """Aligne client.nom sur le nom Facebook/TikTok (jamais un placeholder)."""
    name = (platform_name or '').strip()
    if is_placeholder_name(name):
        return False
    if (client.nom or '').strip() == name:
        return False
    client.nom = name
    client.save(update_fields=['nom'])
    return True
