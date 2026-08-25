import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class FIFOOpeningSnapshot(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sku = models.OneToOneField("master_data.SKU", on_delete=models.PROTECT, related_name="fifo_opening_snapshot")
    cutover_date = models.DateField()
    opening_qty = models.DecimalField(max_digits=18, decimal_places=4)
    frozen_unit_cogs = models.DecimalField(max_digits=18, decimal_places=4)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(frozen_unit_cogs__gte=0), name="inventory_opening_cogs_nonnegative")]

    def delete(self, *args, **kwargs):
        raise ValidationError("FIFO Opening snapshot immutable dan tidak boleh dihapus.")


class FIFOOpeningImportBatch(models.Model):
    class Status(models.TextChoices):
        PARSING = "PARSING", "Parsing"
        READY = "READY", "Ready for approval"
        BLOCKED = "BLOCKED", "Blocked"
        COMMITTED = "COMMITTED", "Committed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    raw_file = models.ForeignKey("imports.RawFile", on_delete=models.PROTECT, related_name="fifo_opening_batches")
    parser_version = models.CharField(max_length=30, default="fifo-opening-v1")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PARSING)
    created_at = models.DateTimeField(auto_now_add=True)
    previewed_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    committed_at = models.DateTimeField(null=True, blank=True)
    total_rows = models.PositiveIntegerField(default=0)
    ready_rows = models.PositiveIntegerField(default=0)
    blocking_issue_count = models.PositiveIntegerField(default=0)
    warning_count = models.PositiveIntegerField(default=0)
    quality_summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at",)

    @property
    def can_approve(self):
        return self.status == self.Status.READY and self.blocking_issue_count == 0


class StagedFIFOOpeningRow(models.Model):
    class ProposedAction(models.TextChoices):
        NEW = "NEW", "New"
        BLOCKED = "BLOCKED", "Blocked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(FIFOOpeningImportBatch, on_delete=models.CASCADE, related_name="staged_rows")
    row_number = models.PositiveIntegerField()
    sku_text = models.CharField(max_length=100)
    sku = models.ForeignKey("master_data.SKU", null=True, blank=True, on_delete=models.PROTECT)
    opening_qty = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    frozen_unit_cogs = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    cutover_date = models.DateField(null=True, blank=True)
    source_layer_key = models.CharField(max_length=240, blank=True)
    proposed_action = models.CharField(max_length=20, choices=ProposedAction.choices, default=ProposedAction.NEW)
    original_data = models.JSONField(default=dict)

    class Meta:
        ordering = ("row_number",)
        constraints = [models.UniqueConstraint(fields=["batch", "row_number"], name="inventory_unique_opening_batch_row")]


class FIFOOpeningImportIssue(models.Model):
    class Severity(models.TextChoices):
        ERROR = "ERROR", "Error"
        WARNING = "WARNING", "Warning"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(FIFOOpeningImportBatch, on_delete=models.CASCADE, related_name="issues")
    staged_row = models.ForeignKey(StagedFIFOOpeningRow, null=True, blank=True, on_delete=models.CASCADE, related_name="issues")
    severity = models.CharField(max_length=10, choices=Severity.choices)
    code = models.CharField(max_length=80)
    field_name = models.CharField(max_length=100, blank=True)
    message = models.TextField()
    is_blocking = models.BooleanField(default=False)

    class Meta:
        ordering = ("-is_blocking", "staged_row__row_number", "code")


class QCInspection(models.Model):
    class Disposition(models.TextChoices):
        REWORK = "REWORK", "Rework"
        REJECTED = "REJECTED", "Rejected"
        ACCEPTED_EXCEPTION = "ACCEPTED_WITH_EXCEPTION", "Accepted with Exception"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    po_line = models.ForeignKey("purchasing.PurchaseOrderLine", on_delete=models.PROTECT, related_name="qc_inspections")
    inspected_at = models.DateTimeField()
    qty_inspected = models.DecimalField(max_digits=18, decimal_places=4)
    qty_passed = models.DecimalField(max_digits=18, decimal_places=4)
    qty_failed = models.DecimalField(max_digits=18, decimal_places=4)
    failed_disposition = models.CharField(max_length=30, choices=Disposition.choices, blank=True)
    notes = models.TextField(blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("inspected_at", "created_at")
        constraints = [
            models.CheckConstraint(condition=Q(qty_inspected__gt=0), name="inventory_qc_inspected_positive"),
            models.CheckConstraint(condition=Q(qty_passed__gte=0), name="inventory_qc_passed_nonnegative"),
            models.CheckConstraint(condition=Q(qty_failed__gte=0), name="inventory_qc_failed_nonnegative"),
        ]

    def clean(self):
        super().clean()
        for name in ("qty_inspected", "qty_passed", "qty_failed"):
            value = getattr(self, name)
            if value != value.to_integral_value():
                raise ValidationError({name: "Qty harus bilangan bulat."})
        if self.qty_passed + self.qty_failed > self.qty_inspected:
            raise ValidationError("Qty Passed + Failed tidak boleh melebihi Qty Inspected.")
        if self.qty_failed > 0 and not self.failed_disposition:
            raise ValidationError({"failed_disposition": "Disposition wajib untuk Qty Failed."})


class QCFollowUp(models.Model):
    class Status(models.TextChoices):
        AWAITING_REWORK = "AWAITING_REWORK", "Menunggu Rework"
        READY_RE_QC = "READY_RE_QC", "Menunggu Re-QC"
        REJECTED = "REJECTED", "Rejected"
        ACCEPTED_EXCEPTION = "ACCEPTED_EXCEPTION", "Accepted with Exception"
        RESOLVED = "RESOLVED", "Lolos Re-QC"

    class DeliveryStatus(models.TextChoices):
        NOT_SHIPPED = "NOT_SHIPPED", "Belum Dikirim"
        IN_TRANSIT = "IN_TRANSIT", "Sedang Dikirim"
        INBOUND = "INBOUND", "Inbound"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_inspection = models.OneToOneField(
        QCInspection,
        on_delete=models.PROTECT,
        related_name="follow_up",
    )
    po_line = models.ForeignKey(
        "purchasing.PurchaseOrderLine",
        on_delete=models.PROTECT,
        related_name="qc_follow_ups",
    )
    status = models.CharField(max_length=30, choices=Status.choices)
    original_failed_qty = models.DecimalField(max_digits=18, decimal_places=4)
    open_qty = models.DecimalField(max_digits=18, decimal_places=4)
    resolved_passed_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    rework_cycle = models.PositiveIntegerField(default=0)
    delivery_status = models.CharField(
        max_length=20,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.NOT_SHIPPED,
    )
    delivery_updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="updated_rejected_deliveries",
    )
    delivery_updated_at = models.DateTimeField(null=True, blank=True)
    delivery_activity = models.ForeignKey(
        "production.ProductionActivity",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="rejected_follow_ups",
    )
    received_date = models.DateField(null=True, blank=True)
    received_warehouse = models.ForeignKey(
        "master_data.Warehouse",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="received_rejected_goods",
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="received_rejected_goods",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at",)
        constraints = [
            models.CheckConstraint(condition=Q(original_failed_qty__gt=0), name="inventory_qc_followup_original_positive"),
            models.CheckConstraint(condition=Q(open_qty__gte=0), name="inventory_qc_followup_open_nonnegative"),
            models.CheckConstraint(condition=Q(resolved_passed_qty__gte=0), name="inventory_qc_followup_passed_nonnegative"),
        ]


class QCFollowUpEvent(models.Model):
    class EventType(models.TextChoices):
        CREATED = "CREATED", "QC Follow-up Dibuat"
        REWORK_COMPLETED = "REWORK_COMPLETED", "Rework Selesai"
        RE_QC = "RE_QC", "Re-QC"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    follow_up = models.ForeignKey(QCFollowUp, on_delete=models.PROTECT, related_name="events")
    event_type = models.CharField(max_length=30, choices=EventType.choices)
    activity_date = models.DateField()
    qty_inspected = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    qty_passed = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    qty_failed = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    failed_disposition = models.CharField(max_length=30, blank=True)
    notes = models.TextField(blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(condition=Q(qty_inspected__gte=0), name="inventory_qc_followup_event_inspected_nonnegative"),
            models.CheckConstraint(condition=Q(qty_passed__gte=0), name="inventory_qc_followup_event_passed_nonnegative"),
            models.CheckConstraint(condition=Q(qty_failed__gte=0), name="inventory_qc_followup_event_failed_nonnegative"),
        ]

    def save(self, *args, **kwargs):
        if self.pk and QCFollowUpEvent.objects.filter(pk=self.pk).exists():
            raise ValidationError("QC Follow-up event bersifat append-only dan tidak boleh diubah.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("QC Follow-up event bersifat append-only dan tidak boleh dihapus.")


class InboundReceipt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    po_line = models.ForeignKey("purchasing.PurchaseOrderLine", on_delete=models.PROTECT, related_name="inbound_receipts")
    delivery_activity = models.ForeignKey(
        "production.ProductionActivity",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="inbound_receipts",
    )
    inbound_date = models.DateField()
    received_qty = models.DecimalField(max_digits=18, decimal_places=4)
    warehouse = models.ForeignKey("master_data.Warehouse", on_delete=models.PROTECT, related_name="inbound_receipts")
    reference = models.CharField(max_length=120, unique=True)
    retail_price_snapshot = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    notes = models.TextField(blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("inbound_date", "created_at")
        constraints = [models.CheckConstraint(condition=Q(received_qty__gt=0), name="inventory_inbound_qty_positive")]

    def clean(self):
        super().clean()
        if self.received_qty != self.received_qty.to_integral_value():
            raise ValidationError({"received_qty": "Received Qty harus bilangan bulat."})


class InventoryMovement(models.Model):
    class MovementType(models.TextChoices):
        OPENING = "OPENING", "Opening"
        INCOMING = "INCOMING", "Incoming"
        SALES_OUT = "SALES_OUT", "Sales Out"
        RETURN_IN = "RETURN_IN", "Return In"
        REJECTED_IN = "REJECTED_IN", "Rejected Goods In"
        ADJUSTMENT_IN = "ADJUSTMENT_IN", "Adjustment In"
        ADJUSTMENT_OUT = "ADJUSTMENT_OUT", "Adjustment Out"

    class Direction(models.TextChoices):
        IN = "IN", "In"
        OUT = "OUT", "Out"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    movement_key = models.CharField(max_length=240, unique=True)
    movement_date = models.DateField()
    movement_type = models.CharField(max_length=30, choices=MovementType.choices)
    direction = models.CharField(max_length=3, choices=Direction.choices)
    sku = models.ForeignKey("master_data.SKU", on_delete=models.PROTECT, related_name="inventory_movements")
    warehouse = models.ForeignKey(
        "master_data.Warehouse",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="inventory_movements",
    )
    quantity = models.DecimalField(max_digits=18, decimal_places=4)
    allocated_cost = models.DecimalField(max_digits=20, decimal_places=4, default=0)
    source_reference = models.CharField(max_length=200)
    sales_line = models.ForeignKey(
        "sales.SalesOrderLine", null=True, blank=True, on_delete=models.PROTECT, related_name="inventory_movements"
    )
    inbound_receipt = models.OneToOneField(
        InboundReceipt, null=True, blank=True, on_delete=models.PROTECT, related_name="movement"
    )
    return_receipt = models.OneToOneField(
        "PhysicalReturnReceipt", null=True, blank=True, on_delete=models.PROTECT, related_name="movement"
    )
    reason = models.TextField(blank=True)
    evidence_reference = models.CharField(max_length=240, blank=True)
    posted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    posted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("movement_date", "movement_type", "sku__sku", "movement_key")
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name="inventory_movement_qty_positive")]

    @property
    def signed_quantity(self):
        return self.quantity if self.direction == self.Direction.IN else -self.quantity

    def delete(self, *args, **kwargs):
        raise ValidationError("Inventory movement adalah ledger immutable dan tidak boleh dihapus.")


class FIFOLayer(models.Model):
    class SourceType(models.TextChoices):
        OPENING = "OPENING", "Opening"
        PURCHASE_ORDER = "PURCHASE_ORDER", "Purchase Order"
        ADJUSTMENT = "ADJUSTMENT", "Approved Adjustment"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    layer_key = models.CharField(max_length=240, unique=True)
    sku = models.ForeignKey("master_data.SKU", on_delete=models.PROTECT, related_name="fifo_layers")
    source_type = models.CharField(max_length=30, choices=SourceType.choices)
    source_reference = models.CharField(max_length=200)
    source_po_line = models.ForeignKey(
        "purchasing.PurchaseOrderLine", null=True, blank=True, on_delete=models.PROTECT, related_name="fifo_layers"
    )
    receipt_date = models.DateField()
    original_qty = models.DecimalField(max_digits=18, decimal_places=4)
    remaining_qty = models.DecimalField(max_digits=18, decimal_places=4)
    unit_cost = models.DecimalField(max_digits=18, decimal_places=4)
    opening_movement = models.OneToOneField(
        InventoryMovement, null=True, blank=True, on_delete=models.PROTECT, related_name="created_fifo_layer"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("receipt_date", "created_at", "layer_key")
        constraints = [
            models.CheckConstraint(condition=Q(original_qty__gt=0), name="inventory_fifo_original_positive"),
            models.CheckConstraint(condition=Q(remaining_qty__gte=0), name="inventory_fifo_remaining_nonnegative"),
            models.CheckConstraint(condition=Q(unit_cost__gte=0), name="inventory_fifo_cost_nonnegative"),
        ]


class FIFOAllocation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    outbound_movement = models.ForeignKey(InventoryMovement, on_delete=models.PROTECT, related_name="fifo_allocations")
    layer = models.ForeignKey(FIFOLayer, on_delete=models.PROTECT, related_name="allocations")
    allocated_qty = models.DecimalField(max_digits=18, decimal_places=4)
    unit_cost = models.DecimalField(max_digits=18, decimal_places=4)
    allocated_cost = models.DecimalField(max_digits=20, decimal_places=4)
    returned_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    class Meta:
        ordering = ("outbound_movement", "layer__receipt_date", "layer__created_at")
        constraints = [
            models.UniqueConstraint(fields=["outbound_movement", "layer"], name="inventory_unique_movement_layer_allocation"),
            models.CheckConstraint(condition=Q(allocated_qty__gt=0), name="inventory_allocation_qty_positive"),
            models.CheckConstraint(condition=Q(returned_qty__gte=0), name="inventory_allocation_returned_nonnegative"),
        ]


class InventoryException(models.Model):
    class Code(models.TextChoices):
        NEGATIVE_OPENING = "NEGATIVE_OPENING", "Negative Opening"
        FIFO_SHORT = "FIFO_SHORT_QTY", "FIFO Short Qty"
        RETURN_SOURCE_MISSING = "RETURN_SOURCE_MISSING", "Return Source Missing"
        INVALID_ADJUSTMENT = "INVALID_ADJUSTMENT", "Invalid Adjustment"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        RESOLVED = "RESOLVED", "Resolved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=40, choices=Code.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    sku = models.ForeignKey("master_data.SKU", on_delete=models.PROTECT, related_name="inventory_exceptions")
    movement = models.ForeignKey(
        InventoryMovement, null=True, blank=True, on_delete=models.PROTECT, related_name="exceptions"
    )
    quantity = models.DecimalField(max_digits=18, decimal_places=4)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    resolution_movement = models.ForeignKey(
        InventoryMovement,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="resolved_exceptions",
    )
    resolution_reason = models.TextField(blank=True)

    class Meta:
        ordering = ("status", "-created_at")

    def __str__(self):
        return f"{self.get_code_display()} · {self.sku.sku} · {self.quantity:g} pcs"


class ExpectedReturn(models.Model):
    class Status(models.TextChoices):
        EXPECTED = "EXPECTED", "Expected"
        PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED", "Partially Received"
        RECEIVED = "RECEIVED", "Received"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sales_line = models.OneToOneField("sales.SalesOrderLine", on_delete=models.PROTECT, related_name="expected_return")
    expected_qty = models.DecimalField(max_digits=18, decimal_places=4)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.EXPECTED)
    detected_at = models.DateTimeField(auto_now_add=True)


class PhysicalReturnReceipt(models.Model):
    class Condition(models.TextChoices):
        SELLABLE = "SELLABLE", "Sellable"
        DAMAGED = "DAMAGED", "Damaged"
        DEFECTIVE = "DEFECTIVE", "Defective"
        MISSING = "MISSING_LOST", "Missing/Lost"
        WRONG_ITEM = "WRONG_ITEM", "Wrong Item"
        WAITING_INSPECTION = "WAITING_INSPECTION", "Waiting Inspection"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sales_line = models.ForeignKey("sales.SalesOrderLine", on_delete=models.PROTECT, related_name="physical_returns")
    received_date = models.DateField()
    quantity = models.DecimalField(max_digits=18, decimal_places=4)
    warehouse = models.ForeignKey("master_data.Warehouse", on_delete=models.PROTECT, related_name="physical_returns")
    condition = models.CharField(max_length=30, choices=Condition.choices)
    notes = models.TextField(blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("received_date", "created_at")
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name="inventory_return_qty_positive")]

    def clean(self):
        super().clean()
        if self.quantity != self.quantity.to_integral_value():
            raise ValidationError({"quantity": "Qty Return harus bilangan bulat."})
