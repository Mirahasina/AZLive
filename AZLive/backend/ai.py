import re

from django.db import models

from .models import Produit


class JPCommentAnalyzer:
    INTENT_PATTERNS = [
        r'JE\s*PRENDS',
        r'JP',
        r'JE\s*VOIS',
        r'VARIANTE',
        r'COMMAND(E|ER)',
    ]

    PRODUCT_SEARCH_PATTERNS = [
        r'JP\s+([A-Z0-9\s]+)',
        r'JE\s*PRENDS\s+([A-Z0-9\s]+)',
        r'VARIANTE\s+([A-Z0-9\s]+)',
        r'([A-Z0-9\s]+)\s+-\s*\d+\s*AR',
    ]

    QUANTITY_PATTERN = r'(?P<quantity>\d+)\s*(?:pcs|pi[eè]ces|x|EX|EX\s*)?'
    COLOR_PATTERN = r'(ROUGE|BLEU|NOIR|BLANC|VERT|JAUNE|ROSE|MARRON|OR|ARGENT)'
    SIZE_PATTERN = r'(S|M|L|XL|XXL|XS|XXS)'

    def analyze(self, comment_text: str) -> dict:
        cleaned = self.normalize(comment_text)
        intent = self.detect_intent(cleaned)
        product_query = self.extract_product_query(cleaned)
        couleur = self.extract_first(self.COLOR_PATTERN, cleaned)
        taille = self.extract_first(self.SIZE_PATTERN, cleaned)
        quantite = self.extract_first(self.QUANTITY_PATTERN, cleaned)
        produit = self.find_best_produit(product_query)

        return {
            'raw_text': comment_text,
            'cleaned_text': cleaned,
            'intent': intent,
            'product_query': product_query,
            'couleur': couleur,
            'taille': taille,
            'quantite': int(quantite) if quantite and quantite.isdigit() else None,
            'produit_trouve': produit.nom if produit else None,
            'produit_id': produit.id if produit else None,
        }

    def normalize(self, text: str) -> str:
        text = text.upper()
        text = re.sub(r'[^A-Z0-9\s\-–]', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def detect_intent(self, text: str) -> str:
        for pattern in self.INTENT_PATTERNS:
            if re.search(pattern, text):
                return 'achat'
        return 'inconnu'

    def extract_product_query(self, text: str) -> str:
        for pattern in self.PRODUCT_SEARCH_PATTERNS:
            match = re.search(pattern, text)
            if match:
                query = match.group(1)
                return self.clean_query(query)
        return text

    def extract_first(self, pattern: str, text: str) -> str | None:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
        return None

    def clean_query(self, query: str) -> str:
        query = query.strip()
        query = re.sub(r'\s+', ' ', query)
        return query

    def find_best_produit(self, query: str):
        if not query:
            return None

        qs = Produit.objects.filter(
            models.Q(nom__icontains=query)
            | models.Q(couleur__icontains=query)
            | models.Q(taille__icontains=query)
        )
        if qs.exists():
            return qs.first()

        tokens = [token for token in query.split() if len(token) > 1]
        for token in tokens:
            qs = Produit.objects.filter(
                models.Q(nom__icontains=token)
                | models.Q(couleur__icontains=token)
                | models.Q(taille__icontains=token)
            )
            if qs.exists():
                return qs.first()

        return None
