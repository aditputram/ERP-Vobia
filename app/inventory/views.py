from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponseNotAllowed
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date

from master_data.models import Category, ProductStatus, SKU
from purchasing.models import PurchaseOrder, PurchaseOrderLine

from imports.services.storage import DuplicateRawFile

from .forms import AdjustmentForm, FIFOOpeningImportUploadForm, InboundForm, OpeningForm, QCForm, ReturnForm, WarehouseForm
from .models import (
    ExpectedReturn,
    FIFOLayer,
    FIFOOpeningImportBatch,
    FIFOOpeningImportIssue,
    InboundReceipt,
    InventoryException,
    InventoryMovement,
    PhysicalReturnReceipt,
    QCInspection,
)
from .services.aging import po_aging_snapshot, refresh_po_close
from .services.fifo import CUTOVER_DATE, inventory_balance, post_adjustment, post_opening, record_inbound, record_physical_return, record_qc
from .services.opening_import import approve_opening_import, create_opening_import
from .services.reporting import filtered_skus, inventory_parent_summary_rows, inventory_summary_rows, movement_ledger_rows


@login_required
def production(request):
    form = QCForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            record_qc(actor=request.user, **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "QC berhasil dicatat. Stock belum bertambah sampai inbound fisik.")
            return redirect("inventory:production")
    return render(
        request,
        "inventory/production.html",
        {
            "qc_form": form,
            "qc_records": QCInspection.objects.select_related("po_line__po", "po_line__sku", "recorded_by")[:200],
        },
    )


@login_required
def overview(request):
    forms = {"warehouse": WarehouseForm(), "adjustment": AdjustmentForm()}
    if request.method == "POST":
        form_name = request.POST.get("form_name")
        if form_name == "warehouse":
            forms["warehouse"] = WarehouseForm(request.POST)
            if forms["warehouse"].is_valid():
                forms["warehouse"].save()
                messages.success(request, "Warehouse berhasil ditambahkan.")
                return redirect("inventory:overview")
        elif form_name == "adjustment":
            forms["adjustment"] = AdjustmentForm(request.POST)
            if forms["adjustment"].is_valid():
                data = forms["adjustment"].cleaned_data
                try:
                    post_adjustment(actor=request.user, **data)
                except ValidationError as exc:
                    forms["adjustment"].add_error(None, exc)
                else:
                    messages.success(request, "Adjustment traceable berhasil diposting dan exception terkait diperbarui.")
                    return redirect("inventory:overview")
        elif form_name == "refresh_aging":
            for po in PurchaseOrder.objects.filter(status=PurchaseOrder.Status.RELEASED):
                refresh_po_close(po.id)
            messages.success(request, "PO Aging dan close/reopen condition diperbarui.")
            return redirect("inventory:overview")
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    category = request.GET.get("category", "")
    stock_status = request.GET.get("stock_status", "")
    sku_type = request.GET.get("sku_type", "sku")
    if sku_type not in {"sku", "parent"}:
        sku_type = "sku"
    requested_as_of_date = request.GET.get("as_of_date", "").strip()
    today = timezone.localdate()
    date_filter_error = ""
    if requested_as_of_date:
        as_of_date = parse_date(requested_as_of_date)
        if as_of_date is None:
            as_of_date = today
            date_filter_error = "Tanggal tidak valid; posisi stok dikembalikan ke hari ini."
    else:
        as_of_date = today
    if as_of_date < CUTOVER_DATE:
        as_of_date = CUTOVER_DATE
        date_filter_error = "Riwayat Warehouse ERP tersedia mulai FIFO cutover 31 July 2026."
    elif as_of_date > today:
        as_of_date = today
        date_filter_error = "Tanggal masa depan tidak dapat dipilih; posisi stok dikembalikan ke hari ini."
    skus = filtered_skus(query=query, status=status, category=category)
    balances = inventory_summary_rows(skus, as_of_date=as_of_date)
    if sku_type == "parent":
        balances = inventory_parent_summary_rows(balances)
    if stock_status:
        balances = [row for row in balances if row["stock_status"] == stock_status]
    total_balance = sum((row["balance"] for row in balances), 0)
    total_fifo_value = sum((row["fifo_value"] for row in balances), 0)
    total_exceptions = sum((row["exception_count"] for row in balances), 0)
    pos = list(PurchaseOrder.objects.filter(status=PurchaseOrder.Status.RELEASED).prefetch_related("lines")[:100])
    for po in pos:
        po.aging_snapshot = po_aging_snapshot(po)
    return render(
        request,
        "inventory/overview.html",
        {
            **forms,
            "balances": balances,
            "exceptions": InventoryException.objects.filter(status=InventoryException.Status.OPEN).select_related("sku", "movement")[:200],
            "pos": pos,
            "opening_batch": FIFOOpeningImportBatch.objects.first(),
            "query": query,
            "selected_status": status,
            "selected_category": category,
            "selected_stock_status": stock_status,
            "sku_type": sku_type,
            "as_of_date": as_of_date,
            "today": today,
            "cutover_date": CUTOVER_DATE,
            "date_filter_error": date_filter_error,
            "product_statuses": ProductStatus.objects.filter(is_active=True),
            "categories": Category.objects.filter(is_active=True),
            "total_balance": total_balance,
            "total_fifo_value": total_fifo_value,
            "total_exceptions": total_exceptions,
            "negative_sku_count": sum(1 for row in balances if row["balance"] < 0),
        },
    )


@login_required
def turnover(request):
    query = request.GET.get("q", "").strip()
    movement_type = request.GET.get("movement_type", "")
    date_from = request.GET.get("date_from") or None
    date_to = request.GET.get("date_to") or None
    skus = filtered_skus(query=query)
    rows = movement_ledger_rows(
        skus,
        date_from=date_from,
        date_to=date_to,
        movement_type=movement_type if movement_type in InventoryMovement.MovementType.values else "",
    )
    page = Paginator(rows, 100).get_page(request.GET.get("page"))
    return render(request, "inventory/turnover.html", {
        "page": page,
        "query": query,
        "movement_type": movement_type,
        "date_from": date_from,
        "date_to": date_to,
        "movement_types": InventoryMovement.MovementType.choices,
        "total_rows": len(rows),
    })


@login_required
def inbound(request):
    form = InboundForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            record_inbound(actor=request.user, **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "Inbound aktual diposting; movement dan FIFO cost layer terbentuk.")
            return redirect("inventory:inbound")
    outstanding = []
    lines = PurchaseOrderLine.objects.filter(po__status=PurchaseOrder.Status.RELEASED).select_related(
        "po", "po__supplier", "sku"
    ).prefetch_related("qc_inspections", "inbound_receipts")
    for line in lines:
        qc_passed = line.qc_passed_before_cutover_qty + sum((row.qty_passed for row in line.qc_inspections.all()), 0)
        received = line.received_before_cutover_qty + sum((row.received_qty for row in line.inbound_receipts.all()), 0)
        outstanding_qty = max(line.ordered_qty - received, 0)
        eligible_qty = max(qc_passed - received, 0)
        if outstanding_qty:
            outstanding.append({"line": line, "qc_passed": qc_passed, "received": received, "outstanding": outstanding_qty, "eligible": eligible_qty})
    receipts = InboundReceipt.objects.select_related("po_line__po", "po_line__sku", "warehouse", "recorded_by").order_by("-inbound_date", "-created_at")[:300]
    return render(request, "inventory/inbound.html", {"form": form, "outstanding": outstanding, "receipts": receipts})


@login_required
def return_log(request):
    form = ReturnForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            _, movement = record_physical_return(actor=request.user, **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            detail = "stock/FIFO dipulihkan" if movement else "tidak menambah stock karena kondisi non-sellable"
            messages.success(request, f"Physical Return dicatat; {detail}.")
            return redirect("inventory:return_log")
    expected = ExpectedReturn.objects.exclude(status=ExpectedReturn.Status.RECEIVED).select_related("sales_line__order", "sales_line__sku")[:300]
    receipts = PhysicalReturnReceipt.objects.select_related("sales_line__order", "sales_line__sku", "warehouse", "recorded_by").order_by("-received_date", "-created_at")[:300]
    return render(request, "inventory/returns.html", {"form": form, "expected_returns": expected, "receipts": receipts})


@login_required
def outbound(request):
    query = request.GET.get("q", "").strip()
    date_from = request.GET.get("date_from") or None
    date_to = request.GET.get("date_to") or None
    source = request.GET.get("source", "")
    rows = InventoryMovement.objects.filter(movement_type=InventoryMovement.MovementType.SALES_OUT).select_related(
        "sku", "sales_line__order", "posted_by"
    )
    if query:
        rows = rows.filter(Q(sku__sku__icontains=query) | Q(sales_line__order__order_number__icontains=query))
    if date_from:
        rows = rows.filter(movement_date__gte=date_from)
    if date_to:
        rows = rows.filter(movement_date__lte=date_to)
    if source:
        rows = rows.filter(sales_line__order__source=source)
    page = Paginator(rows.order_by("-movement_date", "-posted_at"), 100).get_page(request.GET.get("page"))
    return render(request, "inventory/outbound.html", {"page": page, "query": query, "date_from": date_from, "date_to": date_to, "source": source})


@login_required
def opening_import_list(request):
    return render(request, "inventory/opening/list.html", {"batches": FIFOOpeningImportBatch.objects.select_related("raw_file", "approved_by")[:20]})


@login_required
def opening_import_upload(request):
    if request.method == "POST":
        form = FIFOOpeningImportUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                batch = create_opening_import(form.cleaned_data["file"], request.user)
            except DuplicateRawFile as exc:
                existing = exc.raw_file.fifo_opening_batches.order_by("-created_at").first()
                form.add_error("file", f"File identik sudah pernah diunggah. Batch sebelumnya: {existing.id if existing else '-'}.")
            else:
                if batch.status == FIFOOpeningImportBatch.Status.READY:
                    messages.success(request, "FIFO Opening berhasil diparsing dan siap direview.")
                else:
                    messages.warning(request, "FIFO Opening diparsing tetapi memiliki blocking issue.")
                return redirect("inventory:opening_detail", batch_id=batch.id)
    elif request.method == "GET":
        form = FIFOOpeningImportUploadForm()
    else:
        return HttpResponseNotAllowed(["GET", "POST"])
    return render(request, "inventory/opening/upload.html", {"form": form})


@login_required
def opening_import_detail(request, batch_id):
    batch = get_object_or_404(FIFOOpeningImportBatch.objects.select_related("raw_file", "approved_by"), pk=batch_id)
    page = Paginator(batch.staged_rows.select_related("sku"), 50).get_page(request.GET.get("page"))
    issues = batch.issues.select_related("staged_row")
    severity = request.GET.get("severity", "")
    if severity in FIFOOpeningImportIssue.Severity.values:
        issues = issues.filter(severity=severity)
    return render(request, "inventory/opening/detail.html", {"batch": batch, "page": page, "issues": issues[:100]})


@login_required
def opening_import_approve(request, batch_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    batch = get_object_or_404(FIFOOpeningImportBatch, pk=batch_id)
    try:
        _, counts = approve_opening_import(batch.id, request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, f"FIFO Opening committed: {counts['snapshots']} snapshot, {counts['positive_layers']} layer positif, {counts['negative_exceptions']} exception negatif.")
    return redirect("inventory:opening_detail", batch_id=batch.id)
