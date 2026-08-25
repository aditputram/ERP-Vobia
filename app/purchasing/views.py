from collections import defaultdict
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponseNotAllowed
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.shortcuts import get_object_or_404, redirect, render

from inventory.services.aging import po_aging_snapshot
from imports.services.storage import DuplicateRawFile
from master_data.models import Supplier

from .forms import LegacyWIPSupplierRevisionForm, ManualPOForm, POWIPImportUploadForm, SupplierForm
from .models import PPICRequirement, POWIPImportBatch, POWIPImportIssue, PurchaseOrder
from .services.wip_import import approve_po_wip_import, create_po_wip_import
from .services.workflows import (
    cancel_po,
    create_draft_po,
    delete_unused_supplier,
    delete_draft_po,
    release_po,
    revise_legacy_wip_supplier,
)


def _requirements_with_ui_state(selected_requirement_ids=None, selected_quantities=None, need_month=None):
    selected_requirement_ids = selected_requirement_ids or set()
    selected_quantities = selected_quantities or {}
    queryset = (
        PPICRequirement.objects.select_related(
            "sku",
            "sku__product_variant__product",
            "sku__product_variant__product__status",
            "sku__product_variant__product__category",
        )
        .filter(approved_qty__gt=0)
        .prefetch_related("po_lines__po")
        .order_by("need_month", "sku__sku")
    )
    if need_month:
        queryset = queryset.filter(need_month=need_month)
    requirements = list(
        queryset
    )
    for row in requirements:
        row.ui_selected = str(row.id) in selected_requirement_ids
        row.ui_quantity = selected_quantities.get(str(row.id), row.remaining_qty)
        if row.remaining_qty <= 0:
            row.ui_status = "Fully Allocated"
        elif row.ordered_qty > 0:
            row.ui_status = "Partially Allocated"
        else:
            row.ui_status = "Open"
    return requirements


def _generator_eligible_requirements():
    """Workbook Create PO eligibility: one existing non-cancelled PO line consumes the Need Key."""
    requirements = list(
        PPICRequirement.objects.select_related(
            "sku",
            "sku__product_variant",
            "sku__product_variant__product",
            "sku__product_variant__product__status",
            "sku__product_variant__product__category",
            "sku__product_variant__product__subcategory",
        )
        .prefetch_related("po_lines__po")
        .order_by("need_month", "sku__product_variant__product__name", "sku__sku")
    )
    return [
        row
        for row in requirements
        if row.approved_qty > 0
        and not any(line.po.status != PurchaseOrder.Status.CANCELLED for line in row.po_lines.all())
    ]


def _generator_selected(source):
    return {
        "months": [value for value in source.getlist("month") if value],
        "statuses": [value for value in source.getlist("status") if value],
        "categories": [value for value in source.getlist("category") if value],
        "products": [value for value in source.getlist("product") if value],
        "sizes": [value for value in source.getlist("size") if value],
    }


def _generator_filter_rows(rows, selected, fields):
    filtered = rows
    if "months" in fields and selected["months"]:
        filtered = [row for row in filtered if row.need_month.strftime("%Y-%m") in selected["months"]]
    if "statuses" in fields and selected["statuses"]:
        filtered = [
            row
            for row in filtered
            if str(row.sku.product_variant.product.status_id) in selected["statuses"]
        ]
    if "categories" in fields and selected["categories"]:
        filtered = [
            row
            for row in filtered
            if str(row.sku.product_variant.product.category_id) in selected["categories"]
        ]
    if "products" in fields and selected["products"]:
        filtered = [
            row
            for row in filtered
            if str(row.sku.product_variant.product_id) in selected["products"]
        ]
    if "sizes" in fields and selected["sizes"]:
        filtered = [row for row in filtered if (row.sku.size or "—") in selected["sizes"]]
    return filtered


def _unique_options(rows, value_getter, label_getter):
    options = {}
    for row in rows:
        value = str(value_getter(row))
        options[value] = str(label_getter(row))
    return [{"value": value, "label": label} for value, label in sorted(options.items(), key=lambda item: item[1])]


def _generator_state(source):
    base_rows = _generator_eligible_requirements()
    selected = _generator_selected(source)

    month_options = [
        {"value": month.strftime("%Y-%m"), "label": month.strftime("%B %Y")}
        for month in sorted({row.need_month for row in base_rows})
    ]
    valid_months = {option["value"] for option in month_options}
    selected["months"] = [value for value in selected["months"] if value in valid_months]
    after_month = _generator_filter_rows(base_rows, selected, {"months"})
    status_options = _unique_options(
        after_month,
        lambda row: row.sku.product_variant.product.status_id,
        lambda row: row.sku.product_variant.product.status.name,
    )
    valid_statuses = {option["value"] for option in status_options}
    selected["statuses"] = [value for value in selected["statuses"] if value in valid_statuses]
    after_status = _generator_filter_rows(after_month, selected, {"statuses"})
    category_options = _unique_options(
        after_status,
        lambda row: row.sku.product_variant.product.category_id,
        lambda row: row.sku.product_variant.product.category.name,
    )
    valid_categories = {option["value"] for option in category_options}
    selected["categories"] = [value for value in selected["categories"] if value in valid_categories]
    after_category = _generator_filter_rows(after_status, selected, {"categories"})
    product_options = _unique_options(
        after_category,
        lambda row: row.sku.product_variant.product_id,
        lambda row: row.sku.product_variant.product.name,
    )
    valid_products = {option["value"] for option in product_options}
    selected["products"] = [value for value in selected["products"] if value in valid_products]
    after_product = _generator_filter_rows(after_category, selected, {"products"})
    size_options = _unique_options(after_product, lambda row: row.sku.size or "—", lambda row: row.sku.size or "—")
    valid_sizes = {option["value"] for option in size_options}
    selected["sizes"] = [value for value in selected["sizes"] if value in valid_sizes]
    candidates = _generator_filter_rows(after_product, selected, {"sizes"})

    for row in candidates:
        row.po_qty = row.approved_qty
        row.current_cogs = row.sku.current_master_cogs
        row.cogs_field_name = f"cogs_{row.id}"
        if row.cogs_field_name in source:
            row.cogs_input_value = source.get(row.cogs_field_name, "")
        elif row.current_cogs is not None:
            row.cogs_input_value = format(
                row.current_cogs.to_integral_value()
                if row.current_cogs == row.current_cogs.to_integral_value()
                else row.current_cogs,
                "f",
            )
        else:
            row.cogs_input_value = ""
        try:
            row.proposed_cogs = Decimal(row.cogs_input_value)
        except Exception:
            row.proposed_cogs = None
        if (
            row.proposed_cogs is None
            or row.proposed_cogs <= 0
            or row.proposed_cogs != row.proposed_cogs.to_integral_value()
        ):
            row.proposed_cogs = None
        row.incoming_cogs = (
            row.po_qty * row.proposed_cogs if row.proposed_cogs is not None else None
        )

    candidate_months = []
    for need_month in sorted({row.need_month for row in candidates}):
        field_name = f"arrival_{need_month:%Y_%m}"
        candidate_months.append(
            {
                "date": need_month,
                "field_name": field_name,
                "value": source.get(field_name, ""),
            }
        )

    return {
        "selected": selected,
        "month_options": month_options,
        "status_options": status_options,
        "category_options": category_options,
        "product_options": product_options,
        "size_options": size_options,
        "candidates": candidates,
        "candidate_months": candidate_months,
        "candidate_qty": sum((row.po_qty for row in candidates), Decimal("0")),
        "candidate_cogs": sum(
            (row.incoming_cogs or Decimal("0") for row in candidates), Decimal("0")
        ),
        "has_missing_cogs": any(row.proposed_cogs is None for row in candidates),
    }


def _create_generator_pos(request, state):
    errors = []
    supplier = Supplier.objects.filter(pk=request.POST.get("supplier"), is_active=True).first()
    if supplier is None:
        errors.append("Vendor wajib dipilih dari Daftar Vendor aktif.")
    if not state["candidates"]:
        errors.append("Tidak ada requirement eligible untuk filter ini.")
    cogs_by_requirement = {}
    for row in state["candidates"]:
        if row.proposed_cogs is None:
            errors.append(f"PPIC COGS {row.sku.sku} wajib berupa Rupiah bulat positif.")
        else:
            cogs_by_requirement[row.id] = row.proposed_cogs

    grouped = defaultdict(list)
    arrivals = {}
    for row in state["candidates"]:
        grouped[row.need_month].append(row)
    for need_month in grouped:
        field_name = f"arrival_{need_month:%Y_%m}"
        arrival = parse_date(request.POST.get(field_name, ""))
        if arrival is None:
            errors.append(f"Required Arrival {need_month:%B %Y} wajib diisi.")
        else:
            arrivals[need_month] = arrival
    if errors:
        return [], errors, supplier, arrivals

    created = []
    try:
        with transaction.atomic():
            for need_month, rows in sorted(grouped.items()):
                quantities = {row.id: row.po_qty for row in rows}
                created.append(
                    create_draft_po(
                        supplier=supplier,
                        need_month=need_month,
                        required_arrival=arrivals[need_month],
                        actor=request.user,
                        requirement_quantities=quantities,
                        requirement_cogs={row.id: cogs_by_requirement[row.id] for row in rows},
                        notes=request.POST.get("notes", "").strip(),
                    )
                )
    except ValidationError as exc:
        errors.extend(exc.messages)
        return [], errors, supplier, arrivals
    return created, [], supplier, arrivals


def _tracking_rows(filters=None):
    filters = filters or {}
    rows = []
    today = timezone.localdate()
    pos = PurchaseOrder.objects.select_related("supplier").prefetch_related(
        "lines__sku", "lines__qc_inspections", "lines__inbound_receipts"
    )
    query = filters.get("query", "")
    if query:
        pos = pos.filter(Q(po_number__icontains=query) | Q(lines__sku__sku__icontains=query)).distinct()
    if filters.get("supplier"):
        pos = pos.filter(supplier_id=filters["supplier"])
    if filters.get("po_status") in PurchaseOrder.Status.values:
        pos = pos.filter(status=filters["po_status"])
    if filters.get("need_month"):
        try:
            year, month = (int(part) for part in filters["need_month"].split("-", 1))
        except (TypeError, ValueError):
            pass
        else:
            pos = pos.filter(need_month__year=year, need_month__month=month)
    pos = pos[:100]
    for po in pos:
        line_count = 0
        total_qty = Decimal("0")
        total_qc_passed = Decimal("0")
        has_production_qc = False
        total_received = Decimal("0")
        latest_receipt_date = None
        for line in po.lines.all():
            line_count += 1
            total_qty += line.ordered_qty
            inspections = list(line.qc_inspections.all())
            if inspections:
                has_production_qc = True
            total_qc_passed += sum((inspection.qty_passed for inspection in inspections), Decimal("0"))
            receipts = list(line.inbound_receipts.all())
            total_received += line.received_before_cutover_qty + sum(
                (receipt.received_qty for receipt in receipts),
                Decimal("0"),
            )
            for receipt in receipts:
                if latest_receipt_date is None or receipt.inbound_date > latest_receipt_date:
                    latest_receipt_date = receipt.inbound_date

        outstanding = max(total_qty - total_received, Decimal("0"))
        if po.status == PurchaseOrder.Status.CANCELLED:
            schedule_status = "Cancelled"
            progress_status = "Cancelled"
        elif total_qty > 0 and total_received >= total_qty:
            progress_status = "Complete"
            schedule_status = (
                "On Time"
                if not po.required_arrival
                or (latest_receipt_date and latest_receipt_date <= po.required_arrival)
                else "Late"
            )
        elif total_received > 0:
            progress_status = "Partially Received"
            schedule_status = "Risk Late" if po.required_arrival and today > po.required_arrival else "On Track"
        else:
            progress_status = po.get_status_display()
            schedule_status = "Risk Late" if po.required_arrival and today > po.required_arrival else "Open"
        rows.append(
            {
                "po": po,
                "line_count": line_count,
                "total_qty": total_qty,
                "qc_passed": total_qc_passed if has_production_qc else None,
                "received": total_received,
                "outstanding": outstanding,
                "progress_status": progress_status,
                "schedule_status": schedule_status,
                "production_status": "—",
            }
        )
        if po.status == PurchaseOrder.Status.RELEASED:
            from production.services import ensure_production_order, production_snapshot

            production_order = ensure_production_order(po, actor=None)
            rows[-1]["production_status"] = production_snapshot(production_order)["current_label"]
    if filters.get("schedule_status"):
        rows = [row for row in rows if row["schedule_status"] == filters["schedule_status"]]
    return rows


@login_required
def requirements(request):
    base_rows = _requirements_with_ui_state()
    month_options = sorted({row.need_month for row in base_rows})
    selected_need_month = request.GET.get("need_month", "")
    valid_months = {month.strftime("%Y-%m"): month for month in month_options}
    selected_month = valid_months.get(selected_need_month)
    if selected_month is None:
        selected_need_month = ""
    after_month = [row for row in base_rows if not selected_month or row.need_month == selected_month]

    status_options = _unique_options(
        after_month,
        lambda row: row.sku.product_variant.product.status_id,
        lambda row: row.sku.product_variant.product.status.name,
    )
    selected_status = request.GET.get("status", "")
    if selected_status not in {option["value"] for option in status_options}:
        selected_status = ""
    after_status = [
        row
        for row in after_month
        if not selected_status or str(row.sku.product_variant.product.status_id) == selected_status
    ]

    category_options = _unique_options(
        after_status,
        lambda row: row.sku.product_variant.product.category_id,
        lambda row: row.sku.product_variant.product.category.name,
    )
    selected_category = request.GET.get("category", "")
    if selected_category not in {option["value"] for option in category_options}:
        selected_category = ""
    after_category = [
        row
        for row in after_status
        if not selected_category or str(row.sku.product_variant.product.category_id) == selected_category
    ]
    allocation_status_order = ("Open", "Partially Allocated", "Fully Allocated")
    available_allocation_statuses = {row.ui_status for row in after_category}
    allocation_status_options = [
        {"value": value, "label": value}
        for value in allocation_status_order
        if value in available_allocation_statuses
    ]
    valid_allocation_statuses = {option["value"] for option in allocation_status_options}
    selected_allocation_statuses = [
        value
        for value in request.GET.getlist("allocation_status")
        if value in valid_allocation_statuses
    ]
    rows = [
        row
        for row in after_category
        if not selected_allocation_statuses or row.ui_status in selected_allocation_statuses
    ]
    return render(
        request,
        "purchasing/requirements.html",
        {
            "requirements": rows,
            "total_approved": sum((row.approved_qty for row in after_category), Decimal("0")),
            "total_ordered": sum((row.ordered_qty for row in after_category), Decimal("0")),
            "total_remaining": sum((row.remaining_qty for row in after_category), Decimal("0")),
            "requirement_line_count": len(after_category),
            "need_month_options": month_options,
            "selected_need_month": selected_need_month,
            "selected_need_month_date": selected_month,
            "status_options": status_options,
            "selected_status": selected_status,
            "category_options": category_options,
            "selected_category": selected_category,
            "allocation_status_options": allocation_status_options,
            "selected_allocation_statuses": selected_allocation_statuses,
        },
    )


@login_required
def overview(request):
    manual_form = ManualPOForm()
    source = request.POST if request.method == "POST" else request.GET
    generator_state = _generator_state(source)
    show_preview = request.GET.get("review") == "1"
    generator_errors = []
    selected_supplier = None
    if request.method == "POST":
        form_name = request.POST.get("form_name")
        if form_name == "manual_po":
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
        elif form_name == "generator_create":
            show_preview = True
            created, generator_errors, selected_supplier, _ = _create_generator_pos(
                request, generator_state
            )
            if created:
                line_count = sum(po.lines.count() for po in created)
                messages.success(
                    request,
                    f"{len(created)} Draft PO berhasil dibuat dari {line_count} requirement eligible.",
                )
                return redirect("purchasing:purchase_orders")
    pos = list(PurchaseOrder.objects.select_related("supplier", "created_by").prefetch_related("lines")[:100])
    for po in pos:
        po.total_qty = sum((line.ordered_qty for line in po.lines.all()), Decimal("0"))
        po.aging_snapshot = po_aging_snapshot(po) if po.status == PurchaseOrder.Status.RELEASED else None
    return render(
        request,
        "purchasing/overview.html",
        {
            "manual_form": manual_form,
            "pos": pos,
            "generator": generator_state,
            "show_preview": show_preview,
            "generator_errors": generator_errors,
            "suppliers": Supplier.objects.filter(is_active=True).order_by("name"),
            "selected_supplier": selected_supplier,
        },
    )


@login_required
def purchase_orders(request):
    selected = {
        "query": request.GET.get("q", "").strip(),
        "supplier": request.GET.get("supplier", ""),
        "need_month": request.GET.get("need_month", ""),
        "po_status": request.GET.get("po_status", ""),
        "schedule_status": request.GET.get("schedule_status", ""),
    }
    rows = _tracking_rows(selected)
    return render(
        request,
        "purchasing/tracking.html",
        {
            "rows": rows,
            "po_count": len(rows),
            "line_count": sum((row["line_count"] for row in rows), 0),
            "total_po_qty": sum((row["total_qty"] for row in rows), Decimal("0")),
            "total_outstanding": sum((row["outstanding"] for row in rows), Decimal("0")),
            "selected": selected,
            "supplier_options": Supplier.objects.filter(purchase_orders__isnull=False).distinct().order_by("name"),
            "need_month_options": PurchaseOrder.objects.order_by("-need_month").values_list("need_month", flat=True).distinct(),
            "po_status_options": PurchaseOrder.Status.choices,
            "schedule_status_options": ("Open", "On Track", "Risk Late", "On Time", "Late", "Cancelled"),
        },
    )


@login_required
def tracking(request):
    return purchase_orders(request)


@login_required
def vendors(request):
    if request.method == "POST":
        form = SupplierForm(request.POST)
        if form.is_valid():
            supplier = form.save()
            messages.success(request, f"Vendor {supplier.name} berhasil ditambahkan.")
            return redirect("purchasing:vendors")
    else:
        form = SupplierForm()
    suppliers = Supplier.objects.annotate(po_count=Count("purchase_orders")).order_by("name")
    return render(request, "purchasing/vendors.html", {"form": form, "suppliers": suppliers})


@login_required
def vendor_delete(request, supplier_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    supplier = get_object_or_404(Supplier, pk=supplier_id)
    try:
        deleted = delete_unused_supplier(
            supplier.id,
            request.user,
            request.POST.get("reason", "").strip(),
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, f"Vendor {deleted['name']} dihapus; tidak ada Purchase Order yang terdampak.")
    return redirect("purchasing:vendors")


@login_required
def po_detail(request, po_id):
    po = get_object_or_404(
        PurchaseOrder.objects.select_related("supplier", "created_by", "released_by").prefetch_related(
            "lines__sku", "lines__requirement", "lines__qc_inspections", "lines__inbound_receipts"
        ),
        pk=po_id,
    )
    vendor_revision_form = LegacyWIPSupplierRevisionForm(initial={"supplier": po.supplier_id})
    po_lines = list(po.lines.all())
    for line in po_lines:
        line.total_cogs = Decimal(line.ordered_qty) * Decimal(line.cogs_snapshot or 0)
    production_state = None
    activity_rows = []
    if po.status == PurchaseOrder.Status.RELEASED:
        from production.services import ensure_production_order, production_snapshot

        production_order = ensure_production_order(po, actor=request.user)
        production_state = production_snapshot(production_order)
        finalization = getattr(production_order, "cogs_finalization", None)
        final_rows = {
            row["po_line_id"]: row for row in finalization.line_snapshot
        } if finalization else {}
        for line in po_lines:
            row = final_rows.get(str(line.id))
            if row:
                line.final_sellable_qty = Decimal(row["sellable_qty"])
                line.final_unit_cogs = Decimal(row["final_unit_cogs"])
                line.unit_cogs_increase = Decimal(row["unit_cogs_increase"])
                line.total_cogs = line.final_sellable_qty * line.final_unit_cogs
        ordered_qty = production_state["ordered_qty"]
        activity_rows = [
            {"label": "Cutting", "qty": production_state["cmt_quantities"]["CUT"]["completed_qty"]},
            {"label": "Make · Jahit", "qty": production_state["cmt_quantities"]["MAKE"]["completed_qty"]},
            {"label": "Trim · Finishing", "qty": production_state["cmt_quantities"]["TRIM"]["completed_qty"]},
            {"label": "QC Passed", "qty": production_state["passed_qty"]},
            {"label": "Inbound", "qty": production_state["received_qty"]},
        ]
        for row in activity_rows:
            row["po_qty"] = ordered_qty
            row["gap"] = row["qty"] - ordered_qty
    return render(
        request,
        "purchasing/detail.html",
        {
            "po": po,
            "aging": po_aging_snapshot(po),
            "vendor_revision_form": vendor_revision_form,
            "production_state": production_state,
            "po_lines": po_lines,
            "activity_rows": activity_rows,
        },
    )


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
def po_revise_vendor(request, po_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    form = LegacyWIPSupplierRevisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Revisi vendor gagal: periksa vendor dan alasan revisi.")
        return redirect("purchasing:po_detail", po_id=po_id)
    try:
        po = revise_legacy_wip_supplier(
            po_id,
            form.cleaned_data["supplier"],
            request.user,
            form.cleaned_data["reason"],
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(
            request,
            f"Vendor {po.po_number} direvisi menjadi {po.supplier.name}; qty dan COGS snapshot tidak berubah.",
        )
    return redirect("purchasing:po_detail", po_id=po_id)


@login_required
def po_delete_draft(request, po_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    try:
        delete_draft_po(po_id, request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("purchasing:po_detail", po_id=po_id)
    messages.success(request, "Draft PO dihapus; Need Key terkait kembali eligible di PO Generator.")
    return redirect("purchasing:purchase_orders")


@login_required
def po_print(request, po_id):
    po = get_object_or_404(
        PurchaseOrder.objects.select_related(
            "supplier",
            "created_by",
            "released_by",
        ).prefetch_related("lines__sku__product_variant__product"),
        pk=po_id,
    )
    print_lines = []
    subtotal = Decimal("0")
    total_qty = Decimal("0")
    for line in po.lines.all():
        line_total = (
            line.ordered_qty * line.cogs_snapshot
            if line.cogs_snapshot is not None
            else None
        )
        print_lines.append({"line": line, "line_total": line_total})
        total_qty += line.ordered_qty
        if line_total is not None:
            subtotal += line_total
    return render(
        request,
        "purchasing/print.html",
        {
            "po": po,
            "print_lines": print_lines,
            "subtotal": subtotal,
            "total_qty": total_qty,
            "po_date": po.released_at or po.created_at,
        },
    )


@login_required
def po_wip_import_list(request):
    batches = POWIPImportBatch.objects.select_related("raw_file", "approved_by")[:20]
    return render(request, "purchasing/wip/list.html", {"batches": batches})


@login_required
def po_wip_import_upload(request):
    if request.method == "POST":
        form = POWIPImportUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                batch = create_po_wip_import(form.cleaned_data["file"], request.user)
            except DuplicateRawFile as exc:
                existing = exc.raw_file.po_wip_batches.order_by("-created_at").first()
                form.add_error(
                    "file",
                    f"File identik sudah pernah diunggah. Batch sebelumnya: {existing.id if existing else '—'}.",
                )
            else:
                if batch.status == POWIPImportBatch.Status.READY:
                    messages.success(request, "PO WIP berhasil diparsing dan siap direview.")
                else:
                    messages.warning(request, "PO WIP diparsing tetapi memiliki blocking issue.")
                return redirect("purchasing:po_wip_detail", batch_id=batch.id)
    elif request.method == "GET":
        form = POWIPImportUploadForm()
    else:
        return HttpResponseNotAllowed(["GET", "POST"])
    return render(request, "purchasing/wip/upload.html", {"form": form})


@login_required
def po_wip_import_detail(request, batch_id):
    batch = get_object_or_404(
        POWIPImportBatch.objects.select_related("raw_file", "approved_by"),
        pk=batch_id,
    )
    page = Paginator(
        batch.staged_rows.select_related("sku__product_variant__product"),
        50,
    ).get_page(request.GET.get("page"))
    issues = batch.issues.select_related("staged_row")
    severity = request.GET.get("severity", "")
    if severity in POWIPImportIssue.Severity.values:
        issues = issues.filter(severity=severity)
    return render(
        request,
        "purchasing/wip/detail.html",
        {"batch": batch, "page": page, "issues": issues[:100]},
    )


@login_required
def po_wip_import_approve(request, batch_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    batch = get_object_or_404(POWIPImportBatch, pk=batch_id)
    try:
        _, counts = approve_po_wip_import(batch.id, request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(
            request,
            f"PO WIP committed: {counts['purchase_orders']} PO, {counts['lines']} line, "
            f"{counts['outstanding_qty']} pcs. Belum ada stock movement sampai physical Inbound.",
        )
    return redirect("purchasing:po_wip_detail", batch_id=batch.id)
