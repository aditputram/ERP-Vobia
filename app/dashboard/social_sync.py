"""Daily social metrics sync. External calls happen only from explicit jobs."""
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.db import transaction
from django.conf import settings
from django.http import HttpResponseForbidden, JsonResponse
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import tiktok_business
from .instagram import ACCOUNT_ID, USERNAME, ConnectionError as InstagramError, api_get, store_path
from .models import SocialDailyMetric, SocialPeriodMetric, SocialSyncRun
from .tiktok import TikTokConnectionError


PLATFORMS = (SocialDailyMetric.Platform.INSTAGRAM, SocialDailyMetric.Platform.TIKTOK)
ACCOUNT = "vobia.id"
MANUAL_LOCK_ACCOUNT = "vobia.id:manual-refresh"
METRICS = (
    "reach", "impressions", "total_engagement", "accounts_engaged",
    "profile_visits", "website_clicks", "likes", "comments", "shares",
    "new_followers", "lost_followers",
)


def supported_period_ranges(cutoff):
    ranges = set()
    for days in (7, 14, 30, 60, 90):
        start = cutoff - timedelta(days=days - 1)
        ranges.add((start, cutoff))
        ranges.add((start - timedelta(days=days), start - timedelta(days=1)))
    month_start = cutoff.replace(day=1)
    previous_end = month_start - timedelta(days=1)
    previous_start = previous_end.replace(day=1)
    ranges.update({(month_start, cutoff), (previous_start, previous_end)})
    previous_mtd_end = previous_start.replace(day=min(cutoff.day, previous_end.day))
    ranges.add((previous_start, previous_mtd_end))
    return sorted(ranges)


def _complete_period_values(rows_by_date, start, end):
    rows = [rows_by_date.get(start + timedelta(days=offset)) for offset in range((end - start).days + 1)]
    if any(row is None for row in rows):
        return None

    def total(name):
        values = [getattr(row, name) for row in rows]
        return sum(values) if all(value is not None for value in values) else None

    return {name: total(name) for name in METRICS}


@sensitive_variables()
def fetch_instagram_period_uniques(ranges):
    if not ranges:
        return {}
    with store_path().open() as handle:
        token = json.load(handle)["access_token"]
    profile = api_get(token, "me", {"fields": "user_id,username"})
    if str(profile.get("user_id")) != ACCOUNT_ID or profile.get("username", "").lower() != USERNAME:
        raise InstagramError("Akun token Instagram tidak cocok.")

    def fetch(date_range):
        from .instagram_report import metric_values

        start, end = date_range
        names = ("reach", "accounts_engaged")
        params = {
            "period": "day", "metric_type": "total_value",
            "since": start.isoformat(), "until": (end + timedelta(days=1)).isoformat(),
            "metric": ",".join(names),
        }
        values = metric_values(api_get(token, ACCOUNT_ID + "/insights", params), names)
        for name in (name for name, value in values.items() if value is None):
            values[name] = metric_values(
                api_get(token, ACCOUNT_ID + "/insights", {**params, "metric": name}),
                (name,),
            )[name]
        if all(values[name] is None for name in names):
            raise InstagramError("Metrik unik periode Instagram tidak tersedia.")
        return date_range, values

    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(fetch, ranges))


def sync_period_metrics(cutoff):
    ranges = supported_period_ranges(cutoff)
    rows_by_platform = {
        platform: {
            row.date: row for row in SocialDailyMetric.objects.filter(
                platform=platform, account=ACCOUNT,
                date__range=(ranges[0][0], cutoff),
            )
        }
        for platform in PLATFORMS
    }
    complete = {
        platform: {
            date_range: values
            for date_range in ranges
            if (values := _complete_period_values(rows_by_platform[platform], *date_range)) is not None
        }
        for platform in PLATFORMS
    }
    instagram_uniques = fetch_instagram_period_uniques(complete[SocialDailyMetric.Platform.INSTAGRAM])
    synced_at = timezone.now()
    with transaction.atomic():
        for platform in PLATFORMS:
            for (start, end), values in complete[platform].items():
                if platform == SocialDailyMetric.Platform.INSTAGRAM:
                    values = {
                        **values,
                        "reach": instagram_uniques[(start, end)]["reach"],
                        "accounts_engaged": instagram_uniques[(start, end)]["accounts_engaged"],
                    }
                SocialPeriodMetric.objects.update_or_create(
                    platform=platform, account=ACCOUNT, date_from=start, date_to=end,
                    defaults={**values, "synced_at": synced_at},
                )
    return sum(len(periods) for periods in complete.values())


def period_metric(platform, start, end):
    return SocialPeriodMetric.objects.filter(
        platform=platform, account=ACCOUNT, date_from=start, date_to=end,
    ).first()


def _instagram_values(token, day):
    from . import instagram_report
    period = {
        "period": "day", "metric_type": "total_value",
        "since": day.isoformat(), "until": (day + timedelta(days=1)).isoformat(),
    }
    names = (
        "reach", "views", "total_interactions", "accounts_engaged",
        "profile_views", "website_clicks", "likes", "comments", "shares",
    )
    payload = api_get(token, ACCOUNT_ID + "/insights", {**period, "metric": ",".join(names)})
    totals = instagram_report.metric_values(payload, names)
    for name in (name for name, value in totals.items() if value is None):
        single = api_get(token, ACCOUNT_ID + "/insights", {**period, "metric": name})
        totals[name] = instagram_report.metric_values(single, (name,))[name]
    if all(totals[name] is None for name in ("reach", "views", "total_interactions")):
        raise InstagramError("Metrik harian Instagram tidak tersedia.")
    return {
        "reach": totals["reach"], "impressions": totals["views"],
        "total_engagement": totals["total_interactions"],
        "accounts_engaged": totals["accounts_engaged"],
        "profile_visits": totals["profile_views"],
        "website_clicks": totals["website_clicks"],
        "likes": totals["likes"], "comments": totals["comments"],
        "shares": totals["shares"], "new_followers": None, "lost_followers": None,
    }


@sensitive_variables()
def fetch_instagram_days(days):
    with store_path().open() as handle:
        token = json.load(handle)["access_token"]
    profile = api_get(token, "me", {"fields": "user_id,username"})
    if str(profile.get("user_id")) != ACCOUNT_ID or profile.get("username", "").lower() != USERNAME:
        raise InstagramError("Akun token Instagram tidak cocok.")
    return [(day, _instagram_values(token, day)) for day in days]


@sensitive_variables()
def fetch_tiktok_days(days):
    result = []
    for day in days:
        report = tiktok_business.fetch_profile_report(day, day)
        result.append((day, {
            "reach": report["reach"], "impressions": report["views"],
            "total_engagement": report["engagement"],
            "accounts_engaged": report["accounts_engaged"],
            "profile_visits": report["profile_views"],
            "website_clicks": report["website_clicks"],
            "likes": report["likes"], "comments": report["comments"],
            "shares": report["shares"], "new_followers": report["new_followers"],
            "lost_followers": report["lost_followers"],
        }))
    return result


def _claim(platform, cutoff, source, actor, key, *, account=ACCOUNT):
    now = timezone.now()
    with transaction.atomic():
        run, created = SocialSyncRun.objects.select_for_update().get_or_create(
            idempotency_key=key,
            defaults={
                "platform": platform, "account": account, "source": source,
                "actor": actor, "status": SocialSyncRun.Status.RUNNING,
                "cutoff": cutoff, "started_at": now,
            },
        )
        if not created and run.status == SocialSyncRun.Status.COMPLETED:
            return run, False
        if (
            not created and run.status == SocialSyncRun.Status.RUNNING
            and run.started_at >= now - timedelta(hours=2)
        ):
            return run, False
        if not created:
            run.status = SocialSyncRun.Status.RUNNING
            run.started_at = now
            run.completed_at = None
            run.error = ""
            run.source = source
            run.actor = actor
            run.save(update_fields=("status", "started_at", "completed_at", "error", "source", "actor"))
    return run, True


def sync_platform(platform, cutoff, *, lookback_days=4, source="scheduler", actor="", idempotency_key=None):
    key = idempotency_key or f"daily:{platform.lower()}:{cutoff.isoformat()}"
    run, claimed = _claim(platform, cutoff, source, actor, key)
    if not claimed:
        return run
    days = [cutoff - timedelta(days=offset) for offset in reversed(range(lookback_days))]
    try:
        rows = fetch_instagram_days(days) if platform == SocialDailyMetric.Platform.INSTAGRAM else fetch_tiktok_days(days)
        synced_at = timezone.now()
        with transaction.atomic():
            for day, values in rows:
                SocialDailyMetric.objects.update_or_create(
                    platform=platform, account=ACCOUNT, date=day,
                    defaults={**values, "synced_at": synced_at},
                )
            run.status = SocialSyncRun.Status.COMPLETED
            run.completed_at = synced_at
            run.snapshot_at = synced_at
            run.error = ""
            run.save(update_fields=("status", "completed_at", "snapshot_at", "error"))
    except (InstagramError, TikTokConnectionError, OSError, KeyError, ValueError):
        run.status = SocialSyncRun.Status.FAILED
        run.completed_at = timezone.now()
        run.error = "Sinkronisasi gagal; snapshot terakhir tetap dipertahankan."
        run.save(update_fields=("status", "completed_at", "error"))
    except Exception:
        run.status = SocialSyncRun.Status.FAILED
        run.completed_at = timezone.now()
        run.error = "Sinkronisasi gagal; detail rahasia tidak disimpan."
        run.save(update_fields=("status", "completed_at", "error"))
    return run


def sync_daily(*, cutoff=None, lookback_days=4, source="scheduler", actor="", key_prefix="daily"):
    cutoff = cutoff or timezone.localdate() - timedelta(days=1)
    return [
        sync_platform(
            platform, cutoff, lookback_days=lookback_days, source=source, actor=actor,
            idempotency_key=f"{key_prefix}:{platform.lower()}:{cutoff.isoformat()}",
        )
        for platform in PLATFORMS
    ]


def daily_series(platform, start, end):
    rows = SocialDailyMetric.objects.filter(
        platform=platform, account=ACCOUNT, date__range=(start, end),
    ).order_by("date")
    return [{"date": row.date.isoformat(), **{name: getattr(row, name) for name in METRICS}} for row in rows]


def sync_status(platform):
    runs = SocialSyncRun.objects.filter(platform=platform, account=ACCOUNT)
    latest_valid = runs.filter(status=SocialSyncRun.Status.COMPLETED).order_by(
        "-cutoff", "-completed_at",
    ).first()
    latest = runs.order_by("-cutoff", "-started_at").first()
    return {"latest": latest, "latest_valid": latest_valid}


def manual_refresh_state(day=None):
    day = day or timezone.localdate()
    return SocialSyncRun.objects.filter(
        idempotency_key=f"manual-global:{day.isoformat()}", account=MANUAL_LOCK_ACCOUNT,
    ).first()


def run_manual_refresh(actor):
    today = timezone.localdate()
    cutoff = today - timedelta(days=1)
    coordinator, claimed = _claim(
        SocialDailyMetric.Platform.INSTAGRAM, cutoff, "manual", actor,
        f"manual-global:{today.isoformat()}", account=MANUAL_LOCK_ACCOUNT,
    )
    if not claimed:
        return coordinator, [], False
    runs = sync_daily(
        cutoff=cutoff, lookback_days=4, source="manual", actor=actor,
        key_prefix=f"manual:{today.isoformat()}",
    )
    period_metrics_completed = True
    try:
        sync_period_metrics(cutoff)
    except Exception:
        period_metrics_completed = False
    completed = (
        all(run.status == SocialSyncRun.Status.COMPLETED for run in runs)
        and period_metrics_completed
    )
    coordinator.status = SocialSyncRun.Status.COMPLETED if completed else SocialSyncRun.Status.FAILED
    coordinator.completed_at = timezone.now()
    coordinator.snapshot_at = coordinator.completed_at if completed else None
    coordinator.error = "" if completed else "Refresh belum lengkap; dapat dicoba lagi."
    coordinator.save(update_fields=("status", "completed_at", "snapshot_at", "error"))
    return coordinator, runs, True


@csrf_exempt
@require_POST
def scheduled_sync(request):
    if not settings.USE_SQLITE and not request.is_secure():
        return HttpResponseForbidden("Scheduler memerlukan HTTPS.")
    supplied = request.headers.get("X-Vobia-Scheduler-Secret", "")
    if not settings.SOCIAL_SYNC_SECRET or not secrets.compare_digest(supplied, settings.SOCIAL_SYNC_SECRET):
        return HttpResponseForbidden("Scheduler tidak diizinkan.")
    runs = sync_daily(source="scheduler")
    return JsonResponse({
        "runs": [
            {"platform": run.platform, "status": run.status, "cutoff": run.cutoff.isoformat()}
            for run in runs
        ]
    })
