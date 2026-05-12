from django.contrib import admin
from .models import InventoryItem, GardenDesign, GardenImage


@admin.register(InventoryItem)
class InventoryItemAdmin(admin.ModelAdmin):
    list_display = ['name', 'category', 'stock_quantity', 'unit_price']
    list_filter = ['category']
    search_fields = ['name']


@admin.register(GardenDesign)
class GardenDesignAdmin(admin.ModelAdmin):
    list_display = ['name', 'user', 'total_cost', 'created_at', 'updated_at']
    list_filter = ['status', 'created_at']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(GardenImage)
class GardenImageAdmin(admin.ModelAdmin):
    list_display = ['pk', 'uploaded_at']
