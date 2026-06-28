import os
import json
from unittest.mock import patch, MagicMock
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth.models import User
from rest_framework_simplejwt.tokens import RefreshToken

class GenerateLayoutsViewTests(APITestCase):
    def setUp(self):
        # Create a test user
        self.user = User.objects.create_user(username='testuser', password='password123')
        # Generate SimpleJWT token
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {refresh.access_token}')
        self.url = reverse('generate-layouts')
        
        # Populate test plants
        from apps.library.models import InventoryItem
        InventoryItem.objects.create(id=1, name="Oak Tree", category="plant", unit_price=1000)
        InventoryItem.objects.create(id=2, name="Fern", category="plant", unit_price=500)
        InventoryItem.objects.create(id=3, name="Palm Tree", category="plant", unit_price=800)
        InventoryItem.objects.create(id=4, name="Rose", category="plant", unit_price=200)

    def test_generate_layouts_without_auth(self):
        # Clear JWT authentication header
        self.client.credentials()
        response = self.client.post(self.url, {'budget': 5000, 'lot_width': 10, 'lot_length': 10})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_generate_layouts_missing_budget(self):
        response = self.client.post(self.url, {'lot_width': 10.0, 'lot_length': 10.0, 'preferred_plant_ids': ['1']})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("error", response.data)

    def test_generate_layouts_missing_lot_dimensions(self):
        # Missing lot_width
        response = self.client.post(self.url, {'budget': 5000, 'lot_length': 10.0})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        
        # Missing lot_length
        response = self.client.post(self.url, {'budget': 5000, 'lot_width': 10.0})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_generate_layouts_invalid_budget(self):
        # Test negative budget
        response = self.client.post(self.url, {'budget': -100, 'lot_width': 10, 'lot_length': 10})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Test non-integer budget
        response = self.client.post(self.url, {'budget': 'hundred', 'lot_width': 10, 'lot_length': 10})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_generate_layouts_invalid_dimensions(self):
        # Test negative width/length
        response = self.client.post(self.url, {'budget': 5000, 'lot_width': -10, 'lot_length': 10})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Test non-numeric width/length
        response = self.client.post(self.url, {'budget': 5000, 'lot_width': 'ten', 'lot_length': 10})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch.dict(os.environ, {"GEMINI_API_KEY": "test_api_key"})
    @patch("google.genai.Client")
    def test_generate_layouts_success(self, mock_client_class):
        # Setup mock client
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        
        # We need response.parsed to return a GardenDesignSchema Pydantic object for each parallel call
        from apps.library.views_api import GardenDesignSchema, PlantArrangement, PlantCostBreakdown
        
        mock_response_1 = MagicMock()
        mock_response_1.parsed = GardenDesignSchema(
            design_name="Symmetrical Elegance",
            reasoning="A symmetrical layout with balanced green spaces.",
            total_cost=2000,
            plants=[
                PlantArrangement(plant_id="1", gx=5, gz=5, rotation=90.0),
                PlantArrangement(plant_id="2", gx=2, gz=2, rotation=0.0)
            ],
            plant_breakdown=[
                PlantCostBreakdown(plant_id="1", name="Oak Tree", quantity=1, unit_price=1000, subtotal=1000),
                PlantCostBreakdown(plant_id="2", name="Fern", quantity=1, unit_price=500, subtotal=500)
            ]
        )

        mock_response_2 = MagicMock()
        mock_response_2.parsed = GardenDesignSchema(
            design_name="Minimalist Green",
            reasoning="A clean design prioritizing open spaces.",
            total_cost=1500,
            plants=[
                PlantArrangement(plant_id="3", gx=1, gz=1, rotation=45.0)
            ],
            plant_breakdown=[
                PlantCostBreakdown(plant_id="3", name="Palm Tree", quantity=1, unit_price=800, subtotal=800)
            ]
        )

        mock_response_3 = MagicMock()
        mock_response_3.parsed = GardenDesignSchema(
            design_name="Lush Organic",
            reasoning="A dense composition rich in organic patterns.",
            total_cost=3000,
            plants=[
                PlantArrangement(plant_id="4", gx=3, gz=7, rotation=180.0)
            ],
            plant_breakdown=[
                PlantCostBreakdown(plant_id="4", name="Rose", quantity=1, unit_price=200, subtotal=200)
            ]
        )

        mock_client.models.generate_content.side_effect = [mock_response_1, mock_response_2, mock_response_3]

        # Execute POST request
        payload = {
            'budget': 5000,
            'lot_width': 10.0,
            'lot_length': 12.0,
            'preferred_plant_ids': ['1', '2']
        }
        response = self.client.post(self.url, payload, format='json')

        # Assertions
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("designs", response.data)
        self.assertEqual(len(response.data["designs"]), 3)
        
        symmetrical_design = next((d for d in response.data["designs"] if d["design_name"] == "Symmetrical Elegance"), None)
        self.assertIsNotNone(symmetrical_design)
        self.assertEqual(symmetrical_design["plants"][0]["plant_id"], "1")

    @patch.dict(os.environ, {"GEMINI_API_KEY": "test_api_key"})
    @patch("google.genai.Client")
    def test_generate_layouts_api_failure(self, mock_client_class):
        # Setup mock client to throw exception
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.models.generate_content.side_effect = Exception("API connection timed out")

        # Execute POST request
        payload = {'budget': 5000, 'lot_width': 10.0, 'lot_length': 10.0}
        response = self.client.post(self.url, payload, format='json')

        # Assertions
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertIn("error", response.data)
        self.assertIn("The AI service returned an error", response.data["error"])
