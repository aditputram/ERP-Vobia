import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class ProductionOrder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    po = models.OneToOneField(
        "purchasing.PurchaseOrder",
        on_delete=models.PROTECT,
        related_name="production_order",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("po__required_arrival", "po__po_number")

    def __str__(self):
        return f"Production · {self.po}"


class ProductionPlan(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ACTIVE = "ACTIVE", "Active"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    production_order = models.OneToOneField(
        ProductionOrder,
        on_delete=models.PROTECT,
        related_name="plan",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    target_material_purchase_date = models.DateField(null=True, blank=True)
    target_trial_date = models.DateField(null=True, blank=True)
    target_cut_start_date = models.DateField(null=True, blank=True)
    target_cut_end_date = models.DateField(null=True, blank=True)
    target_make_start_date = models.DateField(null=True, blank=True)
    target_make_end_date = models.DateField(null=True, blank=True)
    target_trim_start_date = models.DateField(null=True, blank=True)
    target_trim_end_date = models.DateField(null=True, blank=True)
    target_qc_start_date = models.DateField(null=True, blank=True)
    target_qc_end_date = models.DateField(null=True, blank=True)
    target_inbound_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="created_production_plans",
    )
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="activated_production_plans",
    )
    activated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("production_order__po__po_number",)

    @property
    def is_complete(self):
        required = (
            self.target_material_purchase_date,
            self.target_trial_date,
            self.target_cut_start_date,
            self.target_cut_end_date,
            self.target_make_start_date,
            self.target_make_end_date,
            self.target_trim_start_date,
            self.target_trim_end_date,
            self.target_qc_start_date,
            self.target_qc_end_date,
            self.target_inbound_date,
        )
        return all(required)

    def clean(self):
        super().clean()
        pairs = (
            ("target_cut_start_date", "target_cut_end_date", "Cut"),
            ("target_make_start_date", "target_make_end_date", "Make"),
            ("target_trim_start_date", "target_trim_end_date", "Trim"),
            ("target_qc_start_date", "target_qc_end_date", "QC"),
        )
        for start_name, end_name, label in pairs:
            start = getattr(self, start_name)
            end = getattr(self, end_name)
            if start and end and end < start:
                raise ValidationError({end_name: f"Target selesai {label} tidak boleh sebelum target mulai."})
        if self.status == self.Status.ACTIVE and not self.is_complete:
            raise ValidationError("Seluruh target Production Plan wajib diisi sebelum plan diaktifkan.")

    def __str__(self):
        return f"{self.production_order.po} · {self.get_status_display()}"


class ProductionStage(models.Model):
    class Stage(models.TextChoices):
        MATERIAL_PURCHASE = "MATERIAL_PURCHASE", "Pembelian Material"
        CUT = "CUT", "Cut · Potong"
        MAKE = "MAKE", "Make · Jahit"
        TRIM = "TRIM", "Trim · Finishing"

    class Status(models.TextChoices):
        NOT_STARTED = "NOT_STARTED", "Belum dimulai"
        IN_PROGRESS = "IN_PROGRESS", "Sedang berjalan"
        BLOCKED = "BLOCKED", "Terkendala"
        COMPLETE = "COMPLETE", "Selesai"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    production_order = models.ForeignKey(ProductionOrder, on_delete=models.PROTECT, related_name="stages")
    stage = models.CharField(max_length=30, choices=Stage.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NOT_STARTED)
    target_start_date = models.DateField(null=True, blank=True)
    target_end_date = models.DateField(null=True, blank=True)
    actual_start_date = models.DateField(null=True, blank=True)
    actual_end_date = models.DateField(null=True, blank=True)
    material_arrival_date = models.DateField(null=True, blank=True)
    progress_percent = models.PositiveSmallIntegerField(default=0)
    completed_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    notes = models.TextField(blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="updated_production_stages",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("production_order", "stage")
        constraints = [
            models.UniqueConstraint(
                fields=["production_order", "stage"],
                name="production_unique_order_stage",
            ),
            models.CheckConstraint(
                condition=Q(progress_percent__gte=0) & Q(progress_percent__lte=100),
                name="production_stage_progress_0_100",
            ),
            models.CheckConstraint(
                condition=Q(completed_qty__gte=0),
                name="production_stage_completed_qty_nonnegative",
            ),
        ]

    def clean(self):
        super().clean()
        if self.target_start_date and self.target_end_date and self.target_end_date < self.target_start_date:
            raise ValidationError({"target_end_date": "Target selesai tidak boleh sebelum target mulai."})
        if self.actual_start_date and self.actual_end_date and self.actual_end_date < self.actual_start_date:
            raise ValidationError({"actual_end_date": "Tanggal aktual selesai tidak boleh sebelum aktual mulai."})
        if self.status == self.Status.COMPLETE and self.progress_percent != 100:
            raise ValidationError({"progress_percent": "Tahap Complete wajib memiliki progress 100%."})
        if self.status == self.Status.NOT_STARTED and self.progress_percent != 0:
            raise ValidationError({"progress_percent": "Tahap yang belum dimulai wajib memiliki progress 0%."})
        if self.stage == self.Stage.MATERIAL_PURCHASE and self.status == self.Status.COMPLETE:
            if not self.material_arrival_date:
                raise ValidationError(
                    {"material_arrival_date": "Tanggal material datang wajib diisi sebelum Pembelian Material selesai."}
                )
        elif self.stage != self.Stage.MATERIAL_PURCHASE and self.material_arrival_date:
            raise ValidationError(
                {"material_arrival_date": "Tanggal material datang hanya digunakan pada tahap Pembelian Material."}
            )
        cmt_stages = {self.Stage.CUT, self.Stage.MAKE, self.Stage.TRIM}
        if self.stage not in cmt_stages and self.completed_qty != 0:
            raise ValidationError({"completed_qty": "Qty selesai hanya digunakan untuk tahap Cut, Make, dan Trim."})
        if self.completed_qty != self.completed_qty.to_integral_value():
            raise ValidationError({"completed_qty": "Qty Production harus berupa bilangan bulat."})

    @property
    def operational_status_display(self):
        if self.stage == self.Stage.MATERIAL_PURCHASE:
            return {
                self.Status.NOT_STARTED: "Belum di beli",
                self.Status.IN_PROGRESS: "Menunggu ketersediaan Material",
                self.Status.BLOCKED: "Menunggu ketersediaan Material",
                self.Status.COMPLETE: "Material siap diproses",
            }.get(self.status, self.get_status_display())
        return self.get_status_display()

    def __str__(self):
        return f"{self.production_order.po} · {self.get_stage_display()}"


class ProductionTrial(models.Model):
    class Status(models.TextChoices):
        IN_PROGRESS = "IN_PROGRESS", "Trial sedang dibuat"
        WAITING_APPROVAL = "WAITING_APPROVAL", "Menunggu approval"
        APPROVED = "APPROVED", "Approved"
        REVISION_REQUIRED = "REVISION_REQUIRED", "Perlu revisi"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    production_order = models.ForeignKey(ProductionOrder, on_delete=models.PROTECT, related_name="trials")
    revision = models.PositiveIntegerField()
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.IN_PROGRESS)
    target_trial_date = models.DateField(null=True, blank=True)
    trial_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    decision_notes = models.TextField(blank=True)
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="started_production_trials",
    )
    started_at = models.DateTimeField(auto_now_add=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="submitted_production_trials",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="decided_production_trials",
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("production_order", "-revision")
        constraints = [
            models.UniqueConstraint(
                fields=["production_order", "revision"],
                name="production_unique_trial_revision",
            )
        ]

    def clean(self):
        super().clean()
        if self.status in {self.Status.WAITING_APPROVAL, self.Status.APPROVED, self.Status.REVISION_REQUIRED}:
            if not self.trial_date:
                raise ValidationError({"trial_date": "Tanggal trial wajib diisi sebelum submission."})
        if self.status == self.Status.REVISION_REQUIRED and not self.decision_notes.strip():
            raise ValidationError({"decision_notes": "Alasan revisi wajib diisi."})

    def __str__(self):
        return f"{self.production_order.po} · Trial R{self.revision}"


class ProductionDeliveryOrder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    number = models.CharField(max_length=30, unique=True)
    issue_month = models.DateField()
    sequence = models.PositiveIntegerField()
    delivery_date = models.DateField()
    production_order = models.ForeignKey(
        ProductionOrder,
        on_delete=models.PROTECT,
        related_name="delivery_orders",
    )
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-delivery_date", "-sequence")
        constraints = [
            models.UniqueConstraint(
                fields=("issue_month", "sequence"),
                name="production_unique_delivery_order_month_sequence",
            )
        ]

    def __str__(self):
        return self.number


class ProductionCogsFinalization(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    production_order = models.OneToOneField(
        ProductionOrder,
        on_delete=models.PROTECT,
        related_name="cogs_finalization",
    )
    line_snapshot = models.JSONField(default=list)
    total_po_cost = models.DecimalField(max_digits=20, decimal_places=4)
    total_final_cost = models.DecimalField(max_digits=20, decimal_places=4)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="approved_production_cogs_finalizations",
    )
    approved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-approved_at",)
        permissions = [
            ("approve_cogs_finalization", "Can approve production quantity and COGS finalization"),
        ]

    def save(self, *args, **kwargs):
        if self.pk and ProductionCogsFinalization.objects.filter(pk=self.pk).exists():
            raise ValidationError("Finalisasi Quantity & COGS bersifat final dan tidak boleh diubah.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Finalisasi Quantity & COGS bersifat final dan tidak boleh dihapus.")


class ProductionActivity(models.Model):
    class EntryKind(models.TextChoices):
        SYSTEM = "SYSTEM", "System Event"
        ACTIVITY = "ACTIVITY", "Production Activity"
        CORRECTION = "CORRECTION", "Correction"
        VOID = "VOID", "Void"

    class ActivityType(models.TextChoices):
        MATERIAL_PURCHASE = "MATERIAL_PURCHASE", "Pembelian Material"
        MATERIAL_ARRIVAL = "MATERIAL_ARRIVAL", "Material Tiba di Tempat Produksi"
        TRIAL_SUBMIT = "TRIAL_SUBMIT", "Submit Trial Production"
        TRIAL_APPROVE = "TRIAL_APPROVE", "Approve Trial Production"
        TRIAL_REVISION = "TRIAL_REVISION", "Revision Required Trial Production"
        CUT = "CUT", "Cut · Potong"
        MAKE = "MAKE", "Make · Jahit"
        TRIM = "TRIM", "Trim · Finishing"
        QC = "QC", "Quality Control"
        WAREHOUSE_DELIVERY = "WAREHOUSE_DELIVERY", "Deliver to Warehouse"
        REJECTED_WAREHOUSE_DELIVERY = "REJECTED_WAREHOUSE_DELIVERY", "Deliver Rejected Goods"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    production_order = models.ForeignKey(ProductionOrder, on_delete=models.PROTECT, related_name="activities")
    action = models.CharField(max_length=80)
    stage = models.CharField(max_length=30, blank=True)
    entry_kind = models.CharField(max_length=20, choices=EntryKind.choices, default=EntryKind.SYSTEM)
    activity_type = models.CharField(max_length=30, choices=ActivityType.choices, blank=True)
    activity_date = models.DateField(null=True, blank=True)
    quantity = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    po_line = models.ForeignKey(
        "purchasing.PurchaseOrderLine",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="production_activities",
    )
    delivery_order = models.ForeignKey(
        ProductionDeliveryOrder,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="activities",
    )
    source_activity = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="correction_entries",
    )
    notes = models.TextField(blank=True)
    description = models.TextField()
    before_values = models.JSONField(default=dict, blank=True)
    after_values = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="production_activities",
    )
    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-occurred_at",)
        indexes = [
            models.Index(fields=["production_order", "occurred_at"], name="production__product_ffd8b0_idx"),
            models.Index(
                fields=["production_order", "entry_kind", "activity_type"],
                name="production_entry_type_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and ProductionActivity.objects.filter(pk=self.pk).exists():
            raise ValidationError("Production activity bersifat append-only dan tidak boleh diubah.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Production activity bersifat append-only dan tidak boleh dihapus.")
