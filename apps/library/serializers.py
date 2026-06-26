from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth.models import User
from .models import InventoryItem, GardenDesign, GardenImage, BlackoutDate, ServiceBooking, Order, OrderItem, Attendance

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token['username'] = user.username
        token['first_name'] = user.first_name
        token['last_name'] = user.last_name
        token['email'] = user.email
        token['is_staff'] = user.is_staff
        token['is_superuser'] = user.is_superuser
        return token

class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=True, style={'input_type': 'password'})
    first_name = serializers.CharField(required=False, allow_blank=True, default='')
    last_name = serializers.CharField(required=False, allow_blank=True, default='')

    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'first_name', 'last_name', 'password')

    def create(self, validated_data):
        user = User.objects.create(
            username=validated_data['username'],
            email=validated_data.get('email', ''),
            first_name=validated_data.get('first_name', ''),
            last_name=validated_data.get('last_name', ''),
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
                  'unit_price', 'image_url', 'model_file', 'thumbnail', 'real_world_size']

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
    customer_name = serializers.SerializerMethodField()

    class Meta:
        model = GardenDesign
        fields = ['id', 'name', 'total_cost', 'status', 'created_at', 'updated_at', 'dimensions', 'original_image_url', 'customer_name']

    def get_customer_name(self, obj):
        if obj.user:
            full_name = f"{obj.user.first_name} {obj.user.last_name}".strip()
            return full_name if full_name else obj.user.username
        return 'Anonymous'

class BlackoutDateSerializer(serializers.ModelSerializer):
    class Meta:
        model = BlackoutDate
        fields = '__all__'

class ServiceBookingSerializer(serializers.ModelSerializer):
    customer_name = serializers.SerializerMethodField()
    customer_email = serializers.SerializerMethodField()
    design_details = serializers.SerializerMethodField(read_only=True)
    clock_out_photo_url = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = ServiceBooking
        fields = ['id', 'user', 'customer_name', 'customer_email', 'service_type', 'scheduled_date', 'status', 'contact_number', 'preferred_time', 'service_address', 'notes', 'design', 'design_details', 'clock_out_photo_url', 'created_at', 'updated_at']
        read_only_fields = ['id', 'user', 'customer_name', 'customer_email', 'created_at', 'updated_at', 'design_details', 'clock_out_photo_url']

    def get_customer_name(self, obj):
        if obj.user:
            full_name = f"{obj.user.first_name} {obj.user.last_name}".strip()
            return full_name if full_name else obj.user.username
        return 'Unknown'

    def get_customer_email(self, obj):
        return obj.user.email if obj.user else ''

    def get_design_details(self, obj):
        if obj.design:
            image_url = obj.design.original_image_url
            if not image_url and obj.design.garden_image:
                request = self.context.get('request')
                if request:
                    image_url = request.build_absolute_uri(obj.design.garden_image.image.url)
                else:
                    image_url = obj.design.garden_image.image.url
            return {
                'id': obj.design.id,
                'name': obj.design.name,
                'image_url': image_url
            }
        return None

    def get_clock_out_photo_url(self, obj):
        attendance = obj.attendances.filter(clock_out_photo_url__isnull=False).exclude(clock_out_photo_url='').first()
        if attendance and attendance.clock_out_photo_url:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(attendance.clock_out_photo_url.url)
            return attendance.clock_out_photo_url.url
        return None

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
        fields = ['id', 'customer_name', 'customer_email', 'customer_phone', 'customer_address',
                  'booking_date', 'total_price', 'payment_method', 'status', 'payment_status', 'created_at', 'items']


class ManageUserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False)
    role = serializers.SerializerMethodField(read_only=True)
    input_role = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = User
        fields = ('id', 'username', 'email', 'first_name', 'last_name', 'is_active', 'is_staff', 'is_superuser', 'date_joined', 'password', 'role', 'input_role')
        read_only_fields = ('id', 'date_joined')

    def get_role(self, obj):
        if obj.is_superuser:
            return 'Admin'
        elif obj.is_staff:
            return 'Staff'
        return 'Customer'

    def create(self, validated_data):
        input_role = validated_data.pop('input_role', 'customer')
        password = validated_data.pop('password', None)
        
        # If username is not provided, use email prefix or generate it
        if 'username' not in validated_data or not validated_data['username']:
            email = validated_data.get('email', '')
            if email:
                validated_data['username'] = email.split('@')[0]
            else:
                import random
                validated_data['username'] = 'user_' + str(random.randint(1000, 9999))
        
        # Make sure username is unique
        base_username = validated_data['username']
        username = base_username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}_{counter}"
            counter += 1
        validated_data['username'] = username

        # Map role
        is_staff = False
        is_superuser = False
        if input_role == 'staff':
            is_staff = True
        elif input_role == 'admin':
            is_superuser = True
            is_staff = True

        user = User.objects.create(
            is_staff=is_staff,
            is_superuser=is_superuser,
            **validated_data
        )
        if password:
            user.set_password(password)
        else:
            user.set_password(User.objects.make_random_password())
        user.save()
        return user

    def update(self, instance, validated_data):
        input_role = validated_data.pop('input_role', None)
        password = validated_data.pop('password', None)

        if input_role is not None:
            if input_role == 'staff':
                instance.is_staff = True
                instance.is_superuser = False
            elif input_role == 'admin':
                instance.is_staff = True
                instance.is_superuser = True
            else:
                instance.is_staff = False
                instance.is_superuser = False

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        if password:
            instance.set_password(password)

        instance.save()
        return instance


class AttendanceSerializer(serializers.ModelSerializer):
    staff_name = serializers.SerializerMethodField(read_only=True)
    booking_label = serializers.SerializerMethodField(read_only=True)
    total_hours = serializers.ReadOnlyField()

    class Meta:
        model = Attendance
        fields = [
            'id', 'staff', 'staff_name', 'booking', 'booking_label',
            'clock_in_time', 'clock_out_time',
            'clock_in_photo_url', 'clock_out_photo_url',
            'latitude', 'longitude', 'total_hours',
            'clock_in_address', 'clock_out_address'
        ]
        read_only_fields = ['id', 'staff', 'clock_in_time', 'clock_out_time']

    def get_staff_name(self, obj):
        if obj.staff:
            full_name = f"{obj.staff.first_name} {obj.staff.last_name}".strip()
            return full_name if full_name else obj.staff.username
        return 'Unknown'

    def get_booking_label(self, obj):
        if obj.booking:
            return f"#{obj.booking.id} - {obj.booking.service_type} for {obj.booking.user.username} ({obj.booking.scheduled_date})"
        return 'General'