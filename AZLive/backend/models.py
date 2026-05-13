from django.db import models


class Vendeur(models.Model):
    nom = models.CharField(max_length=255)
    contact = models.CharField(max_length=255)

    def __str__(self):
        return self.nom


class Produit(models.Model):
    nom = models.CharField(max_length=255)
    taille = models.CharField(max_length=50)
    couleur = models.CharField(max_length=50)
    prix = models.DecimalField(max_digits=10, decimal_places=2)
    stock = models.IntegerField()
    photo = models.CharField(max_length=500, blank=True)
    vendeur = models.ForeignKey(Vendeur, on_delete=models.CASCADE, related_name='produits')

    def __str__(self):
        return f"{self.nom} ({self.couleur}, {self.taille})"


class Client(models.Model):
    nom = models.CharField(max_length=255)
    telephone = models.CharField(max_length=20)
    adresse = models.TextField()
    date_livraison_preferee = models.DateField(blank=True, null=True)

    def __str__(self):
        return self.nom


class Commande(models.Model):
    STATUT_JP_CAPTURE = 'jp_capture'
    STATUT_CONFIRME = 'confirme'
    STATUT_PREPARE = 'prepare'
    STATUT_EN_LIVRAISON = 'en_livraison'
    STATUT_LIVRE = 'livre'
    STATUT_ANNULE = 'annule'

    STATUT_CHOICES = [
        (STATUT_JP_CAPTURE, 'JP capturé'),
        (STATUT_CONFIRME, 'Confirmé'),
        (STATUT_PREPARE, 'Préparé'),
        (STATUT_EN_LIVRAISON, 'En livraison'),
        (STATUT_LIVRE, 'Livré'),
        (STATUT_ANNULE, 'Annulé'),
    ]

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='commandes')
    produit = models.ForeignKey(Produit, on_delete=models.CASCADE, related_name='commandes')
    ordre_jp = models.IntegerField(default=1)
    statut = models.CharField(max_length=50, choices=STATUT_CHOICES, default=STATUT_JP_CAPTURE)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date_creation']

    def __str__(self):
        return f"Commande #{self.pk} - {self.client.nom} - {self.produit.nom}"


class Paiement(models.Model):
    METHODE_LIVRAISON = 'livraison'
    METHODE_MOBILE_MONEY = 'mobile_money'

    STATUT_NON_PAYE = 'non_paye'
    STATUT_PAYE = 'paye'

    METHODE_CHOICES = [
        (METHODE_LIVRAISON, 'Paiement à la livraison'),
        (METHODE_MOBILE_MONEY, 'Mobile Money'),
    ]

    STATUT_CHOICES = [
        (STATUT_NON_PAYE, 'Non payé'),
        (STATUT_PAYE, 'Payé'),
    ]

    commande = models.OneToOneField(Commande, on_delete=models.CASCADE, related_name='paiement')
    methode = models.CharField(max_length=50, choices=METHODE_CHOICES, default=METHODE_LIVRAISON)
    statut = models.CharField(max_length=50, choices=STATUT_CHOICES, default=STATUT_NON_PAYE)
    capture_mobile_money = models.CharField(max_length=500, blank=True)

    def __str__(self):
        return f"Paiement commande #{self.commande.pk} - {self.get_statut_display()}"


class Livreur(models.Model):
    nom = models.CharField(max_length=255)
    telephone = models.CharField(max_length=20)

    def __str__(self):
        return self.nom


class Livraison(models.Model):
    STATUT_BUREAU = 'au_bureau'
    STATUT_PREPARATION = 'en_preparation'
    STATUT_ASSIGNE = 'assigne_livreur'
    STATUT_EN_LIVRAISON = 'en_livraison'
    STATUT_LIVRE = 'livre'

    STATUT_CHOICES = [
        (STATUT_BUREAU, 'Au bureau'),
        (STATUT_PREPARATION, 'En préparation'),
        (STATUT_ASSIGNE, 'Assigné livreur'),
        (STATUT_EN_LIVRAISON, 'En livraison'),
        (STATUT_LIVRE, 'Livré'),
    ]

    commande = models.OneToOneField(Commande, on_delete=models.CASCADE, related_name='livraison')
    statut = models.CharField(max_length=50, choices=STATUT_CHOICES, default=STATUT_BUREAU)
    localisation_actuelle = models.CharField(max_length=255, blank=True)
    tracking_notes = models.TextField(blank=True)
    date_assignation = models.DateTimeField(blank=True, null=True)
    date_livraison = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)
    livreur = models.ForeignKey(Livreur, on_delete=models.SET_NULL, blank=True, null=True, related_name='livraisons')

    def __str__(self):
        return f"Livraison commande #{self.commande.pk} - {self.get_statut_display()}"


class Message(models.Model):
    commande = models.ForeignKey(Commande, on_delete=models.CASCADE, related_name='messages')
    contenu = models.TextField()
    date_envoi = models.DateTimeField(auto_now_add=True)
    numero_relance = models.IntegerField(default=0)

    def __str__(self):
        return f"Message commande #{self.commande.pk} - relance {self.numero_relance}"
