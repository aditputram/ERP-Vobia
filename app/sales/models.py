import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
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


class SalesPlanningScenario(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        APPROVED = "APPROVED", "Approved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=180)
    start_month = models.DateField()
    end_month = models.DateField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_sales_planning_scenarios",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="approved_sales_planning_scenarios",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)

    def clean(self):
        super().clean()
        if self.start_month.day != 1 or self.end_month.day != 1:
            raise ValidationError("Periode Scenario harus memakai tanggal pertama setiap bulan.")
        if self.end_month < self.start_month:
            raise ValidationError({"end_month": "Selesai tidak boleh sebelum Mulai."})

    def __str__(self):
        return self.name


class SalesPlan(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scenario = models.ForeignKey(
        SalesPlanningScenario,
        on_delete=models.PROTECT,
        related_name="projections",
    )
    month = models.DateField()
    product = models.ForeignKey(
        "master_data.Product",
        on_delete=models.PROTECT,
        related_name="sales_plans",
    )
    gross_sales_target = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    quantity_target = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("scenario", "month", "product__name")
        constraints = [
            models.UniqueConstraint(fields=["scenario", "month", "product"], name="sales_unique_scenario_month_product_plan"),
            models.CheckConstraint(condition=Q(gross_sales_target__gte=0), name="sales_plan_gross_nonnegative"),
        ]
        permissions = [("approve_sales_plan", "Can approve monthly sales plan")]

    def clean(self):
        super().clean()
        if self.month.day != 1:
            raise ValidationError({"month": "Bulan planning harus memakai tanggal pertama."})
        if self.scenario_id and not self.scenario.start_month <= self.month <= self.scenario.end_month:
            raise ValidationError({"month": "Bulan projection harus berada dalam periode Scenario."})

    def __str__(self):
        return f"{self.scenario} · {self.month:%b %Y} · {self.product}"


class SalesPlanSKU(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(SalesPlan, on_delete=models.CASCADE, related_name="sku_targets")
    sku = models.ForeignKey(
        "master_data.SKU",
        on_delete=models.PROTECT,
        related_name="sales_plan_targets",
    )
    gross_sales_target = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    quantity_target = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("plan", "sku__sku")
        constraints = [
            models.UniqueConstraint(fields=["plan", "sku"], name="sales_unique_plan_sku_target"),
            models.CheckConstraint(condition=Q(gross_sales_target__gte=0), name="sales_plan_sku_gross_nonnegative"),
        ]

    def clean(self):
        super().clean()
        if self.plan_id and self.sku_id and self.plan.product_id != self.sku.product_variant.product_id:
            raise ValidationError({"sku": "SKU harus berada di dalam Product Sales Plan."})

    def __str__(self):
        return f"{self.plan} · {self.sku}"


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
