from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms_partnership import KolMetricForm, KolPartnershipForm, KolPostUrlForm, KolProductFormSet
from .kol_metrics import read_public_metrics
from .models import Campaign, KolPartnership


@login_required
def partnership_list(request):
    request.session["active_module"] = "marketing"
    items = KolPartnership.objects.select_related("campaign", "created_by").prefetch_related("products__product")
    kol_options = list(items.order_by("kol_name").values_list("kol_name", flat=True).distinct())
    campaign_options = list(Campaign.objects.filter(kol_partnerships__isnull=False).order_by("name").distinct())
    selected_kol = request.GET.get("kol", "").strip()
    selected_campaign = request.GET.get("campaign", "").strip()
    if selected_kol in kol_options:
        items = items.filter(kol_name=selected_kol)
    else:
        selected_kol = ""
    if selected_campaign in {str(campaign.id) for campaign in campaign_options}:
        items = items.filter(campaign_id=selected_campaign)
    else:
        selected_campaign = ""
    return render(request, "dashboard/partnership_list.html", {
        "items": items, "kol_options": kol_options, "campaign_options": campaign_options,
        "selected_kol": selected_kol, "selected_campaign": selected_campaign,
    })


@login_required
@require_POST
def partnership_delete(request, partnership_id):
    if not request.user.is_superuser:
        return HttpResponseForbidden()
    item = get_object_or_404(KolPartnership, id=partnership_id)
    item.delete()
    messages.success(request, "Partnership berhasil dihapus.")
    return redirect("dashboard:partnership_list")


def _form(request, instance=None):
    is_edit = not instance._state.adding
    initial = {"budget": f"{instance.budget:.0f}"} if is_edit and request.method == "GET" else None
    form = KolPartnershipForm(request.POST or None, instance=instance, initial=initial)
    formset = KolProductFormSet(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        item = form.save(commit=False)
        if item._state.adding:
            item.created_by = request.user
        item.save()
        formset.instance = item
        formset.save()
        messages.success(request, "Partnership KOL berhasil disimpan.")
        return redirect("dashboard:partnership_detail", partnership_id=item.id)
    return render(request, "dashboard/partnership_form.html", {"form": form, "formset": formset, "item": instance, "is_edit": is_edit})


@login_required
@transaction.atomic
def partnership_create(request):
    return _form(request, KolPartnership())


@login_required
@transaction.atomic
def partnership_edit(request, partnership_id):
    return _form(request, get_object_or_404(KolPartnership, id=partnership_id))


@login_required
def partnership_detail(request, partnership_id):
    item = get_object_or_404(KolPartnership.objects.select_related("campaign").prefetch_related("products__product"), id=partnership_id)
    metric_form = KolMetricForm(instance=item)
    link_form = KolPostUrlForm(instance=item)
    if request.method == "POST" and request.POST.get("action") == "metrics":
        metric_form = KolMetricForm(request.POST, instance=item)
        if metric_form.is_valid():
            metric_form.save()
            item.metrics_updated_at = timezone.now()
            item.metrics_error = ""
            item.save(update_fields=("metrics_updated_at", "metrics_error", "updated_at"))
            messages.success(request, "Metrik manual diperbarui.")
            return redirect("dashboard:partnership_detail", partnership_id=item.id)
    elif request.method == "POST" and request.POST.get("action") == "link":
        link_form = KolPostUrlForm(request.POST, instance=item)
        if link_form.is_valid():
            link_form.save()
            messages.success(request, "Link konten berhasil disimpan.")
            return redirect("dashboard:partnership_detail", partnership_id=item.id)
    elif request.method == "POST" and request.POST.get("action") == "refresh":
        if not item.post_url:
            messages.error(request, "Isi Link Konten lebih dulu.")
        else:
            try:
                values = read_public_metrics(item.post_url, item.platform)
                for field, value in values.items():
                    if value is not None:
                        setattr(item, field, value)
                item.metrics_updated_at = timezone.now()
                item.metrics_error = ""
                item.save(update_fields=(*[field for field, value in values.items() if value is not None], "metrics_updated_at", "metrics_error", "updated_at"))
                messages.success(request, "Metrik publik yang tersedia berhasil dibaca.")
            except (OSError, ValueError) as exc:
                item.metrics_error = str(exc)[:255]
                item.save(update_fields=("metrics_error", "updated_at"))
                messages.warning(request, item.metrics_error)
        return redirect("dashboard:partnership_detail", partnership_id=item.id)
    return render(request, "dashboard/partnership_detail.html", {"item": item, "metric_form": metric_form, "link_form": link_form})
