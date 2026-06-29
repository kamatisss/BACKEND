from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.contrib.auth.models import User


class InventoryItem(models.Model):
    CATEGORY_CHOICES = [
        ('plant', 'Plant'),
        ('hardscape', 'Hardscape'),
        ('furniture', 'Furniture'),
    ]
    name = models.CharField(max_length=100)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='plant')
    description = models.TextField(blank=True)
    stock_quantity = models.IntegerField(default=0)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    image_url = models.CharField(max_length=500, blank=True)
    model_file = models.FileField(upload_to='models/', null=True, blank=True)
    real_world_size = models.FloatField(default=1.0, help_text="Target size/height of the model in meters")
    spacing_cm = models.IntegerField(
        null=True, blank=True,
        help_text="Recommended on-center planting spacing in centimetres (e.g. 45 = 45 cm OC)"
    )
    thumbnail = models.ImageField(upload_to='thumbnails/', null=True, blank=True)

    class Meta:
        ordering = ['category', 'name']

    def __str__(self):
        return f"{self.name} (₱{self.unit_price})"


class GardenImage(models.Model):
    image = models.ImageField(upload_to='gardens/')
    depth_map = models.ImageField(upload_to='depth/', null=True, blank=True)
    normal_map_url = models.CharField(max_length=255, blank=True)
    rock_mask_url = models.CharField(max_length=255, blank=True)
    grass_mask_url = models.CharField(max_length=255, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"GardenImage #{self.pk} ({self.uploaded_at:%Y-%m-%d})"


class GardenDesign(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    name = models.CharField(max_length=100, default='Untitled Design')
    garden_image = models.ForeignKey(GardenImage, on_delete=models.SET_NULL, null=True, blank=True)
    original_image_url = models.CharField(max_length=500, blank=True)
    reference_image_url = models.CharField(
        max_length=1000, blank=True, default='',
        help_text="Garden background photo URL used for AR overlay in staff preview"
    )
    depth_data = models.JSONField(default=dict, blank=True,
                                  help_text="Stores depth/normal/mask URLs")
    placed_items = models.JSONField(default=list,
                                    help_text="[{product_id, position, rotation, scale}]")
    plant_breakdown = models.JSONField(
        default=list, blank=True,
        help_text="[{plant_id, name, quantity, unit_price, subtotal}] — AI-generated procurement list"
    )
    dimensions = models.JSONField(default=dict, blank=True,
                                  help_text="{width, length, terrain_type}")
    total_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    terrain_height = models.FloatField(default=1.5)
    time_of_day = models.FloatField(default=14.0)
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ]
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"{self.name} ({self.created_at:%Y-%m-%d})"


class BlackoutDate(models.Model):
    date = models.DateField(unique=True)
    reason = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date']

    def __str__(self):
        return f"Blackout: {self.date}"


def _default_milestones():
    return [
        {"id": "site_prep",      "label": "Site Preparation",  "completed": False, "completed_at": None},
        {"id": "plant_sourcing", "label": "Plant Sourcing",     "completed": False, "completed_at": None},
        {"id": "installation",   "label": "Installation",       "completed": False, "completed_at": None},
        {"id": "cleanup",        "label": "Cleanup & Handover", "completed": False, "completed_at": None},
    ]


class ServiceBooking(models.Model):
    SERVICE_CHOICES = [
        ('maintenance', 'Maintenance'),
        ('consultation', 'Consultation'),
        ('hardscaping', 'Full Hardscaping'),
        ('Softscape', 'Softscape / Planting Services'),
    ]
    STATUS_CHOICES = [
        ('Pending',    'Pending'),
        ('Preparing',  'Preparing'),
        ('Installing', 'Installing'),
        ('Finished',   'Finished'),
        ('Cancelled',  'Cancelled'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    service_type = models.CharField(max_length=50, choices=SERVICE_CHOICES)
    scheduled_date = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending')
    contact_number = models.CharField(max_length=20, blank=True, default='')
    preferred_time = models.CharField(max_length=50, blank=True, default='')
    service_address = models.TextField(blank=True, default='')
    notes = models.TextField(blank=True)
    design = models.ForeignKey('GardenDesign', on_delete=models.SET_NULL, null=True, blank=True, related_name='bookings')

    # GPS coordinates of the project site (set by staff for geofenced check-in)
    site_lat = models.FloatField(null=True, blank=True, help_text="Project site GPS latitude")
    site_lng = models.FloatField(null=True, blank=True, help_text="Project site GPS longitude")

    # Multi-crew assignment for capacity planning and RBAC data filtering
    assigned_crew = models.ManyToManyField(
        User,
        blank=True,
        related_name='assigned_bookings',
        help_text="Field crew members assigned to this project (supports 1-to-many capacity planning)",
    )

    # Project management fields
    milestones = models.JSONField(
        default=_default_milestones, blank=True,
        help_text="[{id, label, completed, completed_at}]"
    )
    progress_pct = models.PositiveSmallIntegerField(
        default=0,
        help_text="Overall project progress 0-100 set manually by staff"
    )
    staff_notes = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-scheduled_date']

    def __str__(self):
        return f"{self.service_type} for {self.user.username} on {self.scheduled_date}"


class ProjectMilestone(models.Model):
    PHASE_CHOICES = [
        ('design_contract',  'Design Finalized & Contract Signed'),
        ('site_prep',        'Site Preparation Complete'),
        ('installation',     'Installation Service (Hardscape/Softscape)'),
        ('final_inspection', 'Final Inspection & Output Uploaded'),
        # Legacy phases kept for backward compatibility
        ('hardscaping',      'Hardscaping'),
        ('softscaping',      'Softscaping'),
        ('cleanup',          'Cleanup & Handover'),
    ]
    ACTIVE_PHASES = ['design_contract', 'site_prep', 'installation', 'final_inspection']

    booking = models.ForeignKey(ServiceBooking, on_delete=models.CASCADE, related_name='project_milestones')
    phase = models.CharField(max_length=20, choices=PHASE_CHOICES)
    completion_pct = models.PositiveSmallIntegerField(default=0)
    notes = models.TextField(blank=True, default='')
    proof_photo_url = models.CharField(
        max_length=500, blank=True, default='',
        help_text="Clock-out proof photo URL bound when phase is marked 100% complete"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('booking', 'phase')
        ordering = ['booking', 'phase']

    def __str__(self):
        return f"{self.get_phase_display()} for Booking #{self.booking_id} ({self.completion_pct}%)"


class StaffProfile(models.Model):
    ROLE_CHOICES = [
        ('OFFICE_ADMIN', 'Office Admin'),
        ('FIELD_CREW',   'Field Crew'),
    ]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='staff_profile')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='OFFICE_ADMIN')

    def __str__(self):
        return f"{self.user.username} — {self.get_role_display()}"


class StaffAttendance(models.Model):
    booking = models.ForeignKey(ServiceBooking, on_delete=models.CASCADE, related_name='staff_attendances')
    staff = models.ForeignKey(User, on_delete=models.CASCADE, related_name='field_attendances')
    timestamp_checkin = models.DateTimeField(null=True, blank=True)
    timestamp_checkout = models.DateTimeField(null=True, blank=True)
    gps_lat_checkin = models.FloatField(null=True, blank=True)
    gps_lng_checkin = models.FloatField(null=True, blank=True)
    distance_at_checkin_m = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ['-timestamp_checkin']

    def __str__(self):
        return f"StaffAttendance: {self.staff.username} at Booking #{self.booking_id}"


class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"Notification({self.user.username}): {self.message[:60]}"


class Order(models.Model):
    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Paid',    'Paid'),
        ('Shipped', 'Shipped'),
        ('Out for Delivery', 'Out for Delivery'),
        ('Delivered', 'Delivered'),
        ('Cancelled', 'Cancelled'),
    ]
    PAYMENT_METHOD_CHOICES = [
        ('stripe', 'Online Payment (Stripe)'),
        ('cod',    'Cash on Delivery'),
    ]
    user             = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    customer_name    = models.CharField(max_length=150)
    customer_email   = models.EmailField()
    customer_phone   = models.CharField(max_length=20, blank=True, default='')
    customer_address = models.TextField()
    booking_date     = models.DateField(null=True, blank=True)
    total_price      = models.DecimalField(max_digits=12, decimal_places=2)
    payment_method   = models.CharField(max_length=10, choices=PAYMENT_METHOD_CHOICES, default='stripe')
    status           = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending')
    payment_status   = models.CharField(max_length=20, default='Pending')
    created_at       = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Order #{self.pk} - {self.customer_name} [{self.status}]"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, related_name='items', on_delete=models.CASCADE)
    item = models.ForeignKey(InventoryItem, on_delete=models.SET_NULL, null=True)
    quantity = models.PositiveIntegerField(default=1)
    price_at_booking = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        item_name = self.item.name if self.item else "Deleted Item"
        return f"{self.quantity}x {item_name} (Order #{self.order.pk})"


class Attendance(models.Model):
    staff = models.ForeignKey(User, on_delete=models.CASCADE, related_name='attendances')
    booking = models.ForeignKey(ServiceBooking, on_delete=models.SET_NULL, null=True, blank=True, related_name='attendances')
    clock_in_time = models.DateTimeField(null=True, blank=True)
    clock_out_time = models.DateTimeField(null=True, blank=True)
    clock_in_photo_url = models.ImageField(upload_to='attendance_proofs/', null=True, blank=True)
    clock_out_photo_url = models.ImageField(upload_to='attendance_proofs/', null=True, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    clock_in_address = models.CharField(max_length=500, null=True, blank=True)
    clock_out_address = models.CharField(max_length=500, null=True, blank=True)

    class Meta:
        ordering = ['-clock_in_time']

    @property
    def total_hours(self):
        if self.clock_in_time and self.clock_out_time:
            duration = self.clock_out_time - self.clock_in_time
            return round(duration.total_seconds() / 3600.0, 2)
        return None

    def __str__(self):
        return f"Attendance for {self.staff.username} on {self.clock_in_time}"


# ─────────────────────────────────────────────────────────────────────────────
# Project Tracker pipeline
# ─────────────────────────────────────────────────────────────────────────────

STATUS_PROGRESS_MAP = {
    'PENDING':                  0,
    'CONSULTATION_SCHEDULED':   5,
    'SITE_INSPECTION':         10,
    'QUOTATION_APPROVED':      15,
    'DESIGN_PREPARATION':      20,
    'DESIGN_APPROVED':         25,
    'PROJECT_SCHEDULED':       30,
    'MATERIALS_PREPARATION':   40,
    'PROJECT_STARTED':         50,
    'SITE_PREPARATION':        60,
    'PLANTING':                70,
    'HARDSCAPE_INSTALLATION':  75,
    'LIGHTING_INSTALLATION':   85,
    'DECORATION':              90,
    'FINAL_INSPECTION':        95,
    'COMPLETED':              100,
    'CUSTOMER_FEEDBACK':      100,
}


class ProjectTracker(models.Model):
    STATUS_CHOICES = [
        ('PENDING',                'Pending'),
        ('CONSULTATION_SCHEDULED', 'Consultation Scheduled'),
        ('SITE_INSPECTION',        'Site Inspection'),
        ('QUOTATION_APPROVED',     'Quotation Approved'),
        ('DESIGN_PREPARATION',     'Design Preparation'),
        ('DESIGN_APPROVED',        'Design Approved'),
        ('PROJECT_SCHEDULED',      'Project Scheduled'),
        ('MATERIALS_PREPARATION',  'Materials Preparation'),
        ('PROJECT_STARTED',        'Project Started'),
        ('SITE_PREPARATION',       'Site Preparation'),
        ('PLANTING',               'Planting'),
        ('HARDSCAPE_INSTALLATION', 'Hardscape Installation'),
        ('LIGHTING_INSTALLATION',  'Lighting Installation'),
        ('DECORATION',             'Decoration'),
        ('FINAL_INSPECTION',       'Final Inspection'),
        ('COMPLETED',              'Completed'),
        ('CUSTOMER_FEEDBACK',      'Customer Feedback'),
    ]

    booking = models.OneToOneField(
        ServiceBooking, on_delete=models.CASCADE, related_name='tracker'
    )
    status = models.CharField(
        max_length=30, choices=STATUS_CHOICES, default='PENDING'
    )
    progress_percentage = models.PositiveSmallIntegerField(
        default=0,
        help_text='Auto-computed from status — do not set manually'
    )
    supervisor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='supervised_projects'
    )
    estimated_end_date = models.DateField(null=True, blank=True)
    total_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    remaining_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def save(self, *args, **kwargs):
        self.progress_percentage = STATUS_PROGRESS_MAP.get(self.status, 0)
        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"Tracker#{self.pk} — Booking#{self.booking_id} "
            f"[{self.status} {self.progress_percentage}%]"
        )


class ProjectHistoryLog(models.Model):
    """
    Immutable append-only audit trail for ProjectTracker pipeline transitions.
    Each status change creates exactly one log entry; updates are blocked at
    the model level via the save() override.
    """
    project = models.ForeignKey(
        ProjectTracker, on_delete=models.CASCADE, related_name='history_logs'
    )
    status_reached = models.CharField(
        max_length=30, choices=ProjectTracker.STATUS_CHOICES
    )
    timestamp = models.DateTimeField(auto_now_add=True)
    trigger_user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='triggered_project_logs'
    )
    remarks = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['-timestamp']

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError(
                "ProjectHistoryLog entries are immutable — updates are not permitted."
            )
        super().save(*args, **kwargs)

    def __str__(self):
        ts = self.timestamp.strftime('%Y-%m-%d %H:%M') if self.timestamp else '—'
        return f"Log#{self.pk} {self.status_reached} @ {ts}"


class ProjectProgressMedia(models.Model):
    PHASE_CHOICES = [
        ('BEFORE', 'Before'),
        ('DURING', 'During'),
        ('AFTER',  'After'),
    ]

    project = models.ForeignKey(
        ProjectTracker, on_delete=models.CASCADE, related_name='progress_media'
    )
    construction_phase = models.CharField(max_length=10, choices=PHASE_CHOICES)
    file_path = models.ImageField(upload_to='project_progress/%Y/%m/')
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploader = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='uploaded_progress_media'
    )
    description = models.TextField(blank=True, default='')
    location = models.CharField(max_length=255, blank=True, default='')

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return (
            f"Media#{self.pk} [{self.construction_phase}] "
            f"Tracker#{self.project_id}"
        )


class ServiceReview(models.Model):
    booking = models.OneToOneField(
        ServiceBooking, on_delete=models.CASCADE, related_name='review'
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reviews')
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    comment = models.TextField(blank=True, default='')
    is_public = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Review by {self.user.username} for Booking #{self.booking_id} ({self.rating}★)"
