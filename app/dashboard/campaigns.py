from datetime import date, timedelta
from decimal import Decimal
from mimetypes import guess_type
from urllib.parse import urlsplit
from uuid import UUID

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.db.models import Sum
from django.http import FileResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from sales.models import SalesOrderLine
from traffic.models import TrafficProductMetric

from .forms_campaign import CampaignActualTimelineForm, CampaignExpenseForm, CampaignForm, CampaignProductFormSet, CreativeForm
from .models import Campaign, CampaignExpense
from .instagram_report import get_report
from . import tiktok


def _end_date(launch):
    return launch + timedelta(days=30)


def _post_key(url):
    parts = urlsplit(url)
    path = [part for part in parts.path.split("/") if part]
    shortcode = path[1] if len(path) >= 2 and path[0] in {"p", "reel", "tv"} else parts.path.rstrip("/")
    return (parts.hostname or "").removeprefix("www.") + "/" + shortcode


def _instagram_embed_url(url):
    return "https://www.instagram.com" + urlsplit(url).path.rstrip("/") + "/embed/"


def _tiktok_embed_url(url):
    post_id = tiktok.video_id_from_url(url)
    return f"https://www.tiktok.com/player/v1/{post_id}?description=1" if post_id else ""


def _sync_actual_spent(campaign):
    campaign.actual_spent = CampaignExpense.objects.filter(campaign=campaign).aggregate(total=Sum("amount"))["total"] or Decimal("0")
    campaign.save(update_fields=("actual_spent", "updated_at"))


@login_required
def campaign_list(request):
    request.session["active_module"] = "marketing"
    campaigns = Campaign.objects.order_by("-created_at").prefetch_related("products")
    return render(request, "dashboard/campaign_list.html", {"campaigns": campaigns})


@login_required
@require_POST
def campaign_delete(request, campaign_id):
    if not request.user.is_superuser:
        return HttpResponseForbidden()
    campaign = get_object_or_404(Campaign, id=campaign_id)
    try:
        campaign.delete()
    except ProtectedError:
        messages.error(request, "Campaign belum dapat dihapus karena masih dipakai Partnership. Hapus Partnership terkait lebih dulu.")
        return redirect("dashboard:campaign_detail", campaign_id=campaign.id)
    messages.success(request, "Campaign berhasil dihapus.")
    return redirect("dashboard:campaign_list")


@login_required
@transaction.atomic
def campaign_create(request):
    campaign = Campaign(created_by=request.user)
    form = CampaignForm(request.POST or None, request.FILES or None, instance=campaign)
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
    campaign = get_object_or_404(Campaign, id=campaign_id)
    initial = {"budget": f"{campaign.budget:.0f}"} if request.method == "GET" else None
    form = CampaignForm(request.POST or None, request.FILES or None, instance=campaign, initial=initial)
    formset = CampaignProductFormSet(request.POST or None, instance=campaign)
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        form.save()
        formset.save()
        messages.success(request, "Campaign berhasil diperbarui.")
        return redirect("dashboard:campaign_detail", campaign_id=campaign.id)
    return render(request, "dashboard/campaign_form.html", {"form": form, "formset": formset, "is_edit": True, "campaign": campaign})


@login_required
def campaign_cover(request, campaign_id):
    campaign = get_object_or_404(Campaign, id=campaign_id)
    if not campaign.cover:
        return HttpResponseForbidden()
    response = FileResponse(campaign.cover.open("rb"), content_type=guess_type(campaign.cover.name)[0] or "application/octet-stream")
    response["Content-Disposition"] = f'inline; filename="{campaign.cover.name.rsplit("/", 1)[-1]}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
def campaign_detail(request, campaign_id):
    campaign = get_object_or_404(Campaign.objects.prefetch_related("products__product", "creatives", "expenses__created_by", "kol_partnerships"), id=campaign_id)
    expense_form = CampaignExpenseForm()
    actual_timeline_form = CampaignActualTimelineForm(instance=campaign)
    if request.method == "POST" and request.POST.get("action") == "timeline_actual":
        actual_timeline_form = CampaignActualTimelineForm(request.POST, instance=campaign)
        if actual_timeline_form.is_valid():
            actual_timeline_form.save()
            messages.success(request, "Actual timeline berhasil diperbarui.")
            return redirect("dashboard:campaign_detail", campaign_id=campaign.id)
        creative = CreativeForm()
    elif request.method == "POST" and request.POST.get("action") == "expense":
        expense_form = CampaignExpenseForm(request.POST)
        if expense_form.is_valid():
            with transaction.atomic():
                expense = expense_form.save(commit=False)
                expense.campaign = campaign
                expense.created_by = request.user
                expense.save()
                _sync_actual_spent(campaign)
            messages.success(request, "Campaign spent berhasil ditambahkan.")
            return redirect("dashboard:campaign_detail", campaign_id=campaign.id)
        creative = CreativeForm()
    elif request.method == "POST" and request.POST.get("action") == "delete_expense":
        try:
            expense_id = UUID(request.POST.get("expense_id", ""))
        except (TypeError, ValueError):
            messages.error(request, "Campaign spent tidak valid.")
            return redirect("dashboard:campaign_detail", campaign_id=campaign.id)
        expense = get_object_or_404(CampaignExpense, id=expense_id, campaign=campaign)
        with transaction.atomic():
            expense.delete()
            _sync_actual_spent(campaign)
        messages.success(request, "Campaign spent berhasil dihapus.")
        return redirect("dashboard:campaign_detail", campaign_id=campaign.id)
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
    traffic_by_listing = {}
    for metric in TrafficProductMetric.objects.filter(
        product_id__in=[item.product_id for item in campaign.products.all()],
        period_start__lte=end,
        period_end__gte=campaign.prelaunch_date,
    ).values("product_id", "source", "period_start", "marketplace_product_code_snapshot", "traffic_product_key", "visitors"):
        listing = metric["marketplace_product_code_snapshot"] or metric["traffic_product_key"]
        key = (metric["product_id"], metric["source"], metric["period_start"], listing)
        traffic_by_listing[key] = max(traffic_by_listing.get(key, 0), metric["visitors"])
    traffic_totals = {}
    for (product_id, source, _period, _listing), views in traffic_by_listing.items():
        traffic_totals[(product_id, source)] = traffic_totals.get((product_id, source), 0) + views
    rows = []
    for item in campaign.products.all():
        actual = SalesOrderLine.objects.filter(
            is_counted=True, sku__product_variant__product=item.product,
            order__order_date__gte=campaign.prelaunch_date, order__order_date__lte=end,
        ).aggregate(qty=Sum("quantity"), gross=Sum("total_gross_sales"))
        qty, gross = actual["qty"] or 0, actual["gross"] or Decimal("0")
        rows.append({"item": item, "qty": qty, "gross": gross,
                     "qty_achievement": Decimal(qty) / item.target_qty * 100 if item.target_qty else None,
                     "traffic_shopee": traffic_totals.get((item.product_id, "Shopee"), 0),
                     "traffic_tiktok": traffic_totals.get((item.product_id, "Tiktok"), 0)})
    target = sum((row["item"].target_gross_sales for row in rows), Decimal("0"))
    gross = sum((row["gross"] for row in rows), Decimal("0"))
    total_target_qty = sum(row["item"].target_qty for row in rows)
    total_actual_qty = sum(row["qty"] for row in rows)
    product_totals = {
        "target_qty": total_target_qty,
        "actual_qty": total_actual_qty,
        "achievement": Decimal(total_actual_qty) / total_target_qty * 100 if total_target_qty else None,
        "target_gross": target,
        "actual_gross": gross,
        "traffic_shopee": sum(row["traffic_shopee"] for row in rows),
        "traffic_tiktok": sum(row["traffic_tiktok"] for row in rows),
    }
    kol_items = list(campaign.kol_partnerships.all())
    kol_budget = sum((item.budget for item in kol_items), Decimal("0"))
    kol_views = sum(item.views for item in kol_items)
    kol_engagement = sum(item.total_engagement for item in kol_items)
    kol_summary = {
        "posts": len(kol_items), "budget": kol_budget, "views": kol_views, "engagement": kol_engagement,
        "er": Decimal(kol_engagement) / kol_views * 100 if kol_views else None,
        "cpm": kol_budget / kol_views * 1000 if kol_views else None,
    }
    actual_spent = campaign.actual_spent + kol_budget
    roi = gross / actual_spent if actual_spent else None
    instagram = [item for item in campaign.creatives.all() if item.platform == "INSTAGRAM"]
    tiktok_items = [item for item in campaign.creatives.all() if item.platform == "TIKTOK"]
    for item in campaign.creatives.all():
        item.api_matched = False
        item.embed_url = _instagram_embed_url(item.post_url) if item.platform == "INSTAGRAM" else _tiktok_embed_url(item.post_url)
        item.post_metrics = None
        item.comments = None
        item.comments_complete = False
    social = {
        "Instagram": {"posts": len(instagram), "matched": 0, "reach": 0, "reach_matched": 0, "views": 0, "engagement": 0},
        "TikTok": {"posts": len(tiktok_items), "matched": 0, "reach": 0, "reach_matched": 0, "views": 0, "engagement": 0},
    }
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
                "likes": metrics.get("likes"), "comments": metrics.get("comments"),
                "saves": metrics.get("saved"), "shares": metrics.get("shares"),
                "engagement": metrics.get("total_interactions"), "er": metrics.get("er"),
            }
            creative_item.comments = media.get("comments") if media.get("comments_available") else None
            creative_item.comments_complete = bool(media.get("comments_complete"))
            social["Instagram"]["matched"] += 1
            for source, target_key in (("reach", "reach"), ("views", "views"), ("total_interactions", "engagement")):
                value = media["metrics"].get(source)
                if value is not None:
                    social["Instagram"][target_key] += value
                    if target_key == "reach":
                        social["Instagram"]["reach_matched"] += 1
    if tiktok_items:
        try:
            by_id = tiktok.query_videos(tiktok.video_id_from_url(item.post_url) for item in tiktok_items)
            for creative_item in tiktok_items:
                media = by_id.get(tiktok.video_id_from_url(creative_item.post_url))
                if not media:
                    continue
                creative_item.api_matched = True
                reach = (media.get("business") or {}).get("reach")
                creative_item.post_metrics = {
                    "views": media["views"], "reach": reach, "likes": media["likes"],
                    "comments": media["comments"], "saves": None, "shares": media["shares"],
                    "engagement": media["engagement"], "er": media["er"],
                }
                social["TikTok"]["matched"] += 1
                social["TikTok"]["views"] += media["views"]
                social["TikTok"]["engagement"] += media["engagement"]
                if reach is not None:
                    social["TikTok"]["reach"] += reach
                    social["TikTok"]["reach_matched"] += 1
        except tiktok.TikTokConnectionError as exc:
            report_error = " · ".join(filter(None, (report_error, str(exc))))
    for values in social.values():
        count = values["matched"]
        reach_count = values.pop("reach_matched")
        values["reach"] = values["reach"] if reach_count else None
        values["avg_reach"] = values["reach"] / reach_count if reach_count else None
        values["avg_views"] = values["views"] / count if count else None
        values["avg_engagement"] = values["engagement"] / count if count else None
        values["er"] = values["engagement"] / values["views"] * 100 if values["views"] else None
    return render(request, "dashboard/campaign_detail.html", {"campaign": campaign, "rows": rows, "product_totals": product_totals, "kol_summary": kol_summary, "actual_spent": actual_spent, "end": end, "target": target, "gross": gross, "roi": roi, "creative_form": creative, "expense_form": expense_form, "actual_timeline_form": actual_timeline_form, "social": social, "report_error": report_error})
