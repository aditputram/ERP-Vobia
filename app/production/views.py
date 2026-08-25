from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from inventory.models import QCFollowUp, QCFollowUpEvent, QCInspection
from inventory.services.fifo import complete_qc_rework, record_re_qc
from purchasing.models import PurchaseOrder

from .forms import (
    ProductionActivityForm,
    ProductionCorrectionForm,
    ProductionPlanForm,
    ReQCForm,
    ReworkCompletionForm,
)
from .models import ProductionActivity, ProductionOrder, ProductionPlan, ProductionStage, ProductionTrial
from .services import (
    CMT_STAGES,
    approve_production_cogs_finalization,
    correct_production_activity,
    eligible_activity_choices,
    ensure_production_order,
    latest_activity_version,
    next_delivery_order_number,
    production_process_details,
    production_cogs_finalization_card,
    production_snapshot,
    save_production_plan,
    submit_cmt_activity_batch,
    submit_delivery_activity_batch,
    submit_qc_activity_batch,
    submit_rejected_delivery_activity_batch,
    submit_production_activity,
)


def _ensure_released_workflows(actor=None):
    released = PurchaseOrder.objects.filter(status=PurchaseOrder.Status.RELEASED).select_related("supplier")
    for po in released:
        ensure_production_order(po, actor=actor)


def _production_queryset():
    return ProductionOrder.objects.select_related("po", "po__supplier", "plan", "cogs_finalization").prefetch_related(
        "stages",
        "trials",
        "po__lines__sku__product_variant__product",
        "po__lines__qc_inspections",
        "po__lines__qc_follow_ups",
        "po__lines__inbound_receipts",
    )


def _snapshots(queryset):
    return [production_snapshot(row) for row in queryset]


def _qc_follow_up_queryset():
    return QCFollowUp.objects.select_related(
        "source_inspection__recorded_by",
        "po_line__po__supplier",
        "po_line__po__production_order",
        "po_line__sku__product_variant__product",
    ).prefetch_related("events__actor")


def _production_audit_rows(production_order):
    disposition_labels = dict(QCInspection.Disposition.choices)
    rows = []
    activities = production_order.activities.select_related("actor", "po_line__sku")
    for activity in activities:
        description = activity.description
        if (
            activity.activity_type == ProductionActivity.ActivityType.QC
            and Decimal(activity.after_values.get("qty_failed") or 0) > 0
        ):
            disposition = activity.after_values.get("failed_disposition", "")
            if "Tindak lanjut:" not in description:
                description += f" Tindak lanjut: {disposition_labels.get(disposition, disposition or '—')}."
            if "Alasan gagal:" not in description:
                description += (
                    f" Alasan gagal: {activity.notes.strip()}."
                    if activity.notes.strip()
                    else " Alasan gagal: belum dicatat."
                )
        rows.append(
            {
                "occurred_at": activity.occurred_at,
                "activity_date": activity.activity_date,
                "entry_kind_display": activity.get_entry_kind_display(),
                "entry_kind_class": (
                    "blocked"
                    if activity.entry_kind == ProductionActivity.EntryKind.CORRECTION
                    else "ready" if activity.entry_kind == ProductionActivity.EntryKind.ACTIVITY else ""
                ),
                "activity_label": (
                    activity.get_activity_type_display()
                    or {
                        "production_cogs_finalized": "Finalisasi Quantity & COGS",
                    }.get(activity.action, activity.action)
                ),
                "quantity": activity.quantity,
                "sku": activity.po_line.sku.sku if activity.po_line else "",
                "description": description,
                "actor_username": activity.actor.username if activity.actor else "System",
            }
        )

    events = QCFollowUpEvent.objects.filter(
        follow_up__po_line__po=production_order.po,
        event_type__in=(
            QCFollowUpEvent.EventType.REWORK_COMPLETED,
            QCFollowUpEvent.EventType.RE_QC,
        ),
    ).select_related("follow_up__po_line__sku", "actor")
    for event in events:
        sku = event.follow_up.po_line.sku.sku
        if event.event_type == QCFollowUpEvent.EventType.REWORK_COMPLETED:
            quantity = event.qty_inspected or event.follow_up.original_failed_qty
            description = f"Rework {sku}: {quantity:.0f} pcs selesai."
        else:
            quantity = event.qty_inspected
            description = (
                f"Re-QC {sku}: diperiksa {event.qty_inspected:.0f}, "
                f"lolos {event.qty_passed:.0f}, gagal {event.qty_failed:.0f} pcs."
            )
            if event.qty_failed > 0:
                description += (
                    f" Tindak lanjut: "
                    f"{disposition_labels.get(event.failed_disposition, event.failed_disposition or '—')}."
                )
        if event.notes.strip():
            description += f" Catatan: {event.notes.strip()}."
        rows.append(
            {
                "occurred_at": event.created_at,
                "activity_date": event.activity_date,
                "entry_kind_display": "Production Activity",
                "entry_kind_class": "ready",
                "activity_label": event.get_event_type_display(),
                "quantity": quantity,
                "sku": sku,
                "description": description,
                "actor_username": event.actor.username,
            }
        )
    return sorted(rows, key=lambda row: row["occurred_at"], reverse=True)


@login_required
def dashboard(request):
    _ensure_released_workflows(request.user)
    snapshots = _snapshots(_production_queryset().filter(plan__status=ProductionPlan.Status.ACTIVE))
    metrics = {
        "total": len(snapshots),
        "waiting_trial": sum(1 for row in snapshots if row["current_code"] == "TRIAL"),
        "mass_production": sum(1 for row in snapshots if row["current_code"] in {"CUT", "MAKE", "TRIM"}),
        "ready_qc": sum(1 for row in snapshots if row["current_code"] in {"READY_FOR_QC", "QC_IN_PROGRESS"}),
        "late": sum(1 for row in snapshots if row["is_late"]),
    }
    return render(
        request,
        "production/dashboard.html",
        {
            "metrics": metrics,
            "rows": snapshots[:20],
            "waiting_approvals": ProductionTrial.objects.filter(
                status=ProductionTrial.Status.WAITING_APPROVAL
            ).select_related("production_order__po", "production_order__po__supplier", "submitted_by")[:10],
        },
    )


@login_required
def monitoring(request):
    _ensure_released_workflows(request.user)
    query = request.GET.get("q", "").strip()
    stage = request.GET.get("stage", "").strip()
    late = request.GET.get("late", "").strip()
    rows = _snapshots(_production_queryset().filter(plan__status=ProductionPlan.Status.ACTIVE))
    if query:
        query_lower = query.lower()
        rows = [
            row
            for row in rows
            if query_lower in (row["production_order"].po.po_number or "").lower()
            or query_lower in row["production_order"].po.supplier.name.lower()
        ]
    if stage:
        rows = [row for row in rows if row["current_code"] == stage]
    if late == "1":
        rows = [row for row in rows if row["is_late"]]
    qc_follow_up_rows = _qc_follow_up_queryset().filter(
        po_line__po__production_order__plan__status=ProductionPlan.Status.ACTIVE,
        status__in=(
            QCFollowUp.Status.AWAITING_REWORK,
            QCFollowUp.Status.READY_RE_QC,
            QCFollowUp.Status.REJECTED,
            QCFollowUp.Status.ACCEPTED_EXCEPTION,
        ),
    )
    return render(
        request,
        "production/monitoring.html",
        {
            "rows": rows,
            "query": query,
            "selected_stage": stage,
            "late": late,
            "qc_follow_up_rows": qc_follow_up_rows,
            "qc_rework_count": qc_follow_up_rows.filter(status=QCFollowUp.Status.AWAITING_REWORK).count(),
            "qc_ready_count": qc_follow_up_rows.filter(status=QCFollowUp.Status.READY_RE_QC).count(),
            "qc_rejected_count": qc_follow_up_rows.filter(status=QCFollowUp.Status.REJECTED).count(),
        },
    )


@login_required
def rejected_goods(request):
    rejected_items = _qc_follow_up_queryset().filter(status=QCFollowUp.Status.REJECTED)
    rows = []
    for item in rejected_items:
        rejected_event = next(
            (
                event
                for event in item.events.all()
                if event.event_type == QCFollowUpEvent.EventType.RE_QC
                and event.failed_disposition == QCInspection.Disposition.REJECTED
            ),
            None,
        )
        rows.append(
            {
                "item": item,
                "rejected_date": (
                    rejected_event.activity_date
                    if rejected_event
                    else timezone.localdate(item.source_inspection.inspected_at)
                ),
                "reason": (
                    (rejected_event.notes if rejected_event else item.source_inspection.notes).strip()
                    or "Belum dicatat"
                ),
                "actor": rejected_event.actor if rejected_event else item.source_inspection.recorded_by,
            }
        )
    return render(
        request,
        "production/rejected_goods.html",
        {
            "rows": rows,
            "total_rejected_qty": sum((row["item"].open_qty for row in rows), Decimal("0")),
            "delivery_totals": {
                status: sum(
                    (row["item"].open_qty for row in rows if row["item"].delivery_status == status),
                    Decimal("0"),
                )
                for status in QCFollowUp.DeliveryStatus.values
            },
        },
    )


@login_required
def planning(request):
    _ensure_released_workflows(request.user)
    selected_id = request.POST.get("production_order") or request.GET.get("production_order", "")
    selected = None
    if selected_id:
        selected = get_object_or_404(_production_queryset(), pk=selected_id)
    plan = None
    if selected:
        try:
            plan = selected.plan
        except ProductionPlan.DoesNotExist:
            plan = ProductionPlan(production_order=selected, created_by=request.user)
    form = ProductionPlanForm(instance=plan) if selected else None
    if request.method == "POST" and selected:
        form = ProductionPlanForm(request.POST, instance=plan)
        action = request.POST.get("action", "save")
        if form.is_valid():
            try:
                values = {name: form.cleaned_data[name] for name in form.Meta.fields}
                saved_plan = save_production_plan(
                    production_order=selected,
                    values=values,
                    activate=action == "activate",
                    actor=request.user,
                    change_reason=form.cleaned_data.get("change_reason", ""),
                )
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                if action == "activate":
                    success_message = "Production Plan diaktifkan dan masuk Monitoring."
                elif saved_plan.status == ProductionPlan.Status.ACTIVE:
                    success_message = "Revisi Production Plan tersimpan dan Monitoring diperbarui."
                else:
                    success_message = "Production Plan disimpan sebagai Draft."
                messages.success(request, success_message)
                return redirect(f"{reverse('production:planning')}?production_order={selected.id}")
    rows = _production_queryset().order_by("po__po_number")
    planning_rows = rows.filter(
        Q(plan__isnull=True) | Q(plan__status=ProductionPlan.Status.DRAFT)
    )
    return render(
        request,
        "production/planning.html",
        {
            "rows": rows,
            "planning_rows": planning_rows,
            "selected": selected,
            "plan": plan,
            "plan_form": form,
        },
    )


@login_required
def activity(request):
    _ensure_released_workflows(request.user)
    selected_id = request.POST.get("production_order") or request.GET.get("production_order", "")
    selected = None
    if selected_id:
        selected = get_object_or_404(
            _production_queryset().filter(plan__status=ProductionPlan.Status.ACTIVE),
            pk=selected_id,
        )
    follow_up_rows = _qc_follow_up_queryset().filter(po_line__po=selected.po) if selected else QCFollowUp.objects.none()
    follow_up_action = request.POST.get("action") or request.GET.get("action", "")
    follow_up_id = request.POST.get("follow_up") or request.GET.get("follow_up", "")
    selected_follow_up = None
    follow_up_form = None
    if follow_up_id:
        selected_follow_up = get_object_or_404(follow_up_rows, pk=follow_up_id)
        if follow_up_action == "complete_rework":
            follow_up_form = ReworkCompletionForm(request.POST or None, prefix="follow_up")
        elif follow_up_action == "re_qc":
            follow_up_form = ReQCForm(
                request.POST or None,
                max_qty=selected_follow_up.open_qty,
                prefix="follow_up",
            )

    is_follow_up_post = request.method == "POST" and request.POST.get("form_name") == "qc_follow_up"
    if is_follow_up_post and selected_follow_up and follow_up_form and follow_up_form.is_valid():
        try:
            if follow_up_action == "complete_rework":
                complete_qc_rework(
                    follow_up=selected_follow_up,
                    actor=request.user,
                    **follow_up_form.cleaned_data,
                )
                success = "Rework selesai; lanjutkan dengan Re-QC di Production Activity."
                next_action = "re_qc"
            else:
                record_re_qc(
                    follow_up=selected_follow_up,
                    actor=request.user,
                    **follow_up_form.cleaned_data,
                )
                selected_follow_up.refresh_from_db()
                success = "Hasil Re-QC tersimpan dan Ready Inbound diperbarui."
                next_action = (
                    "complete_rework"
                    if selected_follow_up.status == QCFollowUp.Status.AWAITING_REWORK
                    else ""
                )
        except ValidationError as exc:
            follow_up_form.add_error(None, exc)
        else:
            messages.success(request, success)
            target = f"{reverse('production:activity')}?production_order={selected.id}"
            if next_action:
                target += f"&follow_up={selected_follow_up.id}&action={next_action}"
            return redirect(f"{target}#qc-follow-up")

    choices = eligible_activity_choices(selected) if selected else []
    form = ProductionActivityForm(
        request.POST if request.method == "POST" and not is_follow_up_post else None,
        production_order=selected,
        eligible_choices=choices,
        initial={"production_order": selected} if selected else None,
    )
    if request.method == "POST" and form.is_valid():
        submitted_delivery_order = None
        try:
            values = form.cleaned_data.copy()
            production_order = values.pop("production_order")
            activity_type = values.get("activity_type")
            if activity_type in {
                ProductionActivity.ActivityType.CUT,
                ProductionActivity.ActivityType.MAKE,
                ProductionActivity.ActivityType.TRIM,
            }:
                submit_cmt_activity_batch(
                    production_order=production_order,
                    activity_type=activity_type,
                    activity_date=values.get("activity_date"),
                    line_quantities=form.cmt_line_quantities(),
                    notes=values.get("notes", ""),
                    actor=request.user,
                )
            elif activity_type == ProductionActivity.ActivityType.WAREHOUSE_DELIVERY:
                submitted_delivery_order, _activities = submit_delivery_activity_batch(
                    production_order=production_order,
                    activity_date=values.get("activity_date"),
                    line_quantities=form.cmt_line_quantities(),
                    notes=values.get("notes", ""),
                    actor=request.user,
                )
            elif activity_type == ProductionActivity.ActivityType.REJECTED_WAREHOUSE_DELIVERY:
                submitted_delivery_order, _activities = submit_rejected_delivery_activity_batch(
                    production_order=production_order,
                    activity_date=values.get("activity_date"),
                    line_quantities=form.cmt_line_quantities(),
                    notes=values.get("notes", ""),
                    actor=request.user,
                )
            elif activity_type == ProductionActivity.ActivityType.QC:
                submit_qc_activity_batch(
                    production_order=production_order,
                    activity_date=values.get("activity_date"),
                    line_results=form.qc_line_results(),
                    notes=values.get("notes", ""),
                    actor=request.user,
                )
            else:
                for field_name in [
                    *(row["field_name"] for row in form.cmt_rows),
                    *(
                        name
                        for row in form.qc_rows
                        for name in (
                            row["inspected_name"],
                            row["passed_name"],
                            row["disposition_name"],
                            row["reason_name"],
                        )
                    ),
                ]:
                    values.pop(field_name, None)
                submit_production_activity(
                    production_order=production_order,
                    actor=request.user,
                    **values,
                )
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            success_message = "Production Activity berhasil disubmit dan Monitoring diperbarui."
            if submitted_delivery_order:
                success_message = (
                    f"{'Deliver Rejected Goods' if activity_type == ProductionActivity.ActivityType.REJECTED_WAREHOUSE_DELIVERY else 'Deliver to Warehouse'} "
                    f"berhasil disubmit. No. Delivery Order: "
                    f"{submitted_delivery_order.number}."
                )
            messages.success(request, success_message)
            return redirect(f"{reverse('production:activity')}?production_order={production_order.id}")
    history = ProductionActivity.objects.filter(
        entry_kind__in=(
            ProductionActivity.EntryKind.ACTIVITY,
            ProductionActivity.EntryKind.CORRECTION,
            ProductionActivity.EntryKind.VOID,
        )
    ).select_related("production_order__po", "actor", "po_line__sku", "source_activity", "delivery_order")
    if selected:
        history = history.filter(production_order=selected)
    active_orders = _production_queryset().filter(plan__status=ProductionPlan.Status.ACTIVE).order_by("po__po_number")
    return render(
        request,
        "production/activity.html",
        {
            "selected": selected,
            "eligible_choices": choices,
            "activity_form": form,
            "history": history[:200],
            "active_orders": active_orders,
            "snapshot": production_snapshot(selected) if selected else None,
            "qc_follow_up_rows": follow_up_rows,
            "selected_follow_up": selected_follow_up,
            "follow_up_action": follow_up_action,
            "follow_up_form": follow_up_form,
            "next_delivery_order_number": next_delivery_order_number(timezone.localdate())[0],
        },
    )


@login_required
def delivery_order_preview(request):
    delivery_date = parse_date(request.GET.get("date", ""))
    if delivery_date is None:
        return JsonResponse({"error": "Tanggal pengiriman tidak valid."}, status=400)
    return JsonResponse({"number": next_delivery_order_number(delivery_date)[0]})


@login_required
def activity_correction(request, activity_id):
    original = get_object_or_404(
        ProductionActivity.objects.select_related(
            "production_order__po",
            "po_line__sku",
        ),
        pk=activity_id,
        entry_kind=ProductionActivity.EntryKind.ACTIVITY,
    )
    effective = latest_activity_version(original)
    initial = {
        "activity_date": effective.activity_date,
        "quantity": effective.quantity,
        "notes": effective.notes,
    }
    if original.activity_type == ProductionActivity.ActivityType.QC:
        initial.update(
            {
                "qty_inspected": effective.after_values.get("qty_inspected"),
                "qty_passed": effective.after_values.get("qty_passed"),
                "qty_failed": effective.after_values.get("qty_failed"),
                "failed_disposition": effective.after_values.get("failed_disposition", ""),
            }
        )
    form = ProductionCorrectionForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            correct_production_activity(activity=original, actor=request.user, **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "Koreksi tersimpan tanpa menghapus Activity asli.")
            return redirect(
                f"{reverse('production:activity')}?production_order={original.production_order_id}"
            )
    return render(
        request,
        "production/activity_correction.html",
        {"original": original, "effective": effective, "correction_form": form},
    )


@login_required
def qc_follow_up(request):
    return redirect("production:monitoring")


@login_required
def detail(request, production_id):
    _ensure_released_workflows(request.user)
    production_order = get_object_or_404(_production_queryset(), pk=production_id)
    if request.method == "POST":
        messages.info(request, "Production Monitoring bersifat read-only. Catat perubahan melalui Production Activity.")
        return redirect(f"{reverse('production:activity')}?production_order={production_order.id}")
    stages = {row.stage: row for row in production_order.stages.all()}
    snapshot = production_snapshot(production_order)
    process_details = production_process_details(production_order)
    latest = snapshot["trial"]
    trial_po_lines = list(production_order.po.lines.all())
    trial_products_by_id = {}
    for line in trial_po_lines:
        product = line.sku.product_variant.product
        product_row = trial_products_by_id.setdefault(
            product.id,
            {
                "parent_sku": product.parent_sku or "—",
                "product_name": product.name,
                "sizes": set(),
                "total_qty": Decimal("0"),
                "sku_lines": [],
                "detail_id": f"po-product-{product.id}",
            },
        )
        size = line.sku.size or "—"
        product_row["sizes"].add(size)
        product_row["total_qty"] += line.ordered_qty
        product_row["sku_lines"].append(
            {
                "sku": line.sku.sku,
                "size": size,
                "quantity": line.ordered_qty,
            }
        )
    size_order = {size: index for index, size in enumerate(("XS", "S", "M", "L", "XL", "XXL", "XXXL"))}
    for product_row in trial_products_by_id.values():
        product_row["sizes"] = sorted(
            product_row["sizes"],
            key=lambda size: (size_order.get(size.upper(), 999), size.lower()),
        )
        product_row["size_display"] = ", ".join(product_row["sizes"])
        product_row["sku_lines"].sort(
            key=lambda row: (size_order.get(row["size"].upper(), 999), row["size"].lower(), row["sku"].lower())
        )
    trial_products = sorted(
        trial_products_by_id.values(),
        key=lambda row: (row["product_name"].lower(), row["parent_sku"].lower()),
    )
    audit_rows = _production_audit_rows(production_order)
    audit_activity_choices = sorted(
        {row["activity_label"] for row in audit_rows},
        key=str.casefold,
    )
    selected_audit_activity = request.GET.get("activity", "").strip()
    if selected_audit_activity not in audit_activity_choices:
        selected_audit_activity = ""
    if selected_audit_activity:
        audit_rows = [
            row for row in audit_rows if row["activity_label"] == selected_audit_activity
        ]
    stage_numbers = {
        ProductionStage.Stage.CUT: "03",
        ProductionStage.Stage.MAKE: "04",
        ProductionStage.Stage.TRIM: "05",
    }
    return render(
        request,
        "production/detail.html",
        {
            "production_order": production_order,
            "snapshot": snapshot,
            "stage_entries": [
                {
                    "stage": stages[code],
                    "quantity": snapshot["cmt_quantities"].get(code),
                    "step_number": stage_numbers[code],
                    "timing": snapshot["timing"].get(code),
                    "detail": process_details[code],
                    "detail_id": f"process-detail-{code.lower()}",
                }
                for code in CMT_STAGES
            ],
            "qc_detail": process_details["QC"],
            "qc_follow_up_rows": _qc_follow_up_queryset().filter(po_line__po=production_order.po),
            "inbound_detail": process_details["INBOUND"],
            "latest_trial": latest,
            "trial_po_lines": trial_po_lines,
            "trial_products": trial_products,
            "trial_product_count": len(trial_products),
            "activities": audit_rows[:200],
            "audit_activity_choices": audit_activity_choices,
            "selected_audit_activity": selected_audit_activity,
            "cogs_finalization": production_cogs_finalization_card(
                production_order,
                actor=request.user,
            ),
            "today": timezone.localdate(),
        },
    )


@login_required
@permission_required("production.approve_cogs_finalization", raise_exception=True)
def approve_cogs_finalization(request, production_id):
    production_order = get_object_or_404(_production_queryset(), pk=production_id)
    if request.method != "POST":
        return redirect(f"{reverse('production:detail', args=[production_order.id])}#cogs-finalization")
    try:
        approve_production_cogs_finalization(
            production_order=production_order,
            actor=request.user,
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "Final Quantity & COGS berhasil di-approve dan FIFO sudah direvaluasi.")
    return redirect(f"{reverse('production:detail', args=[production_order.id])}#cogs-finalization")


@login_required
def trial_approval(request):
    messages.info(request, "Trial Approval sekarang dicatat melalui Production Activity.")
    return redirect("production:activity")


@login_required
def quality_control(request):
    messages.info(request, "Quality Control sekarang dicatat melalui Production Activity.")
    return redirect("production:activity")
