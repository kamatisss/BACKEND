from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth.models import User
from .models import (
    InventoryItem, GardenDesign, GardenImage, BlackoutDate, ServiceBooking,
    Order, OrderItem, Attendance, ProjectMilestone, StaffAttendance,
    StaffProfile, Notification, ServiceReview,
    ProjectTracker, ProjectHistoryLog, ProjectProgressMedia,
)

def _resolve_canonical_role(user):
    """
    Return the canonical 4-tier role string for a user.

    Canonical values  →  what gets embedded in the JWT and returned on login
    ─────────────────────────────────────────────────────────────────────────
    'SUPER_ADMIN'   superuser flag
    'OFFICE_ADMIN'  is_staff + StaffProfile.role in ('OFFICE_ADMIN', 'Staff', None/missing)
    'FIELD_CREW'    is_staff + StaffProfile.role == 'FIELD_CREW'
    'CUSTOMER'      regular user, no staff flags
    """
    if user.is_superuser:
        return 'SUPER_ADMIN'
    if user.is_staff:
        raw_role = None
        try:
            raw_role = user.staff_profile.role
        except Exception:
            pass
        # Normalise legacy 'Staff' → 'OFFICE_ADMIN' at token-issuance time
        return 'FIELD_CREW' if raw_role == 'FIELD_CREW' else 'OFFICE_ADMIN'
    return 'CUSTOMER'


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

        # Raw DB role — kept for backward compatibility (user.staff_role on frontend)
        try:
            token['staff_role'] = user.staff_profile.role
        except Exception:
            token['staff_role'] = None

        # Canonical role enum — readable as user.role on the frontend after jwtDecode()
        # Normalises legacy 'Staff' values so the frontend never sees an unexpected string
        token['role'] = _resolve_canonical_role(user)

        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        data['username'] = self.user.username
        data['email'] = self.user.email
        # Login response carries the same canonical role the token contains
        data['role'] = _resolve_canonical_role(self.user)
        return data


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
                  'unit_price', 'image_url', 'model_file', 'thumbnail', 'real_world_size', 'spacing_cm']

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
        fields = ['id', 'name', 'original_image_url', 'reference_image_url',
                  'depth_data', 'placed_items', 'plant_breakdown', 'dimensions', 'total_cost',
                  'terrain_height', 'time_of_day', 'status',
                  'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


class GardenDesignListSerializer(serializers.ModelSerializer):
    """Lighter serializer for list views (no placed_items blob)."""
    customer_name = serializers.SerializerMethodField()

    class Meta:
        model = GardenDesign
        fields = ['id', 'name', 'total_cost', 'status', 'created_at', 'updated_at', 'dimensions', 'original_image_url', 'reference_image_url', 'customer_name']

    def get_customer_name(self, obj):
        if obj.user:
            full_name = f"{obj.user.first_name} {obj.user.last_name}".strip()
            return full_name if full_name else obj.user.username
        return 'Anonymous'

class BlackoutDateSerializer(serializers.ModelSerializer):
    class Meta:
        model = BlackoutDate
        fields = '__all__'


class ProjectMilestoneSerializer(serializers.ModelSerializer):
    phase_display = serializers.CharField(source='get_phase_display', read_only=True)

    class Meta:
        model = ProjectMilestone
        fields = ['id', 'phase', 'phase_display', 'completion_pct', 'notes', 'proof_photo_url', 'updated_at']
        read_only_fields = ['id', 'phase_display', 'proof_photo_url', 'updated_at']


class StaffAttendanceSerializer(serializers.ModelSerializer):
    staff_name = serializers.SerializerMethodField()

    class Meta:
        model = StaffAttendance
        fields = [
            'id', 'booking', 'staff', 'staff_name',
            'timestamp_checkin', 'timestamp_checkout',
            'gps_lat_checkin', 'gps_lng_checkin', 'distance_at_checkin_m',
        ]
        read_only_fields = ['id', 'staff', 'staff_name', 'timestamp_checkin', 'distance_at_checkin_m']

    def get_staff_name(self, obj):
        full_name = f"{obj.staff.first_name} {obj.staff.last_name}".strip()
        return full_name or obj.staff.username


class ServiceBookingSerializer(serializers.ModelSerializer):
    customer_name = serializers.SerializerMethodField()
    customer_email = serializers.SerializerMethodField()
    design_details = serializers.SerializerMethodField(read_only=True)
    clock_out_photo_url = serializers.SerializerMethodField(read_only=True)
    project_milestones = ProjectMilestoneSerializer(many=True, read_only=True)
    assigned_crew_names = serializers.SerializerMethodField(read_only=True)
    assigned_crew = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        many=True,
        required=False,
    )

    class Meta:
        model = ServiceBooking
        fields = [
            'id', 'user', 'customer_name', 'customer_email',
            'service_type', 'scheduled_date', 'status',
            'contact_number', 'preferred_time', 'service_address', 'notes',
            'site_lat', 'site_lng',
            'assigned_crew', 'assigned_crew_names',
            'design', 'design_details', 'clock_out_photo_url',
            'milestones', 'progress_pct', 'staff_notes',
            'project_milestones',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'user', 'customer_name', 'customer_email',
            'assigned_crew_names', 'created_at', 'updated_at',
            'design_details', 'clock_out_photo_url',
        ]

    def validate(self, attrs):
        if 'assigned_crew' in attrs and attrs['assigned_crew']:
            request = self.context.get('request')
            if request and request.user:
                user = request.user
                is_authorized = user.is_superuser
                if not is_authorized:
                    try:
                        is_authorized = user.staff_profile.role == 'OFFICE_ADMIN'
                    except Exception:
                        pass
                if not is_authorized:
                    raise serializers.ValidationError({"assigned_crew": "Only Office Admins can assign or change crew."})
        return super().validate(attrs)

    def update(self, instance, validated_data):
        assigned_crew = validated_data.pop('assigned_crew', None)
        instance = super().update(instance, validated_data)
        if assigned_crew is not None:
            instance.assigned_crew.set(assigned_crew)
        return instance

    def get_customer_name(self, obj):
        if obj.user:
            full_name = f"{obj.user.first_name} {obj.user.last_name}".strip()
            return full_name if full_name else obj.user.username
        return 'Unknown'

    def get_customer_email(self, obj):
        return obj.user.email if obj.user else ''

    def get_assigned_crew_names(self, obj):
        names = []
        for u in obj.assigned_crew.all():
            full = f"{u.first_name} {u.last_name}".strip()
            names.append(full or u.username)
        return names

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
                'image_url': image_url,
                'reference_image_url': obj.design.reference_image_url or '',
                'placed_items': obj.design.placed_items or [],
                'plant_breakdown': obj.design.plant_breakdown or [],
                'dimensions': obj.design.dimensions or {},
                'total_cost': float(obj.design.total_cost),
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
    role = serializers.ChoiceField(
        choices=['SUPER_ADMIN', 'OFFICE_ADMIN', 'FIELD_CREW', 'CUSTOMER'],
        required=False
    )
    input_role = serializers.CharField(write_only=True, required=False)
    staff_role = serializers.SerializerMethodField(read_only=True)
    input_staff_role = serializers.ChoiceField(
        choices=['OFFICE_ADMIN', 'FIELD_CREW', ''],
        write_only=True, required=False, allow_blank=True,
    )

    class Meta:
        model = User
        fields = (
            'id', 'username', 'email', 'first_name', 'last_name',
            'is_active', 'is_staff', 'is_superuser', 'date_joined',
            'password', 'role', 'input_role', 'staff_role', 'input_staff_role',
        )
        read_only_fields = ('id', 'date_joined')

    def get_staff_role(self, obj):
        try:
            return obj.staff_profile.role
        except Exception:
            return None

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        # Determine 4-tier role
        if instance.is_superuser:
            ret['role'] = 'SUPER_ADMIN'
        elif instance.is_staff:
            try:
                profile = instance.staff_profile
                if profile.role == 'FIELD_CREW':
                    ret['role'] = 'FIELD_CREW'
                else:
                    ret['role'] = 'OFFICE_ADMIN'
            except Exception:
                ret['role'] = 'OFFICE_ADMIN'
        else:
            ret['role'] = 'CUSTOMER'
        return ret

    def create(self, validated_data):
        role = validated_data.pop('role', None)
        input_role = validated_data.pop('input_role', None)
        input_staff_role = validated_data.pop('input_staff_role', None)
        password = validated_data.pop('password', None)
        
        # Determine role from various inputs for backward compatibility
        if not role:
            if input_role == 'admin':
                role = 'SUPER_ADMIN'
            elif input_role == 'staff':
                if input_staff_role == 'FIELD_CREW':
                    role = 'FIELD_CREW'
                else:
                    role = 'OFFICE_ADMIN'
            else:
                role = 'CUSTOMER'

        # Map role to flags
        is_staff = False
        is_superuser = False
        if role == 'SUPER_ADMIN':
            is_superuser = True
            is_staff = True
        elif role in ('OFFICE_ADMIN', 'FIELD_CREW'):
            is_staff = True

        # Generate unique username
        if 'username' not in validated_data or not validated_data['username']:
            email = validated_data.get('email', '')
            if email:
                validated_data['username'] = email.split('@')[0]
            else:
                import random
                validated_data['username'] = 'user_' + str(random.randint(1000, 9999))
        
        base_username = validated_data['username']
        username = base_username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{base_username}_{counter}"
            counter += 1
        validated_data['username'] = username

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

        # Save staff profile role if applicable
        if role in ('OFFICE_ADMIN', 'FIELD_CREW'):
            StaffProfile.objects.create(user=user, role=role)
        else:
            StaffProfile.objects.filter(user=user).delete()
            
        return user

    def update(self, instance, validated_data):
        role = validated_data.pop('role', None)
        input_role = validated_data.pop('input_role', None)
        input_staff_role = validated_data.pop('input_staff_role', None)
        password = validated_data.pop('password', None)

        # Determine target role
        if role is not None:
            pass
        elif input_role is not None:
            if input_role == 'admin':
                role = 'SUPER_ADMIN'
            elif input_role == 'staff':
                if input_staff_role == 'FIELD_CREW':
                    role = 'FIELD_CREW'
                elif input_staff_role == 'OFFICE_ADMIN':
                    role = 'OFFICE_ADMIN'
                else:
                    role = 'OFFICE_ADMIN'
            else:
                role = 'CUSTOMER'

        if role is not None:
            if role == 'SUPER_ADMIN':
                instance.is_superuser = True
                instance.is_staff = True
                StaffProfile.objects.filter(user=instance).delete()
            elif role == 'OFFICE_ADMIN':
                instance.is_superuser = False
                instance.is_staff = True
                profile, _ = StaffProfile.objects.get_or_create(user=instance)
                profile.role = 'OFFICE_ADMIN'
                profile.save()
            elif role == 'FIELD_CREW':
                instance.is_superuser = False
                instance.is_staff = True
                profile, _ = StaffProfile.objects.get_or_create(user=instance)
                profile.role = 'FIELD_CREW'
                profile.save()
            elif role == 'CUSTOMER':
                instance.is_superuser = False
                instance.is_staff = False
                StaffProfile.objects.filter(user=instance).delete()

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        if password:
            instance.set_password(password)

        instance.save()
        return instance


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ['id', 'message', 'is_read', 'timestamp']
        read_only_fields = ['id', 'message', 'timestamp']


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


class ServiceReviewSerializer(serializers.ModelSerializer):
    reviewer_name = serializers.SerializerMethodField()
    crew_names = serializers.SerializerMethodField()
    service_type = serializers.SerializerMethodField()
    booking = serializers.PrimaryKeyRelatedField(queryset=ServiceBooking.objects.all())

    class Meta:
        model = ServiceReview
        fields = ['id', 'booking', 'rating', 'comment', 'reviewer_name', 'crew_names', 'service_type', 'is_public', 'created_at']
        read_only_fields = ['id', 'reviewer_name', 'crew_names', 'service_type', 'is_public', 'created_at']

    def get_reviewer_name(self, obj):
        first = obj.user.first_name.strip()
        last = obj.user.last_name.strip()
        if first and last:
            return f"{first} {last[0]}."
        return first or obj.user.username

    def get_crew_names(self, obj):
        return [
            (f"{u.first_name} {u.last_name}".strip() or u.username)
            for u in obj.booking.assigned_crew.all()
        ]

    def get_service_type(self, obj):
        return obj.booking.get_service_type_display()

    def validate_rating(self, value):
        if not 1 <= value <= 5:
            raise serializers.ValidationError("Rating must be between 1 and 5.")
        return value

    def validate_booking(self, booking):
        user = self.context['request'].user
        if booking.user != user:
            raise serializers.ValidationError("You can only review your own bookings.")
        if booking.status not in ('Finished', 'Completed'):
            raise serializers.ValidationError("Only completed bookings can be reviewed.")
        if hasattr(booking, 'review'):
            raise serializers.ValidationError("You have already submitted a review for this booking.")
        return booking

    def create(self, validated_data):
        validated_data['user'] = self.context['request'].user
        return super().create(validated_data)


# ─────────────────────────────────────────────────────────────────────────────
# Project Tracker serializers
# ─────────────────────────────────────────────────────────────────────────────

class ProjectHistoryLogSerializer(serializers.ModelSerializer):
    trigger_user_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(
        source='get_status_reached_display', read_only=True
    )

    class Meta:
        model = ProjectHistoryLog
        fields = [
            'id', 'status_reached', 'status_display',
            'timestamp', 'trigger_user', 'trigger_user_name', 'remarks',
        ]
        read_only_fields = ['id', 'timestamp', 'status_display', 'trigger_user_name']

    def get_trigger_user_name(self, obj):
        if obj.trigger_user:
            full = f"{obj.trigger_user.first_name} {obj.trigger_user.last_name}".strip()
            return full or obj.trigger_user.username
        return 'System'


class ProjectProgressMediaSerializer(serializers.ModelSerializer):
    uploader_name = serializers.SerializerMethodField()
    file_url = serializers.SerializerMethodField()
    phase_display = serializers.CharField(
        source='get_construction_phase_display', read_only=True
    )

    class Meta:
        model = ProjectProgressMedia
        fields = [
            'id', 'project', 'construction_phase', 'phase_display',
            'file_path', 'file_url', 'uploaded_at',
            'uploader', 'uploader_name', 'description', 'location',
        ]
        read_only_fields = [
            'id', 'uploaded_at', 'uploader', 'uploader_name',
            'file_url', 'phase_display',
        ]

    def get_uploader_name(self, obj):
        if obj.uploader:
            full = f"{obj.uploader.first_name} {obj.uploader.last_name}".strip()
            return full or obj.uploader.username
        return 'Unknown'

    def get_file_url(self, obj):
        request = self.context.get('request')
        if obj.file_path and hasattr(obj.file_path, 'url'):
            return (
                request.build_absolute_uri(obj.file_path.url)
                if request else obj.file_path.url
            )
        return None


class ProjectTrackerSerializer(serializers.ModelSerializer):
    supervisor_name = serializers.SerializerMethodField()
    booking_customer = serializers.SerializerMethodField()
    booking_service = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    history_logs = ProjectHistoryLogSerializer(many=True, read_only=True)
    progress_media = ProjectProgressMediaSerializer(many=True, read_only=True)

    class Meta:
        model = ProjectTracker
        fields = [
            'id', 'booking', 'booking_customer', 'booking_service',
            'status', 'status_display', 'progress_percentage',
            'supervisor', 'supervisor_name',
            'estimated_end_date', 'total_price', 'remaining_balance',
            'created_at', 'updated_at',
            'history_logs', 'progress_media',
        ]
        read_only_fields = [
            'id', 'progress_percentage', 'created_at', 'updated_at',
            'status_display', 'booking_customer', 'booking_service',
            'supervisor_name',
        ]

    def get_supervisor_name(self, obj):
        if obj.supervisor:
            full = f"{obj.supervisor.first_name} {obj.supervisor.last_name}".strip()
            return full or obj.supervisor.username
        return None

    def get_booking_customer(self, obj):
        if obj.booking and obj.booking.user:
            u = obj.booking.user
            full = f"{u.first_name} {u.last_name}".strip()
            return full or u.username
        return 'Unknown'

    def get_booking_service(self, obj):
        if obj.booking:
            return obj.booking.get_service_type_display()
        return None


class ProjectTrackerListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for list views — omits nested logs and media."""
    supervisor_name = serializers.SerializerMethodField()
    booking_customer = serializers.SerializerMethodField()
    booking_service = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = ProjectTracker
        fields = [
            'id', 'booking', 'booking_customer', 'booking_service',
            'status', 'status_display', 'progress_percentage',
            'supervisor', 'supervisor_name',
            'estimated_end_date', 'total_price', 'remaining_balance',
            'updated_at',
        ]
        read_only_fields = [
            'id', 'progress_percentage', 'updated_at',
            'status_display', 'booking_customer', 'booking_service', 'supervisor_name',
        ]

    def get_supervisor_name(self, obj):
        if obj.supervisor:
            full = f"{obj.supervisor.first_name} {obj.supervisor.last_name}".strip()
            return full or obj.supervisor.username
        return None

    def get_booking_customer(self, obj):
        if obj.booking and obj.booking.user:
            u = obj.booking.user
            full = f"{u.first_name} {u.last_name}".strip()
            return full or u.username
        return 'Unknown'

    def get_booking_service(self, obj):
        if obj.booking:
            return obj.booking.get_service_type_display()
        return None