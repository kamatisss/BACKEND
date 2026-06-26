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

    def test_generate_layouts_without_auth(self):
        # Clear JWT authentication header
        self.client.credentials()
        response = self.client.post(self.url, {'budget': 5000, 'lot_width': 10, 'lot_length': 10})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_generate_layouts_missing_budget(self):
        response = self.client.post(self.url, {'lot_width': 10.0, 'lot_length': 10.0, 'preferred_plant_ids': ['tree_oak']})
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
        
        # Setup mock response
        mock_response = MagicMock()
        
        # We need response.parsed to return a LayoutResponse Pydantic object
        from apps.library.views_api import LayoutResponse, GardenDesign, PlantArrangement
        
        expected_pydantic_response = LayoutResponse(
            designs=[
                GardenDesign(
                    design_name="Symmetrical Elegance",
                    total_cost=2000,
                    plants=[
                        PlantArrangement(plant_id="tree_oak", x=5.0, z=5.0, rotation=90.0),
                        PlantArrangement(plant_id="shrub_fern", x=2.0, z=2.0, rotation=0.0)
                    ]
                ),
                GardenDesign(
                    design_name="Minimalist Green",
                    total_cost=1500,
                    plants=[
                        PlantArrangement(plant_id="tree_palm", x=1.0, z=1.0, rotation=45.0)
                    ]
                ),
                GardenDesign(
                    design_name="Lush Organic",
                    total_cost=3000,
                    plants=[
                        PlantArrangement(plant_id="flower_rose", x=3.5, z=7.2, rotation=180.0)
                    ]
                )
            ]
        )
        mock_response.parsed = expected_pydantic_response
        mock_client.models.generate_content.return_value = mock_response

        # Execute POST request
        payload = {
            'budget': 5000,
            'lot_width': 10.0,
            'lot_length': 12.0,
            'preferred_plant_ids': ['tree_oak', 'shrub_fern']
        }
        response = self.client.post(self.url, payload, format='json')

        # Assertions
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("designs", response.data)
        self.assertEqual(len(response.data["designs"]), 3)
        self.assertEqual(response.data["designs"][0]["design_name"], "Symmetrical Elegance")
        self.assertEqual(response.data["designs"][0]["plants"][0]["plant_id"], "tree_oak")

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
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertIn("error", response.data)
        self.assertIn("Failed to generate layout", response.data["error"])
