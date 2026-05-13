from django.contrib import admin

from .models import Client, Commande, Livraison, Livreur, Paiement, Produit, Vendeur, Message


@admin.register(Vendeur)
class VendeurAdmin(admin.ModelAdmin):
    list_display = ('nom', 'contact')


@admin.register(Produit)
class ProduitAdmin(admin.ModelAdmin):
    list_display = ('nom', 'couleur', 'taille', 'prix', 'stock', 'vendeur')
    list_filter = ('couleur', 'taille', 'vendeur')
    search_fields = ('nom', 'couleur', 'taille')


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ('nom', 'telephone', 'adresse', 'date_livraison_preferee')
    search_fields = ('nom', 'telephone', 'adresse')


@admin.register(Commande)
class CommandeAdmin(admin.ModelAdmin):
    list_display = ('id', 'client', 'produit', 'ordre_jp', 'statut', 'date_creation')
    list_filter = ('statut',)
    search_fields = ('client__nom', 'produit__nom')


@admin.register(Paiement)
class PaiementAdmin(admin.ModelAdmin):
    list_display = ('commande', 'methode', 'statut')
    list_filter = ('statut', 'methode')


@admin.register(Livreur)
class LivreurAdmin(admin.ModelAdmin):
    list_display = ('nom', 'telephone')


@admin.register(Livraison)
class LivraisonAdmin(admin.ModelAdmin):
    list_display = ('commande', 'statut', 'livreur', 'date_assignation', 'date_livraison')
    list_filter = ('statut',)


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('commande', 'date_envoi', 'numero_relance')
    search_fields = ('commande__client__nom', 'contenu')
