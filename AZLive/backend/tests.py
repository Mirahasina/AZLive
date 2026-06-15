from django.test import TestCase
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile

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
        # Pagination enabled — results are nested under 'results' key
        results = data['results']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['nom'], 'Robe Rouge')

    def test_commande_search_endpoint(self):
        client = Client.objects.create(nom='Serge', telephone='0344455667', adresse='Tananarive')
        commande = Commande.objects.create(client=client, produit=self.produit, ordre_jp=1)

        response = self.client.get('/api/commandes/search/', {'q': 'Serge'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        # Pagination enabled — results are nested under 'results' key
        results = data['results']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['id'], commande.id)

        response = self.client.get('/api/commandes/search/', {'q': 'Robe'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['results']), 1)

    def test_jp_relance_endpoint(self):
        client = Client.objects.create(nom='Emilie', telephone='0349988776', adresse='Tana')
        commande = Commande.objects.create(client=client, produit=self.produit, ordre_jp=1)
        Message.objects.create(commande=commande, contenu='Bonjour, merci pour votre JP.', numero_relance=0)

        response = self.client.post('/api/jp-relance/', {'force': True}, content_type='application/json')
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


class BackendGapsAPITest(TestCase):
    def setUp(self):
        # Create seller and linked User account
        self.user = User.objects.create_user(username='vendeur_test', password='password123')
        self.vendeur = Vendeur.objects.create(user=self.user, nom='Vendeur Chic', contact='0341112223')
        self.produit = Produit.objects.create(
            nom='Robe Noire', taille='L', couleur='Noir', prix='60000.00', stock=5, photo='', vendeur=self.vendeur
        )

    def test_stock_lifecycle_on_confirmation(self):
        client = Client.objects.create(nom='Sahondra', telephone='0345556667', adresse='Tana')
        commande = Commande.objects.create(client=client, produit=self.produit, statut=Commande.STATUT_JP_CAPTURE)

        # Initially, stock is 5
        self.assertEqual(self.produit.stock, 5)

        # Confirm command
        commande.statut = Commande.STATUT_CONFIRME
        commande.save()

        # Reload product
        self.produit.refresh_from_db()
        self.assertEqual(self.produit.stock, 4)

        # Cancel command
        commande.statut = Commande.STATUT_ANNULE
        commande.save()

        self.produit.refresh_from_db()
        self.assertEqual(self.produit.stock, 5)

    def test_facebook_webhook_capture(self):
        payload = {
            'sender_facebook_id': 'fb_12345',
            'sender_name': 'Rabe',
            'comment_text': 'JP Robe Noire'
        }
        response = self.client.post('/api/webhooks/facebook/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 201)

        # Verify customer linked by facebook_id
        client = Client.objects.get(facebook_id='fb_12345')
        self.assertEqual(client.nom, 'Rabe')

        # Verify order priority created
        self.assertEqual(Commande.objects.filter(client=client).count(), 1)

    def test_tiktok_webhook_capture(self):
        payload = {
            'sender_tiktok_id': 'tt_67890',
            'sender_name': 'Koto',
            'comment_text': 'JP Robe Noire'
        }
        response = self.client.post('/api/webhooks/tiktok/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 201)

        client = Client.objects.get(tiktok_id='tt_67890')
        self.assertEqual(client.nom, 'Koto')

    def test_upload_payment_screenshot(self):
        client = Client.objects.create(nom='Aina', telephone='0341234567', adresse='Tana')
        commande = Commande.objects.create(client=client, produit=self.produit)

        # Mock image file upload
        mock_file = SimpleUploadedFile("receipt.png", b"file_content", content_type="image/png")

        response = self.client.post(
            f'/api/commandes/{commande.id}/upload-paiement/',
            {'file': mock_file},
            format='multipart'
        )
        self.assertEqual(response.status_code, 200)

        # Verify payment details and automated confirmation
        commande.refresh_from_db()
        self.assertEqual(commande.statut, Commande.STATUT_CONFIRME)
        self.assertEqual(commande.paiement.statut, Paiement.STATUT_PAYE)
        self.assertEqual(commande.paiement.methode, Paiement.METHODE_MOBILE_MONEY)
        self.assertIn('receipt', commande.paiement.capture_mobile_money)
        self.assertTrue(commande.paiement.capture_mobile_money.endswith('.png'))

    def test_thermal_label_generation(self):
        client = Client.objects.create(nom='Fara', telephone='0339999999', adresse='Tana')
        commande = Commande.objects.create(client=client, produit=self.produit)

        response = self.client.get(f'/api/commandes/{commande.id}/etiquette-jp/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('JP ROBE NOIRE', data['label_text'])
        self.assertIn('60,000 Ar', data['label_text'])


    def test_azexpress_shipping_dispatch(self):
        client = Client.objects.create(nom='Rina', telephone='0328888888', adresse='Tana')
        commande = Commande.objects.create(client=client, produit=self.produit)

        response = self.client.post(f'/api/commandes/{commande.id}/lancer-livraison/')
        self.assertEqual(response.status_code, 200)

        commande.refresh_from_db()
        self.assertEqual(commande.statut, Commande.STATUT_EN_LIVRAISON)
        self.assertEqual(commande.livraison.statut, Livraison.STATUT_EN_LIVRAISON)
        self.assertIn('AZX-', commande.livraison.tracking_notes)

    def test_double_ship_blocked(self):
        """Bug #5 fix — un deuxième clic sur Lancer Livraison doit retourner 409."""
        client = Client.objects.create(nom='Tovo', telephone='0321111111', adresse='Tana')
        commande = Commande.objects.create(client=client, produit=self.produit)

        # Premier envoi
        r1 = self.client.post(f'/api/commandes/{commande.id}/lancer-livraison/')
        self.assertEqual(r1.status_code, 200)

        # Deuxième clic — doit être bloqué
        r2 = self.client.post(f'/api/commandes/{commande.id}/lancer-livraison/')
        self.assertEqual(r2.status_code, 409)
        self.assertIn('déjà en statut', r2.json()['detail'])

    def test_dashboard_statistics(self):
        # Create confirmed orders
        client1 = Client.objects.create(nom='User 1', telephone='0341', adresse='A')
        Commande.objects.create(client=client1, produit=self.produit, statut=Commande.STATUT_CONFIRME)

        # Create captured orders
        client2 = Client.objects.create(nom='User 2', telephone='0342', adresse='B')
        Commande.objects.create(client=client2, produit=self.produit, statut=Commande.STATUT_JP_CAPTURE)

        # Stats query with vendeur_id (W6 fix — requis si non authentifié)
        response = self.client.get('/api/dashboard/stats/', {'vendeur_id': self.vendeur.id})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['nombre_jps'], 2)
        self.assertEqual(data['confirmes'], 1)
        self.assertEqual(data['chiffre_affaires'], 60000.00)
        self.assertEqual(data['montant_a_reverser'], 54000.00)  # 90% net payout

    def test_dashboard_requires_vendeur_id(self):
        """W6 fix — le dashboard doit retourner 403 si ni authentifié ni vendeur_id fourni."""
        response = self.client.get('/api/dashboard/stats/')
        self.assertEqual(response.status_code, 403)

    def test_client_serializer_exposes_social_ids(self):
        """W4 fix — les champs facebook_id et tiktok_id doivent apparaître dans l'API."""
        payload = {
            'sender_facebook_id': 'fb_audit_test',
            'sender_name': 'Audit User',
            'comment_text': 'JP Robe Noire'
        }
        response = self.client.post('/api/webhooks/facebook/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 201)
        commande_data = response.json()['commande']
        # Client imbriqué doit exposer facebook_id
        self.assertIn('facebook_id', commande_data['client'])
        self.assertEqual(commande_data['client']['facebook_id'], 'fb_audit_test')

    def test_social_connect_disconnect_endpoints(self):
        # Initial status: not connected
        self.assertFalse(self.vendeur.is_demo_mode)
        self.assertIsNone(self.vendeur.facebook_page_id)

        # Connect Facebook
        payload = {'vendeur_id': self.vendeur.id, 'platform': 'facebook'}
        response = self.client.post('/api/vendeurs/connect/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['facebook_page_id'], 'fb_page_123456789')
        self.assertEqual(response.json()['facebook_page_name'], 'Ma Boutique Facebook Officielle')

        # Connect Demo
        payload = {'vendeur_id': self.vendeur.id, 'platform': 'demo'}
        response = self.client.post('/api/vendeurs/connect/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['is_demo_mode'])
        self.assertIsNone(response.json()['facebook_page_id'])

        # Disconnect All
        payload = {'vendeur_id': self.vendeur.id, 'platform': 'all'}
        response = self.client.post('/api/vendeurs/disconnect/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['is_demo_mode'])

    def test_live_session_endpoints(self):
        from .models import Live, Collaborateur
        collab = Collaborateur.objects.create(nom='Clare Michel', role='operateur', vendeur=self.vendeur)
        live = Live.objects.create(titre="Dressing d'Hiver Premium Antsirabe", vendeur=self.vendeur, operateur=collab)

        response = self.client.get('/api/lives/')
        self.assertEqual(response.status_code, 200)
        # Paginated results
        results = response.json()['results']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['titre'], "Dressing d'Hiver Premium Antsirabe")
        self.assertEqual(results[0]['operateur_nom'], 'Clare Michel')

    def test_product_variants_endpoints(self):
        from .models import Variante
        variant = Variante.objects.create(produit=self.produit, taille='M', couleur='Noir', stock=2)

        response = self.client.get(f'/api/produits/{self.produit.id}/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data['variantes']), 1)
        self.assertEqual(data['variantes'][0]['taille'], 'M')
        self.assertEqual(data['variantes'][0]['stock'], 2)

    def test_client_stats_and_fidelity_endpoints(self):
        client = Client.objects.create(nom='Faratiana Rabe', telephone='0342255588', social_handle='@fara_rabe')
        
        # Test client list has computed fields
        response = self.client.get('/api/clients/')
        self.assertEqual(response.status_code, 200)
        results = response.json()['results']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['social_handle'], '@fara_rabe')
        self.assertEqual(results[0]['sessions_count'], 0)

        # Confirm 2 orders for client to make them loyal
        from .models import Commande
        Commande.objects.create(client=client, produit=self.produit, statut=Commande.STATUT_CONFIRME)
        Commande.objects.create(client=client, produit=self.produit, statut=Commande.STATUT_CONFIRME)

        # Get client stats
        response = self.client.get('/api/clients/stats/', {'vendeur_id': self.vendeur.id})
        self.assertEqual(response.status_code, 200)
        stats = response.json()
        self.assertEqual(stats['nombre_clients'], 1)
        self.assertEqual(stats['clients_fideles_count'], 1)
        self.assertEqual(stats['taux_fidelite'], 100.0)

    def test_facebook_pages_list(self):
        payload = {'vendeur_id': self.vendeur.id, 'platform': 'facebook'}
        self.client.post('/api/vendeurs/connect/', payload, content_type='application/json')

        response = self.client.get('/api/vendeurs/facebook-pages/', {'vendeur_id': self.vendeur.id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 4)
        self.assertEqual(response.json()[0]['nom'], 'AZLive Fashion')

    def test_live_dressing_association(self):
        from .models import Live
        live = Live.objects.create(titre="Live test dressing", vendeur=self.vendeur)

        payload = {
            'produits_dressing_ids': [self.produit.id],
            'pages_facebook': ['AZLive Fashion', 'Boutique Chic Madagascar']
        }
        response = self.client.patch(f'/api/lives/{live.id}/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['produits_dressing']), 1)
        self.assertEqual(response.json()['produits_dressing'][0]['id'], self.produit.id)
        self.assertEqual(response.json()['pages_facebook'], ['AZLive Fashion', 'Boutique Chic Madagascar'])

    def test_malagasy_queue_promotion_logic(self):
        from .models import Client, Commande
        client_a = Client.objects.create(nom='Aina', telephone='0341122334')
        client_b = Client.objects.create(nom='Bodo', telephone='0321122334')
        client_c = Client.objects.create(nom='Chantal', telephone='0334455667')

        # First client orders -> Should go to first priority (ordre_jp = 1)
        response_a = self.client.post('/api/jp-capture/', {
            'comment_text': f"JP {self.produit.nom}",
            'telephone': client_a.telephone,
            'nom': client_a.nom
        }, content_type='application/json')
        self.assertEqual(response_a.status_code, 201)
        self.assertEqual(response_a.json()['commande']['ordre_jp'], 1)
        self.assertIn("nahazo ny JP-nao amin'ny", response_a.json()['message_envoye'])

        # Second client orders -> Should go to waiting list (ordre_jp = 2)
        response_b = self.client.post('/api/jp-capture/', {
            'comment_text': f"JP {self.produit.nom}",
            'telephone': client_b.telephone,
            'nom': client_b.nom
        }, content_type='application/json')
        self.assertEqual(response_b.status_code, 201)
        self.assertEqual(response_b.json()['commande']['ordre_jp'], 2)
        self.assertIn("lisitra miandry", response_b.json()['message_envoye'])

        # Third client orders -> Should go to waiting list (ordre_jp = 3)
        response_c = self.client.post('/api/jp-capture/', {
            'comment_text': f"JP {self.produit.nom}",
            'telephone': client_c.telephone,
            'nom': client_c.nom
        }, content_type='application/json')
        self.assertEqual(response_c.status_code, 201)
        self.assertEqual(response_c.json()['commande']['ordre_jp'], 3)

        # Cancel the first command -> Should trigger promotion of the second command (client_b)
        cmd_a = Commande.objects.get(id=response_a.json()['commande']['id'])
        cmd_a.statut = Commande.STATUT_ANNULE
        cmd_a.save()
        
        # Delete the second command -> Should trigger promotion of the third command (client_c)
        cmd_b = Commande.objects.get(id=response_b.json()['commande']['id'])
        cmd_b.delete()


