from rest_framework import serializers

from .models import Client, Commande, Livraison, Livreur, Paiement, Produit, Vendeur, Message


class VendeurSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vendeur
        fields = ['id', 'nom', 'contact', 'user']


class ProduitSerializer(serializers.ModelSerializer):
    vendeur = VendeurSerializer(read_only=True)
    vendeur_id = serializers.PrimaryKeyRelatedField(queryset=Vendeur.objects.all(), source='vendeur', write_only=True)

    class Meta:
        model = Produit
        fields = ['id', 'nom', 'taille', 'couleur', 'prix', 'stock', 'photo', 'vendeur', 'vendeur_id']


class ClientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Client
        fields = ['id', 'nom', 'telephone', 'adresse', 'date_livraison_preferee', 'facebook_id', 'tiktok_id']


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
        ]


class MessageSerializer(serializers.ModelSerializer):
    commande_id = serializers.PrimaryKeyRelatedField(queryset=Commande.objects.all(), source='commande')

    class Meta:
        model = Message
        fields = ['id', 'commande_id', 'contenu', 'date_envoi', 'numero_relance']
