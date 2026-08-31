"""Daily social metrics sync. External calls happen only from explicit jobs."""
import json
import secrets
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
from .models import SocialDailyMetric, SocialSyncRun
from .tiktok import TikTokConnectionError


PLATFORMS = (SocialDailyMetric.Platform.INSTAGRAM, SocialDailyMetric.Platform.TIKTOK)
ACCOUNT = "vobia.id"
MANUAL_LOCK_ACCOUNT = "vobia.id:manual-refresh"
METRICS = (
    "reach", "impressions", "total_engagement", "accounts_engaged",
    "profile_visits", "website_clicks", "likes", "comments", "shares",
    "new_followers", "lost_followers",
)


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
    latest_valid = SocialSyncRun.objects.filter(
        platform=platform, account=ACCOUNT, status=SocialSyncRun.Status.COMPLETED,
    ).first()
    latest = SocialSyncRun.objects.filter(platform=platform, account=ACCOUNT).first()
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
    completed = all(run.status == SocialSyncRun.Status.COMPLETED for run in runs)
    coordinator.status = SocialSyncRun.Status.COMPLETED if completed else SocialSyncRun.Status.FAILED
    coordinator.completed_at = timezone.now()
    coordinator.snapshot_at = coordinator.completed_at if completed else None
    coordinator.error = "" if completed else "Satu atau lebih platform gagal; refresh dapat dicoba lagi."
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
