from django.core.management.base import BaseCommand
from apps.library.models import InventoryItem


PRODUCTS = [
    # Plants
    {"name": "Coconut Palm", "category": "plant", "model_path": "coconut_palm",
     "unit_price": 850, "stock_quantity": 50, "description": "Tropical palm tree, grows 5-8m. Iconic Filipino landscape staple."},
    {"name": "Bougainvillea", "category": "plant", "model_path": "bougainvillea",
     "unit_price": 350, "stock_quantity": 40, "description": "Colorful flowering vine/shrub. Thrives in full sun, drought-tolerant."},
    {"name": "Bamboo Cluster", "category": "plant", "model_path": "bamboo",
     "unit_price": 600, "stock_quantity": 100, "description": "Fast-growing tropical grass. Great for privacy screens."},
    {"name": "Frangipani", "category": "plant", "model_path": "frangipani",
     "unit_price": 450, "stock_quantity": 25, "description": "Fragrant flowering tree (Plumeria). Low maintenance."},
    {"name": "Bird of Paradise", "category": "plant", "model_path": "bird_of_paradise",
     "unit_price": 500, "stock_quantity": 30, "description": "Exotic tropical flower with striking orange blooms."},
    {"name": "Banana Plant", "category": "plant", "model_path": "banana",
     "unit_price": 300, "stock_quantity": 60, "description": "Large tropical leaf plant. Adds lush volume."},
    {"name": "Traveler's Palm", "category": "plant", "model_path": "travelers_palm",
     "unit_price": 1200, "stock_quantity": 15, "description": "Fan-shaped palm. Dramatic tropical statement piece."},
    {"name": "Hibiscus", "category": "plant", "model_path": "hibiscus",
     "unit_price": 250, "stock_quantity": 5, "description": "National flower candidate. Bright red/pink blossoms."},
    {"name": "Golden Duranta", "category": "plant", "model_path": "duranta",
     "unit_price": 180, "stock_quantity": 80, "description": "Yellow-green hedge shrub. Excellent for borders."},
    {"name": "Santan", "category": "plant", "model_path": "santan",
     "unit_price": 150, "stock_quantity": 120, "description": "Ixora coccinea. Compact flowering shrub, year-round blooms."},

    # Hardscape
    {"name": "River Stones", "category": "hardscape", "model_path": "river_stones",
     "unit_price": 150, "stock_quantity": 200, "description": "Natural pebble cluster for garden borders and accents."},
    {"name": "Stepping Path", "category": "hardscape", "model_path": "stepping_path",
     "unit_price": 2500, "stock_quantity": 20, "description": "Set of 5 natural stone stepping tiles."},
    {"name": "Garden Bench", "category": "furniture", "model_path": "garden_bench",
     "unit_price": 3500, "stock_quantity": 8, "description": "Wooden garden bench, seats 2-3 people."},
    {"name": "Trellis Arch", "category": "hardscape", "model_path": "trellis_arch",
     "unit_price": 1800, "stock_quantity": 5, "description": "Metal garden arch for climbing plants."},
    {"name": "Solar Light", "category": "furniture", "model_path": "solar_light",
     "unit_price": 450, "stock_quantity": 40, "description": "Solar-powered garden path light."},
]


class Command(BaseCommand):
    help = 'Seed the database with tropical plant and hardscape products'

    def handle(self, *args, **options):
        created_count = 0
        for data in PRODUCTS:
            _, created = InventoryItem.objects.get_or_create(
                name=data['name'],
                defaults=data
            )
            if created:
                created_count += 1
                self.stdout.write(f"  [Created] {data['name']} ({data['unit_price']})")
            else:
                self.stdout.write(f"  [Exists]  {data['name']}")

        self.stdout.write(self.style.SUCCESS(
            f"\nDone! Created {created_count} new inventory items "
            f"({InventoryItem.objects.count()} total)"
        ))
