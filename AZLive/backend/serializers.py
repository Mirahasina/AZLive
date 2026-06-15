from rest_framework import serializers

from .models import (
    Client,
    Commande,
    Livraison,
    Livreur,
    Paiement,
    Produit,
    Vendeur,
    Message,
    Collaborateur,
    Live,
    Variante,
    PageFacebook,
    ParametresPlateforme,
)


class PageFacebookSerializer(serializers.ModelSerializer):
    class Meta:
        model = PageFacebook
        fields = ['id', 'page_id', 'nom', 'statut']


class VendeurSerializer(serializers.ModelSerializer):
    pages_facebook = PageFacebookSerializer(many=True, read_only=True)

    class Meta:
        model = Vendeur
        fields = [
            'id', 'nom', 'contact', 'user', 'facebook_page_id', 'facebook_page_name',
            'tiktok_username', 'is_demo_mode', 'pages_facebook'
        ]


class CollaborateurSerializer(serializers.ModelSerializer):
    class Meta:
        model = Collaborateur
        fields = ['id', 'nom', 'telephone', 'role', 'vendeur']




class VarianteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Variante
        fields = ['id', 'produit', 'taille', 'couleur', 'stock']


class ProduitSerializer(serializers.ModelSerializer):
    vendeur = VendeurSerializer(read_only=True)
    vendeur_id = serializers.PrimaryKeyRelatedField(queryset=Vendeur.objects.all(), source='vendeur', write_only=True)
    variantes = VarianteSerializer(many=True, read_only=True)

    class Meta:
        model = Produit
        fields = ['id', 'nom', 'taille', 'couleur', 'prix', 'stock', 'photo', 'vendeur', 'vendeur_id', 'code_jp', 'variantes']


class LiveSerializer(serializers.ModelSerializer):
    chiffre_affaires = serializers.SerializerMethodField()
    nb_fiches = serializers.SerializerMethodField()
    operateur_nom = serializers.SerializerMethodField()
    produits_dressing = ProduitSerializer(many=True, read_only=True)
    produits_dressing_ids = serializers.PrimaryKeyRelatedField(
        queryset=Produit.objects.all(), source='produits_dressing', many=True, write_only=True, required=False
    )

    class Meta:
        model = Live
        fields = [
            'id', 'titre', 'date_live', 'statut', 'vendeur', 'operateur',
            'chiffre_affaires', 'nb_fiches', 'operateur_nom',
            'produits_dressing', 'produits_dressing_ids', 'pages_facebook'
        ]

    def get_chiffre_affaires(self, obj):
        confirmed_status = [
            Commande.STATUT_CONFIRME,
            Commande.STATUT_PREPARE,
            Commande.STATUT_EN_LIVRAISON,
            Commande.STATUT_LIVRE,
        ]
        orders = obj.commandes.filter(statut__in=confirmed_status)
        total = sum(order.produit.prix for order in orders if order.produit)
        return float(total)

    def get_nb_fiches(self, obj):
        return obj.commandes.count()

    def get_operateur_nom(self, obj):
        return obj.operateur.nom if obj.operateur else None



class ClientSerializer(serializers.ModelSerializer):
    sessions_count = serializers.SerializerMethodField()
    montant_valide = serializers.SerializerMethodField()

    class Meta:
        model = Client
        fields = [
            'id',
            'nom',
            'telephone',
            'adresse',
            'date_livraison_preferee',
            'facebook_id',
            'tiktok_id',
            'social_handle',
            'sessions_count',
            'montant_valide',
        ]

    def get_sessions_count(self, obj):
        return obj.commandes.count()

    def get_montant_valide(self, obj):
        confirmed_status = [
            Commande.STATUT_CONFIRME,
            Commande.STATUT_PREPARE,
            Commande.STATUT_EN_LIVRAISON,
            Commande.STATUT_LIVRE,
        ]
        orders = obj.commandes.filter(statut__in=confirmed_status)
        total = sum(order.produit.prix for order in orders if order.produit)
        return float(total)



class PaiementSerializer(serializers.ModelSerializer):
    commande_id = serializers.PrimaryKeyRelatedField(queryset=Commande.objects.all(), source='commande')

    class Meta:
        model = Paiement
        fields = ['id', 'commande_id', 'methode', 'statut', 'capture_mobile_money']


class LivreurSerializer(serializers.ModelSerializer):
    class Meta:
        model = Livreur
        fields = ['id', 'nom', 'telephone']


class LivraisonSerializer(serializers.ModelSerializer):
    commande_id = serializers.PrimaryKeyRelatedField(queryset=Commande.objects.all(), source='commande')
    livreur = LivreurSerializer(read_only=True)
    livreur_id = serializers.PrimaryKeyRelatedField(queryset=Livreur.objects.all(), source='livreur', write_only=True, allow_null=True, required=False)

    class Meta:
        model = Livraison
        fields = [
            'id',
            'commande_id',
            'statut',
            'localisation_actuelle',
            'tracking_notes',
            'date_assignation',
            'date_livraison',
            'updated_at',
            'livreur',
            'livreur_id',
        ]


class CommandeSerializer(serializers.ModelSerializer):
    client = ClientSerializer(read_only=True)
    client_id = serializers.PrimaryKeyRelatedField(queryset=Client.objects.all(), source='client', write_only=True)
    produit = ProduitSerializer(read_only=True)
    produit_id = serializers.PrimaryKeyRelatedField(queryset=Produit.objects.all(), source='produit', write_only=True)
    paiement = PaiementSerializer(read_only=True)
    livraison = LivraisonSerializer(read_only=True)
    live = LiveSerializer(read_only=True)
    live_id = serializers.PrimaryKeyRelatedField(queryset=Live.objects.all(), source='live', write_only=True, allow_null=True, required=False)
    variante = VarianteSerializer(read_only=True)
    variante_id = serializers.PrimaryKeyRelatedField(queryset=Variante.objects.all(), source='variante', write_only=True, allow_null=True, required=False)

    class Meta:
        model = Commande
        fields = [
            'id',
            'client',
            'client_id',
            'produit',
            'produit_id',
            'ordre_jp',
            'statut',
            'date_creation',
            'paiement',
            'livraison',
            'live',
            'live_id',
            'variante',
            'variante_id',
        ]



class MessageSerializer(serializers.ModelSerializer):
    commande_id = serializers.PrimaryKeyRelatedField(queryset=Commande.objects.all(), source='commande')

    class Meta:
        model = Message
        fields = ['id', 'commande_id', 'contenu', 'date_envoi', 'numero_relance']
