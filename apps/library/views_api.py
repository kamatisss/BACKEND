import stripe
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse

from rest_framework import viewsets, status, generics
from rest_framework.decorators import action, api_view
from rest_framework.permissions import AllowAny, IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from django.contrib.auth.models import User
from django.db import transaction
from rest_framework_simplejwt.views import TokenObtainPairView

from .models import InventoryItem, GardenDesign, BlackoutDate, ServiceBooking, Order, OrderItem, Attendance
from .serializers import (
    InventoryItemSerializer,
    GardenDesignSerializer,
    GardenDesignListSerializer,
    UserSerializer,
    CustomTokenObtainPairSerializer,
    BlackoutDateSerializer,
    ServiceBookingSerializer,
    OrderSerializer,
    ManageUserSerializer,
    AttendanceSerializer,
)

class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer

class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    permission_classes = (AllowAny,)
    serializer_class = UserSerializer


class InventoryItemViewSet(viewsets.ModelViewSet):
    """
    GET /api/inventory/          → list (filterable by ?category=plant)
    GET /api/inventory/:id/      → detail
    POST, PUT, PATCH, DELETE     → admin/staff only
    """
    queryset = InventoryItem.objects.all()
    serializer_class = InventoryItemSerializer

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            self.permission_classes = [AllowAny]
        else:
            self.permission_classes = [IsAdminUser]
        return super().get_permissions()

    def get_queryset(self):
        qs = super().get_queryset()
        category = self.request.query_params.get('category')
        if category:
            qs = qs.filter(category=category)
        return qs


class GardenDesignViewSet(viewsets.ModelViewSet):
    """
    GET    /api/designs/        → list saved designs
    POST   /api/designs/        → create new design
    GET    /api/designs/:id/    → load specific design
    PUT    /api/designs/:id/    → save/update design
    DELETE /api/designs/:id/    → delete design
    """
    queryset = GardenDesign.objects.all()
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if self.request.user.is_staff or self.request.user.is_superuser:
            return GardenDesign.objects.all()
        return GardenDesign.objects.filter(user=self.request.user)

    def get_serializer_class(self):
        if self.action == 'list':
            return GardenDesignListSerializer
        return GardenDesignSerializer

    def perform_create(self, serializer):
        user = self.request.user if self.request.user.is_authenticated else None
        serializer.save(user=user)

    def perform_update(self, serializer):
        serializer.save()

    @action(detail=True, methods=['post'])
    def duplicate(self, request, pk=None):
        """POST /api/designs/:id/duplicate/ → clone a design."""
        original = self.get_object()
        copy = GardenDesign.objects.create(
            user=request.user if request.user.is_authenticated else None,
            name=f"{original.name} (Copy)",
            original_image_url=original.original_image_url,
            depth_data=original.depth_data,
            placed_items=original.placed_items,
            dimensions=original.dimensions,
            total_cost=original.total_cost,
            terrain_height=original.terrain_height,
            time_of_day=original.time_of_day,
        )
        return Response(GardenDesignSerializer(copy).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['patch'])
    def submit(self, request, pk=None):
        design = self.get_object()
        design.status = 'submitted'
        design.save()
        return Response({'status': 'design submitted'})

    @action(detail=False, methods=['get'], permission_classes=[IsAdminUser])
    def submitted_designs(self, request):
        qs = GardenDesign.objects.filter(status='submitted')
        serializer = GardenDesignListSerializer(qs, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['patch'], permission_classes=[IsAdminUser])
    def update_status(self, request, pk=None):
        design = self.get_object()
        new_status = request.data.get('status')
        if new_status in ['approved', 'rejected']:
            design.status = new_status
            design.save()
            return Response({'status': new_status})
        return Response({'error': 'Invalid status'}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['patch'], url_path='items/(?P<item_id>[^/.]+)')
    def patch_item(self, request, pk=None, item_id=None):
        """
        PATCH /api/designs/:id/items/:item_id/
        Partially updates a single placed item inside the design's placed_items JSON array.
        Accepts any subset of: position, rotation, scale.
        """
        design = self.get_object()
        placed = design.placed_items or []

        # Find the item by its client-side id (stored as a number or string)
        target = None
        for item in placed:
            if str(item.get('id')) == str(item_id):
                target = item
                break

        if target is None:
            return Response({'error': f'Item {item_id} not found in design.'}, status=status.HTTP_404_NOT_FOUND)

        # Merge only the fields provided
        allowed_fields = {'position', 'rotation', 'scale', 'rotation_y'}
        for field, value in request.data.items():
            if field in allowed_fields:
                if field == 'rotation_y':
                    # Convenience: update just the Y axis of rotation
                    if 'rotation' not in target or not isinstance(target['rotation'], dict):
                        target['rotation'] = {'x': 0, 'y': 0, 'z': 0}
                    target['rotation']['y'] = float(value)
                else:
                    target[field] = value

        design.placed_items = placed
        design.save(update_fields=['placed_items', 'updated_at'])
        return Response({'status': 'updated', 'item': target})


class BlackoutDateViewSet(viewsets.ModelViewSet):
    queryset = BlackoutDate.objects.all()
    serializer_class = BlackoutDateSerializer
    
    def get_permissions(self):
        if self.request.method in ['GET', 'OPTIONS', 'HEAD']:
            self.permission_classes = [AllowAny]
        else:
            self.permission_classes = [IsAdminUser]
        return super().get_permissions()


class ServiceBookingViewSet(viewsets.ModelViewSet):
    queryset = ServiceBooking.objects.all()
    serializer_class = ServiceBookingSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if self.request.user.is_staff or self.request.user.is_superuser:
            return ServiceBooking.objects.all()
        return ServiceBooking.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        design_id = self.request.data.get('design_id') or self.request.data.get('design')
        if design_id:
            try:
                from .models import GardenDesign
                design = GardenDesign.objects.get(id=design_id)
                serializer.save(user=self.request.user, design=design)
            except Exception:
                serializer.save(user=self.request.user)
        else:
            serializer.save(user=self.request.user)

    @action(detail=True, methods=['patch'], permission_classes=[IsAdminUser])
    def update_status(self, request, pk=None):
        booking = self.get_object()
        new_status = request.data.get('status')
        if new_status in dict(ServiceBooking.STATUS_CHOICES):
            booking.status = new_status
            booking.save()
            return Response({'status': new_status})
        return Response({'error': 'Invalid status'}, status=status.HTTP_400_BAD_REQUEST)

class OrderViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = OrderSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # Admins and staff can see all orders
        if self.request.user.is_staff or self.request.user.is_superuser:
            return Order.objects.all().order_by('-created_at')
        # Standard users can only see their own orders
        return Order.objects.filter(user=self.request.user).order_by('-created_at')

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        instance.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['patch'], permission_classes=[IsAuthenticated])
    def update_status(self, request, pk=None):
        order = self.get_object()
        new_status = request.data.get('status')
        new_payment_status = request.data.get('payment_status')

        if not (request.user.is_staff or request.user.is_superuser):
            return Response({'error': 'Unauthorized'}, status=status.HTTP_403_FORBIDDEN)

        if new_status:
            # Map common variants like 'shipped', 'out_for_delivery', 'delivered', 'cancelled' to capitalized choices
            normalized_status = new_status
            if new_status == 'shipped':
                normalized_status = 'Shipped'
            elif new_status == 'out_for_delivery':
                normalized_status = 'Out for Delivery'
            elif new_status == 'delivered':
                normalized_status = 'Delivered'
            elif new_status == 'cancelled':
                normalized_status = 'Cancelled'

            if normalized_status in dict(Order.STATUS_CHOICES):
                order.status = normalized_status
                if normalized_status == 'Delivered' and order.payment_method == 'cod':
                    order.payment_status = 'Paid'
            else:
                return Response({'error': f'Invalid status: {new_status}'}, status=status.HTTP_400_BAD_REQUEST)

        if new_payment_status:
            order.payment_status = new_payment_status

        order.save()
        return Response(OrderSerializer(order).data, status=status.HTTP_200_OK)


@api_view(['POST'])
def checkout(request):
    data = request.data
    customer_name = data.get('customer_name')
    customer_email = data.get('customer_email')
    customer_phone = data.get('customer_phone', '')
    customer_address = data.get('customer_address')
    payment_method = data.get('payment_method', 'stripe')
    items_data = data.get('items', [])
    total_price = data.get('total_price', 0)

    try:
        with transaction.atomic():
            order = Order.objects.create(
                user=request.user if request.user.is_authenticated else None,
                customer_name=customer_name,
                customer_email=customer_email,
                customer_phone=customer_phone,
                customer_address=customer_address,
                payment_method=payment_method,
                total_price=total_price
            )

            for item_data in items_data:
                # select_for_update() locks the row until transaction ends, preventing race conditions
                try:
                    inventory_item = InventoryItem.objects.select_for_update().get(id=item_data['id'])
                except InventoryItem.DoesNotExist:
                    raise ValueError(f"Item with ID {item_data['id']} does not exist.")

                quantity = item_data.get('quantity', 1)

                if inventory_item.stock_quantity < quantity:
                    raise ValueError(f"Insufficient stock for {inventory_item.name}. Available: {inventory_item.stock_quantity}")

                inventory_item.stock_quantity -= quantity
                inventory_item.save()

                OrderItem.objects.create(
                    order=order,
                    item=inventory_item,
                    quantity=quantity,
                    price_at_booking=inventory_item.unit_price
                )

        return Response({'message': 'Order successfully placed!', 'order_id': order.id}, status=status.HTTP_201_CREATED)

    except ValueError as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ─────────────────────────────────────────────────────────────────────────────
# Stripe Payment Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@api_view(['POST'])
def create_checkout_session(request):
    """
    Accept an order_id, look up the Order, and create a Stripe Checkout Session.
    Returns the checkout_url for the React frontend to redirect to.
    """
    stripe.api_key = settings.STRIPE_SECRET_KEY

    order_id = request.data.get('order_id')
    if not order_id:
        return Response({'error': 'order_id is required.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        return Response({'error': 'Order not found.'}, status=status.HTTP_404_NOT_FOUND)

    # Convert total_price from Philippine Peso to centavos (Stripe uses smallest currency unit)
    amount_centavos = int(order.total_price * 100)

    frontend_url = settings.FRONTEND_URL  # e.g. 'http://localhost:5173'
    success_url  = (
        f"{frontend_url}/order-success"
        f"?order_id={order.id}"
        f"&total={order.total_price}"
        f"&name={order.customer_name}"
    )
    cancel_url = f"{frontend_url}/studio"

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'php',
                    'product_data': {
                        'name': f'Garden Studio Order #{order.id}',
                        'description': f'Landscaping items for {order.customer_name}',
                    },
                    'unit_amount': amount_centavos,
                },
                'quantity': 1,
            }],
            mode='payment',
            client_reference_id=str(order.id),  # Used by webhook to find the order
            customer_email=order.customer_email,
            success_url=success_url,
            cancel_url=cancel_url,
        )
        return Response({'checkout_url': session.url})
    except stripe.error.StripeError as e:
        return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@csrf_exempt
def stripe_webhook(request):
    """
    Stripe sends a signed POST here when payment events occur.
    We listen for checkout.session.completed and mark the Order as Paid.
    """
    stripe.api_key      = settings.STRIPE_SECRET_KEY
    webhook_secret      = settings.STRIPE_WEBHOOK_SECRET
    payload             = request.body
    sig_header          = request.META.get('HTTP_STRIPE_SIGNATURE', '')

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except ValueError:
        # Invalid payload
        return HttpResponse(status=400)
    except stripe.error.SignatureVerificationError:
        # Invalid signature
        return HttpResponse(status=400)

    if event['type'] == 'checkout.session.completed':
        session  = event['data']['object']
        order_id = session.get('client_reference_id')
        if order_id:
            try:
                order        = Order.objects.get(id=order_id)
                order.status = 'Paid'
                order.save()
            except Order.DoesNotExist:
                pass  # Order deleted mid-session — ignore gracefully

    return HttpResponse(status=200)


# ─────────────────────────────────────────────────────────────────────────────
# Forgot / Reset Password (no email required)
# ─────────────────────────────────────────────────────────────────────────────

@api_view(['POST'])
def reset_password(request):
    """
    POST /api/reset-password/
    Body: { username, email, new_password }

    Verifies that the given username + email match a real account,
    then updates the password. No email server required.
    Returns 200 on success, 400 on mismatch or validation error.
    """
    username     = request.data.get('username', '').strip()
    email        = request.data.get('email', '').strip().lower()
    new_password = request.data.get('new_password', '')

    if not username or not email or not new_password:
        return Response(
            {'error': 'username, email, and new_password are all required.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    if len(new_password) < 8:
        return Response(
            {'error': 'New password must be at least 8 characters.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        # Return the same message as an email mismatch to avoid username enumeration
        return Response(
            {'error': 'No account found with that username and email combination.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    if user.email.lower() != email:
        return Response(
            {'error': 'No account found with that username and email combination.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    user.set_password(new_password)
    user.save()
    return Response({'message': 'Password reset successfully. You can now log in.'})


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all().order_by('-date_joined')
    serializer_class = ManageUserSerializer
    
    def get_permissions(self):
        from rest_framework.permissions import BasePermission
        
        class IsSuperUser(BasePermission):
            def has_permission(self, request, view):
                return bool(request.user and request.user.is_superuser)
        
        self.permission_classes = [IsSuperUser]
        return super().get_permissions()


class AttendanceViewSet(viewsets.ModelViewSet):
    serializer_class = AttendanceSerializer
    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        from rest_framework.permissions import BasePermission
        class IsStaffOrAdmin(BasePermission):
            def has_permission(self, request, view):
                return bool(request.user and request.user.is_authenticated and (request.user.is_staff or request.user.is_superuser))
        self.permission_classes = [IsStaffOrAdmin]
        return super().get_permissions()

    def get_queryset(self):
        if self.request.user.is_superuser:
            return Attendance.objects.all().order_by('-clock_in_time')
        return Attendance.objects.filter(staff=self.request.user).order_by('-clock_in_time')

    @action(detail=False, methods=['get'])
    def current(self, request):
        """GET /api/attendance/current/ - Get the active clock-in session of the staff member"""
        active_session = Attendance.objects.filter(staff=request.user, clock_out_time__isnull=True).first()
        if active_session:
            return Response({
                'clocked_in': True,
                'attendance': AttendanceSerializer(active_session, context={'request': request}).data
            })
        return Response({
            'clocked_in': False,
            'attendance': None
        })

    @action(detail=False, methods=['post'])
    def clock_in(self, request):
        """POST /api/attendance/clock_in/"""
        from django.utils import timezone
        import base64
        from django.core.files.base import ContentFile

        user = request.user
        if Attendance.objects.filter(staff=user, clock_out_time__isnull=True).exists():
            return Response({'error': 'You are already clocked in. Please clock out first.'}, status=status.HTTP_400_BAD_REQUEST)

        booking_id = request.data.get('booking_id')
        latitude = request.data.get('latitude')
        longitude = request.data.get('longitude')
        address = request.data.get('address') or request.data.get('clock_in_address')
        photo_data = request.data.get('photo') or request.data.get('clock_in_photo_url')

        try:
            latitude = float(latitude) if latitude is not None else None
            longitude = float(longitude) if longitude is not None else None
        except (ValueError, TypeError):
            latitude = None
            longitude = None

        try:
            with transaction.atomic():
                attendance = Attendance(
                    staff=user,
                    booking_id=booking_id,
                    clock_in_time=timezone.now(),
                    latitude=latitude,
                    longitude=longitude,
                    clock_in_address=address
                )

                if photo_data:
                    if isinstance(photo_data, str) and photo_data.startswith('data:image'):
                        try:
                            format, imgstr = photo_data.split(';base64,')
                            ext = format.split('/')[-1].split(';')[0]
                            filename = f"clock_in_{user.id}_{int(timezone.now().timestamp())}.{ext}"
                            attendance.clock_in_photo_url.save(filename, ContentFile(base64.b64decode(imgstr)), save=False)
                        except Exception as e:
                            return Response({'error': f'Failed to decode image: {str(e)}'}, status=status.HTTP_400_BAD_REQUEST)
                    elif hasattr(photo_data, 'read'):
                        attendance.clock_in_photo_url = photo_data

                attendance.save()

                if booking_id:
                    try:
                        booking = ServiceBooking.objects.get(id=booking_id)
                        booking.status = 'In Progress'
                        booking.save()
                    except ServiceBooking.DoesNotExist:
                        pass
        except Exception as e:
            return Response({'error': f'Transaction failed: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(AttendanceSerializer(attendance, context={'request': request}).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'])
    def clock_out(self, request):
        """POST /api/attendance/clock_out/"""
        from django.utils import timezone
        import base64
        from django.core.files.base import ContentFile

        user = request.user
        active_session = Attendance.objects.filter(staff=user, clock_out_time__isnull=True).first()
        if not active_session:
            return Response({'error': 'No active clock-in session found. Please clock in first.'}, status=status.HTTP_400_BAD_REQUEST)

        latitude = request.data.get('latitude')
        longitude = request.data.get('longitude')
        address = request.data.get('address') or request.data.get('clock_out_address')
        photo_data = request.data.get('photo') or request.data.get('clock_out_photo_url')

        try:
            with transaction.atomic():
                try:
                    if latitude is not None:
                        active_session.latitude = float(latitude)
                    if longitude is not None:
                        active_session.longitude = float(longitude)
                except (ValueError, TypeError):
                    pass

                active_session.clock_out_time = timezone.now()
                active_session.clock_out_address = address

                if photo_data:
                    if isinstance(photo_data, str) and photo_data.startswith('data:image'):
                        try:
                            format, imgstr = photo_data.split(';base64,')
                            ext = format.split('/')[-1].split(';')[0]
                            filename = f"clock_out_{user.id}_{int(timezone.now().timestamp())}.{ext}"
                            active_session.clock_out_photo_url.save(filename, ContentFile(base64.b64decode(imgstr)), save=False)
                        except Exception as e:
                            return Response({'error': f'Failed to decode image: {str(e)}'}, status=status.HTTP_400_BAD_REQUEST)
                    elif hasattr(photo_data, 'read'):
                        active_session.clock_out_photo_url = photo_data

                active_session.save()

                if active_session.booking:
                    booking = active_session.booking
                    booking.status = 'Completed'
                    booking.save()

        except Exception as e:
            return Response({'error': f'Transaction failed: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(AttendanceSerializer(active_session, context={'request': request}).data, status=status.HTTP_200_OK)

