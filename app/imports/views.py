from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render

from .forms import ManualSaleForm, MasterImportUploadForm, SalesImportUploadForm
from .models import (
    ImportValidationIssue,
    MasterImportBatch,
    SalesImportBatch,
    SalesImportIssue,
    StagedSalesRow,
)
from .services.master_commit import approve_master_import
from .services.sales_commit import approve_sales_import
from .services.storage import DuplicateRawFile, create_master_import, create_sales_import
from sales.services.manual import create_manual_sale
from sales.services.requirements import import_requirements, summarize_import_requirements
from inventory.models import FIFOOpeningImportBatch


@login_required
def master_import_list(request):
    batches = MasterImportBatch.objects.select_related("raw_file", "raw_file__uploaded_by")[:30]
    return render(request, "imports/master_list.html", {"batches": batches})


@login_required
def master_import_upload(request):
    if request.method == "POST":
        form = MasterImportUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                batch = create_master_import(form.cleaned_data["file"], request.user)
            except DuplicateRawFile as exc:
                existing_batch = exc.raw_file.master_batches.order_by("-created_at").first()
                detail = f" Batch sebelumnya: {existing_batch.id}." if existing_batch else ""
                form.add_error("file", "File identik sudah pernah diunggah." + detail)
            else:
                if batch.status == MasterImportBatch.Status.READY:
                    messages.success(request, "File berhasil diparsing dan siap direview.")
                else:
                    messages.warning(request, "File diparsing tetapi memiliki blocking issue.")
                return redirect("imports:master_detail", batch_id=batch.id)
    elif request.method == "GET":
        form = MasterImportUploadForm()
    else:
        return HttpResponseNotAllowed(["GET", "POST"])
    return render(request, "imports/master_upload.html", {"form": form})


@login_required
def master_import_detail(request, batch_id):
    batch = get_object_or_404(
        MasterImportBatch.objects.select_related("raw_file", "approved_by"),
        pk=batch_id,
    )
    action_filter = request.GET.get("action", "")
    severity_filter = request.GET.get("severity", "")

    staged_rows = batch.staged_rows.select_related("existing_sku")
    if action_filter in {choice for choice, _ in batch.staged_rows.model.ProposedAction.choices}:
        staged_rows = staged_rows.filter(proposed_action=action_filter)
    page = Paginator(staged_rows, 50).get_page(request.GET.get("page"))

    issues = batch.issues.select_related("staged_row")
    if severity_filter in {choice for choice, _ in ImportValidationIssue.Severity.choices}:
        issues = issues.filter(severity=severity_filter)

    context = {
        "batch": batch,
        "page": page,
        "issues": issues[:100],
        "action_filter": action_filter,
        "severity_filter": severity_filter,
    }
    return render(request, "imports/master_detail.html", context)


@login_required
def master_import_approve(request, batch_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    batch = get_object_or_404(MasterImportBatch, pk=batch_id)
    try:
        _, counts = approve_master_import(batch.id, request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(
            request,
            "Master Product berhasil di-commit: "
            f"{counts['created']} baru, {counts['updated']} berubah, "
            f"{counts['unchanged']} tidak berubah.",
        )
    return redirect("imports:master_detail", batch_id=batch.id)


@login_required
def sales_import_list(request):
    manual_form = ManualSaleForm()
    if request.method == "POST":
        manual_form = ManualSaleForm(request.POST)
        if manual_form.is_valid():
            try:
                line = create_manual_sale(actor=request.user, **manual_form.cleaned_data)
            except ValidationError as exc:
                manual_form.add_error(None, exc)
            else:
                if getattr(line, "retail_price_special_case", False):
                    master_retail = f"{line.master_retail_price_at_entry:,.0f}".replace(",", ".")
                    retail_snapshot = f"{line.retail_price_snapshot:,.0f}".replace(",", ".")
                    messages.warning(
                        request,
                        f"SPECIAL CASE HARGA · Transaksi {line.business_key} berhasil diposting. "
                        f"Retail Price master tetap Rp {master_retail}; snapshot transaksi ini saja "
                        f"disesuaikan menjadi Rp {retail_snapshot}.",
                    )
                else:
                    messages.success(request, f"Transaksi manual {line.business_key} berhasil diposting.")
                return redirect("imports:sales_list")
    batches = SalesImportBatch.objects.exclude(
        status=SalesImportBatch.Status.VOIDED
    ).select_related("raw_file", "raw_file__uploaded_by")[:30]
    requirements = import_requirements()
    return render(
        request,
        "imports/sales/list.html",
        {
            "batches": batches,
            "requirements": requirements,
            "requirement_summary": summarize_import_requirements(requirements),
            "manual_form": manual_form,
        },
    )


@login_required
def sales_import_upload(request):
    if request.method == "POST":
        form = SalesImportUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                batch = create_sales_import(
                    form.cleaned_data["file"],
                    form.cleaned_data["source"],
                    request.user,
                )
            except DuplicateRawFile as exc:
                existing_batch = exc.raw_file.sales_batches.order_by("-created_at").first()
                detail = f" Batch sebelumnya: {existing_batch.id}." if existing_batch else ""
                form.add_error("file", "File identik sudah pernah diunggah." + detail)
            else:
                if batch.status == SalesImportBatch.Status.READY:
                    messages.success(request, "File Sales berhasil diparsing dan siap direview.")
                else:
                    messages.warning(request, "File diparsing tetapi memiliki blocking issue.")
                return redirect("imports:sales_detail", batch_id=batch.id)
    elif request.method == "GET":
        form = SalesImportUploadForm()
    else:
        return HttpResponseNotAllowed(["GET", "POST"])
    return render(request, "imports/sales/upload.html", {"form": form})


@login_required
def sales_import_detail(request, batch_id):
    batch = get_object_or_404(
        SalesImportBatch.objects.select_related("raw_file", "approved_by"),
        pk=batch_id,
    )
    action_filter = request.GET.get("action", "")
    severity_filter = request.GET.get("severity", "")
    staged_rows = batch.staged_rows.select_related("sku", "existing_line")
    if action_filter in {choice for choice, _ in StagedSalesRow.ProposedAction.choices}:
        staged_rows = staged_rows.filter(proposed_action=action_filter)
    page = Paginator(staged_rows, 50).get_page(request.GET.get("page"))

    issues = batch.issues.select_related("staged_row")
    if severity_filter in {choice for choice, _ in SalesImportIssue.Severity.choices}:
        issues = issues.filter(severity=severity_filter)
    return render(
        request,
        "imports/sales/detail.html",
        {
            "batch": batch,
            "page": page,
            "issues": issues[:100],
            "commit_enabled": settings.SALES_IMPORT_COMMIT_ENABLED,
            "fifo_opening_ready": FIFOOpeningImportBatch.objects.filter(status=FIFOOpeningImportBatch.Status.COMMITTED).exists(),
        },
    )


@login_required
def sales_import_approve(request, batch_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    batch = get_object_or_404(SalesImportBatch, pk=batch_id)
    try:
        _, counts = approve_sales_import(batch.id, request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(
            request,
            f"Sales committed: {counts['orders_created']} order baru, "
            f"{counts['lines_created']} line baru, {counts['status_updates']} status berubah.",
        )
    return redirect("imports:sales_detail", batch_id=batch.id)
