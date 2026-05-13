from django.test import TestCase

from .models import Client, Commande, Livraison, Livreur, Paiement, Produit, Vendeur, Message


class BackendModelsTest(TestCase):
    def test_create_produit_client_commande(self):
        vendeur = Vendeur.objects.create(nom='Vendeur Live', contact='0341234567')
        produit = Produit.objects.create(
            nom='Robe Rouge', taille='M', couleur='Rouge', prix='45000.00', stock=10, photo='', vendeur=vendeur
        )
        client = Client.objects.create(
            nom='Marie', telephone='0349876543', adresse='Antananarivo', date_livraison_preferee='2026-05-20'
        )
        commande = Commande.objects.create(client=client, produit=produit, ordre_jp=1)

        self.assertEqual(commande.client, client)
        self.assertEqual(commande.produit, produit)
        self.assertEqual(commande.statut, Commande.STATUT_JP_CAPTURE)
        self.assertEqual(str(commande), f"Commande #{commande.pk} - {client.nom} - {produit.nom}")

    def test_paiement_livraison_relations(self):
        vendeur = Vendeur.objects.create(nom='Vendeur Live', contact='0341234567')
        produit = Produit.objects.create(
            nom='Robe Rouge', taille='M', couleur='Rouge', prix='45000.00', stock=10, photo='', vendeur=vendeur
        )
        client = Client.objects.create(nom='Jean', telephone='0347654321', adresse='Antananarivo', date_livraison_preferee='2026-05-21')
        commande = Commande.objects.create(client=client, produit=produit, ordre_jp=2)
        paiement = Paiement.objects.create(commande=commande, methode=Paiement.METHODE_LIVRAISON, statut=Paiement.STATUT_NON_PAYE)
        livreur = Livreur.objects.create(nom='Livreur AZExpress', telephone='0331239876')
        livraison = Livraison.objects.create(commande=commande, statut=Livraison.STATUT_ASSIGNE, livreur=livreur)
        message = Message.objects.create(commande=commande, contenu='Merci, envoyez votre adresse.', numero_relance=0)

        self.assertEqual(paiement.commande, commande)
        self.assertEqual(livraison.commande, commande)
        self.assertEqual(livraison.livreur, livreur)
        self.assertEqual(commande.paiement, paiement)
        self.assertEqual(commande.livraison, livraison)
        self.assertEqual(commande.messages.count(), 1)
        self.assertEqual(message.numero_relance, 0)
        self.assertEqual(str(paiement), f"Paiement commande #{commande.pk} - {paiement.get_statut_display()}")


class BackendAPITest(TestCase):
    def setUp(self):
        self.vendeur = Vendeur.objects.create(nom='Vendeur Live', contact='0341234567')
        self.produit = Produit.objects.create(
            nom='Robe Rouge', taille='M', couleur='Rouge', prix='45000.00', stock=10, photo='', vendeur=self.vendeur
        )

    def test_jp_capture_endpoint_creates_commande(self):
        payload = {
            'comment_text': 'JP ROBE ROUGE',
            'nom': 'Claire',
            'telephone': '0341122334',
            'adresse': 'Antananarivo',
            'date_livraison_preferee': '2026-05-25',
        }
        response = self.client.post('/api/jp-capture/', payload, content_type='application/json')

        self.assertEqual(response.status_code, 201)
        self.assertIn('commande', response.json())
        self.assertEqual(response.json()['produit_reconnu'], 'Robe Rouge')
        self.assertTrue('message_envoye' in response.json())
        self.assertEqual(Commande.objects.count(), 1)
        self.assertEqual(Client.objects.count(), 1)

    def test_produit_list_endpoint(self):
        response = self.client.get('/api/produits/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['nom'], 'Robe Rouge')

    def test_commande_search_endpoint(self):
        client = Client.objects.create(nom='Serge', telephone='0344455667', adresse='Tananarive')
        commande = Commande.objects.create(client=client, produit=self.produit, ordre_jp=1)

        response = self.client.get('/api/commandes/search/', {'q': 'Serge'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['id'], commande.id)

        response = self.client.get('/api/commandes/search/', {'q': 'Robe'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

    def test_jp_relance_endpoint(self):
        client = Client.objects.create(nom='Emilie', telephone='0349988776', adresse='Tana')
        commande = Commande.objects.create(client=client, produit=self.produit, ordre_jp=1)
        Message.objects.create(commande=commande, contenu='Bonjour, merci pour votre JP.', numero_relance=0)

        response = self.client.post('/api/jp-relance/', content_type='application/json')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data['relances']), 1)
        self.assertEqual(data['relances'][0]['commande_id'], commande.id)
        self.assertEqual(data['relances'][0]['numero_relance'], 1)

    def test_jp_analyze_endpoint(self):
        payload = {'comment_text': 'JP ROBE ROUGE taille M couleur rouge'}
        response = self.client.post('/api/jp-analyze/', payload, content_type='application/json')

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['intent'], 'achat')
        self.assertIn('product_query', data)
        self.assertEqual(data['produit_trouve'], 'Robe Rouge')

    def test_ticket_endpoint_returns_ticket_data(self):
        client = Client.objects.create(nom='Hery', telephone='0345566778', adresse='Tana')
        commande = Commande.objects.create(client=client, produit=self.produit, ordre_jp=1)
        livraison = Livraison.objects.create(commande=commande, statut=Livraison.STATUT_ASSIGNE)

        response = self.client.get(f'/api/commandes/{commande.id}/ticket/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['commande_id'], commande.id)
        self.assertEqual(data['client']['nom'], 'Hery')
        self.assertEqual(data['produit']['nom'], 'Robe Rouge')
        self.assertEqual(data['livraison']['statut'], 'Assigné livreur')

    def test_livraison_tracking_endpoint(self):
        client = Client.objects.create(nom='Faly', telephone='0346677889', adresse='Tana')
        commande = Commande.objects.create(client=client, produit=self.produit, ordre_jp=1)
        livraison = Livraison.objects.create(commande=commande, statut=Livraison.STATUT_PREPARATION, localisation_actuelle='Bureau', tracking_notes='Colis en cours de préparation')

        response = self.client.get('/api/livraisons/tracking/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['localisation_actuelle'], 'Bureau')

        response = self.client.get('/api/livraisons/tracking/', {'commande_id': commande.id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['commande_id'], commande.id)
