from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth.models import User
from .models import InventoryItem, GardenDesign, GardenImage, BlackoutDate, ServiceBooking, Order, OrderItem

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token['is_staff'] = user.is_staff
        token['is_superuser'] = user.is_superuser
        return token

class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})

    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'password')

    def create(self, validated_data):
        user = User.objects.create(
            username=validated_data['username'],
            email=validated_data.get('email', ''),
            is_staff=False,
            is_superuser=False
        )
        user.set_password(validated_data['password'])
        user.save()
        return user


class InventoryItemSerializer(serializers.ModelSerializer):
    model_file = serializers.SerializerMethodField()
    thumbnail = serializers.SerializerMethodField()

    class Meta:
        model = InventoryItem
        fields = ['id', 'name', 'category', 'description', 'stock_quantity',
                  'unit_price', 'image_url', 'model_file', 'thumbnail']

    def get_model_file(self, obj):
        request = self.context.get('request')
        if obj.model_file and hasattr(obj.model_file, 'url'):
            if request:
                return request.build_absolute_uri(obj.model_file.url)
            # Fallback if no request context
            return f"/media/{obj.model_file.name}"
        return None

    def get_thumbnail(self, obj):
        request = self.context.get('request')
        if obj.thumbnail and hasattr(obj.thumbnail, 'url'):
            if request:
                return request.build_absolute_uri(obj.thumbnail.url)
            return f"/media/{obj.thumbnail.name}"
        return None


class GardenImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = GardenImage
        fields = '__all__'


class GardenDesignSerializer(serializers.ModelSerializer):
    class Meta:
        model = GardenDesign
        fields = ['id', 'name', 'original_image_url', 'depth_data',
                  'placed_items', 'dimensions', 'total_cost',
                  'terrain_height', 'time_of_day', 'status',
                  'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


class GardenDesignListSerializer(serializers.ModelSerializer):
    """Lighter serializer for list views (no placed_items blob)."""
    class Meta:
        model = GardenDesign
        fields = ['id', 'name', 'total_cost', 'created_at', 'updated_at']

class BlackoutDateSerializer(serializers.ModelSerializer):
    class Meta:
        model = BlackoutDate
        fields = '__all__'

class ServiceBookingSerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceBooking
        fields = ['id', 'user', 'service_type', 'scheduled_date', 'status', 'notes', 'created_at', 'updated_at']
        read_only_fields = ['id', 'user', 'created_at', 'updated_at']

    def validate_scheduled_date(self, value):
        if BlackoutDate.objects.filter(date=value).exists():
            raise serializers.ValidationError("The selected date is unavailable.")
        return value

class OrderItemSerializer(serializers.ModelSerializer):
    item_name = serializers.CharField(source='item.name', read_only=True)

    class Meta:
        model = OrderItem
        fields = ['id', 'item', 'item_name', 'quantity', 'price_at_booking']

class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = ['id', 'customer_name', 'customer_email', 'customer_address', 'booking_date', 'total_price', 'created_at', 'items']