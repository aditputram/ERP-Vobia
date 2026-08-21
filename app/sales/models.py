import uuid

from django.db import models
from django.db.models import Q


class SalesOrder(models.Model):
    class ImportOrigin(models.TextChoices):
        OPERATIONAL = "OPERATIONAL", "Operational import"
        HISTORICAL = "HISTORICAL", "Historical migration"
        MANUAL = "MANUAL", "Manual entry"

    class Source(models.TextChoices):
        SHOPEE = "Shopee", "Shopee"
        TIKTOK = "Tiktok", "TikTok"
        OTHER = "Other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.CharField(max_length=20, choices=Source.choices)
    source_label = models.CharField(max_length=80, blank=True, db_index=True)
    order_number = models.CharField(max_length=160)
    order_datetime = models.DateTimeField()
    shipped_datetime = models.DateTimeField(null=True, blank=True)
    order_date = models.DateField()
    current_status = models.CharField(max_length=180)
    source_status = models.CharField(max_length=255)
    is_final = models.BooleanField(default=False)
    is_pure_cancelled = models.BooleanField(default=False)
    import_origin = models.CharField(
        max_length=20,
        choices=ImportOrigin.choices,
        default=ImportOrigin.OPERATIONAL,
    )
    affects_inventory = models.BooleanField(
        default=True,
        help_text="False untuk histori pra-cutover yang sudah terserap dalam FIFO Opening.",
    )
    first_seen_batch_id = models.UUIDField()
    latest_batch_id = models.UUIDField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-order_datetime", "source", "order_number")
        constraints = [
            models.UniqueConstraint(
                fields=["source_label", "order_number"],
                name="sales_unique_source_label_order",
            )
        ]
        indexes = [
            models.Index(fields=["source", "current_status"]),
            models.Index(fields=["order_date", "source"]),
        ]

    def __str__(self):
        return f"{self.display_source} · {self.order_number}"

    @property
    def display_source(self):
        return self.source_label or self.source


class SalesOrderLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(SalesOrder, on_delete=models.PROTECT, related_name="lines")
    sku = models.ForeignKey(
        "master_data.SKU",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="sales_lines",
    )
    sku_code_snapshot = models.CharField(max_length=100, blank=True, default="", db_index=True)
    product_status_snapshot = models.CharField(max_length=100, blank=True)
    category_snapshot = models.CharField(max_length=100, blank=True, db_index=True)
    subcategory_snapshot = models.CharField(max_length=100, blank=True)
    product_name_snapshot = models.CharField(max_length=255, blank=True, db_index=True)
    variant_name_snapshot = models.CharField(max_length=150, blank=True)
    quantity = models.PositiveIntegerField()
    net_unit_price = models.DecimalField(max_digits=18, decimal_places=4)
    retail_price_snapshot = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    sales_cogs_snapshot = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    total_gross_sales = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    total_net_sales = models.DecimalField(max_digits=20, decimal_places=4)
    total_cogs = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    gpm = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    gpm_rate = models.DecimalField(max_digits=18, decimal_places=8, null=True, blank=True)
    is_counted = models.BooleanField(default=True)
    committed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("order__order_datetime", "order__order_number", "sku__sku")
        constraints = [
            models.UniqueConstraint(
                fields=["order", "sku_code_snapshot"],
                name="sales_unique_order_sku_snapshot_line",
            ),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="sales_line_quantity_positive"),
            models.CheckConstraint(condition=Q(net_unit_price__gte=0), name="sales_line_net_price_nonnegative"),
            models.CheckConstraint(
                condition=Q(retail_price_snapshot__isnull=True)
                | Q(net_unit_price__lte=models.F("retail_price_snapshot")),
                name="sales_line_net_not_above_retail",
            ),
        ]

    @property
    def business_key(self):
        return f"{self.order.display_source}|{self.order.order_number}|{self.sku_code_snapshot}"

    def save(self, *args, **kwargs):
        if not self.sku_code_snapshot and self.sku_id:
            self.sku_code_snapshot = self.sku.sku
        super().save(*args, **kwargs)

    def __str__(self):
        return self.business_key


class SalesStatusHistory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(SalesOrder, on_delete=models.PROTECT, related_name="status_history")
    previous_status = models.CharField(max_length=180, blank=True)
    normalized_status = models.CharField(max_length=180)
    source_status = models.CharField(max_length=255)
    observed_at = models.DateTimeField(auto_now_add=True)
    import_batch_id = models.UUIDField()
    changed_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        related_name="sales_status_changes",
    )

    class Meta:
        ordering = ("-observed_at",)
        indexes = [models.Index(fields=["order", "observed_at"])]

    def __str__(self):
        return f"{self.order} · {self.normalized_status}"
