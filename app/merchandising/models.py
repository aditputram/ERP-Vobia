import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class ProjectionScenario(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        REVISION_DRAFT = "REVISION_DRAFT", "Revision Draft"
        APPROVED = "APPROVED", "Approved"
        SUPERSEDED = "SUPERSEDED", "Superseded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=180)
    start_month = models.DateField()
    end_month = models.DateField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_projection_scenarios",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="approved_projection_scenarios",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    superseded_by = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="superseded_scenarios",
    )

    class Meta:
        ordering = ("-created_at",)

    @property
    def quantities_editable(self):
        return self.status in {self.Status.DRAFT, self.Status.REVISION_DRAFT}

    def clean(self):
        super().clean()
        if self.start_month.day != 1 or self.end_month.day != 1:
            raise ValidationError("Periode scenario harus memakai tanggal pertama setiap bulan.")
        if self.end_month < self.start_month:
            raise ValidationError({"end_month": "End month tidak boleh sebelum start month."})

    def __str__(self):
        return self.name


class ProjectionRule(models.Model):
    class ScopeType(models.TextChoices):
        ALL_PRODUCTS = "ALL_PRODUCTS", "All Products"
        PRODUCT_STATUS = "PRODUCT_STATUS", "Product Status"
        CATEGORY = "CATEGORY", "Category"
        PRODUCT = "PRODUCT", "Product"

    class Method(models.TextChoices):
        INCREASE_PERCENT = "INCREASE_PERCENT", "Increase by %"
        DECREASE_PERCENT = "DECREASE_PERCENT", "Decrease by %"
        SAME_AS_LAST_MONTH = "SAME_AS_LAST_MONTH", "Sama dengan Bulan Lalu"
        TARGET_STOCK_RATIO = "TARGET_STOCK_RATIO", "Target Stock Ratio"
        SELL_OUT_ENDING_MONTHS = "SELL_OUT_ENDING_MONTHS", "Ending Stock Habis dalam X Bulan"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scenario = models.ForeignKey(ProjectionScenario, on_delete=models.PROTECT, related_name="rules")
    target_month = models.DateField()
    scope_type = models.CharField(max_length=30, choices=ScopeType.choices)
    product_status = models.ForeignKey(
        "master_data.ProductStatus",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="projection_rules",
    )
    category = models.ForeignKey(
        "master_data.Category",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="projection_rules",
    )
    product = models.ForeignKey(
        "master_data.Product",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="projection_rules",
    )
    method = models.CharField(max_length=30, choices=Method.choices)
    parameter = models.DecimalField(max_digits=12, decimal_places=4)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ("target_month", "scope_type", "created_at")
        indexes = [models.Index(fields=["scenario", "target_month", "scope_type"])]

    @property
    def priority(self):
        return {
            self.ScopeType.ALL_PRODUCTS: 0,
            self.ScopeType.PRODUCT_STATUS: 100,
            self.ScopeType.CATEGORY: 200,
            self.ScopeType.PRODUCT: 300,
        }[self.scope_type]

    def clean(self):
        super().clean()
        if self.target_month.day != 1:
            raise ValidationError({"target_month": "Target month harus tanggal pertama bulan."})
        selected = {
            self.ScopeType.PRODUCT_STATUS: self.product_status_id,
            self.ScopeType.CATEGORY: self.category_id,
            self.ScopeType.PRODUCT: self.product_id,
        }
        selected_count = sum(bool(value) for value in selected.values())
        if self.scope_type == self.ScopeType.ALL_PRODUCTS:
            if selected_count:
                raise ValidationError("Scope All Products tidak boleh memakai filter khusus.")
        elif not selected.get(self.scope_type) or selected_count != 1:
            raise ValidationError("Pilih tepat satu scope yang sesuai Scope Type.")
        if self.parameter < 0:
            raise ValidationError({"parameter": "Parameter tidak boleh negatif."})
        if self.method == self.Method.TARGET_STOCK_RATIO and self.parameter <= 0:
            raise ValidationError({"parameter": "Target Stock Ratio harus lebih besar dari nol."})
        if self.method == self.Method.SELL_OUT_ENDING_MONTHS:
            if self.parameter <= 0 or self.parameter != self.parameter.to_integral_value():
                raise ValidationError({"parameter": "Jumlah bulan harus bilangan bulat lebih dari nol."})

    def __str__(self):
        return f"{self.scenario} · {self.target_month:%b %Y} · {self.get_method_display()}"


class SalesProjection(models.Model):
    class ApprovalStatus(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        APPROVED = "APPROVED", "Approved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scenario = models.ForeignKey(ProjectionScenario, on_delete=models.PROTECT, related_name="projections")
    month = models.DateField()
    sku = models.ForeignKey("master_data.SKU", on_delete=models.PROTECT, related_name="sales_projections")
    applied_rule = models.ForeignKey(
        ProjectionRule,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="projection_results",
    )
    baseline_month = models.DateField(null=True, blank=True)
    baseline_qty = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    beginning_qty = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    cogs_snapshot = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    retail_price_snapshot = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    net_rate_snapshot = models.DecimalField(max_digits=6, decimal_places=4, default=Decimal("0.97"))
    system_recommendation = models.DecimalField(max_digits=18, decimal_places=4)
    adit_adjustment = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    final_approved_qty = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    approval_status = models.CharField(
        max_length=20,
        choices=ApprovalStatus.choices,
        default=ApprovalStatus.DRAFT,
    )
    explanation = models.TextField(blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="approved_sales_projections",
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("month", "sku__sku")
        constraints = [
            models.UniqueConstraint(
                fields=["scenario", "month", "sku"],
                name="merch_unique_scenario_month_sku_projection",
            ),
            models.CheckConstraint(
                condition=Q(system_recommendation__gte=0),
                name="merch_projection_recommendation_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(cogs_snapshot__gte=0),
                name="merch_projection_cogs_snapshot_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(retail_price_snapshot__gte=0),
                name="merch_projection_retail_snapshot_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(net_rate_snapshot__gte=0) & Q(net_rate_snapshot__lte=1),
                name="merch_projection_net_rate_snapshot_range",
            ),
        ]

    def clean(self):
        super().clean()
        if self.month.day != 1:
            raise ValidationError({"month": "Month harus tanggal pertama bulan."})
        if self.final_approved_qty is not None:
            if self.final_approved_qty < 0:
                raise ValidationError({"final_approved_qty": "Final qty tidak boleh negatif."})
            if self.final_approved_qty != self.final_approved_qty.to_integral_value():
                raise ValidationError({"final_approved_qty": "Final approved Sales Qty harus bilangan bulat."})

    @property
    def proposed_qty(self):
        return self.system_recommendation + (self.adit_adjustment or Decimal("0"))

    def __str__(self):
        return f"{self.month:%b %Y} · {self.sku.sku}"


class IncomingPlan(models.Model):
    class ApprovalStatus(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        APPROVED = "APPROVED", "Approved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scenario = models.ForeignKey(ProjectionScenario, on_delete=models.PROTECT, related_name="incoming_plans")
    month = models.DateField()
    sku = models.ForeignKey("master_data.SKU", on_delete=models.PROTECT, related_name="incoming_plans")
    sales_projection = models.ForeignKey(
        SalesProjection,
        on_delete=models.PROTECT,
        related_name="incoming_plans",
    )
    prior_ending_qty = models.DecimalField(max_digits=18, decimal_places=4)
    minimum_incoming = models.DecimalField(max_digits=18, decimal_places=4)
    target_stock_ratio = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    recommended_incoming = models.DecimalField(max_digits=18, decimal_places=4)
    adit_adjustment = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    final_approved_incoming = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    approval_status = models.CharField(
        max_length=20,
        choices=ApprovalStatus.choices,
        default=ApprovalStatus.DRAFT,
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="approved_incoming_plans",
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("month", "sku__sku")
        constraints = [
            models.UniqueConstraint(
                fields=["scenario", "month", "sku"],
                name="merch_unique_scenario_month_sku_incoming",
            ),
            models.CheckConstraint(condition=Q(minimum_incoming__gte=0), name="merch_minimum_incoming_nonnegative"),
            models.CheckConstraint(condition=Q(recommended_incoming__gte=0), name="merch_recommended_incoming_nonnegative"),
        ]

    def clean(self):
        super().clean()
        if self.final_approved_incoming is not None:
            if self.final_approved_incoming < self.minimum_incoming:
                raise ValidationError(
                    {"final_approved_incoming": "Final Incoming tidak boleh di bawah Minimum Incoming."}
                )
            if self.final_approved_incoming != self.final_approved_incoming.to_integral_value():
                raise ValidationError(
                    {"final_approved_incoming": "Final Approved Incoming harus bilangan bulat."}
                )

    @property
    def proposed_incoming(self):
        return self.recommended_incoming + (self.adit_adjustment or Decimal("0"))

    def __str__(self):
        return f"Incoming {self.month:%b %Y} · {self.sku.sku}"


class IncomingMonthClose(models.Model):
    """Immutable monthly bridge from Warehouse actual inbound to Merchandising."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    month = models.DateField(unique=True)
    cutoff_date = models.DateField()
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="closed_incoming_months",
    )
    closed_at = models.DateTimeField(auto_now_add=True)
    evidence_reference = models.CharField(max_length=240)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ("-month",)

    def clean(self):
        super().clean()
        if self.month.day != 1:
            raise ValidationError({"month": "Month close wajib memakai tanggal pertama bulan."})
        if self.cutoff_date.year != self.month.year or self.cutoff_date.month != self.month.month:
            raise ValidationError({"cutoff_date": "Cutoff harus berada pada bulan yang ditutup."})
        if not self.evidence_reference.strip():
            raise ValidationError({"evidence_reference": "Referensi bukti month close wajib diisi."})


class IncomingMonthlyActual(models.Model):
    """Frozen planning-versus-actual cost bridge per SKU at month close."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    month_close = models.ForeignKey(IncomingMonthClose, on_delete=models.PROTECT, related_name="actual_rows")
    sku = models.ForeignKey("master_data.SKU", on_delete=models.PROTECT, related_name="incoming_monthly_actuals")
    projected_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    projected_cogs = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    projected_ending_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    projected_ending_cogs = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    actual_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    actual_cogs = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    actual_gross = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    actual_ending_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    actual_ending_cogs = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    variance_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    class Meta:
        ordering = ("month_close__month", "sku__sku")
        constraints = [
            models.UniqueConstraint(fields=["month_close", "sku"], name="merch_unique_close_sku_actual")
        ]


class IncomingCarryover(models.Model):
    """Unreceived PO-backed incoming moved once to the following month."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_close = models.ForeignKey(IncomingMonthClose, on_delete=models.PROTECT, related_name="carryovers")
    target_month = models.DateField()
    po_line = models.ForeignKey("purchasing.PurchaseOrderLine", on_delete=models.PROTECT, related_name="incoming_carryovers")
    sku = models.ForeignKey("master_data.SKU", on_delete=models.PROTECT, related_name="incoming_carryovers")
    carryover_qty = models.DecimalField(max_digits=18, decimal_places=4)
    reason = models.CharField(max_length=240, default="Outstanding PO at incoming month close")

    class Meta:
        ordering = ("target_month", "sku__sku", "po_line__po__required_arrival")
        constraints = [
            models.UniqueConstraint(fields=["source_close", "po_line"], name="merch_unique_close_po_line_carryover"),
            models.CheckConstraint(condition=Q(carryover_qty__gt=0), name="merch_carryover_qty_positive"),
        ]

    def clean(self):
        super().clean()
        if self.target_month.day != 1:
            raise ValidationError({"target_month": "Target carry-over wajib tanggal pertama bulan."})
        if self.sku_id and self.po_line_id and self.sku_id != self.po_line.sku_id:
            raise ValidationError("SKU carry-over wajib sama dengan SKU line PO.")


class MerchandisingSnapshotBatch(models.Model):
    """Immutable baseline imported from the operational Vobia MD workbook."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_workbook_id = models.CharField(max_length=160)
    source_file_name = models.CharField(max_length=255)
    source_sha256 = models.CharField(max_length=64, unique=True)
    source_modified_at = models.DateTimeField(null=True, blank=True)
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="imported_merchandising_snapshots",
    )
    imported_at = models.DateTimeField(auto_now_add=True)
    row_count = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("-imported_at",)

    def __str__(self):
        return f"{self.source_file_name} · {self.imported_at:%Y-%m-%d %H:%M}"


class MerchandisingMonthlySnapshot(models.Model):
    """One immutable MD Actual row per SKU and calendar month."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(
        MerchandisingSnapshotBatch,
        on_delete=models.PROTECT,
        related_name="monthly_rows",
    )
    sku = models.ForeignKey(
        "master_data.SKU",
        on_delete=models.PROTECT,
        related_name="merchandising_snapshots",
    )
    source_row = models.PositiveIntegerField()
    month = models.DateField()

    status_snapshot = models.CharField(max_length=120)
    product_snapshot = models.CharField(max_length=255)
    variant_snapshot = models.CharField(max_length=180, blank=True)
    category_snapshot = models.CharField(max_length=160)
    subcategory_snapshot = models.CharField(max_length=160, blank=True)
    size_snapshot = models.CharField(max_length=100, blank=True)
    cogs_snapshot = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    retail_price_snapshot = models.DecimalField(max_digits=22, decimal_places=4, default=0)

    prior_year_ending_qty = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    prior_year_ending_cogs = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    prior_year_ending_gross = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    incoming_qty = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    incoming_cogs = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    incoming_gross = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    beginning_qty = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    beginning_cogs = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    beginning_gross = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    sales_qty = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    sales_cogs = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    sales_gross = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    sales_net = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    ratio = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    ending_qty = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    ending_cogs = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    ending_gross = models.DecimalField(max_digits=22, decimal_places=4, default=0)
    mos = models.DecimalField(max_digits=22, decimal_places=4, null=True, blank=True)

    class Meta:
        ordering = ("month", "source_row")
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "sku", "month"],
                name="merch_unique_snapshot_batch_sku_month",
            )
        ]
        indexes = [
            models.Index(fields=["batch", "month"], name="merchandis_batch_i_75d13a_idx"),
            models.Index(fields=["batch", "status_snapshot"], name="merchandis_batch_i_674304_idx"),
            models.Index(fields=["batch", "category_snapshot"], name="merchandis_batch_i_9c836f_idx"),
            models.Index(fields=["batch", "product_snapshot"], name="merchandis_batch_i_6d2217_idx"),
        ]

    def clean(self):
        super().clean()
        if self.month.day != 1:
            raise ValidationError({"month": "Snapshot month harus tanggal pertama bulan."})

    def __str__(self):
        return f"{self.month:%b %Y} · {self.sku.sku}"
