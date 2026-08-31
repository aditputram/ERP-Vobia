from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Sum
from django.utils import timezone

from audit.services import record_audit
from inventory.models import FIFOLayer, QCFollowUp
from inventory.services.fifo import qc_approved_qty
from master_data.models import SKUValueHistory
from purchasing.models import PurchaseOrder

from .models import (
    ProductionActivity,
    ProductionCogsFinalization,
    ProductionDeliveryOrder,
    ProductionOrder,
    ProductionPlan,
    ProductionStage,
    ProductionTrial,
)


STAGE_SEQUENCE = (
    ProductionStage.Stage.MATERIAL_PURCHASE,
    ProductionStage.Stage.CUT,
    ProductionStage.Stage.MAKE,
    ProductionStage.Stage.TRIM,
)
CMT_STAGES = (
    ProductionStage.Stage.CUT,
    ProductionStage.Stage.MAKE,
    ProductionStage.Stage.TRIM,
)
COGS_QUANT = Decimal("0.0001")


def _timing_status(*, target_start, target_end, actual_start, actual_end):
    """Return a read-only schedule status for Production Monitoring."""
    today = timezone.localdate()
    if actual_end:
        if not target_end or actual_end <= target_end:
            return "Selesai Tepat Waktu", "ready"
        return "Selesai Terlambat", "blocked"
    if target_end and today > target_end:
        return "Terlambat", "blocked"
    if actual_start:
        if target_end and (target_end - today).days <= 1:
            return "Berisiko Terlambat", "parsing"
        return "On Track", "ready"
    if target_start and today >= target_start:
        return "Berisiko Terlambat", "parsing"
    return "Belum Mulai", ""


def _timing_row(*, target_start, target_end, actual_start=None, actual_end=None):
    status, status_class = _timing_status(
        target_start=target_start,
        target_end=target_end,
        actual_start=actual_start,
        actual_end=actual_end,
    )
    return {
        "target_start": target_start,
        "target_end": target_end,
        "actual_start": actual_start,
        "actual_end": actual_end,
        "status": status,
        "status_class": status_class,
    }


def _cumulative_completion_date(rows, *, required_qty, opening_qty=Decimal("0"), quantity_getter, date_getter):
    """Return the activity date on which cumulative Qty first reached its current requirement."""
    if required_qty <= 0 or opening_qty >= required_qty:
        return None
    cumulative = opening_qty
    for row in sorted(rows, key=date_getter):
        cumulative += quantity_getter(row)
        if cumulative >= required_qty:
            return date_getter(row)
    return None


def _effective_cmt_dates(production_order, required_qty):
    """Resolve CMT actual dates from append-only Activity plus its latest correction."""
    resolved = {stage_code: [] for stage_code in CMT_STAGES}
    originals = production_order.activities.filter(
        entry_kind=ProductionActivity.EntryKind.ACTIVITY,
        activity_type__in=CMT_STAGES,
    ).prefetch_related("correction_entries")
    for original in originals:
        corrections = [
            row
            for row in original.correction_entries.all()
            if row.entry_kind == ProductionActivity.EntryKind.CORRECTION
        ]
        effective = max(corrections, key=lambda row: row.occurred_at) if corrections else original
        if effective.activity_date and Decimal(effective.quantity or 0) > 0:
            resolved[original.activity_type].append(effective)

    result = {}
    for stage_code, rows in resolved.items():
        rows.sort(key=lambda row: (row.activity_date, row.occurred_at))
        result[stage_code] = {
            "actual_start": rows[0].activity_date if rows else None,
            "actual_end": _cumulative_completion_date(
                rows,
                required_qty=required_qty,
                quantity_getter=lambda row: Decimal(row.quantity or 0),
                date_getter=lambda row: row.activity_date,
            ),
        }
    return result


def _local_calendar_date(value):
    return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()


def _activity(
    *,
    production_order,
    actor,
    action,
    description,
    stage="",
    before=None,
    after=None,
    entry_kind=ProductionActivity.EntryKind.SYSTEM,
    activity_type="",
    activity_date=None,
    quantity=None,
    po_line=None,
    delivery_order=None,
    source_activity=None,
    notes="",
):
    activity = ProductionActivity.objects.create(
        production_order=production_order,
        actor=actor,
        action=action,
        stage=stage,
        entry_kind=entry_kind,
        activity_type=activity_type,
        activity_date=activity_date,
        quantity=quantity,
        po_line=po_line,
        delivery_order=delivery_order,
        source_activity=source_activity,
        notes=notes,
        description=description,
        before_values=before or {},
        after_values=after or {},
    )
    record_audit(
        actor=actor,
        action=action,
        entity_type="production.productionorder",
        entity_id=production_order.id,
        before_values=before or {},
        after_values=after or {},
        metadata={"po_number": production_order.po.po_number, "stage": stage},
    )
    return activity


@transaction.atomic
def ensure_production_order(po, actor=None):
    if po.status != PurchaseOrder.Status.RELEASED:
        raise ValidationError("Production workflow hanya tersedia untuk PO Released.")
    production_order, created = ProductionOrder.objects.get_or_create(po=po)
    existing = set(production_order.stages.values_list("stage", flat=True))
    ProductionStage.objects.bulk_create(
        [ProductionStage(production_order=production_order, stage=stage) for stage in STAGE_SEQUENCE if stage not in existing]
    )
    if created:
        _activity(
            production_order=production_order,
            actor=actor,
            action="production_workflow_created",
            description="Production monitoring dibuat dari PO Released.",
            after={"po_number": po.po_number},
        )
    return production_order


PLAN_FIELDS = (
    "target_material_purchase_date",
    "target_trial_date",
    "target_cut_start_date",
    "target_cut_end_date",
    "target_make_start_date",
    "target_make_end_date",
    "target_trim_start_date",
    "target_trim_end_date",
    "target_qc_start_date",
    "target_qc_end_date",
    "target_inbound_date",
    "notes",
)


def _plan_values(plan):
    return {
        "status": plan.status,
        **{
            name: str(getattr(plan, name) or "")
            for name in PLAN_FIELDS
        },
    }


@transaction.atomic
def save_production_plan(*, production_order, values, activate, actor, change_reason=""):
    production_order = ProductionOrder.objects.select_for_update().select_related("po").get(
        pk=production_order.pk
    )
    if production_order.po.status != PurchaseOrder.Status.RELEASED:
        raise ValidationError("Production Plan hanya tersedia untuk PO Released.")
    plan, created = ProductionPlan.objects.select_for_update().get_or_create(
        production_order=production_order,
        defaults={"created_by": actor},
    )
    was_active = plan.status == ProductionPlan.Status.ACTIVE
    if activate and was_active:
        raise ValidationError(
            "Production Plan sudah Active. Gunakan Revisi Production Plan untuk mengubah target."
        )
    before = _plan_values(plan)
    for name in PLAN_FIELDS:
        if name in values:
            setattr(plan, name, values[name])
    if activate:
        plan.status = ProductionPlan.Status.ACTIVE
        plan.activated_by = actor
        plan.activated_at = timezone.now()
    proposed = _plan_values(plan)
    if was_active:
        if before == proposed:
            raise ValidationError("Tidak ada perubahan Production Plan yang perlu disimpan.")
        if not change_reason.strip():
            raise ValidationError("Alasan perubahan wajib diisi untuk Revisi Production Plan.")
    plan.full_clean()
    plan.save()

    stages = _stage_map(production_order)
    stage_targets = {
        ProductionStage.Stage.MATERIAL_PURCHASE: (
            plan.target_material_purchase_date,
            None,
        ),
        ProductionStage.Stage.CUT: (plan.target_cut_start_date, plan.target_cut_end_date),
        ProductionStage.Stage.MAKE: (plan.target_make_start_date, plan.target_make_end_date),
        ProductionStage.Stage.TRIM: (plan.target_trim_start_date, plan.target_trim_end_date),
    }
    for stage_code, (target_start, target_end) in stage_targets.items():
        stage = stages[stage_code]
        stage.target_start_date = target_start
        stage.target_end_date = target_end
        stage.updated_by = actor
        stage.save(update_fields=("target_start_date", "target_end_date", "updated_by", "updated_at"))

    after = _plan_values(plan)
    if activate:
        action = "production_plan_activated"
        description = "Production Plan diaktifkan dan PO masuk Production Monitoring."
    elif was_active:
        action = "production_plan_revised"
        description = f"Production Plan Active direvisi. Alasan: {change_reason.strip()}"
    else:
        action = "production_plan_saved"
        description = "Production Plan disimpan sebagai Draft."
    _activity(
        production_order=production_order,
        actor=actor,
        action=action,
        stage="PLAN",
        description=description,
        before=before,
        after={**after, "change_reason": change_reason.strip()},
    )
    return plan


def _stage_map(production_order):
    return {row.stage: row for row in production_order.stages.all()}


def latest_trial(production_order):
    return production_order.trials.order_by("-revision").first()


def trial_is_approved(production_order):
    trial = latest_trial(production_order)
    return bool(trial and trial.status == ProductionTrial.Status.APPROVED)


def qc_is_open(production_order):
    stages = _stage_map(production_order)
    trim = stages.get(ProductionStage.Stage.TRIM)
    inspected = sum(
        (
            line.qc_passed_before_cutover_qty
            + (line.qc_inspections.aggregate(total=Sum("qty_inspected"))["total"] or Decimal("0"))
            for line in production_order.po.lines.all()
        ),
        Decimal("0"),
    )
    return bool(trim and trim.completed_qty > inspected)


def _require_complete(stages, stage, message):
    row = stages.get(stage)
    if not row or row.status != ProductionStage.Status.COMPLETE:
        raise ValidationError(message)


def _cmt_available_qty(stages, stage_code, ordered_qty):
    if stage_code == ProductionStage.Stage.CUT:
        return ordered_qty
    if stage_code == ProductionStage.Stage.MAKE:
        return stages[ProductionStage.Stage.CUT].completed_qty
    if stage_code == ProductionStage.Stage.TRIM:
        return stages[ProductionStage.Stage.MAKE].completed_qty
    return Decimal("0")


def _effective_cmt_line_qty(production_order, activity_type, po_line):
    """Return the append-only effective CMT quantity for one PO line."""
    total = Decimal("0")
    originals = production_order.activities.filter(
        entry_kind=ProductionActivity.EntryKind.ACTIVITY,
        activity_type=activity_type,
        po_line=po_line,
    ).prefetch_related("correction_entries")
    for original in originals:
        corrections = [
            row
            for row in original.correction_entries.all()
            if row.entry_kind == ProductionActivity.EntryKind.CORRECTION
        ]
        effective = max(corrections, key=lambda row: row.occurred_at) if corrections else original
        total += Decimal(effective.quantity or 0)
    return total


def _cmt_line_limit(production_order, activity_type, po_line):
    if activity_type == ProductionStage.Stage.CUT:
        return None
    if activity_type == ProductionStage.Stage.MAKE:
        return _effective_cmt_line_qty(production_order, ProductionStage.Stage.CUT, po_line)
    if activity_type == ProductionStage.Stage.TRIM:
        return _effective_cmt_line_qty(production_order, ProductionStage.Stage.MAKE, po_line)
    return Decimal("0")


def cmt_line_availability(production_order, po_line):
    """Return remaining submit capacity per CMT stage for one PO line."""
    availability = {}
    for activity_type in CMT_STAGES:
        limit_qty = _cmt_line_limit(production_order, activity_type, po_line)
        completed_qty = _effective_cmt_line_qty(production_order, activity_type, po_line)
        availability[activity_type] = (
            None if limit_qty is None else max(limit_qty - completed_qty, Decimal("0"))
        )
    return availability


def qc_line_availability(production_order, po_line):
    """Return Trim output, inspected quantity, and remaining QC for one PO line."""
    trim_qty = _effective_cmt_line_qty(
        production_order,
        ProductionStage.Stage.TRIM,
        po_line,
    )
    inspected_qty = po_line.qc_passed_before_cutover_qty + sum(
        (row.qty_inspected for row in po_line.qc_inspections.all()),
        Decimal("0"),
    )
    return {
        "trim_qty": trim_qty,
        "inspected_qty": inspected_qty,
        "remaining_qty": max(trim_qty - inspected_qty, Decimal("0")),
    }


def delivery_line_availability(production_order, po_line):
    """Return QC Passed quantities not yet submitted for warehouse delivery."""
    shipped_qty = _effective_cmt_line_qty(
        production_order,
        ProductionActivity.ActivityType.WAREHOUSE_DELIVERY,
        po_line,
    )
    return max(qc_approved_qty(po_line) - shipped_qty, Decimal("0"))


def rejected_delivery_line_availability(production_order, po_line):
    return QCFollowUp.objects.filter(
        po_line=po_line,
        status=QCFollowUp.Status.REJECTED,
        delivery_status=QCFollowUp.DeliveryStatus.NOT_SHIPPED,
    ).aggregate(total=Sum("open_qty"))["total"] or Decimal("0")


def next_delivery_order_number(delivery_date):
    issue_month = delivery_date.replace(day=1)
    sequence = (
        ProductionDeliveryOrder.objects.filter(issue_month=issue_month).aggregate(max_sequence=Max("sequence"))[
            "max_sequence"
        ]
        or 0
    ) + 1
    return f"DOP.VOB-{delivery_date:%m/%y}-{sequence:03d}", issue_month, sequence


def production_process_details(production_order):
    """Return effective Product + Size quantities for Monitoring detail cards."""
    lines = list(production_order.po.lines.all())
    cmt_totals = {}
    originals = production_order.activities.filter(
        entry_kind=ProductionActivity.EntryKind.ACTIVITY,
        activity_type__in=CMT_STAGES,
        po_line__isnull=False,
    ).prefetch_related("correction_entries")
    for original in originals:
        corrections = [
            row
            for row in original.correction_entries.all()
            if row.entry_kind == ProductionActivity.EntryKind.CORRECTION
        ]
        effective = max(corrections, key=lambda row: row.occurred_at) if corrections else original
        key = (original.po_line_id, original.activity_type)
        cmt_totals[key] = cmt_totals.get(key, Decimal("0")) + Decimal(effective.quantity or 0)

    grouped = {}
    for line in lines:
        product = line.sku.product_variant.product
        key = (product.id, line.sku.size or "—")
        row = grouped.setdefault(
            key,
            {
                "product_name": product.name,
                "size": line.sku.size or "—",
                ProductionStage.Stage.CUT: Decimal("0"),
                ProductionStage.Stage.MAKE: Decimal("0"),
                ProductionStage.Stage.TRIM: Decimal("0"),
                "QC": Decimal("0"),
                "INBOUND": Decimal("0"),
            },
        )
        for stage_code in CMT_STAGES:
            row[stage_code] += cmt_totals.get((line.id, stage_code), Decimal("0"))
        row["QC"] += qc_approved_qty(line)
        row["INBOUND"] += line.received_before_cutover_qty + sum(
            (receipt.received_qty for receipt in line.inbound_receipts.all()),
            Decimal("0"),
        )

    rows = sorted(
        grouped.values(),
        key=lambda row: (row["product_name"].lower(), row["size"].lower()),
    )
    labels = {
        ProductionStage.Stage.CUT: "Qty Cut",
        ProductionStage.Stage.MAKE: "Qty Make",
        ProductionStage.Stage.TRIM: "Qty Trim",
        "QC": "Qty QC Passed",
        "INBOUND": "Qty Inbound",
    }
    return {
        process_code: {
            "quantity_label": quantity_label,
            "rows": [
                {
                    "product_name": row["product_name"],
                    "size": row["size"],
                    "quantity": row[process_code],
                }
                for row in rows
                if row[process_code] > 0
            ],
        }
        for process_code, quantity_label in labels.items()
    }


@transaction.atomic
def update_stage(
    *,
    production_order,
    stage_code,
    status=None,
    target_start_date=None,
    target_end_date=None,
    actual_start_date=None,
    actual_end_date=None,
    material_arrival_date=None,
    progress_percent=None,
    completed_qty=None,
    is_blocked=False,
    notes="",
    actor,
):
    production_order = ProductionOrder.objects.select_for_update().select_related("po").get(pk=production_order.pk)
    if production_order.po.status != PurchaseOrder.Status.RELEASED:
        raise ValidationError("Tahap Production hanya dapat diubah untuk PO Released.")
    stages = _stage_map(production_order)
    stage = stages.get(stage_code)
    if stage is None:
        raise ValidationError("Tahap Production tidak valid.")
    is_cmt = stage_code in CMT_STAGES
    ordered_qty = production_order.po.lines.aggregate(total=Sum("ordered_qty"))["total"] or Decimal("0")
    if is_cmt:
        completed_qty = Decimal(completed_qty if completed_qty is not None else stage.completed_qty)
        if completed_qty < 0:
            raise ValidationError("Qty selesai tidak boleh negatif.")
        if completed_qty != completed_qty.to_integral_value():
            raise ValidationError("Qty Production harus berupa bilangan bulat.")
        available_qty = _cmt_available_qty(stages, stage_code, ordered_qty)
        has_started = bool(completed_qty > 0 or is_blocked or actual_start_date)
        if has_started and stage_code == ProductionStage.Stage.CUT:
            _require_complete(
                stages,
                ProductionStage.Stage.MATERIAL_PURCHASE,
                "Cut baru dapat berjalan setelah Pembelian Material selesai.",
            )
            if not stages[ProductionStage.Stage.MATERIAL_PURCHASE].material_arrival_date:
                raise ValidationError("Cut menunggu tanggal material datang di tempat produksi.")
            if not trial_is_approved(production_order):
                raise ValidationError("Cut baru dapat berjalan setelah Trial Produksi di-approve.")
        elif has_started and stage_code == ProductionStage.Stage.MAKE and available_qty <= 0:
            raise ValidationError("Make/Jahit baru dapat berjalan setelah ada Qty yang sudah di-Cut.")
        elif has_started and stage_code == ProductionStage.Stage.TRIM and available_qty <= 0:
            raise ValidationError("Trim/Finishing baru dapat berjalan setelah ada Qty yang sudah di-Make.")
        if stage_code != ProductionStage.Stage.CUT and completed_qty > available_qty:
            upstream_label = {
                ProductionStage.Stage.MAKE: "Qty sudah di-Cut",
                ProductionStage.Stage.TRIM: "Qty sudah di-Make",
            }[stage_code]
            raise ValidationError(
                f"{stage.get_stage_display()} tidak boleh melebihi {upstream_label} ({available_qty:.0f} pcs)."
            )
        completion_qty = ordered_qty if stage_code == ProductionStage.Stage.CUT else available_qty
        if completed_qty >= completion_qty and completion_qty > 0:
            status = ProductionStage.Status.COMPLETE
            progress_percent = 100
        elif is_blocked:
            status = ProductionStage.Status.BLOCKED
            progress_percent = min(int((completed_qty / completion_qty) * 100), 100) if completion_qty else 0
        elif has_started:
            status = ProductionStage.Status.IN_PROGRESS
            progress_percent = min(int((completed_qty / completion_qty) * 100), 100) if completion_qty else 0
        else:
            status = ProductionStage.Status.NOT_STARTED
            progress_percent = 0
    elif stage_code == ProductionStage.Stage.MATERIAL_PURCHASE:
        target_end_date = None
        actual_end_date = None
        if material_arrival_date:
            status = ProductionStage.Status.COMPLETE
            progress_percent = 100
        elif status == ProductionStage.Status.COMPLETE:
            raise ValidationError("Tanggal material datang di tempat produksi wajib diisi sebelum tahap diselesaikan.")
        elif status not in {ProductionStage.Status.NOT_STARTED, ProductionStage.Status.IN_PROGRESS}:
            raise ValidationError("Status Pembelian Material tidak valid.")
        else:
            progress_percent = 0
    elif status is None:
        raise ValidationError("Status tahap wajib dipilih.")
    progress_percent = int(progress_percent or 0)
    if status == ProductionStage.Status.COMPLETE:
        progress_percent = 100
        if stage_code == ProductionStage.Stage.MATERIAL_PURCHASE:
            actual_end_date = None
        else:
            actual_end_date = actual_end_date or timezone.localdate()
            actual_start_date = actual_start_date or actual_end_date
    elif status == ProductionStage.Status.NOT_STARTED:
        progress_percent = 0
        actual_start_date = None
        actual_end_date = None
    elif status in {ProductionStage.Status.IN_PROGRESS, ProductionStage.Status.BLOCKED}:
        actual_end_date = None
        if stage_code != ProductionStage.Stage.MATERIAL_PURCHASE:
            actual_start_date = actual_start_date or timezone.localdate()
        if progress_percent >= 100:
            raise ValidationError("Progress 100% hanya boleh dipakai ketika status Complete.")
    before = {
        "status": stage.status,
        "progress_percent": stage.progress_percent,
        "completed_qty": str(stage.completed_qty),
        "target_start_date": str(stage.target_start_date or ""),
        "target_end_date": str(stage.target_end_date or ""),
        "material_arrival_date": str(stage.material_arrival_date or ""),
    }
    stage.status = status
    stage.target_start_date = target_start_date
    stage.target_end_date = target_end_date
    stage.actual_start_date = actual_start_date
    stage.actual_end_date = actual_end_date
    stage.material_arrival_date = material_arrival_date if stage_code == ProductionStage.Stage.MATERIAL_PURCHASE else None
    stage.progress_percent = progress_percent
    stage.completed_qty = completed_qty if is_cmt else Decimal("0")
    stage.notes = notes.strip()
    stage.updated_by = actor
    stage.full_clean()
    stage.save()
    after = {
        "status": stage.status,
        "progress_percent": stage.progress_percent,
        "completed_qty": str(stage.completed_qty),
        "target_start_date": str(stage.target_start_date or ""),
        "target_end_date": str(stage.target_end_date or ""),
        "material_arrival_date": str(stage.material_arrival_date or ""),
    }
    _activity(
        production_order=production_order,
        actor=actor,
        action="production_stage_updated",
        stage=stage.stage,
        description=(
            f"{stage.get_stage_display()} diperbarui: {stage.completed_qty:.0f} pcs selesai, "
            f"status {stage.get_status_display()}."
            if is_cmt
            else f"{stage.get_stage_display()} diperbarui menjadi {stage.operational_status_display}."
        ),
        before=before,
        after=after,
    )
    return stage


@transaction.atomic
def start_trial(*, production_order, target_trial_date, actor):
    production_order = ProductionOrder.objects.select_for_update().select_related("po").get(pk=production_order.pk)
    stages = _stage_map(production_order)
    _require_complete(
        stages,
        ProductionStage.Stage.MATERIAL_PURCHASE,
        "Trial Produksi baru dapat dimulai setelah Pembelian Material selesai.",
    )
    material_stage = stages[ProductionStage.Stage.MATERIAL_PURCHASE]
    if not material_stage.material_arrival_date:
        raise ValidationError("Trial Produksi menunggu tanggal material datang di tempat produksi.")
    if not target_trial_date:
        raise ValidationError("Target tanggal Trial Production wajib diisi.")
    current = latest_trial(production_order)
    if current and current.status in {
        ProductionTrial.Status.IN_PROGRESS,
        ProductionTrial.Status.WAITING_APPROVAL,
        ProductionTrial.Status.APPROVED,
    }:
        raise ValidationError("Trial aktif masih berjalan atau sudah Approved.")
    revision = (current.revision + 1) if current else 1
    trial = ProductionTrial.objects.create(
        production_order=production_order,
        revision=revision,
        status=ProductionTrial.Status.IN_PROGRESS,
        target_trial_date=target_trial_date,
        started_by=actor,
    )
    _activity(
        production_order=production_order,
        actor=actor,
        action="production_trial_started",
        stage="TRIAL",
        description=f"Trial Produksi revision {revision} dimulai dengan target {target_trial_date}.",
        after={"revision": revision, "status": trial.status, "target_trial_date": str(target_trial_date)},
    )
    return trial


@transaction.atomic
def save_trial_target(*, production_order, target_trial_date, actor):
    production_order = ProductionOrder.objects.select_for_update().get(pk=production_order.pk)
    trial = latest_trial(production_order)
    if trial is None or trial.status != ProductionTrial.Status.IN_PROGRESS:
        raise ValidationError("Target hanya dapat diubah pada Trial Production yang sedang berjalan.")
    if not target_trial_date:
        raise ValidationError("Target tanggal Trial Production wajib diisi.")
    before = {"target_trial_date": str(trial.target_trial_date or "")}
    trial.target_trial_date = target_trial_date
    trial.save(update_fields=["target_trial_date"])
    _activity(
        production_order=production_order,
        actor=actor,
        action="production_trial_target_saved",
        stage="TRIAL",
        description=f"Target Trial Produksi revision {trial.revision} disimpan: {target_trial_date}.",
        before=before,
        after={"target_trial_date": str(target_trial_date)},
    )
    return trial


@transaction.atomic
def append_trial_note(*, production_order, note, actor):
    production_order = ProductionOrder.objects.select_for_update().get(pk=production_order.pk)
    trial = latest_trial(production_order)
    if trial is None or trial.status != ProductionTrial.Status.IN_PROGRESS:
        raise ValidationError("Catatan hanya dapat ditambahkan pada Trial Production yang sedang berjalan.")
    note_text = " ".join((note or "").split())
    if not note_text:
        raise ValidationError("Hasil dan Catatan Trial wajib diisi.")

    existing_entries = []
    for line in trial.notes.splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        prefix, separator, content = cleaned.partition(". ")
        existing_entries.append(content if separator and prefix.isdigit() else cleaned)
    existing_entries.append(note_text)
    before = {"notes": trial.notes}
    trial.notes = "\n".join(f"{index}. {entry}" for index, entry in enumerate(existing_entries, start=1))
    trial.save(update_fields=["notes"])
    _activity(
        production_order=production_order,
        actor=actor,
        action="production_trial_note_appended",
        stage="TRIAL",
        description=f"Catatan ke-{len(existing_entries)} Trial Produksi revision {trial.revision} disimpan.",
        before=before,
        after={"notes": trial.notes, "entry_number": len(existing_entries)},
    )
    return trial


@transaction.atomic
def submit_trial(*, production_order, trial_date, actor):
    production_order = ProductionOrder.objects.select_for_update().get(pk=production_order.pk)
    trial = latest_trial(production_order)
    if trial is None or trial.status != ProductionTrial.Status.IN_PROGRESS:
        raise ValidationError("Tidak ada Trial Produksi yang sedang berjalan untuk disubmit.")
    if not trial.target_trial_date:
        raise ValidationError("Simpan Target Tanggal Trial Production sebelum Submit Approval.")
    trial.trial_date = trial_date
    trial.status = ProductionTrial.Status.WAITING_APPROVAL
    trial.submitted_by = actor
    trial.submitted_at = timezone.now()
    trial.full_clean()
    trial.save()
    _activity(
        production_order=production_order,
        actor=actor,
        action="production_trial_submitted",
        stage="TRIAL",
        description=f"Trial Produksi revision {trial.revision} disubmit untuk approval.",
        after={
            "revision": trial.revision,
            "status": trial.status,
            "target_trial_date": str(trial.target_trial_date),
            "trial_date": str(trial.trial_date),
        },
    )
    return trial


@transaction.atomic
def decide_trial(*, trial, decision, decision_notes, actor):
    trial = ProductionTrial.objects.select_for_update().select_related("production_order__po").get(pk=trial.pk)
    if trial.status != ProductionTrial.Status.WAITING_APPROVAL:
        raise ValidationError("Hanya trial berstatus Menunggu Approval yang dapat diputuskan.")
    if decision not in {ProductionTrial.Status.APPROVED, ProductionTrial.Status.REVISION_REQUIRED}:
        raise ValidationError("Keputusan trial tidak valid.")
    trial.status = decision
    trial.decision_notes = decision_notes.strip()
    trial.decided_by = actor
    trial.decided_at = timezone.now()
    trial.full_clean()
    trial.save()
    _activity(
        production_order=trial.production_order,
        actor=actor,
        action="production_trial_approved" if decision == ProductionTrial.Status.APPROVED else "production_trial_revision_required",
        stage="TRIAL",
        description=f"Trial Produksi revision {trial.revision}: {trial.get_status_display()}.",
        after={"revision": trial.revision, "status": trial.status, "decision_notes": trial.decision_notes},
    )
    return trial


def eligible_activity_choices(production_order):
    is_finalized = ProductionCogsFinalization.objects.filter(production_order=production_order).exists()
    try:
        plan = production_order.plan
    except ProductionPlan.DoesNotExist:
        return []
    if plan.status != ProductionPlan.Status.ACTIVE:
        return []
    stages = _stage_map(production_order)
    material = stages[ProductionStage.Stage.MATERIAL_PURCHASE]
    choices = []
    if not material.actual_start_date:
        choices.append((ProductionActivity.ActivityType.MATERIAL_PURCHASE, "Pembelian Material"))
        return choices
    if not material.material_arrival_date:
        choices.append((ProductionActivity.ActivityType.MATERIAL_ARRIVAL, "Material Tiba di Tempat Produksi"))
        return choices

    trial = latest_trial(production_order)
    if trial is None or trial.status in {
        ProductionTrial.Status.REVISION_REQUIRED,
        ProductionTrial.Status.IN_PROGRESS,
    }:
        choices.append((ProductionActivity.ActivityType.TRIAL_SUBMIT, "Submit Trial Production"))
        return choices
    if trial.status == ProductionTrial.Status.WAITING_APPROVAL:
        return [
            (ProductionActivity.ActivityType.TRIAL_APPROVE, "Approve Trial Production"),
            (ProductionActivity.ActivityType.TRIAL_REVISION, "Revision Required Trial Production"),
        ]
    if trial.status != ProductionTrial.Status.APPROVED:
        return choices

    if not is_finalized:
        snapshot = production_snapshot(production_order)
        labels = {
            ProductionStage.Stage.CUT: "Cut · Potong",
            ProductionStage.Stage.MAKE: "Make · Jahit",
            ProductionStage.Stage.TRIM: "Trim · Finishing",
        }
        for stage_code in CMT_STAGES:
            remaining = snapshot["cmt_quantities"][stage_code]["remaining_qty"]
            if stage_code == ProductionStage.Stage.CUT:
                choices.append((stage_code, f"{labels[stage_code]} · tidak dibatasi Qty PO"))
            elif remaining > 0:
                choices.append((stage_code, f"{labels[stage_code]} · maksimal {remaining:.0f} pcs"))
        if snapshot["remaining_qc_qty"] > 0:
            choices.append(
                (
                    ProductionActivity.ActivityType.QC,
                    f"Quality Control · maksimal {snapshot['remaining_qc_qty']:.0f} pcs output Trim",
                )
            )
        if snapshot["ready_to_deliver_qty"] > 0:
            choices.append(
                (
                    ProductionActivity.ActivityType.WAREHOUSE_DELIVERY,
                    f"Deliver to Warehouse · maksimal {snapshot['ready_to_deliver_qty']:.0f} pcs",
                )
            )
    rejected_ready = QCFollowUp.objects.filter(
        po_line__po=production_order.po,
        status=QCFollowUp.Status.REJECTED,
        delivery_status=QCFollowUp.DeliveryStatus.NOT_SHIPPED,
    ).aggregate(total=Sum("open_qty"))["total"] or Decimal("0")
    if rejected_ready > 0:
        choices.append(
            (
                ProductionActivity.ActivityType.REJECTED_WAREHOUSE_DELIVERY,
                f"Deliver Rejected Goods · {rejected_ready:.0f} pcs belum dikirim",
            )
        )
    return choices


@transaction.atomic
def submit_production_activity(
    *,
    production_order,
    activity_type,
    activity_date,
    actor,
    quantity=None,
    po_line=None,
    qty_inspected=None,
    qty_passed=None,
    qty_failed=None,
    failed_disposition="",
    notes="",
    delivery_order=None,
):
    production_order = ProductionOrder.objects.select_for_update().select_related("po").get(
        pk=production_order.pk
    )
    eligible = {value for value, _label in eligible_activity_choices(production_order)}
    if activity_type not in eligible:
        raise ValidationError("Activity ini sudah selesai atau belum memenuhi tahap sebelumnya.")
    plan = production_order.plan
    stages = _stage_map(production_order)
    before_snapshot = production_snapshot(production_order)
    payload = {"activity_date": str(activity_date), "notes": (notes or "").strip()}
    description = ""
    stage_label = activity_type
    activity_quantity = None

    if activity_type == ProductionActivity.ActivityType.MATERIAL_PURCHASE:
        update_stage(
            production_order=production_order,
            stage_code=ProductionStage.Stage.MATERIAL_PURCHASE,
            status=ProductionStage.Status.IN_PROGRESS,
            target_start_date=plan.target_material_purchase_date,
            actual_start_date=activity_date,
            material_arrival_date=None,
            notes=notes,
            actor=actor,
        )
        description = f"Pembelian Material dicatat pada {activity_date}."
    elif activity_type == ProductionActivity.ActivityType.MATERIAL_ARRIVAL:
        material = stages[ProductionStage.Stage.MATERIAL_PURCHASE]
        update_stage(
            production_order=production_order,
            stage_code=ProductionStage.Stage.MATERIAL_PURCHASE,
            status=ProductionStage.Status.COMPLETE,
            target_start_date=plan.target_material_purchase_date,
            actual_start_date=material.actual_start_date,
            material_arrival_date=activity_date,
            notes=notes,
            actor=actor,
        )
        description = f"Material tiba di tempat produksi pada {activity_date}."
    elif activity_type == ProductionActivity.ActivityType.TRIAL_SUBMIT:
        trial = latest_trial(production_order)
        if trial is None or trial.status == ProductionTrial.Status.REVISION_REQUIRED:
            trial = start_trial(
                production_order=production_order,
                target_trial_date=plan.target_trial_date,
                actor=actor,
            )
        if notes:
            append_trial_note(production_order=production_order, note=notes, actor=actor)
        trial = submit_trial(production_order=production_order, trial_date=activity_date, actor=actor)
        payload["trial_id"] = str(trial.id)
        description = f"Trial Production R{trial.revision} disubmit untuk approval."
        stage_label = "TRIAL"
    elif activity_type in {
        ProductionActivity.ActivityType.TRIAL_APPROVE,
        ProductionActivity.ActivityType.TRIAL_REVISION,
    }:
        trial = latest_trial(production_order)
        decision = (
            ProductionTrial.Status.APPROVED
            if activity_type == ProductionActivity.ActivityType.TRIAL_APPROVE
            else ProductionTrial.Status.REVISION_REQUIRED
        )
        trial = decide_trial(trial=trial, decision=decision, decision_notes=notes, actor=actor)
        payload["trial_id"] = str(trial.id)
        description = f"Trial Production R{trial.revision}: {trial.get_status_display()}."
        stage_label = "TRIAL"
    elif activity_type in CMT_STAGES:
        quantity = Decimal(quantity or 0)
        if po_line is None or po_line.po_id != production_order.po_id:
            raise ValidationError("SKU Production harus berasal dari Purchase Order yang dipilih.")
        if quantity <= 0:
            raise ValidationError("Qty Production harus lebih besar dari 0.")
        if quantity != quantity.to_integral_value():
            raise ValidationError("Qty Production harus berupa bilangan bulat.")
        completed_for_line = _effective_cmt_line_qty(production_order, activity_type, po_line)
        line_limit = _cmt_line_limit(production_order, activity_type, po_line)
        if line_limit is not None and completed_for_line + quantity > line_limit:
            upstream_label = {
                ProductionStage.Stage.CUT: "total PO Qty Parent SKU",
                ProductionStage.Stage.MAKE: "Qty Cut",
                ProductionStage.Stage.TRIM: "Qty Make",
            }[activity_type]
            raise ValidationError(
                f"Qty {dict(ProductionActivity.ActivityType.choices)[activity_type]} SKU "
                f"{po_line.sku.sku} tidak boleh melebihi {upstream_label} "
                f"({line_limit:.0f} pcs)."
            )
        stage = stages[activity_type]
        new_completed = stage.completed_qty + quantity
        target_dates = {
            ProductionStage.Stage.CUT: (plan.target_cut_start_date, plan.target_cut_end_date),
            ProductionStage.Stage.MAKE: (plan.target_make_start_date, plan.target_make_end_date),
            ProductionStage.Stage.TRIM: (plan.target_trim_start_date, plan.target_trim_end_date),
        }
        target_start, target_end = target_dates[activity_type]
        completion_qty = (
            before_snapshot["ordered_qty"]
            if activity_type == ProductionStage.Stage.CUT
            else stages[
                ProductionStage.Stage.CUT
                if activity_type == ProductionStage.Stage.MAKE
                else ProductionStage.Stage.MAKE
            ].completed_qty
        )
        updated = update_stage(
            production_order=production_order,
            stage_code=activity_type,
            target_start_date=target_start,
            target_end_date=target_end,
            actual_start_date=stage.actual_start_date or activity_date,
            actual_end_date=activity_date if new_completed >= completion_qty and completion_qty > 0 else None,
            completed_qty=new_completed,
            notes=notes,
            actor=actor,
        )
        activity_quantity = quantity
        payload.update(
            {
                "quantity": str(quantity),
                "sku": po_line.sku.sku,
                "completed_before": str(stage.completed_qty),
                "completed_after": str(updated.completed_qty),
            }
        )
        description = (
            f"{updated.get_stage_display()} SKU {po_line.sku.sku} {quantity:.0f} pcs dicatat; "
            f"total selesai menjadi {updated.completed_qty:.0f} pcs."
        )
    elif activity_type == ProductionActivity.ActivityType.QC:
        if po_line is None or po_line.po_id != production_order.po_id:
            raise ValidationError("SKU QC harus berasal dari Purchase Order yang dipilih.")
        from inventory.services.fifo import record_qc

        inspected_at = timezone.make_aware(datetime.combine(activity_date, time(hour=12)))
        qc = record_qc(
            po_line=po_line,
            inspected_at=inspected_at,
            qty_inspected=qty_inspected,
            qty_passed=qty_passed,
            qty_failed=qty_failed,
            disposition=failed_disposition,
            notes=notes,
            actor=actor,
        )
        activity_quantity = Decimal(qty_inspected)
        payload.update(
            {
                "qc_inspection_id": str(qc.id),
                "sku": po_line.sku.sku,
                "qty_inspected": str(qc.qty_inspected),
                "qty_passed": str(qc.qty_passed),
                "qty_failed": str(qc.qty_failed),
                "failed_disposition": qc.failed_disposition,
            }
        )
        description = (
            f"QC {po_line.sku.sku}: diperiksa {qc.qty_inspected:.0f}, "
            f"lolos {qc.qty_passed:.0f}, gagal {qc.qty_failed:.0f} pcs."
        )
        if qc.qty_failed > 0:
            description += f" Tindak lanjut: {qc.get_failed_disposition_display()}."
            if qc.notes.strip():
                description += f" Alasan gagal: {qc.notes.strip()}."
            else:
                description += " Alasan gagal: belum dicatat."
        stage_label = "QC"
    elif activity_type == ProductionActivity.ActivityType.WAREHOUSE_DELIVERY:
        if delivery_order is None:
            raise ValidationError("No. Delivery Order wajib dibuat untuk pengiriman ke Warehouse.")
        quantity = Decimal(quantity or 0)
        if po_line is None or po_line.po_id != production_order.po_id:
            raise ValidationError("SKU pengiriman harus berasal dari Purchase Order yang dipilih.")
        if quantity <= 0 or quantity != quantity.to_integral_value():
            raise ValidationError("Qty pengiriman harus berupa bilangan bulat lebih besar dari 0.")
        available = delivery_line_availability(production_order, po_line)
        if quantity > available:
            raise ValidationError(
                f"Qty pengiriman SKU {po_line.sku.sku} tidak boleh melebihi Qty siap dikirim "
                f"({available:.0f} pcs)."
            )
        shipped_before = _effective_cmt_line_qty(
            production_order,
            ProductionActivity.ActivityType.WAREHOUSE_DELIVERY,
            po_line,
        )
        activity_quantity = quantity
        payload.update(
            {
                "quantity": str(quantity),
                "sku": po_line.sku.sku,
                "shipped_before": str(shipped_before),
                "shipped_after": str(shipped_before + quantity),
            }
        )
        description = f"Deliver to Warehouse SKU {po_line.sku.sku} {quantity:.0f} pcs dicatat."
        if delivery_order:
            payload["delivery_order_number"] = delivery_order.number
            description = f"{delivery_order.number} · {description}"
        stage_label = "INBOUND"

    after_snapshot = production_snapshot(production_order)
    payload.update(
        {
            "current_stage_before": before_snapshot["current_code"],
            "current_stage_after": after_snapshot["current_code"],
        }
    )
    return _activity(
        production_order=production_order,
        actor=actor,
        action="production_activity_submitted",
        stage=stage_label,
        entry_kind=ProductionActivity.EntryKind.ACTIVITY,
        activity_type=activity_type,
        activity_date=activity_date,
        quantity=activity_quantity,
        po_line=po_line,
        delivery_order=delivery_order,
        notes=notes,
        description=description,
        after=payload,
    )


@transaction.atomic
def submit_cmt_activity_batch(
    *,
    production_order,
    activity_type,
    activity_date,
    line_quantities,
    notes="",
    actor,
):
    """Submit one atomic CMT entry batch while keeping one auditable row per SKU."""
    if activity_type not in CMT_STAGES:
        raise ValidationError("Batch Qty per SKU hanya berlaku untuk Cut, Make, dan Trim.")
    line_quantities = [(line, Decimal(qty)) for line, qty in line_quantities if Decimal(qty or 0) > 0]
    if not line_quantities:
        raise ValidationError("Isi minimal satu Qty Production per SKU.")
    created = []
    for po_line, quantity in line_quantities:
        created.append(
            submit_production_activity(
                production_order=production_order,
                activity_type=activity_type,
                activity_date=activity_date,
                quantity=quantity,
                po_line=po_line,
                notes=notes,
                actor=actor,
            )
        )
    return created


@transaction.atomic
def submit_qc_activity_batch(
    *,
    production_order,
    activity_date,
    line_results,
    notes="",
    actor,
):
    """Submit one atomic QC batch while keeping one auditable row per SKU."""
    line_results = [row for row in line_results if Decimal(row[1] or 0) > 0]
    if not line_results:
        raise ValidationError("Isi minimal satu Qty Diperiksa per SKU.")
    return [
        submit_production_activity(
            production_order=production_order,
            activity_type=ProductionActivity.ActivityType.QC,
            activity_date=activity_date,
            po_line=po_line,
            qty_inspected=inspected,
            qty_passed=passed,
            qty_failed=failed,
            failed_disposition=disposition,
            notes=failure_reason or notes,
            actor=actor,
        )
        for po_line, inspected, passed, failed, disposition, failure_reason in line_results
    ]


@transaction.atomic
def submit_delivery_activity_batch(*, production_order, activity_date, line_quantities, notes="", actor):
    line_quantities = [(line, Decimal(qty)) for line, qty in line_quantities if Decimal(qty or 0) > 0]
    if not line_quantities:
        raise ValidationError("Isi minimal satu Qty pengiriman per SKU.")
    number, issue_month, sequence = next_delivery_order_number(activity_date)
    delivery_order = ProductionDeliveryOrder.objects.create(
        number=number,
        issue_month=issue_month,
        sequence=sequence,
        delivery_date=activity_date,
        production_order=production_order,
        notes=(notes or "").strip(),
        created_by=actor,
    )
    activities = [
        submit_production_activity(
            production_order=production_order,
            activity_type=ProductionActivity.ActivityType.WAREHOUSE_DELIVERY,
            activity_date=activity_date,
            quantity=quantity,
            po_line=po_line,
            notes=notes,
            actor=actor,
            delivery_order=delivery_order,
        )
        for po_line, quantity in line_quantities
    ]
    return delivery_order, activities


@transaction.atomic
def submit_rejected_delivery_activity_batch(*, production_order, activity_date, line_quantities, notes="", actor):
    line_quantities = [(line, Decimal(qty)) for line, qty in line_quantities if Decimal(qty or 0) > 0]
    if not line_quantities:
        raise ValidationError("Isi minimal satu Qty Rejected Goods per SKU.")
    number, issue_month, sequence = next_delivery_order_number(activity_date)
    delivery_order = ProductionDeliveryOrder.objects.create(
        number=number,
        issue_month=issue_month,
        sequence=sequence,
        delivery_date=activity_date,
        production_order=production_order,
        notes=(notes or "").strip(),
        created_by=actor,
    )
    activities = []
    for po_line, quantity in line_quantities:
        if po_line.po_id != production_order.po_id:
            raise ValidationError("SKU Rejected Goods harus berasal dari Purchase Order yang dipilih.")
        follow_ups = list(
            QCFollowUp.objects.select_for_update().filter(
                po_line=po_line,
                status=QCFollowUp.Status.REJECTED,
                delivery_status=QCFollowUp.DeliveryStatus.NOT_SHIPPED,
            )
        )
        available = sum((row.open_qty for row in follow_ups), Decimal("0"))
        if quantity != available:
            raise ValidationError(
                f"Qty Rejected Goods {po_line.sku.sku} harus sama dengan Qty belum dikirim "
                f"({available:.0f} pcs)."
            )
        activity = _activity(
            production_order=production_order,
            actor=actor,
            action="rejected_goods_delivery_submitted",
            stage="INBOUND",
            entry_kind=ProductionActivity.EntryKind.ACTIVITY,
            activity_type=ProductionActivity.ActivityType.REJECTED_WAREHOUSE_DELIVERY,
            activity_date=activity_date,
            quantity=quantity,
            po_line=po_line,
            delivery_order=delivery_order,
            notes=notes,
            description=f"{delivery_order.number} · Deliver Rejected Goods SKU {po_line.sku.sku} {quantity:.0f} pcs.",
            after={
                "delivery_order_number": delivery_order.number,
                "sku": po_line.sku.sku,
                "quantity": str(quantity),
                "stock_effect": "NONE",
            },
        )
        QCFollowUp.objects.filter(pk__in=[row.pk for row in follow_ups]).update(
            delivery_status=QCFollowUp.DeliveryStatus.IN_TRANSIT,
            delivery_activity=activity,
            delivery_updated_by=actor,
            delivery_updated_at=timezone.now(),
        )
        activities.append(activity)
    return delivery_order, activities


def latest_activity_version(activity):
    return activity.correction_entries.filter(
        entry_kind=ProductionActivity.EntryKind.CORRECTION,
    ).order_by("-occurred_at").first() or activity


@transaction.atomic
def correct_production_activity(
    *,
    activity,
    activity_date,
    reason,
    actor,
    quantity=None,
    qty_inspected=None,
    qty_passed=None,
    qty_failed=None,
    failed_disposition="",
    notes="",
):
    activity = ProductionActivity.objects.select_for_update().select_related(
        "production_order__po",
        "production_order__plan",
        "po_line__sku",
    ).get(pk=activity.pk)
    if activity.entry_kind != ProductionActivity.EntryKind.ACTIVITY:
        raise ValidationError("Hanya Production Activity asli yang dapat dikoreksi.")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Alasan koreksi wajib diisi.")
    effective = latest_activity_version(activity)
    production_order = activity.production_order
    plan = production_order.plan
    before = {
        "activity_date": str(effective.activity_date or ""),
        "quantity": str(effective.quantity or ""),
        "notes": effective.notes,
        **effective.after_values,
    }
    after = {"activity_date": str(activity_date), "notes": (notes or "").strip(), "reason": reason}
    corrected_quantity = None

    if activity.activity_type in CMT_STAGES:
        if quantity is None:
            raise ValidationError("Qty yang benar wajib diisi untuk koreksi CMT.")
        new_entry_qty = Decimal(quantity)
        old_entry_qty = Decimal(effective.quantity or 0)
        delta = new_entry_qty - old_entry_qty
        stages = _stage_map(production_order)
        stage = stages[activity.activity_type]
        new_completed = stage.completed_qty + delta
        if new_completed < 0:
            raise ValidationError("Koreksi menghasilkan Qty selesai negatif.")
        if activity.po_line_id:
            current_line_qty = _effective_cmt_line_qty(
                production_order,
                activity.activity_type,
                activity.po_line,
            )
            corrected_line_qty = current_line_qty + delta
            line_limit = _cmt_line_limit(
                production_order,
                activity.activity_type,
                activity.po_line,
            )
            if corrected_line_qty < 0:
                raise ValidationError(f"Koreksi Qty {activity.po_line.sku.sku} tidak boleh negatif.")
            if line_limit is not None and corrected_line_qty > line_limit:
                raise ValidationError(
                    f"Koreksi Qty {activity.po_line.sku.sku} harus berada pada rentang "
                    f"0–{line_limit:.0f} pcs sesuai output tahap sebelumnya."
                )
            downstream_type = {
                ProductionStage.Stage.CUT: ProductionStage.Stage.MAKE,
                ProductionStage.Stage.MAKE: ProductionStage.Stage.TRIM,
            }.get(activity.activity_type)
            if downstream_type:
                downstream_line_qty = _effective_cmt_line_qty(
                    production_order,
                    downstream_type,
                    activity.po_line,
                )
                if corrected_line_qty < downstream_line_qty:
                    raise ValidationError(
                        f"Koreksi {activity.po_line.sku.sku} tidak boleh lebih kecil dari output "
                        f"tahap berikutnya ({downstream_line_qty:.0f} pcs)."
                    )
        if activity.activity_type == ProductionStage.Stage.CUT:
            downstream = stages[ProductionStage.Stage.MAKE].completed_qty
            if new_completed < downstream:
                raise ValidationError(
                    f"Total Cut setelah koreksi tidak boleh lebih kecil dari Qty Make ({downstream:.0f} pcs)."
                )
        if activity.activity_type == ProductionStage.Stage.MAKE:
            downstream = stages[ProductionStage.Stage.TRIM].completed_qty
            if new_completed < downstream:
                raise ValidationError(
                    f"Total Make setelah koreksi tidak boleh lebih kecil dari Qty Trim ({downstream:.0f} pcs)."
                )
        target_dates = {
            ProductionStage.Stage.CUT: (plan.target_cut_start_date, plan.target_cut_end_date),
            ProductionStage.Stage.MAKE: (plan.target_make_start_date, plan.target_make_end_date),
            ProductionStage.Stage.TRIM: (plan.target_trim_start_date, plan.target_trim_end_date),
        }
        target_start, target_end = target_dates[activity.activity_type]
        completion_qty = (
            production_snapshot(production_order)["ordered_qty"]
            if activity.activity_type == ProductionStage.Stage.CUT
            else stages[
                ProductionStage.Stage.CUT
                if activity.activity_type == ProductionStage.Stage.MAKE
                else ProductionStage.Stage.MAKE
            ].completed_qty
        )
        update_stage(
            production_order=production_order,
            stage_code=activity.activity_type,
            target_start_date=target_start,
            target_end_date=target_end,
            actual_start_date=stage.actual_start_date if new_completed > 0 else None,
            actual_end_date=activity_date if new_completed >= completion_qty and completion_qty > 0 else None,
            completed_qty=new_completed,
            notes=notes,
            actor=actor,
        )
        corrected_quantity = new_entry_qty
        after.update(
            {
                "quantity": str(new_entry_qty),
                "delta": str(delta),
                "completed_after": str(new_completed),
            }
        )
    elif activity.activity_type == ProductionActivity.ActivityType.WAREHOUSE_DELIVERY:
        if quantity is None:
            raise ValidationError("Qty yang benar wajib diisi untuk koreksi pengiriman.")
        new_entry_qty = Decimal(quantity)
        if new_entry_qty < 0 or new_entry_qty != new_entry_qty.to_integral_value():
            raise ValidationError("Qty koreksi pengiriman harus berupa bilangan bulat non-negatif.")
        received_for_delivery = (
            activity.inbound_receipts.aggregate(total=Sum("received_qty"))["total"] or Decimal("0")
        )
        if new_entry_qty < received_for_delivery:
            raise ValidationError(
                f"Qty pengiriman tidak boleh lebih kecil dari Qty yang sudah diterima gudang "
                f"({received_for_delivery:.0f} pcs)."
            )
        current_line_shipped = _effective_cmt_line_qty(
            production_order,
            ProductionActivity.ActivityType.WAREHOUSE_DELIVERY,
            activity.po_line,
        )
        other_shipped = current_line_shipped - Decimal(effective.quantity or 0)
        passed = qc_approved_qty(activity.po_line)
        if other_shipped + new_entry_qty > passed:
            raise ValidationError(
                f"Total pengiriman tidak boleh melebihi Qty QC Passed ({passed:.0f} pcs)."
            )
        corrected_quantity = new_entry_qty
        after.update(
            {
                "quantity": str(new_entry_qty),
                "shipped_after": str(other_shipped + new_entry_qty),
            }
        )
    elif activity.activity_type == ProductionActivity.ActivityType.QC:
        from inventory.models import QCInspection

        qc_id = effective.after_values.get("qc_inspection_id") or activity.after_values.get("qc_inspection_id")
        qc = QCInspection.objects.select_for_update().get(pk=qc_id)
        if None in (qty_inspected, qty_passed, qty_failed):
            raise ValidationError("Seluruh Qty QC yang benar wajib diisi.")
        inspected = Decimal(qty_inspected)
        passed = Decimal(qty_passed)
        failed = Decimal(qty_failed)
        if passed + failed != inspected:
            raise ValidationError("Qty Lolos + Qty Gagal harus sama dengan Qty Diperiksa.")
        other_line_inspected = (
            qc.po_line.qc_inspections.exclude(pk=qc.pk).aggregate(total=Sum("qty_inspected"))["total"]
            or Decimal("0")
        ) + qc.po_line.qc_passed_before_cutover_qty
        line_trim_qty = _effective_cmt_line_qty(
            production_order,
            ProductionStage.Stage.TRIM,
            qc.po_line,
        )
        if other_line_inspected + inspected > line_trim_qty:
            raise ValidationError("Koreksi QC melebihi output Trim pada SKU ini.")
        other_po_inspected = sum(
            (
                line.qc_passed_before_cutover_qty
                + (
                    line.qc_inspections.exclude(pk=qc.pk).aggregate(total=Sum("qty_inspected"))["total"]
                    or Decimal("0")
                )
                for line in production_order.po.lines.all()
            ),
            Decimal("0"),
        )
        trim_qty = production_order.stages.get(stage=ProductionStage.Stage.TRIM).completed_qty
        if other_po_inspected + inspected > trim_qty:
            raise ValidationError(f"Koreksi QC melebihi output Trim ({trim_qty:.0f} pcs).")
        other_passed = (
            qc.po_line.qc_passed_before_cutover_qty
            + (
                qc.po_line.qc_inspections.exclude(pk=qc.pk).aggregate(total=Sum("qty_passed"))["total"]
                or Decimal("0")
            )
        )
        received = qc.po_line.received_before_cutover_qty + (
            qc.po_line.inbound_receipts.aggregate(total=Sum("received_qty"))["total"] or Decimal("0")
        )
        if other_passed + passed < received:
            raise ValidationError("Qty QC Passed setelah koreksi tidak boleh lebih kecil dari Qty yang sudah Inbound.")
        qc.inspected_at = timezone.make_aware(datetime.combine(activity_date, time(hour=12)))
        qc.qty_inspected = inspected
        qc.qty_passed = passed
        qc.qty_failed = failed
        qc.failed_disposition = failed_disposition
        qc.notes = notes
        qc.full_clean()
        qc.save()
        corrected_quantity = inspected
        after.update(
            {
                "qc_inspection_id": str(qc.id),
                "sku": qc.po_line.sku.sku,
                "qty_inspected": str(inspected),
                "qty_passed": str(passed),
                "qty_failed": str(failed),
                "failed_disposition": failed_disposition,
            }
        )
    elif activity.activity_type in {
        ProductionActivity.ActivityType.MATERIAL_PURCHASE,
        ProductionActivity.ActivityType.MATERIAL_ARRIVAL,
    }:
        material = production_order.stages.get(stage=ProductionStage.Stage.MATERIAL_PURCHASE)
        update_stage(
            production_order=production_order,
            stage_code=ProductionStage.Stage.MATERIAL_PURCHASE,
            status=material.status,
            target_start_date=plan.target_material_purchase_date,
            actual_start_date=(activity_date if activity.activity_type == ProductionActivity.ActivityType.MATERIAL_PURCHASE else material.actual_start_date),
            material_arrival_date=(activity_date if activity.activity_type == ProductionActivity.ActivityType.MATERIAL_ARRIVAL else material.material_arrival_date),
            notes=notes,
            actor=actor,
        )
    elif activity.activity_type == ProductionActivity.ActivityType.TRIAL_SUBMIT:
        trial_id = effective.after_values.get("trial_id") or activity.after_values.get("trial_id")
        trial = ProductionTrial.objects.select_for_update().get(pk=trial_id)
        trial.trial_date = activity_date
        trial.notes = notes or trial.notes
        trial.full_clean()
        trial.save(update_fields=("trial_date", "notes"))
        after["trial_id"] = str(trial.id)
    else:
        raise ValidationError("Keputusan approval/revision tidak dikoreksi langsung; gunakan revision activity baru.")

    description = f"Koreksi {activity.get_activity_type_display()} dibuat. Alasan: {reason}"
    return _activity(
        production_order=production_order,
        actor=actor,
        action="production_activity_corrected",
        stage=activity.stage,
        entry_kind=ProductionActivity.EntryKind.CORRECTION,
        activity_type=activity.activity_type,
        activity_date=activity_date,
        quantity=corrected_quantity,
        po_line=activity.po_line,
        source_activity=activity,
        delivery_order=activity.delivery_order,
        notes=notes,
        description=description,
        before=before,
        after=after,
    )


def production_snapshot(production_order):
    stages = _stage_map(production_order)
    trial = latest_trial(production_order)
    po = production_order.po
    lines = list(po.lines.all())
    ordered = sum((line.ordered_qty for line in lines), Decimal("0"))
    inspected = sum(
        (
            line.qc_passed_before_cutover_qty
            + (line.qc_inspections.aggregate(total=Sum("qty_inspected"))["total"] or Decimal("0"))
            for line in lines
        ),
        Decimal("0"),
    )
    passed = sum((qc_approved_qty(line) for line in lines), Decimal("0"))
    rework_re_qc_qty = sum(
        (
            follow_up.open_qty
            for line in lines
            for follow_up in line.qc_follow_ups.all()
            if follow_up.status in {
                QCFollowUp.Status.AWAITING_REWORK,
                QCFollowUp.Status.READY_RE_QC,
            }
        ),
        Decimal("0"),
    )
    rejected_qty = sum(
        (
            follow_up.open_qty
            for line in lines
            for follow_up in line.qc_follow_ups.all()
            if follow_up.status == QCFollowUp.Status.REJECTED
        ),
        Decimal("0"),
    )
    received = sum(
        (
            line.received_before_cutover_qty
            + (line.inbound_receipts.aggregate(total=Sum("received_qty"))["total"] or Decimal("0"))
            for line in lines
        ),
        Decimal("0"),
    )
    shipped = sum(
        (
            _effective_cmt_line_qty(
                production_order,
                ProductionActivity.ActivityType.WAREHOUSE_DELIVERY,
                line,
            )
            for line in lines
        ),
        Decimal("0"),
    )
    linked_received = sum(
        (
            line.inbound_receipts.filter(delivery_activity__isnull=False).aggregate(total=Sum("received_qty"))[
                "total"
            ]
            or Decimal("0")
            for line in lines
        ),
        Decimal("0"),
    )
    legacy_received = received - linked_received
    delivering = max(shipped - linked_received, Decimal("0"))
    ready_to_deliver = max(passed - shipped - legacy_received, Decimal("0"))
    trim_complete = bool(
        stages.get(ProductionStage.Stage.TRIM)
        and stages[ProductionStage.Stage.TRIM].status == ProductionStage.Status.COMPLETE
        and stages[ProductionStage.Stage.TRIM].completed_qty > 0
    )
    trim_completed_qty = (
        stages[ProductionStage.Stage.TRIM].completed_qty
        if stages.get(ProductionStage.Stage.TRIM)
        else Decimal("0")
    )
    cmt_quantities = {}
    for stage_code in CMT_STAGES:
        stage_row = stages.get(stage_code)
        completed_qty = stage_row.completed_qty if stage_row else Decimal("0")
        available_qty = _cmt_available_qty(stages, stage_code, ordered) if stage_row else Decimal("0")
        cmt_quantities[stage_code] = {
            "available_qty": available_qty,
            "completed_qty": completed_qty,
            "remaining_qty": max(available_qty - completed_qty, Decimal("0")),
            "waiting_upstream_qty": max(ordered - available_qty, Decimal("0")),
        }
    if inspected > 0:
        if trim_complete and inspected >= trim_completed_qty:
            current_code, current_label = "QC_COMPLETE", "QC Complete"
        else:
            current_code, current_label = "QC_IN_PROGRESS", "QC In Progress"
    elif trim_complete:
        current_code, current_label = "READY_FOR_QC", "Ready for QC"
    elif stages.get(ProductionStage.Stage.TRIM) and stages[ProductionStage.Stage.TRIM].status != ProductionStage.Status.NOT_STARTED:
        current_code, current_label = "TRIM", "Trim · Finishing"
    elif stages.get(ProductionStage.Stage.MAKE) and stages[ProductionStage.Stage.MAKE].status != ProductionStage.Status.NOT_STARTED:
        current_code, current_label = "MAKE", "Make · Jahit"
    elif stages.get(ProductionStage.Stage.CUT) and stages[ProductionStage.Stage.CUT].status != ProductionStage.Status.NOT_STARTED:
        current_code, current_label = "CUT", "Cut · Potong"
    elif trial:
        current_code, current_label = "TRIAL", {
            ProductionTrial.Status.IN_PROGRESS: "Trial Production In Progress",
            ProductionTrial.Status.WAITING_APPROVAL: "Trial Production Waiting Approval",
            ProductionTrial.Status.APPROVED: "Trial Production Approved",
            ProductionTrial.Status.REVISION_REQUIRED: "Trial Production Revision Required",
        }[trial.status]
    else:
        current_code, current_label = "MATERIAL_PURCHASE", "Waiting Material Purchase"

    try:
        plan = production_order.plan
    except ProductionPlan.DoesNotExist:
        plan = None

    material_stage = stages.get(ProductionStage.Stage.MATERIAL_PURCHASE)
    cut_completed_qty = cmt_quantities[ProductionStage.Stage.CUT]["completed_qty"]
    make_completed_qty = cmt_quantities[ProductionStage.Stage.MAKE]["completed_qty"]
    material_status_display = (
        "Material telah diproses"
        if cut_completed_qty > 0
        else material_stage.operational_status_display
    )
    inbound_complete = bool(trim_complete and inspected >= trim_completed_qty and passed > 0 and received >= passed)
    qc_complete = bool(trim_complete and trim_completed_qty > 0 and inspected >= trim_completed_qty)

    if inbound_complete:
        passed_process_label, next_process_label = "Inbound Warehouse", "Completed"
    elif delivering > 0:
        passed_process_label, next_process_label = "Deliver to Warehouse", "Warehouse Receive"
    elif qc_complete:
        passed_process_label, next_process_label = "Quality Control", "Deliver to Warehouse"
    elif trim_complete:
        passed_process_label, next_process_label = "Trim · Finishing", "Quality Control"
    elif ordered > 0 and make_completed_qty >= ordered:
        passed_process_label, next_process_label = "Make · Jahit", "Trim · Finishing"
    elif ordered > 0 and cut_completed_qty >= ordered:
        passed_process_label, next_process_label = "Cut - Potong", "Make · Jahit"
    elif trial and trial.status == ProductionTrial.Status.APPROVED:
        passed_process_label, next_process_label = "Trial Production Approved", "Cut - Potong"
    elif material_stage and material_stage.material_arrival_date:
        passed_process_label, next_process_label = "Material Ready", "Trial Production"
    elif material_stage and material_stage.actual_start_date:
        passed_process_label, next_process_label = "Pembelian Material", "Material Arrival"
    elif plan and plan.status == ProductionPlan.Status.ACTIVE:
        passed_process_label, next_process_label = "Production Plan Active", "Pembelian Material"
    else:
        passed_process_label, next_process_label = "Belum ada", "Production Plan"
    plan_targets = {
        "MATERIAL_PURCHASE": getattr(plan, "target_material_purchase_date", None),
        "TRIAL": getattr(plan, "target_trial_date", None),
        "CUT": getattr(plan, "target_cut_end_date", None),
        "MAKE": getattr(plan, "target_make_end_date", None),
        "TRIM": getattr(plan, "target_trim_end_date", None),
        "READY_FOR_QC": getattr(plan, "target_qc_end_date", None),
        "QC_IN_PROGRESS": getattr(plan, "target_qc_end_date", None),
        "QC_COMPLETE": getattr(plan, "target_inbound_date", None),
    }
    target = plan_targets.get(current_code) or next(
        (
            stage.target_end_date
            for stage in reversed([stages[code] for code in STAGE_SEQUENCE if code in stages])
            if stage.target_end_date
        ),
        po.required_arrival,
    )
    is_late = bool(target and target < timezone.localdate() and received < ordered)
    timing = {}
    if plan:
        cmt_actual_dates = _effective_cmt_dates(production_order, ordered)
        cmt_target_fields = {
            ProductionStage.Stage.CUT: (plan.target_cut_start_date, plan.target_cut_end_date),
            ProductionStage.Stage.MAKE: (plan.target_make_start_date, plan.target_make_end_date),
            ProductionStage.Stage.TRIM: (plan.target_trim_start_date, plan.target_trim_end_date),
        }
        for stage_code, (target_start, target_end) in cmt_target_fields.items():
            timing[stage_code] = _timing_row(
                target_start=target_start,
                target_end=target_end,
                actual_start=cmt_actual_dates[stage_code]["actual_start"],
                actual_end=cmt_actual_dates[stage_code]["actual_end"],
            )

        qc_rows = sorted(
            [inspection for line in lines for inspection in line.qc_inspections.all()],
            key=lambda row: row.inspected_at,
        )
        qc_actual_start = _local_calendar_date(qc_rows[0].inspected_at) if qc_rows else None
        qc_actual_end = _cumulative_completion_date(
            qc_rows,
            required_qty=trim_completed_qty,
            opening_qty=sum((line.qc_passed_before_cutover_qty for line in lines), Decimal("0")),
            quantity_getter=lambda row: row.qty_inspected,
            date_getter=lambda row: _local_calendar_date(row.inspected_at),
        )
        timing["QC"] = _timing_row(
            target_start=plan.target_qc_start_date,
            target_end=plan.target_qc_end_date,
            actual_start=qc_actual_start,
            actual_end=qc_actual_end,
        )

        inbound_rows = sorted(
            [receipt for line in lines for receipt in line.inbound_receipts.all()],
            key=lambda row: row.inbound_date,
        )
        inbound_actual_start = inbound_rows[0].inbound_date if inbound_rows else None
        inbound_actual_end = _cumulative_completion_date(
            inbound_rows,
            required_qty=passed,
            opening_qty=sum((line.received_before_cutover_qty for line in lines), Decimal("0")),
            quantity_getter=lambda row: row.received_qty,
            date_getter=lambda row: row.inbound_date,
        )
        timing["INBOUND"] = _timing_row(
            target_start=plan.target_inbound_date,
            target_end=plan.target_inbound_date,
            actual_start=inbound_actual_start,
            actual_end=inbound_actual_end,
        )
    return {
        "production_order": production_order,
        "stages": stages,
        "trial": trial,
        "ordered_qty": ordered,
        "inspected_qty": inspected,
        "remaining_qc_qty": max(trim_completed_qty - inspected, Decimal("0")),
        "passed_qty": passed,
        "rework_re_qc_qty": rework_re_qc_qty,
        "rejected_qty": rejected_qty,
        "received_qty": received,
        "ready_inbound_qty": max(passed - received, Decimal("0")),
        "ready_to_deliver_qty": ready_to_deliver,
        "delivering_qty": delivering,
        "shipped_qty": shipped,
        "material_status_display": material_status_display,
        "current_code": current_code,
        "current_label": current_label,
        "current_process_label": current_label,
        "passed_process_label": passed_process_label,
        "next_process_label": next_process_label,
        "target_date": target,
        "is_late": is_late,
        "qc_open": trim_completed_qty > inspected,
        "trim_completed_qty": trim_completed_qty,
        "plan": plan,
        "plan_status": plan.status if plan else "UNPLANNED",
        "cmt_quantities": cmt_quantities,
        "timing": timing,
    }


def production_cogs_finalization_card(production_order, actor=None):
    try:
        approved = production_order.cogs_finalization
    except ProductionCogsFinalization.DoesNotExist:
        approved = None
    if approved:
        decimal_fields = (
            "ordered_qty",
            "sellable_qty",
            "rejected_qty",
            "shortage_qty",
            "excess_qty",
            "initial_unit_cogs",
            "final_unit_cogs",
            "unit_cogs_increase",
            "original_total_cost",
        )
        rows = []
        for snapshot_row in approved.line_snapshot:
            row = {**snapshot_row}
            for name in decimal_fields:
                row[name] = Decimal(row.get(name, "0"))
            row.setdefault("parent_sku", "—")
            rows.append(row)
        approved_parents = {}
        for row in rows:
            parent = approved_parents.setdefault(
                row.get("parent_id") or row["po_line_id"],
                {"ordered": Decimal("0"), "output": Decimal("0")},
            )
            parent["ordered"] += row["ordered_qty"]
            parent["output"] += row["sellable_qty"] + row["rejected_qty"]
        return {
            "rows": rows,
            "approved": approved,
            "ready": False,
            "can_approve": False,
            "status": "Approved",
            "status_class": "ready",
            "blockers": [],
            "total_ordered_qty": sum((row["ordered_qty"] for row in rows), Decimal("0")),
            "total_sellable_qty": sum((row["sellable_qty"] for row in rows), Decimal("0")),
            "total_rejected_qty": sum((row["rejected_qty"] for row in rows), Decimal("0")),
            "total_shortage_qty": sum(
                (max(parent["ordered"] - parent["output"], Decimal("0")) for parent in approved_parents.values()),
                Decimal("0"),
            ),
            "total_excess_qty": sum(
                (max(parent["output"] - parent["ordered"], Decimal("0")) for parent in approved_parents.values()),
                Decimal("0"),
            ),
            "total_po_cost": approved.total_po_cost,
        }

    snapshot = production_snapshot(production_order)
    blockers = []
    if snapshot["remaining_qc_qty"] > 0:
        blockers.append(f"Masih ada {snapshot['remaining_qc_qty']:.0f} pcs yang wajib di-QC.")
    unresolved = QCFollowUp.objects.filter(
        po_line__po=production_order.po,
        status__in=(
            QCFollowUp.Status.AWAITING_REWORK,
            QCFollowUp.Status.READY_RE_QC,
            QCFollowUp.Status.ACCEPTED_EXCEPTION,
        ),
    ).count()
    if unresolved:
        blockers.append(f"Masih ada {unresolved} tindak lanjut QC yang belum final.")
    if snapshot["ready_to_deliver_qty"] > 0 or snapshot["delivering_qty"] > 0:
        blockers.append("Seluruh QC Passed wajib selesai dikirim dan diterima Warehouse.")
    if snapshot["received_qty"] != snapshot["passed_qty"]:
        blockers.append("Qty Inbound harus sama dengan Qty QC Passed.")

    rows = []
    parent_groups = {}
    for line in production_order.po.lines.select_related(
        "sku__product_variant__product__status"
    ).prefetch_related("inbound_receipts", "qc_follow_ups", "fifo_layers"):
        ordered_qty = Decimal(line.ordered_qty)
        sellable_qty = line.received_before_cutover_qty + sum(
            (receipt.received_qty for receipt in line.inbound_receipts.all()), Decimal("0")
        )
        rejected_qty = sum(
            (
                follow_up.open_qty
                for follow_up in line.qc_follow_ups.all()
                if follow_up.status == QCFollowUp.Status.REJECTED
            ),
            Decimal("0"),
        )
        output_qty = sellable_qty + rejected_qty
        shortage_qty = max(ordered_qty - output_qty, Decimal("0"))
        excess_qty = max(output_qty - ordered_qty, Decimal("0"))
        initial_unit_cogs = Decimal(line.cogs_snapshot or 0)
        original_total_cost = ordered_qty * initial_unit_cogs
        product = line.sku.product_variant.product
        if line.cogs_snapshot is None:
            blockers.append(f"COGS snapshot {line.sku.sku} belum tersedia.")
        if any(layer.allocations.exists() for layer in line.fifo_layers.all()):
            blockers.append(f"FIFO {line.sku.sku} sudah terpakai Sales Out dan memerlukan adjustment Finance.")
        row = {
            "po_line_id": str(line.id),
            "parent_id": str(product.id),
            "parent_sku": product.parent_sku or product.code,
            "sku": line.sku.sku,
            "product_name": product.name,
            "size": line.sku.size or "—",
            "ordered_qty": ordered_qty,
            "sellable_qty": sellable_qty,
            "rejected_qty": rejected_qty,
            "shortage_qty": shortage_qty,
            "excess_qty": excess_qty,
            "initial_unit_cogs": initial_unit_cogs,
            "original_total_cost": original_total_cost,
        }
        rows.append(row)
        group = parent_groups.setdefault(
            row["parent_id"],
            {"rows": [], "ordered": Decimal("0"), "sellable": Decimal("0"), "rejected": Decimal("0"), "cost": Decimal("0")},
        )
        group["rows"].append(row)
        group["ordered"] += ordered_qty
        group["sellable"] += sellable_qty
        group["rejected"] += rejected_qty
        group["cost"] += original_total_cost

    total_shortage_qty = Decimal("0")
    total_excess_qty = Decimal("0")
    for group in parent_groups.values():
        output_qty = group["sellable"] + group["rejected"]
        total_shortage_qty += max(group["ordered"] - output_qty, Decimal("0"))
        total_excess_qty += max(output_qty - group["ordered"], Decimal("0"))
        if group["sellable"] <= 0:
            blockers.append(f"Parent SKU {group['rows'][0]['parent_sku']} tidak memiliki barang sellable untuk menyerap biaya.")
            final_unit_cogs = Decimal("0")
        else:
            final_unit_cogs = (group["cost"] / group["sellable"]).quantize(
                COGS_QUANT,
                rounding=ROUND_HALF_UP,
            )
        for row in group["rows"]:
            row["final_unit_cogs"] = final_unit_cogs
            row["unit_cogs_increase"] = final_unit_cogs - row["initial_unit_cogs"]

    ready = not blockers
    return {
        "rows": rows,
        "approved": None,
        "ready": ready,
        "can_approve": bool(
            ready and actor and actor.has_perm("production.approve_cogs_finalization")
        ),
        "status": "Ready for Approval" if ready else "Belum Siap",
        "status_class": "parsing" if ready else "blocked",
        "blockers": blockers,
        "total_ordered_qty": sum((row["ordered_qty"] for row in rows), Decimal("0")),
        "total_sellable_qty": sum((row["sellable_qty"] for row in rows), Decimal("0")),
        "total_rejected_qty": sum((row["rejected_qty"] for row in rows), Decimal("0")),
        "total_shortage_qty": total_shortage_qty,
        "total_excess_qty": total_excess_qty,
        "total_po_cost": sum((row["original_total_cost"] for row in rows), Decimal("0")),
    }


@transaction.atomic
def approve_production_cogs_finalization(*, production_order, actor):
    if not actor.has_perm("production.approve_cogs_finalization"):
        raise PermissionDenied("Hanya Manager atau user berwenang yang dapat approve finalisasi COGS.")
    production_order = ProductionOrder.objects.select_for_update().select_related("po").get(
        pk=production_order.pk
    )
    if ProductionCogsFinalization.objects.filter(production_order=production_order).exists():
        raise ValidationError("Finalisasi Quantity & COGS sudah di-approve.")
    card = production_cogs_finalization_card(production_order, actor=actor)
    if not card["ready"]:
        raise ValidationError(card["blockers"])

    line_snapshot = [
        {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in row.items()
        }
        for row in card["rows"]
    ]
    finalization = ProductionCogsFinalization.objects.create(
        production_order=production_order,
        line_snapshot=line_snapshot,
        total_po_cost=card["total_po_cost"],
        total_final_cost=card["total_po_cost"],
        approved_by=actor,
    )

    lines = {
        str(line.id): line
        for line in production_order.po.lines.select_for_update().select_related(
            "sku__product_variant__product__status"
        )
    }
    parent_movements = {}
    parent_costs = {}
    for row in card["rows"]:
        line = lines[row["po_line_id"]]
        layers = list(
            FIFOLayer.objects.select_for_update()
            .filter(source_po_line=line)
            .select_related("opening_movement")
            .order_by("receipt_date", "created_at")
        )
        if sum((layer.original_qty for layer in layers), Decimal("0")) != row["sellable_qty"]:
            raise ValidationError(f"FIFO layer {row['sku']} tidak sama dengan Final Sellable Qty.")
        if any(layer.allocations.exists() for layer in layers):
            raise ValidationError(f"FIFO {row['sku']} sudah terpakai Sales Out.")

        updated_movements = parent_movements.setdefault(row["parent_id"], [])
        parent_costs[row["parent_id"]] = parent_costs.get(row["parent_id"], Decimal("0")) + row["original_total_cost"]
        for layer in layers:
            layer.unit_cost = row["final_unit_cogs"]
            layer.save(update_fields=("unit_cost",))
            movement = layer.opening_movement
            movement.allocated_cost = layer.original_qty * row["final_unit_cogs"]
            movement.save(update_fields=("allocated_cost",))
            updated_movements.append(movement)

        previous_master_cogs = line.sku.current_master_cogs
        if previous_master_cogs != row["final_unit_cogs"]:
            line.sku.current_master_cogs = row["final_unit_cogs"]
            line.sku.save(update_fields=("current_master_cogs", "updated_at"))
            SKUValueHistory.objects.create(
                sku=line.sku,
                retail_price=line.sku.current_retail_price,
                master_cogs=row["final_unit_cogs"],
                product_status=line.sku.product_variant.product.status,
                source_batch_id=finalization.id,
                changed_by=actor,
                changes={
                    "current_master_cogs": {
                        "before": str(previous_master_cogs or ""),
                        "after": str(row["final_unit_cogs"]),
                        "reason": "Production Quantity & COGS finalization",
                    }
                },
            )

    for parent_id, movements in parent_movements.items():
        if not movements:
            continue
        residual = parent_costs[parent_id] - sum(
            (movement.allocated_cost for movement in movements), Decimal("0")
        )
        if residual:
            movements[-1].allocated_cost += residual
            movements[-1].save(update_fields=("allocated_cost",))

    ProductionActivity.objects.create(
        production_order=production_order,
        action="production_cogs_finalized",
        entry_kind=ProductionActivity.EntryKind.SYSTEM,
        description=(
            f"Finalisasi Quantity & COGS di-approve. Final sellable "
            f"{card['total_sellable_qty']:.0f} pcs; rejected {card['total_rejected_qty']:.0f} pcs; "
            f"total biaya PO Rp {card['total_po_cost']:.0f}."
        ),
        after_values={
            "finalization_id": str(finalization.id),
            "sellable_qty": str(card["total_sellable_qty"]),
            "rejected_qty": str(card["total_rejected_qty"]),
            "shortage_qty": str(card["total_shortage_qty"]),
            "total_po_cost": str(card["total_po_cost"]),
        },
        actor=actor,
    )
    record_audit(
        actor=actor,
        action="production_cogs_finalized",
        entity_type="production.productioncogsfinalization",
        entity_id=finalization.id,
        after_values={
            "production_order": str(production_order.id),
            "po_number": production_order.po.po_number,
            "line_snapshot": line_snapshot,
            "total_po_cost": str(card["total_po_cost"]),
        },
    )
    return finalization
