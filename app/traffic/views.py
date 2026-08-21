from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render

from imports.services.storage import DuplicateRawFile

from .forms import TrafficUploadForm
from .models import TrafficImportBatch, TrafficPeriodState, TrafficProductMetric
from .services.ingestion import commit_batch, create_traffic_import, mark_period_complete, reopen_period
from .services.requirements import traffic_requirements


@login_required
def overview(request):
    form = TrafficUploadForm()
    if request.method == "POST" and request.POST.get("form_name") == "upload":
        form = TrafficUploadForm(request.POST, request.FILES)
        if form.is_valid():
            data = form.cleaned_data
            try:
                batch = create_traffic_import(actor=request.user, uploaded_file=data.pop("file"), **data)
            except (ValidationError, DuplicateRawFile) as exc:
                form.add_error("file", " ".join(getattr(exc, "messages", [str(exc)])))
            else:
                messages.success(request, "Traffic diparsing. Review hasil mapping sebelum commit.")
                return redirect("traffic:detail", batch_id=batch.id)
    return render(
        request,
        "traffic/overview.html",
        {
            "form": form,
            "requirements": traffic_requirements(),
            "batches": TrafficImportBatch.objects.select_related("raw_file")[:30],
            "states": TrafficPeriodState.objects.all(),
            "metrics": TrafficProductMetric.objects.select_related("product")[:100],
        },
    )


@login_required
def detail(request, batch_id):
    batch = get_object_or_404(TrafficImportBatch.objects.select_related("raw_file"), pk=batch_id)
    return render(request, "traffic/detail.html", {"batch": batch, "rows": batch.staged_rows.select_related("product")[:200], "issues": batch.issues.select_related("staged_row")[:200]})


@login_required
def approve(request, batch_id):
    if request.method == "POST":
        try:
            commit_batch(batch_id, request.user)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, "Traffic canonical berhasil diperbarui tanpa duplicate period/product.")
    return redirect("traffic:detail", batch_id=batch_id)


@login_required
def complete(request):
    if request.method == "POST":
        try:
            mark_period_complete(request.POST["source"], date.fromisoformat(request.POST["month"]), request.user)
        except (ValidationError, KeyError, ValueError) as exc:
            messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
        else:
            messages.success(request, "Periode traffic ditandai complete.")
    return redirect("traffic:overview")


@login_required
def reopen(request):
    if request.method == "POST":
        try:
            reopen_period(request.POST["source"], date.fromisoformat(request.POST["month"]), request.user, request.POST.get("reason", ""))
        except (ValidationError, KeyError, ValueError, TrafficPeriodState.DoesNotExist) as exc:
            messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
        else:
            messages.success(request, "Periode traffic dibuka kembali dan tercatat di audit trail.")
    return redirect("traffic:overview")
