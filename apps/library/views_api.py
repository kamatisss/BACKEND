import stripe
import os
import json
import logging
import time
import hashlib
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from rest_framework.views import APIView
from google import genai
from google.genai import types
from google.genai.errors import APIError
from pydantic import BaseModel, Field
from typing import List
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)

# ── AI latency optimisation ─────────────────────────────────────
GRID_COLS = 20          # compressed coordinate grid width
GRID_ROWS = 20          # compressed coordinate grid height
MAX_RETRIES = 3
_CACHE_TTL = 300        # seconds – layouts cached for 5 min

_layout_cache: dict = {}


def _make_cache_key(budget: int, lot_w: float, lot_l: float, pids: list) -> str:
    raw = f"{budget}|{round(lot_w,1)}|{round(lot_l,1)}|{','.join(sorted(str(p) for p in pids))}"
    return hashlib.md5(raw.encode()).hexdigest()


def _get_cached(key: str):
    entry = _layout_cache.get(key)
    if entry and time.time() - entry[0] < _CACHE_TTL:
        return entry[1]
    _layout_cache.pop(key, None)
    return None


def _set_cached(key: str, data: dict) -> None:
    _layout_cache[key] = (time.time(), data)


def _expand_grid_coords(plants: list, lot_width: float, lot_length: float) -> list:
    """Convert AI-returned gx/gz grid indices into real-world x/z metres."""
    cell_w = lot_width / GRID_COLS
    cell_l = lot_length / GRID_ROWS
    result = []
    for p in plants:
        item = dict(p)
        item['x'] = round(item.pop('gx', 0) * cell_w, 3)
        item['z'] = round(item.pop('gz', 0) * cell_l, 3)
        result.append(item)
    return result


def _haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return distance in metres between two GPS coordinates."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def bind_proof_to_milestone(booking_id: int, phase: str, photo_url: str) -> None:
    """Attach a proof photo URL to the matching ProjectMilestone record."""
    ProjectMilestone.objects.filter(booking_id=booking_id, phase=phase).update(proof_photo_url=photo_url)

# ───────────────────────────────────────────────────────────────
from django.http import HttpResponse

from rest_framework import viewsets, status, generics
from rest_framework.decorators import action, api_view
from rest_framework.permissions import AllowAny, IsAuthenticated, IsAdminUser, BasePermission
from rest_framework.response import Response

class IsOfficeAdmin(BasePermission):
    """
    Grants access to superusers and staff members whose role resolves to OFFICE_ADMIN.

    Transition-phase strategy
    ─────────────────────────
    • Accepts 'OFFICE_ADMIN'  — current canonical enum value
    • Accepts 'Staff'         — legacy string used before the role enum migration
    • Falls back to is_staff  — catches manually-created admin accounts that have no
                                StaffProfile row yet (never locked out silently)
    • Explicitly denies 'FIELD_CREW' even when is_staff is True
    """
    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        # Customers can never have a StaffProfile — early exit
        if not user.is_staff:
            return False

        # Resolve the raw role string from StaffProfile (may be absent on legacy accounts)
        resolved_role = None
        try:
            resolved_role = user.staff_profile.role
        except Exception:
            pass

        # ── DEBUG: print exactly what the permission class sees ──────────────
        print(
            f"DEBUG assign_crew permission | "
            f"email={user.email!r} "
            f"is_staff={user.is_staff} "
            f"user.role (direct attr)={getattr(user, 'role', 'NO_ROLE')!r} "
            f"resolved_role (staff_profile.role)={resolved_role!r}"
        )
        # ─────────────────────────────────────────────────────────────────────

        # FIELD_CREW is staff but must never be able to assign crew
        if resolved_role == 'FIELD_CREW':
            return False

        # Accept current canonical value, legacy value, or any un-profiled staff account
        return resolved_role in ('OFFICE_ADMIN', 'Staff') or user.is_staff

from django.contrib.auth.models import User
from django.db import transaction
from rest_framework_simplejwt.views import TokenObtainPairView

from .models import (
    InventoryItem, GardenDesign, BlackoutDate, ServiceBooking,
    Order, OrderItem, Attendance, ProjectMilestone, StaffAttendance,
    StaffProfile, Notification, ServiceReview,
    ProjectTracker, ProjectHistoryLog, ProjectProgressMedia,
)
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
    ProjectMilestoneSerializer,
    StaffAttendanceSerializer,
    NotificationSerializer,
    ServiceReviewSerializer,
    ProjectTrackerSerializer,
    ProjectTrackerListSerializer,
    ProjectHistoryLogSerializer,
    ProjectProgressMediaSerializer,
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
            self.permission_classes = [IsOfficeAdmin]
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
            self.permission_classes = [IsOfficeAdmin]
        return super().get_permissions()


class ServiceBookingViewSet(viewsets.ModelViewSet):
    queryset = ServiceBooking.objects.all()
    serializer_class = ServiceBookingSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        base = ServiceBooking.objects.select_related('design', 'design__garden_image').prefetch_related('assigned_crew')

        if not (user.is_staff or user.is_superuser):
            return base.filter(user=user)

        # Use getattr to avoid StaffProfile.DoesNotExist silently falling through to base.all()
        role = getattr(getattr(user, 'staff_profile', None), 'role', None)

        if role == 'FIELD_CREW':
            # M2M filter — .distinct() prevents duplicate rows from the join
            return base.filter(assigned_crew=user).distinct()

        # OFFICE_ADMIN / SUPER_ADMIN / legacy staff accounts
        return base.all()

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

    @action(detail=True, methods=['patch'], permission_classes=[IsAuthenticated])
    def update_status(self, request, pk=None):
        user = request.user
        if not (user.is_staff or user.is_superuser):
            return Response({'error': 'Staff access required.'}, status=status.HTTP_403_FORBIDDEN)

        new_status = request.data.get('status')

        # FIELD_CREW: may only mark a booking Finished — all other transitions are admin-only
        if not user.is_superuser:
            try:
                if user.staff_profile.role == 'FIELD_CREW' and new_status != 'Finished':
                    return Response(
                        {'error': 'Field crew members can only mark bookings as Finished.'},
                        status=status.HTTP_403_FORBIDDEN,
                    )
            except Exception:
                pass

        booking = self.get_object()
        if new_status in dict(ServiceBooking.STATUS_CHOICES):
            booking.status = new_status
            booking.save()
            return Response({'status': new_status})
        return Response({'error': 'Invalid status'}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['patch'], permission_classes=[IsAdminUser])
    def update_milestones(self, request, pk=None):
        booking = self.get_object()
        milestones   = request.data.get('milestones')
        progress_pct = request.data.get('progress_pct')
        staff_notes  = request.data.get('staff_notes')

        if milestones is not None:
            booking.milestones = milestones
        if progress_pct is not None:
            try:
                booking.progress_pct = max(0, min(100, int(progress_pct)))
            except (ValueError, TypeError):
                return Response({'error': 'progress_pct must be an integer 0-100'},
                                status=status.HTTP_400_BAD_REQUEST)
        if staff_notes is not None:
            booking.staff_notes = staff_notes

        booking.save()
        serializer = ServiceBookingSerializer(booking, context={'request': request})
        return Response(serializer.data)

    @action(detail=True, methods=['post'], url_path='check_in_at_site', permission_classes=[IsAuthenticated])
    def check_in_at_site(self, request, pk=None):
        from django.utils import timezone
        booking = self.get_object()
        try:
            lat = float(request.data['latitude'])
            lng = float(request.data['longitude'])
        except (KeyError, TypeError, ValueError):
            return Response({'error': 'latitude and longitude are required numeric fields.'},
                            status=status.HTTP_400_BAD_REQUEST)

        # Enforce: staff must have an active Attendance clock-in session for THIS booking.
        active_att = Attendance.objects.filter(
            staff=request.user, clock_out_time__isnull=True
        ).first()
        if active_att is None:
            return Response(
                {'error': f'You are not clocked in. Clock in to Booking #{booking.id} via Attendance Tracking first.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if active_att.booking_id != booking.id:
            return Response(
                {'error': (
                    f'Your active attendance session is linked to Booking #{active_att.booking_id}, '
                    f'not Booking #{booking.id}. Switch your attendance session first.'
                )},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if booking.site_lat is not None and booking.site_lng is not None:
            dist_m = _haversine(lat, lng, booking.site_lat, booking.site_lng)
            if dist_m > 50:
                return Response(
                    {'error': f'You are {dist_m:.0f} m from the site. Must be within 50 m to check in.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            dist_m = None

        attendance = StaffAttendance.objects.create(
            booking=booking,
            staff=request.user,
            timestamp_checkin=timezone.now(),
            gps_lat_checkin=lat,
            gps_lng_checkin=lng,
            distance_at_checkin_m=dist_m,
        )
        if booking.status in ['Pending', 'Preparing']:
            booking.status = 'Installing'
            booking.save()
        return Response(StaffAttendanceSerializer(attendance).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['patch'], url_path='update_milestone', permission_classes=[IsAdminUser])
    def update_milestone(self, request, pk=None):
        booking = self.get_object()
        phase = request.data.get('phase')
        completion_pct = request.data.get('completion_pct')
        notes = request.data.get('notes', '')
        proof_photo_url_direct = request.data.get('proof_photo_url', '')

        valid_phases = dict(ProjectMilestone.PHASE_CHOICES)
        if phase not in valid_phases:
            return Response(
                {'error': f'Invalid phase. Choices: {list(valid_phases.keys())}'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if completion_pct is None:
            return Response({'error': 'completion_pct is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            pct = max(0, min(100, int(completion_pct)))
        except (ValueError, TypeError):
            return Response({'error': 'completion_pct must be an integer 0-100.'}, status=status.HTTP_400_BAD_REQUEST)

        milestone, _ = ProjectMilestone.objects.get_or_create(booking=booking, phase=phase)
        milestone.completion_pct = pct
        if notes:
            milestone.notes = notes

        # Accept a directly-supplied proof URL (e.g. from field crew image uploads)
        if proof_photo_url_direct:
            milestone.proof_photo_url = proof_photo_url_direct
        # Auto-bind proof photo from the most recent clock-out when phase completes
        elif pct == 100 and not milestone.proof_photo_url:
            latest_checkout = (
                Attendance.objects.filter(booking=booking, clock_out_time__isnull=False)
                .exclude(clock_out_photo_url='').exclude(clock_out_photo_url__isnull=True)
                .order_by('-clock_out_time').first()
            )
            if latest_checkout and latest_checkout.clock_out_photo_url:
                try:
                    milestone.proof_photo_url = latest_checkout.clock_out_photo_url.url
                except Exception:
                    pass

        milestone.save()

        # Progress is based on the 4 active pipeline phases only (not legacy phases)
        active_phases = ProjectMilestone.ACTIVE_PHASES
        completed_count = ProjectMilestone.objects.filter(
            booking=booking, phase__in=active_phases, completion_pct=100
        ).count()
        booking.progress_pct = int(completed_count / len(active_phases) * 100)
        
        # Auto-update status to Installing if not already there, since active work is happening
        if booking.status in ['Pending', 'Preparing']:
            booking.status = 'Installing'
        booking.save()

        serializer = ServiceBookingSerializer(booking, context={'request': request})
        return Response({
            'milestone': ProjectMilestoneSerializer(milestone).data,
            'progress_pct': booking.progress_pct,
            'booking': serializer.data,
        })


    @action(detail=False, methods=['get'], url_path='field_crew_members', permission_classes=[IsAdminUser])
    def field_crew_members(self, request):
        """GET /api/bookings/field_crew_members/ — list active FIELD_CREW users for assignment dropdowns."""
        crew = User.objects.filter(
            staff_profile__role='FIELD_CREW', is_active=True
        ).select_related('staff_profile').order_by('first_name', 'username')
        return Response([{
            'id': u.id,
            'username': u.username,
            'name': f"{u.first_name} {u.last_name}".strip() or u.username,
            'email': u.email,
        } for u in crew])

    @action(detail=True, methods=['post', 'patch', 'put'], url_path='assign_crew', permission_classes=[IsOfficeAdmin])
    def assign_crew(self, request, pk=None):
        """POST/PATCH/PUT /api/bookings/:id/assign_crew/
        Accepts crew_ids (array) to support multi-crew capacity planning.
        Also accepts legacy crew_id (single int) for backward compatibility.
        Sends notifications only to newly added crew members.
        """
        booking = self.get_object()

        # Resolve crew_ids — accept array or single legacy value
        crew_ids = request.data.get('crew_ids')
        if crew_ids is None:
            single = request.data.get('crew_id') or request.data.get('assigned_crew')
            crew_ids = [single] if single else []

        # Empty list → clear all crew
        if not crew_ids:
            booking.assigned_crew.clear()
            return Response(ServiceBookingSerializer(booking, context={'request': request}).data)

        # Validate each crew member
        crew_users = []
        for cid in crew_ids:
            try:
                crew_user = User.objects.select_related('staff_profile').get(pk=cid, is_active=True)
            except User.DoesNotExist:
                return Response({'error': f'User {cid} not found.'}, status=status.HTTP_404_NOT_FOUND)
            try:
                if crew_user.staff_profile.role != 'FIELD_CREW':
                    return Response(
                        {'error': f'{crew_user.username} is not a Field Crew member.'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            except Exception:
                return Response(
                    {'error': f'{crew_user.username} does not have a Field Crew profile.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            crew_users.append(crew_user)

        # Capture previously assigned IDs so we only notify newcomers
        previously_assigned = set(booking.assigned_crew.values_list('id', flat=True))
        booking.assigned_crew.set(crew_users)

        service_label = dict(ServiceBooking.SERVICE_CHOICES).get(booking.service_type, booking.service_type.title())
        location = booking.service_address.strip() or 'Location TBD'
        for crew_user in crew_users:
            if crew_user.id not in previously_assigned:
                Notification.objects.create(
                    user=crew_user,
                    message=(
                        f"You have been assigned to {service_label} at {location} "
                        f"(Booking #{booking.id}, scheduled {booking.scheduled_date})."
                    ),
                )

        return Response(ServiceBookingSerializer(booking, context={'request': request}).data)

    @action(detail=True, methods=['get'], url_path='generate_pdf', permission_classes=[IsAuthenticated])
    def generate_pdf(self, request, pk=None):
        """GET /api/bookings/<id>/generate_pdf/  →  Download a printable Work Order PDF."""
        booking = self.get_object()

        user = request.user
        role = getattr(getattr(user, 'staff_profile', None), 'role', None)
        if not (user.is_staff or user.is_superuser):
            if not booking.assigned_crew.filter(pk=user.pk).exists():
                return Response(
                    {'error': 'You are not assigned to this booking.'},
                    status=status.HTTP_403_FORBIDDEN,
                )

        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.units import cm
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib import colors
            from reportlab.platypus import (
                SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
                HRFlowable, Image as RLImage, Flowable, PageBreak,
            )
            from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
            from reportlab.graphics.shapes import (
                Drawing, Circle, Rect, Line, String as GrStr,
            )
            from reportlab.graphics import renderPDF as _renderPDF
            import io as _io
            import math as _math
        except ImportError:
            return Response(
                {'error': 'PDF library (reportlab) is not installed on this server.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        buffer = _io.BytesIO()
        PAGE_W, PAGE_H = A4
        MARGIN = 1.8 * cm

        doc = SimpleDocTemplate(
            buffer, pagesize=A4,
            rightMargin=MARGIN, leftMargin=MARGIN,
            topMargin=MARGIN, bottomMargin=MARGIN,
        )

        # ── Colour palette ────────────────────────────────────────────
        DARK  = colors.HexColor('#0f172a')
        MID   = colors.HexColor('#475569')
        LIGHT = colors.HexColor('#94a3b8')
        RULE  = colors.HexColor('#e2e8f0')
        GREEN = colors.HexColor('#059669')
        ORANGE = colors.HexColor('#ea580c')
        BG    = colors.HexColor('#f8fafc')
        BG2   = colors.HexColor('#f0fdf4')

        H1    = ParagraphStyle('H1',    fontName='Helvetica-Bold', fontSize=20, textColor=DARK, alignment=TA_CENTER, spaceAfter=2)
        H2    = ParagraphStyle('H2',    fontName='Helvetica-Bold', fontSize=10, textColor=DARK, spaceBefore=10, spaceAfter=5)
        SUB   = ParagraphStyle('SUB',   fontName='Helvetica',      fontSize=9,  textColor=MID,  alignment=TA_CENTER, spaceAfter=4)
        LBL   = ParagraphStyle('LBL',   fontName='Helvetica-Bold', fontSize=8,  textColor=LIGHT)
        VAL   = ParagraphStyle('VAL',   fontName='Helvetica',      fontSize=9,  textColor=DARK)
        SMALL = ParagraphStyle('SMALL', fontName='Helvetica',      fontSize=7,  textColor=LIGHT, alignment=TA_CENTER)
        CELL  = ParagraphStyle('CELL',  fontName='Helvetica',      fontSize=8,  textColor=DARK)
        CELLR = ParagraphStyle('CELLR', fontName='Helvetica',      fontSize=8,  textColor=DARK,  alignment=TA_RIGHT)
        CELLC = ParagraphStyle('CELLC', fontName='Helvetica',      fontSize=8,  textColor=MID,   alignment=TA_CENTER)
        HEAD  = ParagraphStyle('HEAD',  fontName='Helvetica-Bold', fontSize=8,  textColor=colors.white)
        HEADR = ParagraphStyle('HEADR', fontName='Helvetica-Bold', fontSize=8,  textColor=colors.white, alignment=TA_RIGHT)
        HEADC = ParagraphStyle('HEADC', fontName='Helvetica-Bold', fontSize=8,  textColor=colors.white, alignment=TA_CENTER)

        col_full = PAGE_W - 2 * MARGIN

        # ── Inline flowable for ReportLab vector drawings ─────────────
        class _DrawingFlowable(Flowable):
            def __init__(self, drawing):
                Flowable.__init__(self)
                self._d = drawing
                self.width  = drawing.width
                self.height = drawing.height
            def draw(self):
                _renderPDF.draw(self._d, self.canv, 0, 0)

        # ── Pre-parse Design Placements & Dimensions ──────────────────
        placed_raw = (booking.design.placed_items or []) if booking.design else []
        dims       = (booking.design.dimensions or {}) if booking.design else {}
        plant_num  = {}  # pid str → 1-based number; shared by the BOM No. column
        plant_groups = {}  # pid str → {'label': str, 'positions': [(x,z)]}

        if placed_raw and isinstance(placed_raw, list):
            # Parse plant positions from both AI-format and 3D-studio-format items
            for item in placed_raw:
                if 'x' in item and 'z' in item:
                    pid   = str(item.get('plant_id', item.get('productId', '?')))
                    label = item.get('name') or pid
                    x, z  = float(item['x']), float(item['z'])
                elif 'position' in item:
                    pid   = str(item.get('productId', item.get('modelType', '?')))
                    label = item.get('name') or pid
                    x = float(item['position'].get('x', 0))
                    z = float(item['position'].get('z', 0))
                else:
                    continue
                plant_groups.setdefault(pid, {'label': label, 'positions': []})['positions'].append((x, z))

            if plant_groups:
                # Assign stable 1-based numbers so the legend and BOM can cross-reference
                for seq, pid in enumerate(plant_groups.keys(), 1):
                    plant_num[pid] = seq

        story = []

        # ── Page 1: Administrative Details & Render ───────────────────
        story.append(Paragraph('WORK ORDER', H1))
        story.append(Paragraph(
            f'Booking #{booking.id}  ·  {booking.get_service_type_display()}  ·  {booking.scheduled_date}  ·  Status: {booking.status}',
            SUB,
        ))
        story.append(HRFlowable(width='100%', thickness=2, color=GREEN, spaceAfter=8))

        # Project details table in a clean 2-column layout to save vertical space
        story.append(Paragraph('PROJECT DETAILS', H2))
        customer = booking.user
        cname = f"{customer.first_name} {customer.last_name}".strip() or customer.username
        scheduled_val = f"{booking.scheduled_date}  ·  {booking.preferred_time or 'Morning'}"
        meta_data = [
            [
                Paragraph('CUSTOMER', LBL), Paragraph(cname, VAL),
                Paragraph('SCHEDULED', LBL), Paragraph(scheduled_val, VAL)
            ],
            [
                Paragraph('CONTACT', LBL), Paragraph(booking.contact_number or '—', VAL),
                Paragraph('STATUS', LBL), Paragraph(booking.status, VAL)
            ],
            [
                Paragraph('SITE ADDRESS', LBL), Paragraph(booking.service_address or '—', VAL),
                Paragraph('PROGRESS', LBL), Paragraph(f"{booking.progress_pct}%", VAL)
            ]
        ]
        meta_tbl = Table(meta_data, colWidths=[3.0*cm, col_full/2 - 3.0*cm, 3.0*cm, col_full/2 - 3.0*cm])
        meta_tbl.setStyle(TableStyle([
            ('ROWBACKGROUNDS', (0, 0), (-1, -1), [BG, colors.white]),
            ('TOPPADDING',     (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING',  (0, 0), (-1, -1), 5),
            ('LEFTPADDING',    (0, 0), (-1, -1), 7),
            ('RIGHTPADDING',   (0, 0), (-1, -1), 7),
            ('GRID',           (0, 0), (-1, -1), 0.3, RULE),
            ('VALIGN',         (0, 0), (-1, -1), 'TOP'),
        ]))
        story.append(meta_tbl)

        # Assigned crew
        crew_names = []
        for u in booking.assigned_crew.all():
            full = f"{u.first_name} {u.last_name}".strip()
            crew_names.append(full or u.username)
        if crew_names:
            story.append(Paragraph('ASSIGNED CREW', H2))
            story.append(Paragraph('  ·  '.join(crew_names), VAL))
            story.append(Spacer(1, 4))

        # AI Design Photo (on Page 1 if present)
        ai_url = None
        if booking.design:
            ai_url = booking.design.original_image_url or None
        if ai_url:
            try:
                import requests as _req
                from PIL import Image as PILImage
                resp = _req.get(ai_url, timeout=8)
                if resp.status_code == 200:
                    img_buf = _io.BytesIO(resp.content)
                    pil = PILImage.open(img_buf).convert('RGB')
                    pil.thumbnail((700, 300), PILImage.LANCZOS)
                    out = _io.BytesIO()
                    pil.save(out, format='JPEG', quality=85)
                    out.seek(0)
                    story.append(Paragraph('AI DESIGN RENDER', H2))
                    story.append(RLImage(out, width=col_full, height=5.5*cm, kind='proportional'))
                    story.append(Spacer(1, 4))
            except Exception:
                pass

        # ── Page 2: Dedicated Blueprint & Field Reference ─────────────
        ref_url = None
        if booking.design:
            ref_url = booking.design.reference_image_url or None

        # Build Page 2 if blueprint design or site reference photo exists
        if plant_groups or (ref_url and ref_url != ai_url):
            story.append(PageBreak())

            if plant_groups:
                story.append(Paragraph('GARDEN LAYOUT BLUEPRINT  (Overhead / Orthographic View)', H2))

                # Redesigned 25m x 10m grid scaling
                lot_w = 25.0
                lot_l = 10.0

                DIAG_W  = float(col_full)
                DIAG_H  = 13.5 * cm  # Fulfills min 50% vertical page real estate requirement
                D_MARG  = 35.0

                usable_w = DIAG_W - 2 * D_MARG
                usable_h = DIAG_H - 2 * D_MARG
                scale    = min(usable_w / lot_w, usable_h / lot_l)

                lot_px_w = lot_w * scale
                lot_px_h = lot_l * scale
                ox = (DIAG_W - lot_px_w) / 2
                oy = (DIAG_H - lot_px_h) / 2

                drw = Drawing(DIAG_W, DIAG_H)

                # Lot background + green boundary frame
                drw.add(Rect(ox, oy, lot_px_w, lot_px_h,
                             fillColor=colors.HexColor('#f0fdf4'),
                             strokeColor=GREEN, strokeWidth=1.5))

                # Minor grid lines (every 1 m) — very faint
                GRID_MINOR = colors.HexColor('#d1fae5')
                for xi in range(1, int(lot_w)):
                    gx = ox + xi * scale
                    drw.add(Line(gx, oy, gx, oy + lot_px_h,
                                 strokeColor=GRID_MINOR, strokeWidth=0.3))
                for zi in range(1, int(lot_l)):
                    gz = oy + zi * scale
                    drw.add(Line(ox, gz, ox + lot_px_w, gz,
                                 strokeColor=GRID_MINOR, strokeWidth=0.3))

                # Major grid lines (every 5 m) — slightly more visible
                GRID_MAJOR = colors.HexColor('#6ee7b7')
                for xi in range(5, int(lot_w), 5):
                    gx = ox + xi * scale
                    drw.add(Line(gx, oy, gx, oy + lot_px_h,
                                 strokeColor=GRID_MAJOR, strokeWidth=0.8))
                for zi in range(5, int(lot_l), 5):
                    gz = oy + zi * scale
                    drw.add(Line(ox, gz, ox + lot_px_w, gz,
                                 strokeColor=GRID_MAJOR, strokeWidth=0.8))

                # Tick labels every 5 m along width axis (bottom)
                for xi in range(0, int(lot_w) + 1, 5):
                    gx = ox + xi * scale
                    drw.add(GrStr(gx, oy - 12, f'{xi}m',
                                  textAnchor='middle', fontSize=7, fillColor=LIGHT))

                # Tick labels every 5 m along length axis (left)
                for zi in range(0, int(lot_l) + 1, 5):
                    gz = oy + zi * scale
                    drw.add(GrStr(ox - 12, gz - 2.5, f'{zi}m',
                                  textAnchor='end', fontSize=7, fillColor=LIGHT))

                # Clear orientation markers (House Side / Fence Side)
                drw.add(GrStr(ox + lot_px_w / 2, oy + lot_px_h + 15, "FENCE SIDE",
                              textAnchor='middle', fontSize=9, fontName='Helvetica-Bold', fillColor=DARK))
                drw.add(GrStr(ox + lot_px_w / 2, oy - 25, "HOUSE SIDE",
                              textAnchor='middle', fontSize=9, fontName='Helvetica-Bold', fillColor=DARK))

                # North indicator (top-right corner, outside grid but inside canvas margin)
                drw.add(GrStr(ox + lot_px_w, oy + lot_px_h + 15,
                              'N ↑', textAnchor='end', fontSize=9, fontName='Helvetica-Bold', fillColor=MID))

                # Dimension span labels
                drw.add(GrStr(ox + lot_px_w / 2, oy - 38,
                              f'Width: {lot_w:.0f} m',
                              textAnchor='middle', fontSize=8,
                              fillColor=MID, fontName='Helvetica-Bold'))

                from reportlab.graphics.shapes import Group as _GrGrp
                h_lbl_grp = _GrGrp()
                h_lbl_grp.add(GrStr(0, 0, f'Length: {lot_l:.0f} m',
                                    textAnchor='middle', fontSize=8,
                                    fillColor=MID, fontName='Helvetica-Bold'))
                h_lbl_grp.transform = (0, 1, -1, 0, ox - 26, oy + lot_px_h / 2)
                drw.add(h_lbl_grp)

                # Plant markers (e.g. 1.24, 2.25) rendered as circles with inner labels
                PALETTE = [
                    '#059669', '#0ea5e9', '#f59e0b', '#8b5cf6',
                    '#ef4444', '#06b6d4', '#84cc16', '#f97316',
                ]
                legend_entries = []   # [(seq, label, hex_color)]
                r_pt = 11.0  # Larger marker size for clear text legibility

                for pid, data in plant_groups.items():
                    seq     = plant_num[pid]
                    hex_col = PALETTE[(seq - 1) % len(PALETTE)]
                    rl_col  = colors.HexColor(hex_col)
                    legend_entries.append((seq, data['label'], hex_col))

                    for idx, (x, z) in enumerate(data['positions']):
                        # Use abs to guard against negative coordinates
                        cx = ox + abs(float(x)) * scale
                        cy = oy + abs(float(z)) * scale
                        cx = max(ox + r_pt, min(ox + lot_px_w - r_pt, cx))
                        cy = max(oy + r_pt, min(oy + lot_px_h - r_pt, cy))
                        drw.add(Circle(cx, cy, r_pt,
                                       fillColor=rl_col,
                                       strokeColor=colors.white, strokeWidth=0.8))
                        # Format label as Species.Instance (e.g. 1.24, 2.25)
                        marker_str = f"{seq}.{idx+1}"
                        drw.add(GrStr(cx, cy - r_pt * 0.25,
                                      marker_str,
                                      textAnchor='middle',
                                      fontSize=6.5,
                                      fontName='Helvetica-Bold',
                                      fillColor=colors.white))

                story.append(_DrawingFlowable(drw))
                story.append(Spacer(1, 5))

                # Plant Legend immediately adjacent to/below the Blueprint
                HLEG = ParagraphStyle('HLEG', fontName='Helvetica-Bold', fontSize=8,
                                      textColor=MID, spaceAfter=3)
                story.append(Paragraph('PLANT LEGEND  (Markers format: Species.Instance, e.g. 1.24 is instance 24 of species 1)', HLEG))
                legend_entries.sort(key=lambda e: e[0])
                leg_cols = 3
                leg_row_data = [legend_entries[i:i+leg_cols]
                                for i in range(0, len(legend_entries), leg_cols)]
                for row in leg_row_data:
                    cells = []
                    for seq, lbl, hex_col in row:
                        cells.append(Paragraph(
                            f'<font color="{hex_col}"><b>&#9679;</b></font>'
                            f'  <b>{seq}.</b>  {lbl}',
                            CELL,
                        ))
                    while len(cells) < leg_cols:
                        cells.append(Paragraph('', CELL))
                    story.append(Table(
                        [cells],
                        colWidths=[col_full / leg_cols] * leg_cols,
                        style=TableStyle([
                            ('TOPPADDING',    (0, 0), (-1, -1), 2),
                            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
                            ('LEFTPADDING',   (0, 0), (-1, -1), 4),
                        ]),
                    ))
                story.append(Spacer(1, 6))

            # Site Reference — "Before" Photo (Grouped with blueprint and legend on Page 2)
            if ref_url and ref_url != ai_url:
                try:
                    import requests as _req
                    from PIL import Image as PILImage, ImageEnhance as _PILEnh
                    resp = _req.get(ref_url, timeout=8)
                    if resp.status_code == 200:
                        img_buf = _io.BytesIO(resp.content)
                        pil = PILImage.open(img_buf).convert('RGB')
                        # Boost contrast and sharpness for high-contrast field print
                        pil = _PILEnh.Contrast(pil).enhance(1.25)
                        pil = _PILEnh.Sharpness(pil).enhance(1.3)
                        pil.thumbnail((700, 300), PILImage.LANCZOS)
                        out = _io.BytesIO()
                        pil.save(out, format='JPEG', quality=88)
                        out.seek(0)
                        story.append(Paragraph('SITE REFERENCE  —  "Before" Photo  (use as visual anchor on site)', H2))
                        story.append(RLImage(out, width=col_full, height=5.5*cm, kind='proportional'))
                        story.append(Spacer(1, 4))
                except Exception:
                    pass

        # ── Page 3: Technical Specifications & Workflow Sign-off ─────
        bom = []
        if booking.design and booking.design.plant_breakdown:
            bom = booking.design.plant_breakdown

        if bom or booking.milestones or booking.staff_notes:
            # Force technical details and acknowledgement to a new page to remain the final workflow step
            if plant_groups or (ref_url and ref_url != ai_url):
                story.append(PageBreak())

            if bom:
                # ── Build spacing lookup ───────────────────────────────────
                spacing_map = {}   # plant_id (int) → spacing label string

                # Compute nearest-neighbour average spacing from placed_items
                nn_spacing = {}   # pid str → avg metres (always >= 0)
                if placed_raw:
                    pos_by_pid = {}
                    for item in placed_raw:
                        pid_key = str(item.get('plant_id', item.get('productId', '')))
                        if not pid_key:
                            continue
                        if 'x' in item and 'z' in item:
                            pos_by_pid.setdefault(pid_key, []).append(
                                (abs(float(item['x'])), abs(float(item['z']))))
                        elif 'position' in item:
                            pos_by_pid.setdefault(pid_key, []).append((
                                abs(float(item['position'].get('x', 0))),
                                abs(float(item['position'].get('z', 0))),
                            ))
                    for pid_key, positions in pos_by_pid.items():
                        if len(positions) < 2:
                            continue
                        dists = []
                        for i, (x1, z1) in enumerate(positions):
                            min_d = min(
                                _math.sqrt((x2-x1)**2 + (z2-z1)**2)
                                for j, (x2, z2) in enumerate(positions) if i != j
                            )
                            dists.append(min_d)
                        avg = sum(dists) / len(dists)
                        if avg > 0:
                            nn_spacing[pid_key] = avg

                # Priority: explicit spacing_cm → computed NN → derived from real_world_size
                # Sanitized: No prefix characters like '~', formatted as positive integers
                plant_ids = [int(r['plant_id']) for r in bom if r.get('plant_id')]
                if plant_ids:
                    from .models import InventoryItem as _Inv
                    for inv in _Inv.objects.filter(id__in=plant_ids).values('id', 'spacing_cm', 'real_world_size'):
                        pid_int = inv['id']
                        if inv['spacing_cm']:
                            cm_val = abs(int(inv['spacing_cm']))
                            spacing_map[pid_int] = f"{cm_val} cm OC"
                        elif str(pid_int) in nn_spacing:
                            cm_val = abs(round(nn_spacing[str(pid_int)] * 100))
                            if cm_val > 0:
                                spacing_map[pid_int] = f"{cm_val} cm OC"
                        elif inv['real_world_size']:
                            cm_val = abs(round(float(inv['real_world_size']) * 150))
                            if cm_val > 0:
                                spacing_map[pid_int] = f"{cm_val} cm OC"

                # ── BOM table — columns: No. | Plant / Item | Qty | Spacing | Unit | Total ──
                story.append(Paragraph('PLANT BILL OF MATERIALS', H2))
                # Width proportions: No.(0.05) | Name(0.33) | Qty(0.08) | Spacing(0.18) | Unit(0.18) | Total(0.18)
                cw = [col_full * r for r in (0.05, 0.33, 0.08, 0.18, 0.18, 0.18)]
                bom_headers = [
                    Paragraph('No.', HEADC),
                    Paragraph('Plant / Item', HEAD),
                    Paragraph('Qty', HEADC),
                    Paragraph('Spacing', HEADC),
                    Paragraph('Unit (₱)', HEADR),
                    Paragraph('Total (₱)', HEADR),
                ]
                bom_rows = [bom_headers]

                grand = 0.0
                for r in bom:
                    pid_int    = int(r['plant_id']) if r.get('plant_id') else None
                    pid_str    = str(r.get('plant_id', ''))
                    qty        = float(r.get('quantity', 0))
                    unit_price = float(r.get('unit_price', 0))
                    line_total = qty * unit_price
                    grand     += line_total
                    spacing_lbl = spacing_map.get(pid_int, '100 cm OC') if pid_int else '—'
                    num_lbl    = str(plant_num.get(pid_str, '')) if pid_str in plant_num else '—'
                    bom_rows.append([
                        Paragraph(num_lbl, CELLC),
                        Paragraph(r.get('name', ''), CELL),
                        Paragraph(str(int(qty)) if qty == int(qty) else f'{qty:.1f}', CELLC),
                        Paragraph(spacing_lbl, CELLC),
                        Paragraph(f"{unit_price:,.2f}", CELLR),
                        Paragraph(f"{line_total:,.2f}", CELLR),
                    ])

                GT  = ParagraphStyle('GT',  fontName='Helvetica-Bold', fontSize=8,  textColor=DARK)
                GTR = ParagraphStyle('GTR', fontName='Helvetica-Bold', fontSize=10, textColor=GREEN, alignment=TA_RIGHT)
                bom_rows.append([
                    Paragraph('', CELLC),
                    Paragraph('GRAND TOTAL', GT),
                    Paragraph('', CELLC),
                    Paragraph('', CELLC),
                    Paragraph('', CELLR),
                    Paragraph(f"₱ {grand:,.2f}", GTR),
                ])
                bom_tbl = Table(bom_rows, colWidths=cw)
                bom_tbl.setStyle(TableStyle([
                    ('BACKGROUND',     (0, 0),  (-1, 0),  DARK),
                    ('ROWBACKGROUNDS', (0, 1),  (-1, -2), [BG, colors.white]),
                    ('BACKGROUND',     (0, -1), (-1, -1), BG2),
                    ('LINEABOVE',      (0, -1), (-1, -1), 1.0, GREEN),
                    ('LINEBELOW',      (0, -1), (-1, -1), 1.0, GREEN),
                    ('TOPPADDING',     (0, 0),  (-1, -1), 5),
                    ('BOTTOMPADDING',  (0, 0),  (-1, -1), 5),
                    ('LEFTPADDING',    (0, 0),  (-1, -1), 6),
                    ('RIGHTPADDING',   (0, 0),  (-1, -1), 6),
                    ('GRID',           (0, 0),  (-1, -1), 0.2, RULE),
                    ('VALIGN',         (0, 0),  (-1, -1), 'MIDDLE'),
                    ('LINEAFTER',      (0, 0),  (0, -1),  0.5, RULE),
                ]))
                story.append(bom_tbl)
                story.append(Spacer(1, 6))

            # ── Project milestones ────────────────────────────────────
            if booking.milestones:
                story.append(Paragraph('PROJECT MILESTONES', H2))
                mc = [col_full * r for r in (0.55, 0.15, 0.30)]
                DONE_STYLE  = ParagraphStyle('DONE',  fontName='Helvetica-Bold', fontSize=9,
                                             textColor=GREEN, alignment=TA_CENTER)
                PEND_STYLE  = ParagraphStyle('PEND',  fontName='Helvetica',      fontSize=9,
                                             textColor=LIGHT, alignment=TA_CENTER)
                m_rows = [[Paragraph(h, HEAD) for h in ['Milestone', 'Status', 'Completed At']]]
                for m in booking.milestones:
                    done = bool(m.get('completed', False))
                    m_rows.append([
                        Paragraph(m.get('label', ''), CELL),
                        Paragraph('✔  Done' if done else '○  Pending', DONE_STYLE if done else PEND_STYLE),
                        Paragraph(str(m.get('completed_at') or '—')[:10], CELL),
                    ])
                m_tbl = Table(m_rows, colWidths=mc)
                m_tbl.setStyle(TableStyle([
                    ('BACKGROUND',     (0, 0), (-1, 0),  DARK),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [BG, colors.white]),
                    ('TOPPADDING',     (0, 0), (-1, -1), 6),
                    ('BOTTOMPADDING',  (0, 0), (-1, -1), 6),
                    ('LEFTPADDING',    (0, 0), (-1, -1), 8),
                    ('RIGHTPADDING',   (0, 0), (-1, -1), 8),
                    ('GRID',           (0, 0), (-1, -1), 0.2, RULE),
                    ('BOX',            (0, 0), (-1, -1), 1.5, DARK),
                    ('LINEBEFORE',     (0, 1), (0, -1),  3.0, GREEN),
                ]))
                story.append(m_tbl)
                story.append(Spacer(1, 6))

            # ── Field notes ───────────────────────────────────────────
            if booking.staff_notes:
                story.append(Paragraph('FIELD NOTES', H2))
                story.append(Paragraph(booking.staff_notes, VAL))
                story.append(Spacer(1, 6))

            # ── Client Acknowledgement — bordered box ─────────────────
            ACK_HDR = ParagraphStyle('ACKHDR', fontName='Helvetica-Bold', fontSize=9,
                                      textColor=colors.white)
            ACK_NOTE = ParagraphStyle('ACKNOTE', fontName='Helvetica', fontSize=8,
                                       textColor=MID, spaceAfter=6)
            SIG_LBL  = ParagraphStyle('SIGLBL', fontName='Helvetica-Bold', fontSize=7,
                                       textColor=LIGHT, spaceAfter=2)
            SIG_NOTE = ParagraphStyle('SIGNOTE', fontName='Helvetica', fontSize=6,
                                       textColor=LIGHT, alignment=TA_CENTER)

            ack_content = [
                [Paragraph('CLIENT ACKNOWLEDGEMENT', ACK_HDR), ''],
                [Paragraph(
                    'By signing below the client confirms that the work described in this Work Order '
                    'has been completed to a satisfactory standard.',
                    ACK_NOTE,
                ), ''],
                [
                    [
                        Paragraph('Client Signature:', SIG_LBL),
                        Spacer(1, 0.85 * cm),
                        HRFlowable(width='100%', thickness=0.8, color=DARK, spaceAfter=3),
                        Paragraph('Signature over Printed Name', SIG_NOTE),
                    ],
                    [
                        Paragraph('Date:', SIG_LBL),
                        Spacer(1, 0.85 * cm),
                        HRFlowable(width='100%', thickness=0.8, color=DARK, spaceAfter=3),
                        Paragraph('MM / DD / YYYY', SIG_NOTE),
                    ],
                ],
            ]

            ack_tbl = Table(
                ack_content,
                colWidths=[col_full * 0.65, col_full * 0.35],
            )
            ack_tbl.setStyle(TableStyle([
                ('BACKGROUND',    (0, 0), (-1, 0),  DARK),
                ('SPAN',          (0, 0), (-1, 0)),
                ('TOPPADDING',    (0, 0), (-1, 0),  6),
                ('BOTTOMPADDING', (0, 0), (-1, 0),  6),
                ('LEFTPADDING',   (0, 0), (-1, 0),  10),
                ('BACKGROUND',    (0, 1), (-1, 1),  BG),
                ('SPAN',          (0, 1), (-1, 1)),
                ('TOPPADDING',    (0, 1), (-1, 1),  6),
                ('BOTTOMPADDING', (0, 1), (-1, 1),  4),
                ('LEFTPADDING',   (0, 1), (-1, 1),  10),
                ('RIGHTPADDING',  (0, 1), (-1, 1),  10),
                ('VALIGN',        (0, 2), (-1, 2),  'BOTTOM'),
                ('TOPPADDING',    (0, 2), (-1, 2),  8),
                ('BOTTOMPADDING', (0, 2), (-1, 2),  10),
                ('LEFTPADDING',   (0, 2), (-1, 2),  10),
                ('RIGHTPADDING',  (0, 2), (-1, 2),  10),
                ('BOX',           (0, 0), (-1, -1), 1.5, DARK),
                ('LINEAFTER',     (0, 2), (0, 2),   0.5, RULE),
            ]))
            story.append(ack_tbl)

        # ── Footer ───────────────────────────────────────────────────
        from django.utils import timezone as _tz
        story.append(Spacer(1, 10))
        story.append(HRFlowable(width='100%', thickness=0.3, color=RULE, spaceAfter=4))
        story.append(Paragraph(
            f'Generated {_tz.localdate().strftime("%B %d, %Y")}  ·  Garden Studio  ·  '
            f'Work Order #{booking.id}  ·  CONFIDENTIAL — INTERNAL FIELD USE ONLY',
            SMALL,
        ))

        # ── Diagonal watermark on every page ─────────────────────────
        _WM_W, _WM_H = PAGE_W, PAGE_H

        def _watermark(canv, _doc):
            canv.saveState()
            canv.setFont('Helvetica-Bold', 52)
            canv.setFillColor(colors.Color(0.72, 0.72, 0.72, alpha=0.09))
            canv.translate(_WM_W / 2, _WM_H / 2)
            canv.rotate(42)
            canv.drawCentredString(0, 30, 'CONFIDENTIAL')
            canv.setFont('Helvetica', 18)
            canv.setFillColor(colors.Color(0.72, 0.72, 0.72, alpha=0.07))
            canv.drawCentredString(0, -22, 'INTERNAL — FIELD USE ONLY')
            canv.restoreState()

        doc.build(story, onFirstPage=_watermark, onLaterPages=_watermark)
        buffer.seek(0)

        from django.http import HttpResponse as DjangoHttpResponse
        pdf_response = DjangoHttpResponse(buffer.getvalue(), content_type='application/pdf')
        pdf_response['Content-Disposition'] = f'attachment; filename="work-order-{booking.id}.pdf"'
        pdf_response['Access-Control-Expose-Headers'] = 'Content-Disposition'
        return pdf_response


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
def upload_design_image(request):
    """
    POST /api/upload-design-image/
    Accepts a single image file, saves it to media/design_refs/, and returns its absolute URL.
    Used by the AI Designer to persist the garden background photo before creating a GardenDesign.
    """
    if not request.user or not request.user.is_authenticated:
        return Response({'error': 'Authentication required.'}, status=status.HTTP_401_UNAUTHORIZED)
    if 'image' not in request.FILES:
        return Response({'error': 'No image provided.'}, status=status.HTTP_400_BAD_REQUEST)

    image_file = request.FILES['image']
    from django.core.files.storage import default_storage
    from django.conf import settings as django_settings

    ext = os.path.splitext(image_file.name)[1] or '.jpg'
    filename = f"design_refs/ref_{int(time.time())}{ext}"
    saved_path = default_storage.save(filename, image_file)

    media_url = getattr(django_settings, 'MEDIA_URL', '/media/').rstrip('/')
    url = request.build_absolute_uri(f'{media_url}/{saved_path}')
    return Response({'url': url}, status=status.HTTP_201_CREATED)


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

    @action(detail=True, methods=['get'])
    def activity_log(self, request, pk=None):
        """
        GET /api/users/:id/activity_log/
        Returns chronological log of recent customer/staff actions.
        """
        user = self.get_object()
        
        role = 'Customer'
        if user.is_superuser:
            role = 'Admin'
        elif user.is_staff:
            role = 'Staff'
            
        logs = []
        
        if role == 'Staff':
            attendances = Attendance.objects.filter(staff=user).order_by('-clock_in_time')[:10]
            for att in attendances:
                if att.clock_in_time:
                    logs.append({
                        'type': 'clock_in',
                        'timestamp': att.clock_in_time.isoformat(),
                        'details': f"Clocked in at {att.clock_in_address or 'Unknown location'}",
                        'booking_id': att.booking_id,
                        'booking_type': att.booking.service_type if att.booking else None
                    })
                if att.clock_out_time:
                    logs.append({
                        'type': 'clock_out',
                        'timestamp': att.clock_out_time.isoformat(),
                        'details': f"Clocked out at {att.clock_out_address or 'Unknown location'} (Total Hours: {att.total_hours})",
                        'booking_id': att.booking_id,
                        'booking_type': att.booking.service_type if att.booking else None
                    })
        else:
            # Customer bookings
            bookings = ServiceBooking.objects.filter(user=user).order_by('-created_at')[:10]
            for b in bookings:
                logs.append({
                    'type': 'booking',
                    'timestamp': b.created_at.isoformat() if b.created_at else None,
                    'details': f"Service Booking #{b.id} ({b.service_type}) - Status: {b.status} (Scheduled: {b.scheduled_date})",
                    'id': b.id
                })
            # Customer orders
            orders = Order.objects.filter(user=user).order_by('-created_at')[:10]
            for o in orders:
                logs.append({
                    'type': 'order',
                    'timestamp': o.created_at.isoformat() if o.created_at else None,
                    'details': f"Order #{o.id} - Total: ₱{o.total_price:,.2f} - Status: {o.status} - Payment: {o.payment_status}",
                    'id': o.id
                })
                
        # Sort logs by timestamp descending
        logs.sort(key=lambda x: x['timestamp'] or '', reverse=True)
        
        return Response({
            'user_id': user.id,
            'username': user.username,
            'role': role,
            'activity_logs': logs
        })



class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Notification.objects.filter(user=self.request.user)

    @action(detail=True, methods=['patch'])
    def mark_read(self, request, pk=None):
        notif = self.get_object()
        notif.is_read = True
        notif.save(update_fields=['is_read'])
        return Response({'status': 'read'})

    @action(detail=False, methods=['post'])
    def mark_all_read(self, request):
        Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
        return Response({'status': 'all read'})


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

                # Auto-update status to Installing if not already there, since active work is happening
                if booking_id:
                    try:
                        from .models import ServiceBooking
                        booking = ServiceBooking.objects.get(id=booking_id)
                        if booking.status in ['Pending', 'Preparing']:
                            booking.status = 'Installing'
                            booking.save()
                    except Exception:
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

                # Booking status transitions are managed explicitly via the Staff Dashboard.
                # Clock-out saves the attendance record only — no automatic status side-effects.

        except Exception as e:
            return Response({'error': f'Transaction failed: {str(e)}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(AttendanceSerializer(active_session, context={'request': request}).data, status=status.HTTP_200_OK)


# ───────────────────────────────────────────────────────────────
# MOCK PLANT CATALOG & GEMINI GENERATION VIEW
# ───────────────────────────────────────────────────────────────

MOCK_PLANT_CATALOG = [
    {"id": "tree_oak", "name": "Oak Tree", "price": 1200},
    {"id": "flower_rose", "name": "Rose Flower", "price": 150},
    {"id": "shrub_fern", "name": "Fern Shrub", "price": 250},
    {"id": "tree_palm", "name": "Palm Tree", "price": 800},
    {"id": "flower_lavender", "name": "Lavender Flower", "price": 180},
    {"id": "shrub_boxwood", "name": "Boxwood Shrub", "price": 300},
    {"id": "plant_bamboo", "name": "Bamboo Plant", "price": 450},
    {"id": "plant_banana", "name": "Banana Plant", "price": 350},
]

class PlantArrangement(BaseModel):
    plant_id: str
    gx: int = Field(..., ge=0)   # grid column 0..GRID_COLS-1
    gz: int = Field(..., ge=0)   # grid row    0..GRID_ROWS-1
    rotation: float

class PlantCostBreakdown(BaseModel):
    plant_id: str
    name: str
    quantity: int
    unit_price: int
    subtotal: int

class GardenDesignSchema(BaseModel):
    design_name: str
    reasoning: str
    total_cost: int
    plants: List[PlantArrangement]
    plant_breakdown: List[PlantCostBreakdown]

class LayoutResponse(BaseModel):
    designs: List[GardenDesignSchema]


_DESIGN_STYLES = ('Symmetrical', 'Minimalist', 'Lush / Organic')


def _call_single_design(client, style: str, system_instruction: str, base_prompt: str, image_part) -> dict:
    """Generate one garden design for *style*; safe to call from a thread pool."""
    style_prompt = (
        f"{base_prompt}\n"
        f"Generate exactly ONE '{style}' garden design. Set the design_name field to '{style}'."
    )
    contents = [image_part, style_prompt] if image_part else style_prompt
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=contents,
                config={
                    "system_instruction": system_instruction,
                    "response_mime_type": "application/json",
                    "response_schema": GardenDesignSchema,
                    "temperature": 0.4,
                },
            )
            last_err = None
            break
        except APIError as e:
            last_err = e
            err_str = str(e)
            if ('503' in err_str or 'UNAVAILABLE' in err_str or '429' in err_str) and attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
            break
    if last_err is not None:
        raise last_err
    if hasattr(response, 'parsed') and response.parsed is not None:
        return response.parsed.model_dump()
    return json.loads(response.text)


class GenerateLayoutsView(APIView):
    """
    POST /api/generate-layouts/
    Accepts:
    - budget: int (required)
    - preferred_plant_ids: list of str (optional)

    Generates 3 distinct garden design options using Gemini 2.5 (3 parallel calls).
    Guest users receive the generated JSON without it being saved to the database.
    """
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        # 1. Parse request body
        try:
            data = request.data
        except Exception:
            return Response(
                {"error": "Invalid request body. Expected JSON or multipart/form-data."},
                status=status.HTTP_400_BAD_REQUEST
            )

        budget = data.get("budget")
        lot_width = data.get("lot_width")
        lot_length = data.get("lot_length")
        preferred_plant_ids_raw = data.get("preferred_plant_ids", [])

        # 2. Validate inputs
        if budget is None:
            return Response(
                {"error": "Budget is a required field."},
                status=status.HTTP_400_BAD_REQUEST
            )
        if lot_width is None:
            return Response(
                {"error": "Lot width (lot_width) is a required field."},
                status=status.HTTP_400_BAD_REQUEST
            )
        if lot_length is None:
            return Response(
                {"error": "Lot length (lot_length) is a required field."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            budget_int = int(budget)
            if budget_int <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            return Response(
                {"error": "Budget must be a positive integer."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 45% of the total budget is available for plant & material costs.
        # The remaining 55% covers labor (35%) and service/markup (20%).
        plant_budget_int = int(budget_int * 0.45)
        plant_budget_floor = int(plant_budget_int * 0.75)

        try:
            lot_width_float = float(lot_width)
            lot_length_float = float(lot_length)
            if lot_width_float <= 0.0 or lot_length_float <= 0.0:
                raise ValueError()
        except (ValueError, TypeError):
            return Response(
                {"error": "Lot width and lot length must be positive numbers."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Parse preferred_plant_ids (could be stringified JSON array if multipart)
        preferred_plant_ids = []
        if preferred_plant_ids_raw:
            if isinstance(preferred_plant_ids_raw, str):
                try:
                    preferred_plant_ids = json.loads(preferred_plant_ids_raw)
                except Exception:
                    return Response(
                        {"error": "preferred_plant_ids must be a valid JSON array string."},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            elif isinstance(preferred_plant_ids_raw, list):
                preferred_plant_ids = preferred_plant_ids_raw
            else:
                return Response(
                    {"error": "preferred_plant_ids must be a list or a JSON array string."},
                    status=status.HTTP_400_BAD_REQUEST
                )

        # Extract optional image — Vision-then-Math pipeline:
        # 1. Vision AI identifies placement zones only (< 100-token response)
        # 2. resolve_coordinates() maps zones → grid cells (pure Python math)
        # 3. Layout AI receives excluded cell list in text — no image bytes needed
        image_file = request.FILES.get('image')
        image_part = None          # layout calls are text-only after zone resolution
        existing_elements = []     # [{zone, label, x, z, blocks_planting, is_existing}]
        excluded_cells: set = set()

        if image_file:
            try:
                from .utils import scan_image_for_existing_elements, resolve_coordinates
                zone_elements = scan_image_for_existing_elements(image_file)

                for elem in zone_elements:
                    coords = resolve_coordinates(
                        elem['zone'], lot_width_float, lot_length_float
                    )
                    existing_elements.append({
                        **elem,
                        'x': coords['x'],
                        'z': coords['z'],
                    })
                    if elem.get('blocks_planting', True):
                        excluded_cells.update(coords['excluded_cells'])

            except Exception as e:
                logger.exception("Failed during Vision zone scan.")
                return Response(
                    {"error": f"Failed to process uploaded image: {str(e)}"},
                    status=status.HTTP_400_BAD_REQUEST
                )

        # 3. Check for Gemini API key
        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        if not api_key or api_key == "your_gemini_api_key_here":
            logger.error("Gemini/Google API key is not configured or is the default placeholder in environment.")
            return Response(
                {"error": "Gemini/Google API key is not configured on the server. Please check environment configuration."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )

        # 4. Construct client and prompt
        try:
            client = genai.Client(api_key=api_key)
        except Exception as e:
            logger.exception("Failed to initialize GenAI client.")
            return Response(
                {"error": f"Failed to initialize AI Client: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        # Query plant inventory from database early so it's available for both prompt and validation
        available_plants = list(InventoryItem.objects.filter(category='plant'))
        catalog_ids = {str(p.id) for p in available_plants}
        plant_price_map = {str(p.id): float(p.unit_price) for p in available_plants}

        # Fail-safe budget guard — warn when plant budget can't cover minimum lot density
        _prices = [float(p.unit_price) for p in available_plants]
        _cheapest = min(_prices) if _prices else 200.0
        _lot_area = lot_width_float * lot_length_float
        _min_plants = max(5, int(_lot_area / 15))
        _min_viable = _min_plants * _cheapest
        _budget_warning = None
        if plant_budget_int < _min_viable:
            _budget_warning = (
                "Budget may be too tight for an area of this size. "
                "Consider increasing your budget or choosing low-cost flora."
            )

        # Build system instruction — zone exclusions injected as exact grid cells,
        # not fuzzy coordinate radii. Vision AI already did the image analysis.
        system_instruction = (
            "You are an expert landscape architect. Your primary goal is to maximise plant density "
            "within the user's budget — treat the budget as a TARGET, not a ceiling. "
            "Keep adding plants and upgrading to higher-value specimens until total_cost is as "
            "close to the budget as possible WITHOUT going over. "
            "Aim for at least 75% of the budget. A design using only 30% is a failure.\n\n"
            "Generate ONE garden design layout of the style named in the user prompt. "
            "Layout the selected plants logically across the coordinate grid. "
            f"All plants must use integer grid indices: "
            f"gx in 0..{GRID_COLS-1} (column), gz in 0..{GRID_ROWS-1} (row). "
            "The backend expands these to real metres — never output float x/z coordinates. "
            "Rotation must be between 0.0 and 360.0 degrees. "
            "Total cost must not exceed the specified budget. "
            "Make the design distinct and creative.\n\n"
        )

        if excluded_cells:
            excl_str = ', '.join(f"({gx},{gz})" for gx, gz in sorted(excluded_cells))
            system_instruction += (
                f"OCCUPIED CELLS — DO NOT PLACE PLANTS HERE: {excl_str}\n"
                "These cells contain existing structures identified from the site photo. "
                "Place new plants only in cells NOT on this list.\n\n"
            )

        system_instruction += (
            "REASONING (MANDATORY): Fill 'reasoning' with 1-2 sentences explaining your choices — "
            "plant selection, layout, spacing, budget targeting, and any site constraints.\n\n"
            "COST BREAKDOWN (MANDATORY): Fill 'plant_breakdown' with itemised costs: "
            "plant_id, name, quantity, unit_price, subtotal. Sum of subtotals must equal total_cost.\n\n"
            "PLANT ID (MANDATORY): Every plant_id must be an exact integer ID string from the "
            "Provided Plant Catalog. Never invent IDs. Invented IDs are auto-discarded."
        )

        # Text summary of site context for the user prompt (no image bytes needed)
        photo_emphasis = ""
        if existing_elements:
            site_features = ', '.join(
                f"{e['label']} ({e['zone']})" for e in existing_elements
            )
            photo_emphasis = f"\nSite analysis from reference photo: {site_features}.\n"

        # Build text-based catalog for the prompt
        catalog_text = ""
        for plant in available_plants:
            desc = f" ({plant.description})" if plant.description else ""
            catalog_text += f"- ID: '{plant.id}', Name: '{plant.name}', Cost: {int(plant.unit_price)} PHP{desc}\n"

        user_prompt = f"""
Provided Plant Catalog:
{catalog_text}

User Budget & Lot Constraints:
Plant & Material Budget: ₱{plant_budget_int} (spend between ₱{plant_budget_floor} and ₱{plant_budget_int})
Grid layout: {GRID_COLS}×{GRID_ROWS} cells spanning {lot_width_float}m × {lot_length_float}m
  gx 0..{GRID_COLS-1} → x 0..{lot_width_float}m | gz 0..{GRID_ROWS-1} → z 0..{lot_length_float}m
Preferred Plant IDs (prioritize these): {json.dumps(preferred_plant_ids)}
{photo_emphasis}
BUDGET RULE: The design's total_cost MUST be between ₱{plant_budget_floor} and ₱{plant_budget_int}.
Maximise plant count and use higher-value specimens to reach this range.
The plant_id field in your response for each plant MUST be the database ID (integer string) of the chosen plant from the provided catalog.
Generate a single complete garden design layout.
"""
        # 5. Cache check — skip for image requests (site-specific, not cacheable)
        cache_key = None
        if not image_file:
            cache_key = _make_cache_key(budget_int, lot_width_float, lot_length_float, preferred_plant_ids)
            cached_data = _get_cached(cache_key)
            if cached_data is not None:
                logger.info("Returning cached layout (key=%s)", cache_key)
                # Attach the current budget_warning (lot/budget check is instant — not cached)
                cached_data["budget_warning"] = _budget_warning
                return Response(cached_data, status=status.HTTP_200_OK)

        # 6. Fire 3 parallel single-design calls via thread pool
        designs_raw: list = []
        call_errors: list = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            future_map = {
                pool.submit(
                    _call_single_design, client, style,
                    system_instruction, user_prompt, image_part
                ): style
                for style in _DESIGN_STYLES
            }
            for future in as_completed(future_map):
                style = future_map[future]
                try:
                    designs_raw.append(future.result())
                except Exception as exc:
                    logger.error("Design '%s' generation failed: %s", style, exc)
                    call_errors.append(str(exc))

        if not designs_raw:
            joined = ' '.join(call_errors)
            if '503' in joined or 'UNAVAILABLE' in joined:
                msg = "The AI service is temporarily overloaded. Please wait a moment and try again."
            elif '429' in joined:
                msg = "Too many requests to the AI service. Please wait a moment and try again."
            else:
                msg = "The AI service returned an error. Please try again."
            logger.error("All parallel Gemini calls failed: %s", joined)
            return Response({"error": msg}, status=status.HTTP_502_BAD_GATEWAY)

        response_data = {"designs": designs_raw, "budget_warning": _budget_warning}

        # 7. Validate plant IDs — strip any the AI invented that aren't in our catalog
        if isinstance(response_data, dict) and "designs" in response_data:
            for design in response_data["designs"]:
                raw_plants = design.get("plants") if isinstance(design.get("plants"), list) else []
                validated = [p for p in raw_plants if str(p.get("plant_id", "")) in catalog_ids]
                # Expand compressed gx/gz grid indices → real-world x/z metres
                design["plants"] = _expand_grid_coords(validated, lot_width_float, lot_length_float)
                recalculated_cost = sum(
                    plant_price_map.get(str(p.get("plant_id", "")), 0)
                    for p in validated
                )
                plant_cost = recalculated_cost
                full_cost = round(plant_cost / 0.45) if plant_cost > 0 else 0
                design["plant_cost"] = plant_cost
                design["total_cost"] = full_cost
                design["cost_breakdown"] = {
                    "plants": plant_cost,
                    "labor": round(full_cost * 0.35),
                    "service": round(full_cost * 0.20),
                    "total": full_cost,
                }

                # Reconstruct and ensure 100% correct plant_breakdown matching the validated plants
                from collections import Counter
                plant_counts = Counter(str(p.get("plant_id", "")) for p in validated if not p.get("is_existing"))
                
                recalculated_breakdown = []
                for pid, qty in plant_counts.items():
                    db_item = next((item for item in available_plants if str(item.id) == pid), None)
                    if db_item:
                        price = int(db_item.unit_price)
                        recalculated_breakdown.append({
                            "plant_id": pid,
                            "name": db_item.name,
                            "quantity": qty,
                            "unit_price": price,
                            "subtotal": price * qty
                        })
                design["plant_breakdown"] = recalculated_breakdown
                # Flag designs where plant budget utilisation is under 50%
                if recalculated_cost < plant_budget_int * 0.5:
                    design["density_note"] = "increase plant density"
                    logger.warning(
                        "Design '%s' uses only %.0f%% of plant budget (₱%.0f / ₱%d). Flagged for low density.",
                        design.get("design_name", "?"),
                        recalculated_cost / plant_budget_int * 100 if plant_budget_int else 0,
                        recalculated_cost, plant_budget_int
                    )

        # Merge existing elements into each design's plant list
        if isinstance(response_data, dict) and "designs" in response_data:
            for design in response_data["designs"]:
                if "plants" not in design or not isinstance(design["plants"], list):
                    design["plants"] = []

                for elem in existing_elements:
                    merged_item = dict(elem)
                    # Ensure plant_id exists so the frontend renderer never errors
                    if "plant_id" not in merged_item:
                        merged_item["plant_id"] = (
                            merged_item.get("type")
                            or merged_item.get("zone")
                            or "existing_element"
                        )
                    merged_item["is_existing"] = True
                    if "rotation" not in merged_item:
                        merged_item["rotation"] = 0.0
                    design["plants"].append(merged_item)

        # Write to cache for image-free requests before returning
        if cache_key is not None:
            _set_cached(cache_key, response_data)

        return Response(response_data, status=status.HTTP_200_OK)


class ServiceReviewViewSet(viewsets.ModelViewSet):
    serializer_class = ServiceReviewSerializer
    http_method_names = ['get', 'post', 'head', 'options']

    def get_permissions(self):
        if self.action == 'public':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return ServiceReview.objects.none()
        if user.is_staff or user.is_superuser:
            return ServiceReview.objects.select_related('booking', 'user').all()
        return ServiceReview.objects.filter(user=user).select_related('booking')

    @action(detail=False, methods=['get'], permission_classes=[AllowAny])
    def public(self, request):
        reviews = (
            ServiceReview.objects
            .filter(is_public=True)
            .select_related('booking', 'user')
            .prefetch_related('booking__assigned_crew')
            .order_by('-created_at')[:12]
        )
        serializer = self.get_serializer(reviews, many=True)
        return Response(serializer.data)


# ─────────────────────────────────────────────────────────────────────────────
# Project Tracker endpoints
# ─────────────────────────────────────────────────────────────────────────────

class ProjectTrackerViewSet(viewsets.ModelViewSet):
    """
    GET    /api/projects/                           List all trackers
                                                    (staff/admin → all;
                                                     customer → own bookings only)
    GET    /api/projects/<id>/                      Full detail with history & media
    POST   /api/projects/                           Create tracker  [admin/staff]
    PATCH  /api/projects/<id>/                      Edit fields     [admin/staff]
    PATCH  /api/projects/<id>/update-status/        Advance pipeline status,
                                                    auto-log & recompute %  [admin/staff]
    POST   /api/projects/<id>/upload-media/         Attach a progress photo  [staff]
    """
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.action == 'list':
            return ProjectTrackerListSerializer
        return ProjectTrackerSerializer

    def get_queryset(self):
        user = self.request.user
        qs = ProjectTracker.objects.select_related(
            'booking', 'booking__user', 'supervisor',
        ).prefetch_related(
            'history_logs',
            'history_logs__trigger_user',
            'progress_media',
            'progress_media__uploader',
        )
        if user.is_staff or user.is_superuser:
            return qs.all()
        # Customers see only the tracker linked to their own bookings
        return qs.filter(booking__user=user)

    def get_permissions(self):
        # Write operations (create / edit / status advance) require admin/staff
        if self.action in ('create', 'update', 'partial_update', 'destroy', 'update_status'):
            return [IsAuthenticated(), IsOfficeAdmin()]
        return [IsAuthenticated()]

    def perform_create(self, serializer):
        tracker = serializer.save()
        ProjectHistoryLog.objects.create(
            project=tracker,
            status_reached=tracker.status,
            trigger_user=self.request.user,
            remarks='Project tracker initialized.',
        )

    # ── GET /api/projects/supervisor-options/ ────────────────────────────────
    @action(detail=False, methods=['get'], url_path='supervisor-options')
    def supervisor_options(self, request):
        crew = User.objects.filter(
            staff_profile__role='FIELD_CREW',
            is_active=True,
        ).select_related('staff_profile').order_by('first_name', 'username')
        return Response([
            {
                'id': u.id,
                'name': f"{u.first_name} {u.last_name}".strip() or u.username,
            }
            for u in crew
        ])

    # ── PATCH /api/projects/<id>/update-status/ ──────────────────────────────
    @action(detail=True, methods=['patch'], url_path='update-status')
    def update_status(self, request, pk=None):
        tracker = self.get_object()

        new_status = (request.data.get('status') or '').strip()
        remarks = (request.data.get('remarks') or '').strip()

        valid_statuses = dict(ProjectTracker.STATUS_CHOICES)
        if not new_status or new_status not in valid_statuses:
            return Response(
                {
                    'error': 'Invalid or missing status.',
                    'valid_choices': list(valid_statuses.keys()),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        previous_status = tracker.status
        tracker.status = new_status
        tracker.save()  # progress_percentage auto-recomputed inside save()

        # Append an immutable audit log entry for this transition
        ProjectHistoryLog.objects.create(
            project=tracker,
            status_reached=new_status,
            trigger_user=request.user,
            remarks=remarks,
        )

        logger.info(
            "ProjectTracker #%s: %s → %s by user #%s",
            tracker.pk, previous_status, new_status, request.user.pk,
        )

        serializer = ProjectTrackerSerializer(
            tracker, context={'request': request}
        )
        return Response(serializer.data)

    # ── POST /api/projects/<id>/upload-media/ ────────────────────────────────
    @action(detail=True, methods=['post'], url_path='upload-media')
    def upload_media(self, request, pk=None):
        # Field crew and admin can both upload photos; customers cannot
        if not (request.user.is_staff or request.user.is_superuser):
            return Response(
                {'error': 'Staff access required to upload progress media.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        tracker = self.get_object()

        construction_phase = (
            request.data.get('construction_phase') or ''
        ).strip().upper()
        description = (request.data.get('description') or '').strip()
        location = (request.data.get('location') or '').strip()
        file_obj = request.FILES.get('file')

        if construction_phase not in ('BEFORE', 'DURING', 'AFTER'):
            return Response(
                {'error': "construction_phase must be 'BEFORE', 'DURING', or 'AFTER'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not file_obj:
            return Response(
                {'error': 'A file upload is required (multipart field name: file).'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        media = ProjectProgressMedia.objects.create(
            project=tracker,
            construction_phase=construction_phase,
            file_path=file_obj,
            uploader=request.user,
            description=description,
            location=location,
        )

        serializer = ProjectProgressMediaSerializer(
            media, context={'request': request}
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)
