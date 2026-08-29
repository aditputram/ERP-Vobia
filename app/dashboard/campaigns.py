import calendar
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Sum
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render

from sales.models import SalesOrderLine

from .forms_campaign import CampaignForm, CampaignProductFormSet, CampaignSpendForm, CreativeForm
from .models import Campaign
from .instagram_report import get_report


def _end_date(launch):
    year, month = (launch.year + 1, 1) if launch.month == 12 else (launch.year, launch.month + 1)
    return date(year, month, min(launch.day, calendar.monthrange(year, month)[1])) - timedelta(days=1)


def _post_key(url):
    parts = urlsplit(url)
    path = [part for part in parts.path.split("/") if part]
    shortcode = path[1] if len(path) >= 2 and path[0] in {"p", "reel", "tv"} else parts.path.rstrip("/")
    return (parts.hostname or "").removeprefix("www.") + "/" + shortcode


def _instagram_embed_url(url):
    return "https://www.instagram.com" + urlsplit(url).path.rstrip("/") + "/embed/"


def _guard(request):
    return request.user.is_superuser


@login_required
def campaign_list(request):
    if not _guard(request):
        return HttpResponseForbidden()
    request.session["active_module"] = "marketing"
    return render(request, "dashboard/campaign_list.html", {"campaigns": Campaign.objects.prefetch_related("products")})


@login_required
@transaction.atomic
def campaign_create(request):
    if not _guard(request):
        return HttpResponseForbidden()
    campaign = Campaign(created_by=request.user)
    form = CampaignForm(request.POST or None, instance=campaign)
    formset = CampaignProductFormSet(request.POST or None, instance=campaign)
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        form.save()
        formset.save()
        messages.success(request, "Campaign berhasil dibuat.")
        return redirect("dashboard:campaign_detail", campaign_id=campaign.id)
    return render(request, "dashboard/campaign_form.html", {"form": form, "formset": formset})


@login_required
@transaction.atomic
def campaign_edit(request, campaign_id):
    if not _guard(request):
        return HttpResponseForbidden()
    campaign = get_object_or_404(Campaign, id=campaign_id)
    initial = {"budget": f"{campaign.budget:.0f}"} if request.method == "GET" else None
    form = CampaignForm(request.POST or None, instance=campaign, initial=initial)
    formset = CampaignProductFormSet(request.POST or None, instance=campaign)
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        form.save()
        formset.save()
        messages.success(request, "Campaign berhasil diperbarui.")
        return redirect("dashboard:campaign_detail", campaign_id=campaign.id)
    return render(request, "dashboard/campaign_form.html", {"form": form, "formset": formset, "is_edit": True, "campaign": campaign})


@login_required
def campaign_detail(request, campaign_id):
    if not _guard(request):
        return HttpResponseForbidden()
    campaign = get_object_or_404(Campaign.objects.prefetch_related("products__product", "creatives"), id=campaign_id)
    spend_form = CampaignSpendForm(instance=campaign)
    if request.method == "POST" and request.POST.get("action") == "spend":
        spend_form = CampaignSpendForm(request.POST, instance=campaign)
        if spend_form.is_valid():
            spend_form.save()
            messages.success(request, "Budget dan actual spent diperbarui.")
            return redirect("dashboard:campaign_detail", campaign_id=campaign.id)
        creative = CreativeForm()
    elif request.method == "POST":
        creative = CreativeForm(request.POST)
        if creative.is_valid():
            item = creative.save(commit=False)
            item.campaign = campaign
            item.save()
            return redirect("dashboard:campaign_detail", campaign_id=campaign.id)
    else:
        creative = CreativeForm()
    end = _end_date(campaign.launch_date)
    rows = []
    for item in campaign.products.all():
        actual = SalesOrderLine.objects.filter(
            is_counted=True, sku__product_variant__product=item.product,
            order__order_date__gte=campaign.prelaunch_date, order__order_date__lte=end,
        ).aggregate(qty=Sum("quantity"), gross=Sum("total_gross_sales"))
        qty, gross = actual["qty"] or 0, actual["gross"] or Decimal("0")
        rows.append({"item": item, "qty": qty, "gross": gross,
                     "qty_achievement": Decimal(qty) / item.target_qty * 100 if item.target_qty else None,
                     "gross_achievement": gross / item.target_gross_sales * 100 if item.target_gross_sales else None})
    target = sum((row["item"].target_gross_sales for row in rows), Decimal("0"))
    gross = sum((row["gross"] for row in rows), Decimal("0"))
    roi = gross / campaign.actual_spent if campaign.actual_spent else None
    instagram = [item for item in campaign.creatives.all() if item.platform == "INSTAGRAM"]
    for item in campaign.creatives.all():
        item.api_matched = False
        item.embed_url = _instagram_embed_url(item.post_url) if item.platform == "INSTAGRAM" else ""
        item.post_metrics = None
        item.comments = None
        item.comments_complete = False
    social = {"Instagram": {"posts": len(instagram), "matched": 0, "reach": 0, "views": 0, "engagement": 0}}
    report_error = ""
    if instagram:
        report, report_error = get_report(campaign.prelaunch_date, end)
        by_url = {_post_key(item["permalink"]): item for item in (report or {}).get("contents", []) if item.get("permalink")}
        for creative_item in instagram:
            media = by_url.get(_post_key(creative_item.post_url))
            if not media:
                continue
            creative_item.api_matched = True
            metrics = media["metrics"]
            creative_item.post_metrics = {
                "views": metrics.get("views"), "reach": metrics.get("reach"),
                "engagement": metrics.get("total_interactions"), "er": metrics.get("er"),
            }
            creative_item.comments = media.get("comments") if media.get("comments_available") else None
            creative_item.comments_complete = bool(media.get("comments_complete"))
            social["Instagram"]["matched"] += 1
            for source, target_key in (("reach", "reach"), ("views", "views"), ("total_interactions", "engagement")):
                value = media["metrics"].get(source)
                if value is not None:
                    social["Instagram"][target_key] += value
    for values in social.values():
        count = values["matched"]
        values["avg_reach"] = values["reach"] / count if count else None
        values["avg_views"] = values["views"] / count if count else None
        values["avg_engagement"] = values["engagement"] / count if count else None
        values["er"] = values["engagement"] / values["views"] * 100 if values["views"] else None
    return render(request, "dashboard/campaign_detail.html", {"campaign": campaign, "rows": rows, "end": end, "target": target, "gross": gross, "roi": roi, "creative_form": creative, "spend_form": spend_form, "social": social, "report_error": report_error})
