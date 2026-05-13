from django.urls import path

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
)

urlpatterns = [
    path('vendeurs/', VendeurListCreateView.as_view(), name='vendeur-list-create'),
    path('produits/', ProduitListCreateView.as_view(), name='produit-list-create'),
    path('produits/<int:pk>/', ProduitDetailView.as_view(), name='produit-detail'),
    path('commandes/', CommandeListCreateView.as_view(), name='commande-list-create'),
    path('commandes/search/', CommandeSearchAPIView.as_view(), name='commande-search'),
    path('commandes/<int:pk>/', CommandeDetailView.as_view(), name='commande-detail'),
    path('commandes/<int:commande_id>/ticket/', TicketAPIView.as_view(), name='commande-ticket'),
    path('livraisons/tracking/', LivraisonTrackingAPIView.as_view(), name='livraison-tracking'),
    path('jp-capture/', JPCaptureAPIView.as_view(), name='jp-capture'),
    path('jp-analyze/', JPAnalyseAPIView.as_view(), name='jp-analyze'),
    path('jp-relance/', JPRelanceAPIView.as_view(), name='jp-relance'),
]
