import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class RawFile(models.Model):
    class DatasetType(models.TextChoices):
        MASTER_PRODUCT = "MASTER_PRODUCT", "Master Product"
        FIFO_OPENING = "FIFO_OPENING", "FIFO Opening"
        PO_WIP = "PO_WIP", "PO WIP Migration"
        SALES_SHOPEE = "SALES_SHOPEE", "Sales Shopee"
        SALES_TIKTOK = "SALES_TIKTOK", "Sales TikTok"
        SALES_HISTORICAL = "SALES_HISTORICAL", "Sales Historical"
        TRAFFIC_SHOPEE = "TRAFFIC_SHOPEE", "Traffic Shopee"
        TRAFFIC_TIKTOK = "TRAFFIC_TIKTOK", "Traffic TikTok"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dataset_type = models.CharField(max_length=40, choices=DatasetType.choices)
    original_filename = models.CharField(max_length=255)
    storage_path = models.CharField(max_length=500, unique=True)
    checksum_sha256 = models.CharField(max_length=64)
    byte_size = models.PositiveBigIntegerField()
    detected_format = models.CharField(max_length=12)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_raw_files",
    )
    source_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-uploaded_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["dataset_type", "checksum_sha256"],
                name="imports_unique_dataset_checksum",
            )
        ]

    def delete(self, *args, **kwargs):
        raise ValidationError("Raw import file adalah bukti audit dan tidak boleh dihapus.")

    def __str__(self):
        return self.original_filename


class MasterImportBatch(models.Model):
    class Status(models.TextChoices):
        PARSING = "PARSING", "Parsing"
        READY = "READY", "Ready for approval"
        BLOCKED = "BLOCKED", "Blocked"
        COMMITTED = "COMMITTED", "Committed"
        REJECTED = "REJECTED", "Dibatalkan"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    raw_file = models.ForeignKey(RawFile, on_delete=models.PROTECT, related_name="master_batches")
    parser_version = models.CharField(max_length=30, default="master-v1")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PARSING)
    created_at = models.DateTimeField(auto_now_add=True)
    previewed_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="approved_master_imports",
    )
    committed_at = models.DateTimeField(null=True, blank=True)
    total_rows = models.PositiveIntegerField(default=0)
    new_rows = models.PositiveIntegerField(default=0)
    changed_rows = models.PositiveIntegerField(default=0)
    unchanged_rows = models.PositiveIntegerField(default=0)
    blocking_issue_count = models.PositiveIntegerField(default=0)
    warning_count = models.PositiveIntegerField(default=0)
    quality_summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at",)

    @property
    def can_approve(self):
        return self.status == self.Status.READY and self.blocking_issue_count == 0

    def __str__(self):
        return f"Master import {self.id} · {self.status}"


class StagedMasterRow(models.Model):
    class ProposedAction(models.TextChoices):
        NEW = "NEW", "New"
        UPDATE = "UPDATE", "Update"
        UNCHANGED = "UNCHANGED", "Unchanged"
        BLOCKED = "BLOCKED", "Blocked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(MasterImportBatch, on_delete=models.CASCADE, related_name="staged_rows")
    row_number = models.PositiveIntegerField()
    source = models.CharField(max_length=100)
    sku = models.CharField(max_length=100)
    parent_sku = models.CharField(max_length=100, blank=True)
    article = models.CharField(max_length=255)
    category = models.CharField(max_length=100)
    subcategory = models.CharField(max_length=100, blank=True)
    variant = models.CharField(max_length=150, blank=True)
    sub_variant = models.CharField(max_length=100, blank=True)
    product_status = models.CharField(max_length=100)
    cogs = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    retail_price = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    shopee_code = models.CharField(max_length=160, blank=True)
    tiktok_code = models.CharField(max_length=160, blank=True)
    proposed_action = models.CharField(
        max_length=20,
        choices=ProposedAction.choices,
        default=ProposedAction.NEW,
    )
    existing_sku = models.ForeignKey(
        "master_data.SKU",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="staged_master_updates",
    )
    original_data = models.JSONField(default=dict)

    class Meta:
        ordering = ("row_number",)
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "row_number"],
                name="imports_unique_master_row_number",
            )
        ]
        indexes = [models.Index(fields=["batch", "sku"])]

    @property
    def product_key(self):
        # Parent SKU is a marketplace/listing family and can contain multiple
        # sellable products (Article).  The canonical Product grain is the
        # combination of Parent SKU + Article; rows without a parent stay
        # isolated so the importer never guesses their relationship.
        parent_key = self.parent_sku or f"UNPARENTED::{self.sku}"
        return f"{parent_key}::ARTICLE::{self.article}"

    def __str__(self):
        return f"Row {self.row_number}: {self.sku}"


class ImportValidationIssue(models.Model):
    class Severity(models.TextChoices):
        ERROR = "ERROR", "Error"
        WARNING = "WARNING", "Warning"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(MasterImportBatch, on_delete=models.CASCADE, related_name="issues")
    staged_row = models.ForeignKey(
        StagedMasterRow,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="issues",
    )
    severity = models.CharField(max_length=10, choices=Severity.choices)
    code = models.CharField(max_length=80)
    field_name = models.CharField(max_length=80, blank=True)
    message = models.TextField()
    is_blocking = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-is_blocking", "staged_row__row_number", "code")
        indexes = [
            models.Index(fields=["batch", "is_blocking"]),
            models.Index(fields=["batch", "severity"]),
        ]

    def __str__(self):
        row = self.staged_row.row_number if self.staged_row else "batch"
        return f"{self.severity} {self.code} @ {row}"


class SalesImportBatch(models.Model):
    class Mode(models.TextChoices):
        OPERATIONAL = "OPERATIONAL", "Operational"
        HISTORICAL = "HISTORICAL", "Historical migration"

    class Source(models.TextChoices):
        SHOPEE = "Shopee", "Shopee"
        TIKTOK = "Tiktok", "TikTok"
        CANONICAL = "Canonical", "Canonical Vobia Sales"

    class Status(models.TextChoices):
        PARSING = "PARSING", "Parsing"
        READY = "READY", "Ready for approval"
        BLOCKED = "BLOCKED", "Blocked"
        COMMITTED = "COMMITTED", "Committed"
        VOIDED = "VOIDED", "Dibatalkan"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    raw_file = models.ForeignKey(RawFile, on_delete=models.PROTECT, related_name="sales_batches")
    source = models.CharField(max_length=20, choices=Source.choices)
    mode = models.CharField(max_length=20, choices=Mode.choices, default=Mode.OPERATIONAL)
    parser_version = models.CharField(max_length=30, default="sales-v1")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PARSING)
    created_at = models.DateTimeField(auto_now_add=True)
    previewed_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="approved_sales_imports",
    )
    committed_at = models.DateTimeField(null=True, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="voided_sales_imports",
    )
    void_reason = models.TextField(blank=True, default="")
    total_rows = models.PositiveIntegerField(default=0)
    new_rows = models.PositiveIntegerField(default=0)
    status_update_rows = models.PositiveIntegerField(default=0)
    unchanged_rows = models.PositiveIntegerField(default=0)
    ignored_cancel_rows = models.PositiveIntegerField(default=0)
    out_of_scope_rows = models.PositiveIntegerField(default=0)
    blocking_issue_count = models.PositiveIntegerField(default=0)
    warning_count = models.PositiveIntegerField(default=0)
    data_start = models.DateTimeField(null=True, blank=True)
    data_end = models.DateTimeField(null=True, blank=True)
    quality_summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at",)

    @property
    def can_approve(self):
        return self.status == self.Status.READY and self.blocking_issue_count == 0

    def __str__(self):
        return f"{self.source} sales import {self.id}"


class StagedSalesRow(models.Model):
    class ProposedAction(models.TextChoices):
        NEW = "NEW", "New"
        STATUS_UPDATE = "STATUS_UPDATE", "Status update"
        UNCHANGED = "UNCHANGED", "Unchanged"
        PURE_CANCEL = "PURE_CANCEL", "Pure cancellation"
        OUT_OF_SCOPE = "OUT_OF_SCOPE", "Before cutover"
        BLOCKED = "BLOCKED", "Blocked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(SalesImportBatch, on_delete=models.CASCADE, related_name="staged_rows")
    row_number = models.PositiveIntegerField()
    order_number = models.CharField(max_length=160)
    source_label = models.CharField(max_length=80, blank=True)
    source_group = models.CharField(max_length=40, blank=True)
    source_status = models.CharField(max_length=255)
    normalized_status = models.CharField(max_length=180)
    is_final = models.BooleanField(default=False)
    is_pure_cancelled = models.BooleanField(default=False)
    order_datetime = models.DateTimeField(null=True, blank=True)
    shipped_datetime = models.DateTimeField(null=True, blank=True)
    sku_text = models.CharField(max_length=100)
    sku = models.ForeignKey(
        "master_data.SKU",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="staged_sales_rows",
    )
    quantity = models.IntegerField(null=True, blank=True)
    net_unit_price = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    retail_price_snapshot = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    sales_cogs_snapshot = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    total_gross_sales_snapshot = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    total_net_sales_snapshot = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    total_cogs_snapshot = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    gpm_snapshot = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    product_status_snapshot = models.CharField(max_length=100, blank=True)
    category_snapshot = models.CharField(max_length=100, blank=True)
    subcategory_snapshot = models.CharField(max_length=100, blank=True)
    product_name_snapshot = models.CharField(max_length=255, blank=True)
    variant_name_snapshot = models.CharField(max_length=150, blank=True)
    proposed_action = models.CharField(
        max_length=20,
        choices=ProposedAction.choices,
        default=ProposedAction.NEW,
    )
    existing_line = models.ForeignKey(
        "sales.SalesOrderLine",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="staged_updates",
    )
    selected_source_data = models.JSONField(default=dict)

    class Meta:
        ordering = ("row_number",)
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "row_number"],
                name="imports_unique_sales_row_number",
            )
        ]
        indexes = [
            models.Index(fields=["batch", "order_number", "sku_text"]),
            models.Index(fields=["batch", "proposed_action"]),
        ]

    @property
    def business_key(self):
        return f"{self.source_label or self.batch.source}|{self.order_number}|{self.sku_text}"

    def __str__(self):
        return f"Row {self.row_number}: {self.business_key}"


class SalesImportIssue(models.Model):
    class Severity(models.TextChoices):
        ERROR = "ERROR", "Error"
        WARNING = "WARNING", "Warning"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(SalesImportBatch, on_delete=models.CASCADE, related_name="issues")
    staged_row = models.ForeignKey(
        StagedSalesRow,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="issues",
    )
    severity = models.CharField(max_length=10, choices=Severity.choices)
    code = models.CharField(max_length=80)
    field_name = models.CharField(max_length=80, blank=True)
    message = models.TextField()
    is_blocking = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-is_blocking", "staged_row__row_number", "code")
        indexes = [
            models.Index(fields=["batch", "is_blocking"]),
            models.Index(fields=["batch", "code"]),
        ]

    def __str__(self):
        return f"{self.severity} {self.code}"
