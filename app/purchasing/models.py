import uuid
from datetime import date

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class PurchaseOrderNumberSequence(models.Model):
    issue_month = models.DateField(unique=True)
    last_sequence = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        super().clean()
        if self.issue_month.day != 1:
            raise ValidationError({"issue_month": "Issue Month harus tanggal pertama bulan."})


class PPICRequirement(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    need_month = models.DateField()
    sku = models.ForeignKey("master_data.SKU", on_delete=models.PROTECT, related_name="ppic_requirements")
    incoming_plan = models.ForeignKey(
        "merchandising.IncomingPlan",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="ppic_requirements",
    )
    approved_qty = models.DecimalField(max_digits=18, decimal_places=4)
    revision = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("need_month", "sku__sku")
        constraints = [
            models.UniqueConstraint(fields=["need_month", "sku"], name="purchasing_unique_need_month_sku"),
            models.CheckConstraint(condition=Q(approved_qty__gt=0), name="purchasing_requirement_qty_positive"),
        ]

    @property
    def ordered_qty(self):
        return sum((line.ordered_qty for line in self.po_lines.exclude(po__status="CANCELLED")), 0)

    @property
    def remaining_qty(self):
        return max(self.approved_qty - self.ordered_qty, 0)

    def clean(self):
        super().clean()
        if self.need_month.day != 1:
            raise ValidationError({"need_month": "Need Month harus tanggal pertama bulan."})
        if self.approved_qty != self.approved_qty.to_integral_value():
            raise ValidationError({"approved_qty": "Requirement harus bilangan bulat."})

    def __str__(self):
        return f"{self.need_month:%b %Y} · {self.sku.sku}"


class PPICRequirementRevision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    requirement = models.ForeignKey(PPICRequirement, on_delete=models.PROTECT, related_name="revisions")
    revision = models.PositiveIntegerField()
    previous_qty = models.DecimalField(max_digits=18, decimal_places=4)
    approved_qty = models.DecimalField(max_digits=18, decimal_places=4)
    reason = models.TextField(blank=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("requirement", "revision")
        constraints = [
            models.UniqueConstraint(fields=["requirement", "revision"], name="purchasing_unique_requirement_revision")
        ]


class PurchaseOrder(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        RELEASED = "RELEASED", "Released"
        CANCELLED = "CANCELLED", "Cancelled"

    class Source(models.TextChoices):
        INCOMING_PLAN = "INCOMING_PLAN", "Approved Incoming Plan"
        MANUAL_NEW_PRODUCT = "MANUAL_NEW_PRODUCT", "Manual – New Product"
        LEGACY_WIP = "LEGACY_WIP", "PO WIP · outstanding per 31 July 2026"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    po_number = models.CharField(max_length=40, unique=True, null=True, blank=True)
    sequence = models.PositiveIntegerField(null=True, blank=True)
    issue_month = models.DateField(null=True, blank=True)
    supplier = models.ForeignKey("master_data.Supplier", on_delete=models.PROTECT, related_name="purchase_orders")
    need_month = models.DateField()
    required_arrival = models.DateField(null=True, blank=True)
    source = models.CharField(max_length=30, choices=Source.choices, default=Source.INCOMING_PLAN)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_purchase_orders")
    created_at = models.DateTimeField(auto_now_add=True)
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="released_purchase_orders",
    )
    released_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="cancelled_purchase_orders",
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.TextField(blank=True)
    close_date = models.DateField(null=True, blank=True)
    reopened_at = models.DateTimeField(null=True, blank=True)
    migration_cutoff_date = models.DateField(null=True, blank=True)
    migration_evidence_reference = models.CharField(max_length=240, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["issue_month", "sequence"],
                condition=Q(sequence__isnull=False),
                name="purchasing_unique_issue_month_sequence",
            )
        ]

    def clean(self):
        super().clean()
        if self.need_month.day != 1:
            raise ValidationError({"need_month": "Need Month harus tanggal pertama bulan."})
        if self.issue_month and self.issue_month.day != 1:
            raise ValidationError({"issue_month": "Issue Month harus tanggal pertama bulan."})
        if self.status == self.Status.RELEASED and self.source != self.Source.LEGACY_WIP and not self.issue_month:
            raise ValidationError({"issue_month": "PO Released wajib memiliki Issue Month."})
        if self.status == self.Status.CANCELLED and not self.cancellation_reason.strip():
            raise ValidationError({"cancellation_reason": "Alasan pembatalan wajib diisi."})
        if self.source == self.Source.LEGACY_WIP:
            if self.migration_cutoff_date != date(2026, 7, 31):
                raise ValidationError({"migration_cutoff_date": "PO WIP wajib memakai cutoff 31 July 2026."})
            if not self.migration_evidence_reference.strip():
                raise ValidationError({"migration_evidence_reference": "PO WIP wajib memiliki referensi bukti migrasi."})

    def delete(self, *args, **kwargs):
        if self.status != self.Status.DRAFT:
            raise ValidationError("PO released/cancelled tidak boleh dihapus.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return self.po_number or f"Draft {str(self.id)[:8]}"


class PurchaseOrderLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    po = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="lines")
    requirement = models.ForeignKey(
        PPICRequirement,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="po_lines",
    )
    sku = models.ForeignKey("master_data.SKU", on_delete=models.PROTECT, related_name="purchase_order_lines")
    ordered_qty = models.DecimalField(max_digits=18, decimal_places=4)
    cogs_snapshot = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    received_before_cutover_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    qc_passed_before_cutover_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("po", "sku__sku")
        constraints = [
            models.UniqueConstraint(fields=["po", "sku"], name="purchasing_unique_po_sku"),
            models.CheckConstraint(condition=Q(ordered_qty__gt=0), name="purchasing_po_line_qty_positive"),
            models.CheckConstraint(
                condition=Q(cogs_snapshot__gte=0) | Q(cogs_snapshot__isnull=True),
                name="purchasing_po_cogs_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(received_before_cutover_qty__gte=0),
                name="purchasing_legacy_received_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(qc_passed_before_cutover_qty__gte=0),
                name="purchasing_legacy_qc_nonnegative",
            ),
        ]

    def clean(self):
        super().clean()
        if self.ordered_qty != self.ordered_qty.to_integral_value():
            raise ValidationError({"ordered_qty": "PO Qty harus bilangan bulat."})
        if self.requirement_id:
            if self.requirement.need_month != self.po.need_month or self.requirement.sku_id != self.sku_id:
                raise ValidationError("Requirement harus memiliki Need Month dan SKU yang sama dengan line PO.")
        if self.received_before_cutover_qty > self.ordered_qty:
            raise ValidationError({"received_before_cutover_qty": "Received sebelum cutoff tidak boleh melebihi PO Qty."})
        if self.qc_passed_before_cutover_qty > self.ordered_qty:
            raise ValidationError({"qc_passed_before_cutover_qty": "QC Passed sebelum cutoff tidak boleh melebihi PO Qty."})
        if self.received_before_cutover_qty > self.qc_passed_before_cutover_qty:
            raise ValidationError("Received sebelum cutoff tidak boleh melebihi QC Passed sebelum cutoff.")

    def __str__(self):
        return f"{self.po} · {self.sku.sku}"


class POWIPImportBatch(models.Model):
    class Status(models.TextChoices):
        PARSING = "PARSING", "Parsing"
        READY = "READY", "Ready for approval"
        BLOCKED = "BLOCKED", "Blocked"
        COMMITTED = "COMMITTED", "Committed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    raw_file = models.ForeignKey(
        "imports.RawFile",
        on_delete=models.PROTECT,
        related_name="po_wip_batches",
    )
    parser_version = models.CharField(max_length=40, default="po-wip-v1")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PARSING)
    created_at = models.DateTimeField(auto_now_add=True)
    previewed_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="approved_po_wip_imports",
    )
    committed_at = models.DateTimeField(null=True, blank=True)
    total_rows = models.PositiveIntegerField(default=0)
    ready_rows = models.PositiveIntegerField(default=0)
    po_count = models.PositiveIntegerField(default=0)
    total_outstanding_qty = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    blocking_issue_count = models.PositiveIntegerField(default=0)
    warning_count = models.PositiveIntegerField(default=0)
    quality_summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at",)

    @property
    def can_approve(self):
        return self.status == self.Status.READY and self.blocking_issue_count == 0

    def __str__(self):
        return f"PO WIP import {self.id} · {self.status}"


class StagedPOWIPRow(models.Model):
    class ProposedAction(models.TextChoices):
        NEW = "NEW", "New PO WIP line"
        BLOCKED = "BLOCKED", "Blocked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(POWIPImportBatch, on_delete=models.CASCADE, related_name="staged_rows")
    row_number = models.PositiveIntegerField()
    po_number = models.CharField(max_length=40)
    parent_sku = models.CharField(max_length=100, blank=True)
    sku_text = models.CharField(max_length=100)
    product_name_source = models.CharField(max_length=255, blank=True)
    outstanding_qty = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    sku = models.ForeignKey(
        "master_data.SKU",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="staged_po_wip_rows",
    )
    proposed_cogs_snapshot = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    proposed_action = models.CharField(
        max_length=20,
        choices=ProposedAction.choices,
        default=ProposedAction.NEW,
    )
    original_data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("row_number",)
        constraints = [
            models.UniqueConstraint(fields=["batch", "row_number"], name="purchasing_unique_po_wip_row_number")
        ]
        indexes = [
            models.Index(
                fields=["batch", "po_number", "sku_text"],
                name="purch_wip_batch_po_sku_idx",
            )
        ]

    def __str__(self):
        return f"Row {self.row_number}: {self.po_number} · {self.sku_text}"


class POWIPImportIssue(models.Model):
    class Severity(models.TextChoices):
        ERROR = "ERROR", "Error"
        WARNING = "WARNING", "Warning"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(POWIPImportBatch, on_delete=models.CASCADE, related_name="issues")
    staged_row = models.ForeignKey(
        StagedPOWIPRow,
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
            models.Index(fields=["batch", "is_blocking"], name="purch_wip_issue_block_idx"),
            models.Index(fields=["batch", "code"], name="purch_wip_issue_code_idx"),
        ]

    def __str__(self):
        return f"{self.severity} {self.code}"
