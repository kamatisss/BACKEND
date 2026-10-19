import os
import json
from datetime import timedelta
from unittest.mock import patch, MagicMock
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth.models import User
from rest_framework_simplejwt.tokens import RefreshToken

class GenerateLayoutsViewTests(APITestCase):
    """AI Garden Designer: hybrid Gemini + smart layout engine. Customers always get 3 designs."""

    def setUp(self):
        from django.core.cache import cache
        from apps.library.models import InventoryItem
        cache.clear()  # throttle history, the layout cache and the Gemini breaker live here
        self.user = User.objects.create_user(username='testuser', password='password123')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.user).access_token}')
        self.url = reverse('generate-layouts')
        InventoryItem.objects.create(id=1, name="Oak Tree", category="plant", unit_price=1000, stock_quantity=10, real_world_size=4.0)
        InventoryItem.objects.create(id=2, name="Fern", category="plant", unit_price=500, stock_quantity=20, real_world_size=1.0)
        InventoryItem.objects.create(id=3, name="Palm Tree", category="plant", unit_price=800, stock_quantity=2, real_world_size=5.0)
        InventoryItem.objects.create(id=4, name="Rose", category="plant", unit_price=200, stock_quantity=40, real_world_size=0.5)
        self.valid = {'budget': 80000, 'lot_width': 10, 'lot_length': 12, 'preferred_plant_ids': ['1', '2']}

    def _ai_design(self, plants):
        from apps.library.garden_ai import GardenDesignSchema, PlantArrangement
        response = MagicMock()
        response.parsed = GardenDesignSchema(
            design_name='Lush / Organic', reasoning='Layered tropical planting.', total_cost=0,
            plants=[PlantArrangement(plant_id=p, gx=gx, gz=gz, rotation=0.0) for p, gx, gz in plants],
            plant_breakdown=[],
        )
        return response

    def _assert_three_valid_designs(self, data, budget=80000):
        from dataclasses import replace
        from apps.library import layout_engine as E
        from apps.library.models import InventoryItem, PricingSettings
        from apps.library.pricing import installed_price, plant_budget_from_all_in
        self.assertEqual([d['design_name'] for d in data['designs']], list(E.STYLES))
        ps = PricingSettings.current()
        items = {str(i.id): i for i in InventoryItem.objects.filter(category='plant')}
        specs = [replace(s, price=installed_price(ps, items[s.id])) for s in E.specs_from_inventory(items.values())]
        plant_budget = plant_budget_from_all_in(budget, 120, ps)
        site = E.Site(10, 12, E.zone_cells([z['zone'] for z in data.get('detected_zones', [])]))
        for d in data['designs']:
            plants = [p for p in d['plants'] if not p.get('is_existing')]
            self.assertTrue(plants, d['design_name'])
            self.assertEqual(E.check(plants, specs, site, plant_budget), [], d['design_name'])
            self.assertEqual(d['plant_cost'], sum(r['subtotal'] for r in d['plant_breakdown']))
            # The all-in estimate stays within the customer's budget
            self.assertLessEqual(d['cost_breakdown']['total'], budget + 2, d['design_name'])
            self.assertEqual(d['cost_breakdown']['total'], sum(l['amount'] for l in d['cost_breakdown']['lines']))

    # ── validation & access ──────────────────────────────────────────────────
    def test_generate_layouts_without_auth(self):
        self.client.credentials()
        response = self.client.post(self.url, self.valid, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_invalid_input(self):
        for bad in ({'lot_width': 10, 'lot_length': 10}, {'budget': -100, 'lot_width': 10, 'lot_length': 10},
                    {'budget': 'hundred', 'lot_width': 10, 'lot_length': 10}, {'budget': 5000, 'lot_length': 10},
                    {'budget': 5000, 'lot_width': -10, 'lot_length': 10}, {'budget': 5000, 'lot_width': 'ten', 'lot_length': 10},
                    {**self.valid, 'keep_clear_zones': 'not json'}):
            self.assertEqual(self.client.post(self.url, bad, format='json').status_code, 400, bad)

    # ── availability: the feature never depends on Gemini ───────────────────
    @patch.dict(os.environ, {'GEMINI_API_KEY': 'your_gemini_api_key_here'})
    def test_no_api_key_still_returns_three_smart_layouts(self):
        r = self.client.post(self.url, self.valid, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['ai_status'], 'disabled')
        self.assertTrue(all(d['source'] == 'engine' for d in r.data['designs']))
        self._assert_three_valid_designs(r.data)

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'test_api_key'})
    @patch('google.genai.Client')
    def test_hybrid_ai_design_is_checked_and_repaired(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        # Two oaks in the same cell, a palm over stock, an invented id
        mock_client.models.generate_content.return_value = self._ai_design(
            [('1', 5, 5), ('1', 5, 5), ('3', 2, 2), ('3', 10, 2), ('3', 15, 2), ('999', 8, 8)])
        r = self.client.post(self.url, self.valid, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['ai_status'], 'ok')
        self.assertEqual(mock_client.models.generate_content.call_count, 1)  # one Gemini call, not three
        sources = {d['design_name']: d['source'] for d in r.data['designs']}
        self.assertEqual(sources, {'Symmetrical': 'engine', 'Minimalist': 'engine', 'Lush / Organic': 'ai'})
        lush = r.data['designs'][2]
        self.assertTrue(lush['adjustments'])  # the customer is told what was fixed
        self._assert_three_valid_designs(r.data)

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'test_api_key'})
    @patch('google.genai.Client')
    def test_gemini_failure_falls_back_and_pauses_gemini(self, mock_client_class):
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.models.generate_content.side_effect = Exception(
            '429 RESOURCE_EXHAUSTED quotaId GenerateRequestsPerDayPerProjectPerModel-FreeTier')
        r = self.client.post(self.url, self.valid, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['ai_status'], 'quota')
        self.assertIn("today's limit", r.data['notice'])
        self._assert_three_valid_designs(r.data)
        # Next request: Gemini isn't even tried until the quota resets
        calls = mock_client.models.generate_content.call_count
        r2 = self.client.post(self.url, {**self.valid, 'seed': 7}, format='json')
        self.assertEqual(r2.data['ai_status'], 'quota')
        self.assertEqual(mock_client.models.generate_content.call_count, calls)

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'test_api_key', 'AI_MODE': 'engine_only'})
    @patch('google.genai.Client')
    def test_engine_only_mode_never_calls_gemini(self, mock_client_class):
        r = self.client.post(self.url, self.valid, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['ai_status'], 'disabled')
        mock_client_class.assert_not_called()

    # ── quotas ───────────────────────────────────────────────────────────────
    @patch.dict(os.environ, {'GEMINI_API_KEY': 'test_api_key'})
    @patch('google.genai.Client')
    def test_daily_ai_quota_then_smart_layouts_and_cache_is_free(self, mock_client_class):
        from apps.library.garden_ai import AILayoutThrottle
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.models.generate_content.return_value = self._ai_design([('1', 5, 5), ('2', 10, 10)])
        with patch.object(AILayoutThrottle, 'get_rate', return_value='1/day'):
            AILayoutThrottle.rate, AILayoutThrottle.num_requests = None, None
            self.assertEqual(self.client.post(self.url, self.valid, format='json').data['ai_status'], 'ok')
            # Same request again: served from cache, no Gemini call, no quota used
            again = self.client.post(self.url, self.valid, format='json')
            self.assertEqual(again.status_code, 200)
            self.assertEqual(mock_client.models.generate_content.call_count, 1)
            # A different request: the customer's AI quota is used → smart layouts, not an error
            other = self.client.post(self.url, {**self.valid, 'seed': 3}, format='json')
            self.assertEqual(other.status_code, 200)
            self.assertEqual(other.data['ai_status'], 'user_quota')
            self.assertTrue(all(d['source'] == 'engine' for d in other.data['designs']))
            # Staff are exempt
            staff = User.objects.create_user(username='office', password='pw', is_staff=True)
            self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(staff).access_token}')
            self.assertEqual(self.client.post(self.url, {**self.valid, 'seed': 4}, format='json').data['ai_status'], 'ok')
        AILayoutThrottle.rate, AILayoutThrottle.num_requests = None, None

    # ── keep-clear zones ─────────────────────────────────────────────────────
    @patch.dict(os.environ, {'GEMINI_API_KEY': 'your_gemini_api_key_here'})
    def test_customer_zones_are_kept_clear(self):
        from apps.library import layout_engine as E
        zones = ['foreground_center', 'midground_left']
        r = self.client.post(self.url, {**self.valid, 'keep_clear_zones': zones}, format='json')
        self.assertEqual(r.data['zone_scan'], 'customer')
        cells = E.zone_cells(zones)
        for d in r.data['designs']:
            for p in d['plants']:
                if not p.get('is_existing'):
                    self.assertNotIn((int(p['x'] / 0.5), int(p['z'] / 0.6)), cells)

    def test_zone_orientation_front_is_near_the_viewer(self):
        from apps.library import layout_engine as E
        from apps.library.utils import resolve_coordinates
        # z grows towards the viewer in the 3D/AR view, so "foreground" must be the high-z rows
        self.assertGreater(resolve_coordinates('foreground_center', 10, 10)['z'],
                           resolve_coordinates('background_perimeter', 10, 10)['z'])
        self.assertEqual(E.ZONES['background_perimeter']['gz'], (0, 4))

    @patch('apps.library.garden_ai.scan_image_zones')
    @patch.dict(os.environ, {'GEMINI_API_KEY': 'test_api_key'})
    def test_scan_zones_reports_failure_instead_of_hiding_it(self, mock_scan):
        url = '/api/scan-zones/'
        photo = SimpleUploadedFile('yard.jpg', b'fake', content_type='image/jpeg')
        mock_scan.return_value = ([{'zone': 'foreground_left', 'label': 'path', 'blocks_planting': True,
                                    'is_existing': True}], None)
        r = self.client.post(url, {'image': photo}, format='multipart')
        self.assertEqual(r.data, {'status': 'ok', 'zones': mock_scan.return_value[0]})
        mock_scan.return_value = ([], 'unavailable')
        photo.seek(0)
        r = self.client.post(url, {'image': photo}, format='multipart')
        self.assertEqual(r.data['status'], 'failed')  # the UI asks the customer to mark zones

    # ── the engine's guarantees, on random gardens ───────────────────────────
    def test_engine_rules_hold_on_random_gardens(self):
        import random
        from apps.library import layout_engine as E
        rng = random.Random(42)
        base = [('1', 'Oak', 1000, 4.0), ('2', 'Fern', 500, 1.0), ('3', 'Palm', 800, 5.0), ('4', 'Rose', 200, 0.5)]
        for case in range(60):
            specs = [E.PlantSpec(i, n, p, rng.randint(1, 30), h, E.spacing_for(h)) for i, n, p, h in base]
            site = E.Site(rng.uniform(3, 25), rng.uniform(3, 25),
                          E.zone_cells(rng.sample(list(E.ZONES), rng.randint(0, 3))))
            budget = rng.choice([800, 3000, 9000, 30000])
            for style in E.STYLES:
                d = E.generate(style, specs, site, budget, seed=case)
                self.assertEqual(E.check(d['plants'], specs, site, budget), [], (case, style))
            messy = [{'plant_id': rng.choice(['1', '2', '3', '4', '999']), 'x': rng.uniform(-2, 27),
                      'z': rng.uniform(-2, 27), 'rotation': 0} for _ in range(rng.randint(0, 40))]
            fixed, _ = E.repair(messy, specs, site, budget, seed=case)
            self.assertEqual(E.check(fixed, specs, site, budget), [], (case, 'repair'))

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'your_gemini_api_key_here'})
    def test_budget_too_small_for_the_site_says_what_it_takes(self):
        r = self.client.post(self.url, {**self.valid, 'budget': 20000}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn("doesn't cover the basics", r.data['error'])
        self.assertGreater(r.data['min_budget'], 20000)
        ok = self.client.post(self.url, {**self.valid, 'budget': r.data['min_budget']}, format='json')
        self.assertEqual(ok.status_code, 200)

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'your_gemini_api_key_here'})
    def test_budget_range_gives_good_better_best(self):
        r = self.client.post(self.url, {**self.valid, 'budget': None, 'budget_min': 60000, 'budget_max': 120000},
                             format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['budget_range'], {'min': 60000, 'max': 120000})
        by_style = {d['design_name']: d for d in r.data['designs']}
        self.assertEqual(by_style['Minimalist']['target_budget'], 60000)
        self.assertEqual(by_style['Symmetrical']['target_budget'], 90000)
        self.assertEqual(by_style['Lush / Organic']['target_budget'], 120000)
        for d in r.data['designs']:
            self.assertLessEqual(d['cost_breakdown']['total'], d['target_budget'] + 2, d['design_name'])
        self.assertGreater(by_style['Lush / Organic']['total_cost'], by_style['Symmetrical']['total_cost'])

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'your_gemini_api_key_here'})
    def test_range_low_end_is_lifted_to_a_workable_budget(self):
        r = self.client.post(self.url, {**self.valid, 'budget': None, 'budget_min': 1000, 'budget_max': 90000},
                             format='json')
        self.assertEqual(r.status_code, 200, r.data)
        minimalist = next(d for d in r.data['designs'] if d['design_name'] == 'Minimalist')
        self.assertGreater(minimalist['target_budget'], 1000)
        self.assertTrue([p for p in minimalist['plants'] if not p.get('is_existing')])

    @patch.dict(os.environ, {'GEMINI_API_KEY': 'your_gemini_api_key_here'})
    def test_plants_only_mode_has_no_labour_or_site_work(self):
        r = self.client.post(self.url, {**self.valid, 'budget': None, 'budget_min': 5000, 'budget_max': 10000,
                                        'install': False}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertFalse(r.data['install'])
        for d in r.data['designs']:
            keys = [l['key'] for l in d['cost_breakdown']['lines']]
            self.assertEqual(keys, ['plants', 'delivery'])
            self.assertLessEqual(d['cost_breakdown']['total'], d['target_budget'])
            self.assertEqual(d['cost_breakdown']['low'], d['cost_breakdown']['high'])  # exact catalog prices

    def test_inverted_range_is_rejected(self):
        r = self.client.post(self.url, {**self.valid, 'budget': None, 'budget_min': 90000, 'budget_max': 50000},
                             format='json')
        self.assertEqual(r.status_code, 400)

    def test_budget_suggestion_for_the_yard(self):
        r = self.client.get('/api/pricing/suggest/', {'area_sqm': 120})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['bands'][2]['label'], '₱50K – ₱100K')   # same labels as the Project Hub
        low, high = r.data['suggested']['low'], r.data['suggested']['high']
        self.assertLess(low, high)
        self.assertGreaterEqual(low, 15000)                               # never below the minimum project
        small = self.client.get('/api/pricing/suggest/', {'area_sqm': 120, 'mode': 'plants'}).data
        self.assertLess(small['suggested']['high'], high)                 # plants only costs less
        self.assertEqual(self.client.get('/api/pricing/suggest/', {'area_sqm': 'x'}).status_code, 400)

    def test_engine_explains_unused_budget(self):
        from apps.library import layout_engine as E
        specs = [E.PlantSpec('1', 'Oak', 1000, 2, 4.0, E.spacing_for(4.0))]
        d = E.generate('Minimalist', specs, E.Site(5, 5), 100000)
        self.assertIn('stock', d['budget_note'].lower())


class ProductReviewTests(APITestCase):
    def setUp(self):
        from apps.library.models import InventoryItem, Order, OrderItem
        self.user = User.objects.create_user(username='buyer', password='password123')
        self.other = User.objects.create_user(username='other', password='password123')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.user).access_token}')

        self.plant = InventoryItem.objects.create(name="Hibiscus", category="plant", unit_price=300)
        self.order = Order.objects.create(
            user=self.user, customer_name='Buyer', customer_email='b@example.com',
            customer_address='Somewhere', total_price=600, status='Delivered',
        )
        self.line = OrderItem.objects.create(order=self.order, item=self.plant, quantity=2, price_at_booking=300)
        self.url = reverse('product-review-list')

    def _post(self, line=None, rating=5):
        return self.client.post(self.url, {'order_item': (line or self.line).id, 'rating': rating,
                                           'comment': 'Healthy plant'}, format='json')

    def test_review_delivered_item(self):
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['product'], self.plant.id)
        self.assertEqual(response.data['order_id'], self.order.id)

    def test_duplicate_review_rejected(self):
        self._post()
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('order_item', response.data)

    def test_undelivered_order_rejected(self):
        self.order.status = 'Shipped'
        self.order.save()
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_other_users_order_rejected(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.other).access_token}')
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_rating_rejected(self):
        response = self._post(rating=6)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_requires_auth(self):
        self.client.credentials()
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_order_and_inventory_expose_review_data(self):
        self._post(rating=4)
        order = self.client.get(reverse('order-detail', args=[self.order.id])).data
        self.assertEqual(order['items'][0]['review']['rating'], 4)

        item = self.client.get(reverse('inventoryitem-detail', args=[self.plant.id])).data
        self.assertEqual(item['avg_rating'], 4.0)
        self.assertEqual(item['review_count'], 1)

        public = self.client.get(reverse('product-review-public'), {'product': self.plant.id})
        self.assertEqual(public.status_code, status.HTTP_200_OK)
        self.assertEqual(len(public.data), 1)
        self.assertEqual(public.data[0]['product_name'], 'Hibiscus')


class ProjectWorkflowTests(APITestCase):
    """Booking → crew → sign-off → billing workflow."""

    SITE = (14.5995, 120.9842)  # Manila

    def setUp(self):
        from apps.library.models import StaffProfile, ServiceBooking, ProjectTracker, GardenDesign
        self.ServiceBooking, self.ProjectTracker = ServiceBooking, ProjectTracker
        self.admin = User.objects.create_user(username='office', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.admin, role='OFFICE_ADMIN')
        self.crew = User.objects.create_user(username='crew', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        self.other_crew = User.objects.create_user(username='crew2', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.other_crew, role='FIELD_CREW')
        self.customer = User.objects.create_user(username='cust', password='pw')

        self.design = GardenDesign.objects.create(name='D', total_cost=40000, user=self.customer)
        self.booking = ServiceBooking.objects.create(
            user=self.customer, service_type='Softscape', scheduled_date='2026-11-01',
            status='Installing', design=self.design, site_lat=self.SITE[0], site_lng=self.SITE[1],
        )
        self.booking.assigned_crew.add(self.crew)

    def _as(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _status(self, status_value):
        return self.client.patch(f'/api/bookings/{self.booking.id}/update_status/', {'status': status_value}, format='json')

    # ── sign-off ────────────────────────────────────────────────────────────
    def test_crew_cannot_finish_directly(self):
        self._as(self.crew)
        self.assertEqual(self._status('Finished').status_code, status.HTTP_403_FORBIDDEN)

    def test_crew_submits_for_review_and_admins_are_notified(self):
        from apps.library.models import Notification
        self._as(self.crew)
        self.assertEqual(self._status('For Review').status_code, status.HTTP_200_OK)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'For Review')
        self.assertTrue(Notification.objects.filter(user=self.admin, message__contains='awaiting your sign-off').exists())
        self.assertFalse(Notification.objects.filter(user=self.crew).exists())

    def test_unassigned_crew_cannot_touch_booking(self):
        self._as(self.other_crew)
        self.assertEqual(self._status('For Review').status_code, status.HTTP_404_NOT_FOUND)

    def test_admin_sign_off_completes_tracker(self):
        self.booking.status = 'For Review'
        self.booking.save()
        self._as(self.admin)
        self.assertEqual(self._status('Finished').status_code, status.HTTP_200_OK)
        tracker = self.ProjectTracker.objects.get(booking=self.booking)  # created for this legacy booking
        self.assertEqual(tracker.status, 'COMPLETED')
        self.assertEqual(tracker.total_price, 40000)
        self.assertTrue(tracker.history_logs.filter(remarks__contains='Signed off').exists())

    # ── project tracker / billing ───────────────────────────────────────────
    def test_new_booking_gets_tracker_priced_from_design(self):
        self._as(self.customer)
        response = self.client.post('/api/bookings/', {
            'service_type': 'maintenance', 'scheduled_date': '2027-01-15', 'design': self.design.id,
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        tracker = self.ProjectTracker.objects.get(booking_id=response.data['id'])
        self.assertEqual((tracker.total_price, tracker.remaining_balance), (40000, 40000))
        detail = self.client.get(f"/api/bookings/{response.data['id']}/").data
        self.assertEqual(detail['tracker']['amount_paid'], 0.0)

    def test_record_payment(self):
        tracker = self.ProjectTracker.objects.create(booking=self.booking, total_price=40000, remaining_balance=40000)
        url = f'/api/projects/{tracker.id}/record-payment/'
        self._as(self.crew)
        self.assertEqual(self.client.post(url, {'amount': 1000}, format='json').status_code, status.HTTP_403_FORBIDDEN)
        self._as(self.admin)
        self.assertEqual(self.client.post(url, {'amount': 50000}, format='json').status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.post(url, {'amount': -5}, format='json').status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.post(url, {'amount': 12000, 'remarks': 'Downpayment'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tracker.refresh_from_db()
        self.assertEqual(tracker.remaining_balance, 28000)
        self.assertTrue(tracker.history_logs.filter(remarks__contains='Downpayment').exists())

    def test_balance_cannot_exceed_price(self):
        tracker = self.ProjectTracker.objects.create(booking=self.booking, total_price=40000, remaining_balance=40000)
        self._as(self.admin)
        response = self.client.patch(f'/api/projects/{tracker.id}/', {'total_price': 30000}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_completing_tracker_finishes_booking(self):
        tracker = self.ProjectTracker.objects.create(booking=self.booking, total_price=40000, remaining_balance=0)
        self._as(self.admin)
        self.client.patch(f'/api/projects/{tracker.id}/update-status/', {'status': 'COMPLETED'}, format='json')
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'Finished')

    # ── clock-in geofence ───────────────────────────────────────────────────
    def _clock_in(self, lat=None, lng=None):
        body = {'booking_id': self.booking.id}
        if lat is not None:
            body.update(latitude=lat, longitude=lng)
        return self.client.post('/api/attendance/clock_in/', body, format='json')

    def test_clock_in_requires_assignment(self):
        self._as(self.other_crew)
        self.assertEqual(self._clock_in(*self.SITE).status_code, status.HTTP_403_FORBIDDEN)

    def test_clock_in_requires_gps_when_site_is_pinned(self):
        self._as(self.crew)
        self.assertEqual(self._clock_in().status_code, status.HTTP_400_BAD_REQUEST)

    def test_clock_in_rejects_far_away(self):
        self._as(self.crew)
        response = self._clock_in(self.SITE[0] + 0.01, self.SITE[1])  # ~1.1 km north
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('m from the job site', response.data['error'])

    def test_clock_in_on_site(self):
        self._as(self.crew)
        self.assertEqual(self._clock_in(self.SITE[0] + 0.0001, self.SITE[1]).status_code, status.HTTP_201_CREATED)

    def test_clock_in_rejected_after_submission(self):
        self.booking.status = 'For Review'
        self.booking.save()
        self._as(self.crew)
        self.assertEqual(self._clock_in(*self.SITE).status_code, status.HTTP_400_BAD_REQUEST)


import io
import shutil
import tempfile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

TEMP_MEDIA = tempfile.mkdtemp(prefix='portfolio-tests-')


def _photo_with_gps(name='site.jpg'):
    """A JPEG carrying EXIF GPS data, like a phone photo."""
    from PIL import Image
    img = Image.new('RGB', (64, 48), (34, 139, 34))
    exif = Image.Exif()
    exif[0x010F] = 'PhoneMaker'                       # Make
    exif[0x8825] = {1: 'N', 2: (14.0, 35.0, 58.0), 3: 'E', 4: (120.0, 59.0, 3.0)}  # GPS IFD
    buf = io.BytesIO()
    img.save(buf, format='JPEG', exif=exif)
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/jpeg')


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class ProjectHubTests(APITestCase):
    """Phase 0 crew photos + Phase 1 portfolio workflow."""

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEMP_MEDIA, ignore_errors=True)

    def setUp(self):
        from apps.library.models import StaffProfile, ServiceBooking, GardenDesign, ProjectTracker, ServiceReview
        self.admin = User.objects.create_user(username='office', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.admin, role='OFFICE_ADMIN')
        self.crew = User.objects.create_user(username='crew', password='pw', is_staff=True, first_name='Jun')
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        self.other_crew = User.objects.create_user(username='crew2', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.other_crew, role='FIELD_CREW')
        self.customer = User.objects.create_user(username='cust', password='pw', first_name='Maria', last_name='Santos')

        design = GardenDesign.objects.create(
            name='D', total_cost=85000, user=self.customer, dimensions={'width': 10, 'length': 12},
            plant_breakdown=[{'plant_id': '999', 'name': 'Bird of Paradise', 'quantity': 4}],
        )
        self.booking = ServiceBooking.objects.create(
            user=self.customer, service_type='Softscape', scheduled_date='2026-10-01', status='For Review',
            design=design, service_address='12 Mabini St., Makati, Metro Manila',
        )
        self.booking.assigned_crew.add(self.crew)
        ProjectTracker.objects.create(booking=self.booking, total_price=85000, remaining_balance=0)
        ServiceReview.objects.create(booking=self.booking, user=self.customer, rating=5, comment='Beautiful work!')

    def _as(self, user):
        if user is None:
            self.client.credentials()
        else:
            self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _upload(self, phase='AFTER'):
        return self.client.post(f'/api/bookings/{self.booking.id}/progress-media/',
                                {'construction_phase': phase, 'file': _photo_with_gps(), 'description': 'Done'},
                                format='multipart')

    def _sign_off(self):
        self._as(self.admin)
        r = self.client.patch(f'/api/bookings/{self.booking.id}/update_status/', {'status': 'Finished'}, format='json')
        self.assertEqual(r.status_code, 200)
        from apps.library.models import PortfolioProject
        return PortfolioProject.objects.get(booking=self.booking)

    # ── Phase 0: crew progress photos ───────────────────────────────────────
    def test_crew_upload_strips_gps_metadata(self):
        from PIL import Image
        from apps.library.models import ProjectProgressMedia
        self._as(self.crew)
        r = self._upload()
        self.assertEqual(r.status_code, 201, r.data)
        stored = ProjectProgressMedia.objects.get(pk=r.data['id'])
        with Image.open(stored.file_path.path) as img:
            self.assertEqual(len(img.getexif()), 0)

    def test_upload_permissions(self):
        self._as(self.other_crew)
        self.assertEqual(self._upload().status_code, 404)   # not assigned → can't see the booking
        self._as(self.customer)
        self.assertEqual(self._upload().status_code, 403)
        self.assertEqual(self.client.get(f'/api/bookings/{self.booking.id}/progress-media/').status_code, 200)

    def test_upload_rejects_non_images(self):
        self._as(self.crew)
        r = self.client.post(f'/api/bookings/{self.booking.id}/progress-media/',
                             {'construction_phase': 'AFTER', 'file': SimpleUploadedFile('x.jpg', b'not an image')},
                             format='multipart')
        self.assertEqual(r.status_code, 400)

    # ── Phase 1: portfolio ──────────────────────────────────────────────────
    def test_sign_off_drafts_portfolio_and_asks_consent(self):
        from apps.library.models import Notification
        self._as(self.crew)
        self._upload('BEFORE')
        self._upload('AFTER')
        project = self._sign_off()
        self.assertEqual(project.status, 'draft')
        self.assertEqual(project.title, 'Softscape / Planting Services in Makati')
        self.assertEqual(project.city, 'Makati')
        self.assertEqual(project.price_band, '₱50K – ₱100K')
        self.assertEqual(project.lot_area_sqm, 120.0)
        self.assertEqual(project.consent.status, 'pending')
        self.assertEqual(project.media.count(), 2)
        self.assertFalse(project.media.filter(is_public=True).exists())
        self.assertEqual(project.cover.kind, 'after')
        self.assertTrue(Notification.objects.filter(user=self.customer, message__contains='feature your garden').exists())

    def test_publish_requires_consent_and_public_photo(self):
        self._as(self.crew)
        self._upload('AFTER')
        project = self._sign_off()
        publish = f'/api/portfolio/{project.id}/publish/'
        r = self.client.post(publish)
        self.assertEqual(r.status_code, 400)
        self.assertIn('consent', r.data['error'])

        self._as(self.customer)
        r = self.client.post(f'/api/portfolio/mine/{project.id}/consent/',
                             {'decision': 'grant', 'name_display': 'initials', 'include_review': True}, format='json')
        self.assertEqual(r.status_code, 200)

        self._as(self.admin)
        self.assertIn('no photos', self.client.post(publish).data['error'])
        media = project.media.first()
        self.client.patch(f'/api/portfolio/{project.id}/media/{media.id}/', {'is_public': True}, format='json')
        self.assertEqual(self.client.post(publish).status_code, 200)

        # Public views: no login, privacy-safe fields only
        self._as(None)
        listing = self.client.get('/api/portfolio/public/').data
        self.assertEqual([p['slug'] for p in listing['results']], [project.slug])
        detail = self.client.get(f'/api/portfolio/public/{project.slug}/').data
        self.assertEqual(detail['client'], 'Maria S.')
        self.assertEqual(detail['testimonial']['rating'], 5)
        self.assertEqual(detail['price_band'], '₱50K – ₱100K')
        self.assertEqual(detail['plants'][0]['name'], 'Bird of Paradise')
        self.assertTrue(detail['verified']['signed_off'])
        body = str(detail)
        for private in ('Mabini', '85000', 'Santos', "'cust'", 'Metro Manila'):
            self.assertNotIn(private, body)

    def test_unpublished_project_is_private(self):
        self._as(self.crew)
        self._upload()
        project = self._sign_off()
        url = f'/api/portfolio/public/{project.slug}/'
        self._as(None)
        self.assertEqual(self.client.get(url).status_code, 404)
        self._as(self.customer)
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data['is_preview'])
        self.assertEqual(r.data['client'], 'Private client')  # no consent yet → anonymous

    def test_withdrawing_consent_unpublishes(self):
        self._as(self.crew)
        self._upload()
        project = self._sign_off()
        project.media.update(is_public=True)
        self._as(self.customer)
        self.client.post(f'/api/portfolio/mine/{project.id}/consent/', {'decision': 'grant'}, format='json')
        self._as(self.admin)
        self.client.post(f'/api/portfolio/{project.id}/publish/')
        self._as(self.customer)
        self.client.post(f'/api/portfolio/mine/{project.id}/consent/', {'decision': 'withdraw'}, format='json')
        project.refresh_from_db()
        self.assertEqual(project.status, 'draft')
        self.assertEqual(project.consent.status, 'withdrawn')

    def test_house_photos_hidden_unless_customer_allows(self):
        self._as(self.crew)
        self._upload('BEFORE')
        self._upload('AFTER')
        project = self._sign_off()
        project.media.update(is_public=True)
        house = project.media.get(kind='after')
        self.client.patch(f'/api/portfolio/{project.id}/media/{house.id}/', {'shows_house': True}, format='json')

        self._as(self.customer)
        self.client.post(f'/api/portfolio/mine/{project.id}/consent/',
                         {'decision': 'grant', 'show_house': 'false'}, format='json')
        self._as(self.admin)
        self.assertEqual(self.client.post(f'/api/portfolio/{project.id}/publish/').status_code, 200)

        self._as(None)
        detail = self.client.get(f'/api/portfolio/public/{project.slug}/').data
        self.assertNotIn(house.id, [m['id'] for m in detail['media']])
        self.assertNotIn(house.image.name.split('/')[-1], detail['cover_url'] or '')

    def test_city_from_address_never_returns_street(self):
        from apps.library.portfolio import city_from_address
        cases = {
            '12 Mabini St., Makati, Metro Manila': 'Makati',
            'Pinag-isa, Cotta, Lucena, 2nd District, Calabarzon, 4301, Philippines': 'Lucena',
            'Brgy. Ibabang Polo, Pagbilao, Quezon, Calabarzon, 4302, Philippines': 'Pagbilao',
            '19 Molave Rd., Cainta, Rizal': 'Cainta',
            'Lot 5 Blk 3, Greenwoods Subd., Pasig': 'Pasig',
            'Lucena City, Quezon': 'Lucena City',
            'Tarlac': 'Tarlac',
            '12 Mabini St., Metro Manila': '',
            '': '',
        }
        for address, city in cases.items():
            self.assertEqual(city_from_address(address), city, address)

    # ── Phase 2: showcase & track record ────────────────────────────────────
    def test_detail_has_3d_layout_and_shop_links_but_no_reference_photo(self):
        from apps.library.models import InventoryItem
        plant = InventoryItem.objects.create(name='Bird of Paradise', category='plant', unit_price=450, stock_quantity=3)
        design = self.booking.design
        design.placed_items = [{'plant_id': str(plant.id), 'x': 1, 'z': 2, 'rotation': 0}]
        design.reference_image_url = 'http://example.com/media/house-front.jpg'
        design.save()
        self._as(self.crew)
        self._upload('AFTER')
        project = self._sign_off()
        project.media.update(is_public=True)
        self._as(self.customer)
        self.client.post(f'/api/portfolio/mine/{project.id}/consent/', {'decision': 'grant'}, format='json')
        self._as(self.admin)
        self.client.post(f'/api/portfolio/{project.id}/publish/')

        self._as(None)
        detail = self.client.get(f'/api/portfolio/public/{project.slug}/').data
        self.assertEqual(detail['design']['width'], 10)
        self.assertEqual(len(detail['design']['placed_items']), 1)
        self.assertEqual(detail['plants'][0]['product_id'], plant.id)  # matched by name
        self.assertEqual(detail['plants'][0]['product']['price'], 450.0)
        self.assertNotIn('house-front', str(detail))

    def test_track_record_counts_real_jobs_and_hides_private_credentials(self):
        import datetime
        from apps.library.models import BusinessCredential
        self._sign_off()
        today = datetime.date.today()
        BusinessCredential.objects.create(kind='permit', title="Mayor's Permit", issuer='City of Lucena',
                                          reference_no='MP-123', expires_on=today + datetime.timedelta(days=200))
        BusinessCredential.objects.create(kind='permit', title='Old Permit', expires_on=today - datetime.timedelta(days=1))
        BusinessCredential.objects.create(kind='award', title='Internal note', is_public=False)

        self._as(None)
        data = self.client.get('/api/portfolio/track-record/').data
        stats = data['stats']
        self.assertEqual(stats['projects_completed'], 1)
        self.assertEqual(stats['area_sqm'], 120)
        self.assertEqual(stats['plants_installed'], 4)
        self.assertEqual(stats['average_rating'], 5.0)
        self.assertEqual(stats['repeat_customers'], 0)
        self.assertEqual([c['title'] for c in data['credentials']], ["Mayor's Permit"])
        self.assertEqual(data['credentials'][0]['reference_no'], '')  # hidden unless the admin opts in

    def test_credentials_admin_only_and_validated(self):
        url = '/api/business-credentials/'
        for user in (None, self.crew, self.customer):
            self._as(user)
            self.assertIn(self.client.get(url).status_code, (401, 403))
        self._as(self.admin)
        bad = self.client.post(url, {'title': 'X', 'document': SimpleUploadedFile('x.exe', b'MZ')}, format='multipart')
        self.assertEqual(bad.status_code, 400)
        r = self.client.post(url, {'title': 'BIR COR', 'kind': 'registration', 'issued_on': '2026-01-10',
                                   'expires_on': '2025-01-01'}, format='json')
        self.assertIn('expires_on', r.data)
        r = self.client.post(url, {'title': 'BIR COR', 'kind': 'registration',
                                   'document': SimpleUploadedFile('cor.pdf', b'%PDF-1.4')}, format='multipart')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(r.data['document_url'].endswith('.pdf'))

    def test_portfolio_admin_api_is_office_only(self):
        for user in (self.crew, self.customer):
            self._as(user)
            self.assertEqual(self.client.get('/api/portfolio/').status_code, 403)
        self._as(self.admin)
        self.assertEqual(self.client.get('/api/portfolio/').status_code, 200)

    def test_customer_cannot_answer_for_someone_else(self):
        project = self._sign_off()
        self._as(self.other_crew)
        r = self.client.post(f'/api/portfolio/mine/{project.id}/consent/', {'decision': 'grant'}, format='json')
        self.assertEqual(r.status_code, 404)


class IncomeAnalyticsTests(APITestCase):
    def setUp(self):
        from django.utils import timezone
        from apps.library.models import (
            InventoryItem, Order, OrderItem, ServiceBooking, GardenDesign, ProjectTracker,
        )
        self.admin = User.objects.create_superuser(username='admin', password='password123')
        self.customer = User.objects.create_user(username='cust', password='password123')
        self.url = reverse('income-analytics')
        today = timezone.localdate()

        plant = InventoryItem.objects.create(name="Hibiscus", category="plant", unit_price=300)
        order_kwargs = dict(user=self.customer, customer_name='C', customer_email='c@example.com',
                            customer_address='Addr')
        delivered = Order.objects.create(total_price=750, status='Delivered', **order_kwargs)
        OrderItem.objects.create(order=delivered, item=plant, quantity=2, price_at_booking=300)
        Order.objects.create(total_price=1000, status='Pending', **order_kwargs)      # pipeline
        Order.objects.create(total_price=5000, status='Cancelled', **order_kwargs)    # excluded

        design = GardenDesign.objects.create(name='D', total_cost=8000)
        ServiceBooking.objects.create(user=self.customer, service_type='hardscaping',
                                      scheduled_date=today, status='Finished', design=design)
        priced = ServiceBooking.objects.create(user=self.customer, service_type='maintenance',
                                               scheduled_date=today, status='Completed', design=design)
        ProjectTracker.objects.create(booking=priced, total_price=12000)  # tracker price wins over design
        ServiceBooking.objects.create(user=self.customer, service_type='consultation',
                                      scheduled_date=today + timedelta(days=7), status='Pending')  # unpriced pipeline

    def _get(self, **params):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.admin).access_token}')
        return self.client.get(self.url, params)

    def test_requires_superuser(self):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.customer).access_token}')
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN)

    def test_invalid_range(self):
        self.assertEqual(self._get(range='5y').status_code, status.HTTP_400_BAD_REQUEST)

    def test_revenue_definitions(self):
        data = self._get(range='30d').data
        self.assertEqual(data['ecommerce']['earned'], 750.0)
        self.assertEqual(data['ecommerce']['pipeline'], 1000.0)
        self.assertEqual(data['ecommerce']['cancelled_count'], 1)
        self.assertEqual(data['landscape']['earned'], 20000.0)  # 8000 design + 12000 tracker
        self.assertEqual(data['landscape']['pipeline_count'], 1)  # future-dated booking still counts
        self.assertEqual(data['landscape']['unpriced_count'], 1)
        self.assertEqual(sum(t['landscape'] for t in data['trend']), 20000.0)
        self.assertEqual(sum(t['ecommerce'] for t in data['trend']), 750.0)
        # Category breakdown is item sales, excluding the shipping fee
        self.assertEqual(data['ecommerce_by_category'], [{'label': 'Plants', 'value': 600.0}])
        self.assertEqual(data['landscape_by_service'][0], {'label': 'Maintenance', 'value': 12000.0, 'count': 1})


class StudioFunnelTests(APITestCase):
    """Anonymous guest Studio funnel events + the office summary."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.url = '/api/funnel-events/'

    def _event(self, name, session='a1b2c3d4-0000-0000-0000-000000000001'):
        return self.client.post(self.url, {'name': name, 'session_id': session}, format='json')

    def test_events_are_anonymous_deduped_and_validated(self):
        from apps.library.models import FunnelEvent
        self.assertEqual(self._event('studio_opened').status_code, 204)
        self.assertEqual(self._event('studio_opened').status_code, 204)  # same tab again: not double-counted
        self.assertEqual(FunnelEvent.objects.count(), 1)
        self.assertEqual(self._event('hacked').status_code, 400)
        self.assertEqual(self._event('studio_opened', session='x').status_code, 400)

    def test_summary_shows_conversion_for_office_only(self):
        for i in range(4):
            self._event('studio_opened', session=f'session-000{i}')
        self._event('ai_teaser_clicked', session='session-0000')
        self._event('account_created', session='session-0000')
        self.assertIn(self.client.get('/api/funnel-events/summary/').status_code, (401, 403))

        admin = User.objects.create_user(username='office', password='pw', is_staff=True)
        from apps.library.models import StaffProfile
        StaffProfile.objects.create(user=admin, role='OFFICE_ADMIN')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(admin).access_token}')
        data = self.client.get('/api/funnel-events/summary/').data
        steps = {s['name']: s for s in data['steps']}
        self.assertEqual(steps['studio_opened']['count'], 4)
        self.assertEqual(steps['ai_teaser_clicked']['rate'], 25.0)
        self.assertEqual(data['conversion_rate'], 25.0)


class CustomerReviewTests(APITestCase):
    """Landing page review carousel + office moderation."""

    def setUp(self):
        from apps.library.models import (
            StaffProfile, ServiceBooking, ServiceReview, InventoryItem, Order, OrderItem, ProductReview,
        )
        self.admin = User.objects.create_user(username='office', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.admin, role='OFFICE_ADMIN')
        self.crew = User.objects.create_user(username='crew', password='pw', is_staff=True, first_name='Jun', last_name='Reyes')
        StaffProfile.objects.create(user=self.crew, role='FIELD_CREW')
        self.maria = User.objects.create_user(username='maria@example.com', password='pw', first_name='Maria', last_name='Santos')
        self.nameless = User.objects.create_user(username='secret_handle', password='pw')

        def booking(user, address='12 Mabini St., Makati, Metro Manila'):
            b = ServiceBooking.objects.create(user=user, service_type='Softscape', scheduled_date='2026-10-01',
                                              status='Finished', service_address=address)
            b.assigned_crew.add(self.crew)
            return b

        self.good = ServiceReview.objects.create(booking=booking(self.maria), user=self.maria, rating=5,
                                                 comment='The crew turned our bare yard into a lovely tropical garden.')
        self.dupe = ServiceReview.objects.create(booking=booking(self.maria), user=self.maria, rating=5,
                                                 comment='The crew turned our bare yard into a lovely tropical garden!')
        self.low = ServiceReview.objects.create(booking=booking(self.nameless), user=self.nameless, rating=2,
                                                comment='Arrived two days late and left some soil on the driveway.')
        self.private = ServiceReview.objects.create(booking=booking(self.nameless), user=self.nameless, rating=5,
                                                    comment='Wonderful work, but please keep this to yourselves.', is_public=False)

        plant = InventoryItem.objects.create(name='Monstera', category='plant', unit_price=850, stock_quantity=5)
        order = Order.objects.create(user=self.maria, customer_name='M', customer_email='m@example.com',
                                     customer_address='Addr', total_price=850, status='Delivered')
        self.product_good = ProductReview.objects.create(
            order_item=OrderItem.objects.create(order=order, item=plant, quantity=1, price_at_booking=850),
            product=plant, user=self.maria, rating=4, comment='Arrived healthy and well packed, already growing new leaves.')
        self.junk = ProductReview.objects.create(
            order_item=OrderItem.objects.create(order=order, item=plant, quantity=1, price_at_booking=850),
            product=plant, user=self.maria, rating=5, comment='drdfdfdfdg')

    def _as(self, user):
        if user is None:
            self.client.credentials()
        else:
            self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _highlights(self, **params):
        self._as(None)
        r = self.client.get('/api/review-highlights/', params)
        self.assertEqual(r.status_code, 200)
        return r.data

    def test_highlights_mix_sources_and_filter_noise(self):
        data = self._highlights()
        ids = [(c['source'], c['id']) for c in data['results']]
        # The near-duplicate, junk text, the 2-star and the private review are all left out
        self.assertEqual(ids, [('service', self.dupe.id), ('product', self.product_good.id)])
        service = data['results'][0]
        self.assertEqual(service['name'], 'Maria S.')
        self.assertEqual(service['context']['city'], 'Makati')
        self.assertEqual(data['results'][1]['context']['link'], f'/shop?product={self.product_good.product_id}')
        body = str(data)
        for private in ('maria@example.com', 'secret_handle', 'Santos', 'Mabini', 'keep this to yourselves'):
            self.assertNotIn(private, body)

    def test_summary_is_honest(self):
        from apps.library.models import ServiceReview
        ServiceReview.objects.filter(pk=self.low.pk).update(is_hidden=True)
        summary = self._highlights()['summary']['service']
        # 3 public reviews (5, 5, 2): the hidden 2-star still counts, the private one doesn't
        self.assertEqual(summary['count'], 3)
        self.assertEqual(summary['average'], 4.0)
        self.assertEqual(summary['distribution']['2'], 1)

    def test_source_filter(self):
        data = self._highlights(source='product')
        self.assertEqual([c['source'] for c in data['results']], ['product'])
        self._as(None)
        self.assertEqual(self.client.get('/api/review-highlights/', {'source': 'x'}).status_code, 400)

    def test_moderation_is_office_only(self):
        for user in (None, self.crew, self.maria):
            self._as(user)
            self.assertIn(self.client.get('/api/review-moderation/').status_code, (401, 403))
            self.assertIn(self.client.patch(f'/api/review-moderation/service/{self.good.id}/', {'is_hidden': True},
                                            format='json').status_code, (401, 403))
        self._as(self.admin)
        r = self.client.get('/api/review-moderation/', {'status': 'needs_reply'})
        self.assertEqual([(x['source'], x['id']) for x in r.data['results']], [('service', self.low.id)])

    def test_featuring_hiding_and_replies(self):
        from apps.library.models import Notification

        def url(src, pk):
            return f'/api/review-moderation/{src}/{pk}/'

        self._as(self.admin)
        # A private review can never be featured
        r = self.client.patch(url('service', self.private.id), {'is_featured': True}, format='json')
        self.assertEqual(r.status_code, 400)
        # Featuring overrides the automatic 4-star+ / quality picks
        self.assertEqual(self.client.patch(url('service', self.low.id), {'is_featured': True}, format='json').status_code, 200)
        r = self.client.patch(url('service', self.low.id), {'reply': 'Sorry about the delay. We have fixed our scheduling.'}, format='json')
        self.assertIsNotNone(r.data['replied_at'])
        self.assertTrue(Notification.objects.filter(user=self.nameless, message__contains='replied to your review').exists())

        first = self._highlights()['results'][0]
        self.assertEqual(first['id'], self.low.id)
        self.assertEqual(first['name'], 'Verified customer')  # no name on the account, never the username
        self.assertIn('scheduling', first['reply'])

        # Hiding removes it from every public list and unfeatures it
        self._as(self.admin)
        r = self.client.patch(url('service', self.low.id), {'is_hidden': True}, format='json')
        self.assertFalse(r.data['is_featured'])
        self.assertNotIn(self.low.id, [c['id'] for c in self._highlights()['results']])
        self.assertNotIn(self.low.id, [x['id'] for x in self.client.get('/api/reviews/public/').data])

    def test_customer_can_submit_a_private_review(self):
        from apps.library.models import ServiceBooking
        booking = ServiceBooking.objects.create(user=self.maria, service_type='maintenance',
                                                scheduled_date='2026-10-02', status='Finished')
        self._as(self.maria)
        r = self.client.post('/api/reviews/', {'booking': booking.id, 'rating': 5, 'is_public': False,
                                               'comment': 'Great, but I would rather this stayed private.'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertFalse(r.data['is_public'])
        self.assertNotIn(r.data['id'], [c['id'] for c in self._highlights(source='service')['results']])

    def test_public_service_reviews_credit_crew_by_first_name(self):
        self._as(None)
        data = self.client.get('/api/reviews/public/').data
        self.assertTrue(data)
        self.assertEqual(data[0]['crew_names'], ['Jun'])
        self.assertNotIn('Reyes', str(data))


class CheckoutSecurityTests(APITestCase):
    """The server prices every order; nothing the browser sends can change what is charged."""

    def setUp(self):
        from apps.library.models import InventoryItem
        self.user = User.objects.create_user(username='buyer', password='pw')
        self.other = User.objects.create_user(username='other', password='pw')
        self.plant = InventoryItem.objects.create(name='Fern', category='plant', unit_price=500, stock_quantity=10)
        self._as(self.user)

    def _as(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')

    def _order(self, items, **extra):
        return self.client.post('/api/checkout/', {
            'customer_name': 'Buyer', 'customer_email': 'b@example.com', 'customer_address': 'Lucena',
            'payment_method': 'cod', 'items': items, **extra}, format='json')

    def test_client_total_is_ignored(self):
        from apps.library.models import Order
        r = self._order([{'id': self.plant.id, 'quantity': 2}], total_price=1)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['total_price'], 1150.0)  # 2 × ₱500 + ₱150 delivery
        self.assertEqual(Order.objects.get(pk=r.data['order_id']).total_price, 1150)

    def test_bad_quantities_and_empty_carts_are_rejected(self):
        for items in ([], [{'id': self.plant.id, 'quantity': -10}], [{'id': self.plant.id, 'quantity': 0}],
                      [{'id': self.plant.id, 'quantity': 'lots'}], [{'quantity': 1}]):
            self.assertEqual(self._order(items).status_code, 400, items)
        self.plant.refresh_from_db()
        self.assertEqual(self.plant.stock_quantity, 10)  # a negative quantity can't add stock

    def test_duplicate_lines_are_merged_against_stock(self):
        r = self._order([{'id': self.plant.id, 'quantity': 6}, {'id': self.plant.id, 'quantity': 6}])
        self.assertEqual(r.status_code, 400)
        self.assertIn('Insufficient stock', r.data['error'])

    def test_delivery_fee_comes_from_the_rate_card(self):
        from apps.library.models import PricingSettings
        ps = PricingSettings.current()
        ps.shipping_fee = 250
        ps.save()
        self.assertEqual(self._order([{'id': self.plant.id, 'quantity': 1}]).data['total_price'], 750.0)

    def test_only_the_owner_can_pay_and_never_twice(self):
        from apps.library.models import Order
        order_id = self._order([{'id': self.plant.id, 'quantity': 1}]).data['order_id']
        self._as(self.other)
        self.assertEqual(self.client.post('/api/create-checkout-session/', {'order_id': order_id}, format='json').status_code, 404)
        Order.objects.filter(pk=order_id).update(payment_status='Paid')
        self._as(self.user)
        r = self.client.post('/api/create-checkout-session/', {'order_id': order_id}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('already Paid', r.data['error'])


class PricingTests(APITestCase):
    """Rate card: transparent estimate lines, admin-only edits, sample-rate flag."""

    def setUp(self):
        from apps.library.models import InventoryItem, StaffProfile
        self.palm = InventoryItem.objects.create(name='Palm', category='plant', unit_price=1000, stock_quantity=5, real_world_size=5)
        self.fern = InventoryItem.objects.create(name='Fern', category='plant', unit_price=100, stock_quantity=50, real_world_size=0.5)
        self.bench = InventoryItem.objects.create(name='Bench', category='furniture', unit_price=3000, stock_quantity=3)
        self.admin = User.objects.create_user(username='office', password='pw', is_staff=True)
        StaffProfile.objects.create(user=self.admin, role='OFFICE_ADMIN')

    def test_estimate_lines_with_sample_rates(self):
        r = self.client.post('/api/pricing/estimate/', {'area_sqm': 50, 'items': [
            {'product_id': self.palm.id, 'quantity': 2}, {'product_id': self.fern.id, 'quantity': 10},
            {'product_id': self.bench.id, 'quantity': 1}]}, format='json')
        self.assertEqual(r.status_code, 200)
        lines = {l['key']: l['amount'] for l in r.data['lines']}
        self.assertEqual(lines['plants'], 2 * 1000 + 10 * 100 + 3000)
        self.assertEqual(lines['labour'], 2 * 350 + 10 * 40 + 100)   # tall, low, other
        self.assertEqual(lines['site_prep'], 50 * 150)
        self.assertEqual(lines['materials'], 50 * 120)
        self.assertEqual(lines['mobilization'], 1500)
        subtotal = 6000 + 1200 + 7500 + 6000 + 1500
        self.assertEqual(lines['design_fee'], round(subtotal * 0.10))
        self.assertNotIn('vat', lines)                                # not VAT-registered by default
        self.assertEqual(r.data['total'], sum(lines.values()))
        self.assertLess(r.data['low'], r.data['total'])
        self.assertGreater(r.data['high'], r.data['total'])
        self.assertTrue(r.data['is_sample_rates'])

    def test_minimum_charge_and_vat(self):
        from apps.library.models import PricingSettings
        ps = PricingSettings.current()
        ps.vat_registered = True
        ps.save()
        r = self.client.post('/api/pricing/estimate/', {'area_sqm': 1, 'items': [{'product_id': self.fern.id, 'quantity': 1}]}, format='json')
        lines = {l['key']: l['amount'] for l in r.data['lines']}
        self.assertIn('minimum', lines)
        self.assertEqual(lines['vat'], 1800)                          # 12% of the ₱15,000 minimum
        self.assertEqual(r.data['total'], 16800)

    def test_only_office_admins_edit_rates_and_sample_flag_clears(self):
        self.assertEqual(self.client.get('/api/pricing/').status_code, 200)
        self.assertIn(self.client.patch('/api/pricing/', {'planting_tall': 1}, format='json').status_code, (401, 403))
        customer = User.objects.create_user(username='cust', password='pw')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(customer).access_token}')
        self.assertEqual(self.client.patch('/api/pricing/', {'planting_tall': 1}, format='json').status_code, 403)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.admin).access_token}')
        bad = self.client.patch('/api/pricing/', {'design_fee_percent': 250, 'site_prep_per_sqm': -5}, format='json')
        self.assertEqual(set(bad.data), {'design_fee_percent', 'site_prep_per_sqm'})
        r = self.client.patch('/api/pricing/', {'planting_tall': '500', 'vat_registered': True}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['planting_tall'], 500.0)
        self.assertTrue(r.data['vat_registered'])
        self.assertFalse(r.data['is_sample'])

    def test_estimate_validates_input(self):
        for body in ({'items': 'x'}, {'items': [{'product_id': 'a'}]}, {'items': [{'product_id': self.fern.id, 'quantity': -1}]},
                     {'items': [], 'area_sqm': -3}):
            self.assertEqual(self.client.post('/api/pricing/estimate/', body, format='json').status_code, 400, body)
