from django.urls import path
from rest_framework.authtoken import views as token_views

from .views import (
    CommandeDetailView,
    CommandeListCreateView,
    CommandeSearchAPIView,
    JPAnalyseAPIView,
    JPRelanceAPIView,
    JPCaptureAPIView,
    LivraisonTrackingAPIView,
    ProduitDetailView,
    ProduitListCreateView,
    TicketAPIView,
    VendeurListCreateView,
    CommandeUploadPaiementAPIView,
    CommandeEtiquetteJPAPIView,
    CommandeLancerLivraisonAPIView,
    DashboardStatsAPIView,
)
from .webhooks import FacebookWebhookView, TikTokWebhookView

urlpatterns = [
    # Auth
    path('auth/login/', token_views.obtain_auth_token, name='auth-login'),

    # Vendeurs & Produits
    path('vendeurs/', VendeurListCreateView.as_view(), name='vendeur-list-create'),
    path('produits/', ProduitListCreateView.as_view(), name='produit-list-create'),
    path('produits/<int:pk>/', ProduitDetailView.as_view(), name='produit-detail'),

    # Commandes — routes spécifiques AVANT la route générique <int:pk>/
    path('commandes/', CommandeListCreateView.as_view(), name='commande-list-create'),
    path('commandes/search/', CommandeSearchAPIView.as_view(), name='commande-search'),
    path('commandes/<int:pk>/upload-paiement/', CommandeUploadPaiementAPIView.as_view(), name='commande-upload-paiement'),
    path('commandes/<int:pk>/etiquette-jp/', CommandeEtiquetteJPAPIView.as_view(), name='commande-etiquette-jp'),
    path('commandes/<int:pk>/lancer-livraison/', CommandeLancerLivraisonAPIView.as_view(), name='commande-lancer-livraison'),
    path('commandes/<int:commande_id>/ticket/', TicketAPIView.as_view(), name='commande-ticket'),
    path('commandes/<int:pk>/', CommandeDetailView.as_view(), name='commande-detail'),

    # Livraisons
    path('livraisons/tracking/', LivraisonTrackingAPIView.as_view(), name='livraison-tracking'),

    # JP
    path('jp-capture/', JPCaptureAPIView.as_view(), name='jp-capture'),
    path('jp-analyze/', JPAnalyseAPIView.as_view(), name='jp-analyze'),
    path('jp-relance/', JPRelanceAPIView.as_view(), name='jp-relance'),

    # Webhooks réseaux sociaux
    path('webhooks/facebook/', FacebookWebhookView.as_view(), name='webhook-facebook'),
    path('webhooks/tiktok/', TikTokWebhookView.as_view(), name='webhook-tiktok'),

    # Dashboard
    path('dashboard/stats/', DashboardStatsAPIView.as_view(), name='dashboard-stats'),
]
