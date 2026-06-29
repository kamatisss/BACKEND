from django.urls import path, include
from rest_framework import routers

from .views import generate_depth
from .views_api import (
    InventoryItemViewSet, GardenDesignViewSet, RegisterView, BlackoutDateViewSet,
    ServiceBookingViewSet, checkout, OrderViewSet, create_checkout_session,
    stripe_webhook, reset_password, UserViewSet, AttendanceViewSet,
    NotificationViewSet, GenerateLayoutsView, upload_design_image, ServiceReviewViewSet,
    ProjectTrackerViewSet,
)

router = routers.DefaultRouter()
router.register(r'inventory', InventoryItemViewSet)
router.register(r'designs', GardenDesignViewSet)
router.register(r'blackout-dates', BlackoutDateViewSet)
router.register(r'bookings', ServiceBookingViewSet)
router.register(r'orders', OrderViewSet, basename='order')
router.register(r'users', UserViewSet, basename='user')
router.register(r'attendance', AttendanceViewSet, basename='attendance')
router.register(r'notifications', NotificationViewSet, basename='notification')
router.register(r'reviews', ServiceReviewViewSet, basename='review')
router.register(r'projects', ProjectTrackerViewSet, basename='project')

urlpatterns = [
    path('', include(router.urls)),
    path('generate-depth/', generate_depth, name='generate-depth'),
    path('generate-layouts/', GenerateLayoutsView.as_view(), name='generate-layouts'),
    path('register/', RegisterView.as_view(), name='auth_register'),
    path('checkout/', checkout, name='checkout'),
    path('create-checkout-session/', create_checkout_session, name='create_checkout_session'),
    path('stripe-webhook/', stripe_webhook, name='stripe_webhook'),
    path('reset-password/', reset_password, name='reset_password'),
    path('upload-design-image/', upload_design_image, name='upload_design_image'),
]
