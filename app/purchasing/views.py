from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.utils import timezone
from django.shortcuts import get_object_or_404, redirect, render

from inventory.services.aging import po_aging_snapshot

from .forms import ManualPOForm, POHeaderForm, SupplierForm
from .models import PPICRequirement, PurchaseOrder
from .services.workflows import cancel_po, create_draft_po, release_po, review_po


@login_required
def overview(request):
    header_form = POHeaderForm()
    manual_form = ManualPOForm()
    supplier_form = SupplierForm()
    po_preview = None
    selected_requirement_ids = set()
    selected_quantities = {}
    if request.method == "POST":
        form_name = request.POST.get("form_name")
        if form_name == "supplier":
            supplier_form = SupplierForm(request.POST)
            if supplier_form.is_valid():
                supplier_form.save()
                messages.success(request, "Supplier berhasil ditambahkan.")
                return redirect("purchasing:overview")
        elif form_name == "manual_po":
            manual_form = ManualPOForm(request.POST)
            if manual_form.is_valid():
                data = manual_form.cleaned_data
                try:
                    po = create_draft_po(
                        supplier=data["supplier"],
                        need_month=data["need_month"],
                        required_arrival=data["required_arrival"],
                        actor=request.user,
                        manual_lines=[(data["sku"], data["quantity"])],
                        notes=data["notes"],
                    )
                except ValidationError as exc:
                    manual_form.add_error(None, exc)
                else:
                    messages.success(request, "Draft PO manual berhasil dibuat. Review lalu Release.")
                    return redirect("purchasing:po_detail", po_id=po.id)
        elif form_name == "requirement_po":
            header_form = POHeaderForm(request.POST)
            if header_form.is_valid():
                quantities = {}
                for requirement_id in request.POST.getlist("requirements"):
                    raw = request.POST.get(f"qty_{requirement_id}", "")
                    try:
                        quantities[requirement_id] = Decimal(raw)
                        selected_quantities[requirement_id] = Decimal(raw)
                    except (InvalidOperation, TypeError):
                        header_form.add_error(None, "Qty requirement tidak valid.")
                if not quantities:
                    header_form.add_error(None, "Pilih minimal satu requirement.")
                selected_requirement_ids = set(quantities)
                if not header_form.errors:
                    data = header_form.cleaned_data
                    try:
                        if request.POST.get("action") == "create":
                            po = create_draft_po(
                                supplier=data["supplier"],
                                need_month=data["need_month"],
                                required_arrival=data["required_arrival"],
                                actor=request.user,
                                requirement_quantities=quantities,
                                notes=data["notes"],
                            )
                        else:
                            po_preview = review_po(
                                supplier=data["supplier"],
                                need_month=data["need_month"],
                                required_arrival=data["required_arrival"],
                                requirement_quantities=quantities,
                            )
                    except ValidationError as exc:
                        header_form.add_error(None, exc)
                    else:
                        if request.POST.get("action") == "create":
                            messages.success(request, "Draft PO berhasil dibuat. Review detail lalu Release.")
                            return redirect("purchasing:po_detail", po_id=po.id)
                        messages.info(request, "Review PO siap. Belum ada PO atau inventory record yang dibuat.")
    requirements = [
        row
        for row in PPICRequirement.objects.select_related("sku", "sku__product_variant__product").prefetch_related("po_lines__po")
        if row.remaining_qty > 0
    ]
    for row in requirements:
        row.ui_selected = str(row.id) in selected_requirement_ids
        row.ui_quantity = selected_quantities.get(str(row.id), row.remaining_qty)
    pos = list(PurchaseOrder.objects.select_related("supplier", "created_by").prefetch_related("lines")[:100])
    for po in pos:
        po.total_qty = sum((line.ordered_qty for line in po.lines.all()), Decimal("0"))
        po.aging_snapshot = po_aging_snapshot(po) if po.status == PurchaseOrder.Status.RELEASED else None
    return render(
        request,
        "purchasing/overview.html",
        {
            "header_form": header_form,
            "manual_form": manual_form,
            "supplier_form": supplier_form,
            "requirements": requirements,
            "pos": pos,
            "po_preview": po_preview,
        },
    )


@login_required
def tracking(request):
    rows = []
    today = timezone.localdate()
    lines = PurchaseOrder.objects.select_related("supplier").prefetch_related(
        "lines__sku", "lines__qc_inspections", "lines__inbound_receipts"
    )[:100]
    for po in lines:
        for line in po.lines.all():
            qc_passed = line.qc_passed_before_cutover_qty + (line.qc_inspections.aggregate(total=Sum("qty_passed"))["total"] or Decimal("0"))
            received = line.received_before_cutover_qty + (line.inbound_receipts.aggregate(total=Sum("received_qty"))["total"] or Decimal("0"))
            outstanding = max(line.ordered_qty - received, Decimal("0"))
            latest_receipt = line.inbound_receipts.order_by("-inbound_date").first()
            if po.status == PurchaseOrder.Status.CANCELLED:
                schedule_status = "Cancelled"
                progress_status = "Cancelled"
            elif received >= line.ordered_qty:
                progress_status = "Complete"
                schedule_status = (
                    "On Time"
                    if not po.required_arrival or (latest_receipt and latest_receipt.inbound_date <= po.required_arrival)
                    else "Late"
                )
            elif received > 0:
                progress_status = "Partially Received"
                schedule_status = "Risk Late" if po.required_arrival and today > po.required_arrival else "On Track"
            else:
                progress_status = po.get_status_display()
                schedule_status = "Risk Late" if po.required_arrival and today > po.required_arrival else "Open"
            rows.append(
                {
                    "po": po,
                    "line": line,
                    "qc_passed": qc_passed,
                    "received": received,
                    "outstanding": outstanding,
                    "progress_status": progress_status,
                    "schedule_status": schedule_status,
                }
            )
    return render(request, "purchasing/tracking.html", {"rows": rows})


@login_required
def po_detail(request, po_id):
    po = get_object_or_404(
        PurchaseOrder.objects.select_related("supplier", "created_by", "released_by").prefetch_related(
            "lines__sku", "lines__requirement", "lines__qc_inspections", "lines__inbound_receipts"
        ),
        pk=po_id,
    )
    return render(request, "purchasing/detail.html", {"po": po, "aging": po_aging_snapshot(po)})


@login_required
def po_release(request, po_id):
    if request.method == "POST":
        try:
            po = release_po(po_id, request.user)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"{po.po_number} berhasil dirilis; COGS sudah dibekukan.")
    return redirect("purchasing:po_detail", po_id=po_id)


@login_required
def po_cancel(request, po_id):
    if request.method == "POST":
        try:
            po = cancel_po(po_id, request.user, request.POST.get("reason", ""))
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"{po.po_number} dibatalkan dengan audit reason.")
    return redirect("purchasing:po_detail", po_id=po_id)


@login_required
def po_print(request, po_id):
    po = get_object_or_404(PurchaseOrder.objects.select_related("supplier").prefetch_related("lines__sku__product_variant__product"), pk=po_id)
    return render(request, "purchasing/print.html", {"po": po})
