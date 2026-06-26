from django.db import models
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
    depth_data = models.JSONField(default=dict, blank=True,
                                  help_text="Stores depth/normal/mask URLs")
    placed_items = models.JSONField(default=list,
                                    help_text="[{product_id, position, rotation, scale}]")
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


class ServiceBooking(models.Model):
    SERVICE_CHOICES = [
        ('maintenance', 'Maintenance'),
        ('consultation', 'Consultation'),
        ('hardscaping', 'Full Hardscaping'),
    ]
    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Confirmed', 'Confirmed'),
        ('In Progress', 'In Progress'),
        ('Completed', 'Completed'),
        ('Cancelled', 'Cancelled'),
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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-scheduled_date']

    def __str__(self):
        return f"{self.service_type} for {self.user.username} on {self.scheduled_date}"


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
