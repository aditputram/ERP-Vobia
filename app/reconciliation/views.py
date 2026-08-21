from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from .models import ReconciliationRun
from .services.engine import run_reconciliation


@login_required
def overview(request):
    if request.method == "POST":
        run = run_reconciliation(request.user)
        if run.status == ReconciliationRun.Status.PASSED:
            messages.success(request, "Reconciliation selesai: semua integrity check lulus.")
        else:
            messages.error(request, f"Reconciliation menemukan {run.issues.count()} issue. Buka hasil untuk detail.")
        return redirect("reconciliation:detail", run_id=run.id)
    return render(request, "reconciliation/overview.html", {"runs": ReconciliationRun.objects.all()[:30]})


@login_required
def detail(request, run_id):
    run = get_object_or_404(ReconciliationRun, pk=run_id)
    return render(request, "reconciliation/detail.html", {"run": run, "issues": run.issues.all()})
