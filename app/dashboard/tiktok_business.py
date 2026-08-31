"""Read-only TikTok Accounts API OAuth connection."""
import json
import logging
import os
import secrets
import tempfile
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.http import HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET

from .tiktok import TikTokConnectionError, runtime_allowed


logger = logging.getLogger(__name__)
STATE_SALT = "dashboard.tiktok_business.oauth"
PROFILE_FIELDS = (
    "audience_activity", "audience_ages", "audience_cities", "audience_countries",
    "audience_genders", "bio_link_clicks", "comments", "daily_lost_followers",
    "daily_new_followers", "daily_total_followers", "display_name", "engaged_audience",
    "followers_count", "likes", "profile_views", "shares", "total_likes",
    "unique_video_views", "username", "video_views", "videos_count",
)
DAILY_SUM_FIELDS = (
    "unique_video_views", "video_views", "likes", "comments", "shares",
    "engaged_audience", "profile_views", "bio_link_clicks", "daily_new_followers",
    "daily_lost_followers",
)


def store_path():
    return Path(settings.TIKTOK_CONNECTION_DIR) / "tiktok_business.json"


def redirect_uri(request):
    return request.build_absolute_uri("/marketing/tiktok/business/callback/")


def status_only():
    if not store_path().exists():
        return None
    with store_path().open() as handle:
        saved = json.load(handle)
    return {key: saved.get(key) for key in ("open_id", "scope", "verified_at", "expires_in")}


def load_connection():
    with store_path().open() as handle:
        return json.load(handle)


def save_connection(payload):
    path = store_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise TikTokConnectionError("Lokasi penyimpanan koneksi tidak aman.")
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tiktok-business-")
    try:
        with os.fdopen(descriptor, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@sensitive_variables()
def api_request(url, *, json_data=None, token=""):
    body = json.dumps(json_data).encode() if json_data is not None else None
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Access-Token"] = token
    try:
        with urlopen(Request(url, data=body, headers=headers), timeout=15) as response:
            payload = json.loads(response.read(1024 * 1024))
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read(1024 * 1024))
        except (json.JSONDecodeError, OSError, TypeError):
            raise TikTokConnectionError(f"TikTok Business API gagal merespons (HTTP {exc.code}).") from None
    except Exception as exc:
        logger.warning("TikTok Business transport failed: %s: %s", type(exc).__name__, exc)
        raise TikTokConnectionError("TikTok Business API belum dapat dihubungi.") from None
    if not isinstance(payload, dict):
        raise TikTokConnectionError("TikTok Business API mengembalikan respons yang tidak valid.")
    if payload.get("code") not in {0, "0"}:
        code = str(payload.get("code", "unknown"))[:32]
        message = str(payload.get("message") or "izin belum lengkap")[:240]
        raise TikTokConnectionError(f"TikTok Business API menolak permintaan: {message} (code {code}).")
    return payload.get("data") or {}


@sensitive_variables()
def access_token():
    saved = load_connection()
    try:
        verified_at = datetime.fromisoformat(saved["verified_at"])
        expires_at = verified_at + timedelta(seconds=int(saved.get("expires_in") or 0))
    except (KeyError, TypeError, ValueError):
        expires_at = timezone.now()
    if expires_at > timezone.now() + timedelta(minutes=5):
        return saved["access_token"]
    refreshed = api_request(
        "https://business-api.tiktok.com/open_api/v1.3/tt_user/oauth2/refresh_token/",
        json_data={
            "client_id": settings.TIKTOK_BUSINESS_APP_ID,
            "client_secret": settings.TIKTOK_BUSINESS_APP_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": saved["refresh_token"],
        },
    )
    if not refreshed.get("access_token") or not refreshed.get("refresh_token"):
        raise TikTokConnectionError("Token TikTok Business perlu dihubungkan ulang.")
    saved.update(refreshed)
    saved["verified_at"] = timezone.now().isoformat()
    save_connection(saved)
    return saved["access_token"]


def nonnegative_int(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def percentage_rows(profile, key):
    rows = []
    for source in profile.get(key, []):
        if not isinstance(source, dict):
            continue
        try:
            percentage = float(source.get("percentage"))
        except (TypeError, ValueError):
            continue
        rows.append({**source, "percentage_display": percentage * 100})
    return rows


def profile_date_ranges(start, end):
    """TikTok accepts at most 60 inclusive calendar days per profile request."""
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=59))
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


@sensitive_variables()
def fetch_profile_report(start, end, *, saved=None, token=None):
    saved = saved or load_connection()
    token = token or access_token()
    profile, daily_by_date = {}, {}
    for chunk_start, chunk_end in profile_date_ranges(start, end):
        chunk = api_request(
            "https://business-api.tiktok.com/open_api/v1.3/business/get/?" + urlencode({
                "business_id": saved["open_id"],
                "start_date": chunk_start.isoformat(),
                "end_date": chunk_end.isoformat(),
                "fields": json.dumps(PROFILE_FIELDS),
            }),
            token=token,
        )
        profile.update({key: value for key, value in chunk.items() if key != "metrics"})
        for item in chunk.get("metrics", []):
            try:
                day = datetime.fromisoformat(str(item["date"]).replace("Z", "+00:00")).date()
            except (KeyError, TypeError, ValueError):
                continue
            if start <= day <= end:
                daily_by_date[day] = item
    daily = list(daily_by_date.values())
    daily.sort(key=lambda item: item.get("date", ""))
    totals = {}
    for field in DAILY_SUM_FIELDS:
        values = [nonnegative_int(item.get(field)) for item in daily]
        available = [value for value in values if value is not None]
        totals[field] = sum(available) if available else None
    profile = dict(profile)
    for key in ("audience_ages", "audience_cities", "audience_countries", "audience_genders"):
        profile[key] = percentage_rows(profile, key)
    return {
        "profile": profile,
        "daily": daily,
        "totals": totals,
        "reach": totals["unique_video_views"],
        "views": totals["video_views"],
        "likes": totals["likes"],
        "comments": totals["comments"],
        "shares": totals["shares"],
        "engagement": sum(totals[key] for key in ("likes", "comments", "shares"))
        if all(totals[key] is not None for key in ("likes", "comments", "shares")) else None,
        "accounts_engaged": totals["engaged_audience"],
        "profile_views": totals["profile_views"],
        "website_clicks": totals["bio_link_clicks"],
        "new_followers": totals["daily_new_followers"],
        "lost_followers": totals["daily_lost_followers"],
        "follower_growth": (
            totals["daily_new_followers"] - totals["daily_lost_followers"]
            if totals["daily_new_followers"] is not None and totals["daily_lost_followers"] is not None
            else None
        ),
    }


@sensitive_variables()
def fetch_video_report(start, end, *, saved=None, token=None):
    saved = saved or load_connection()
    token = token or access_token()
    video_fields = [
        "item_id", "thumbnail_url", "share_url", "embed_url", "caption", "likes",
        "comments", "shares", "favorites", "video_views", "create_time",
        "total_time_watched", "average_time_watched", "reach",
    ]
    videos, cursor = {}, 0
    for _ in range(20):
        page = api_request(
            "https://business-api.tiktok.com/open_api/v1.3/business/video/list/?" + urlencode({
                "business_id": saved["open_id"],
                "cursor": cursor,
                "fields": json.dumps(video_fields),
            }),
            token=token,
        )
        stop = False
        for item in page.get("videos", []):
            try:
                published = datetime.fromtimestamp(int(item["create_time"]), tz=dt_timezone.utc).date()
            except (KeyError, TypeError, ValueError, OSError):
                continue
            if published < start:
                stop = True
            elif published <= end and item.get("item_id"):
                videos[str(item["item_id"])] = item
        if stop or not page.get("has_more"):
            break
        next_cursor = page.get("cursor")
        if next_cursor in (None, cursor):
            break
        cursor = next_cursor
    return videos


@sensitive_variables()
def fetch_report(start, end):
    saved = load_connection()
    token = access_token()
    report = fetch_profile_report(start, end, saved=saved, token=token)
    report["videos"] = fetch_video_report(start, end, saved=saved, token=token)
    return report


@login_required
@require_GET
def oauth_start(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Koneksi TikTok hanya dapat dikelola Super Admin.")
    if not runtime_allowed(request) or not settings.TIKTOK_BUSINESS_APP_ID or not settings.TIKTOK_BUSINESS_APP_SECRET:
        return HttpResponseForbidden("Konfigurasi aman TikTok Business belum aktif.")
    state = signing.dumps(
        {"user_id": str(request.user.pk), "nonce": secrets.token_urlsafe(16)},
        salt=STATE_SALT,
        compress=True,
    )
    request.session["tiktok_business_oauth_state"] = state
    return redirect("https://www.tiktok.com/v2/auth/authorize?" + urlencode({
        "client_key": settings.TIKTOK_BUSINESS_APP_ID,
        "scope": ",".join((
            "user.info.basic",
            "user.info.username",
            "user.info.stats",
            "user.info.profile",
            "user.account.type",
            "user.insights",
            "video.list",
            "video.insights",
            "comment.list",
        )),
        "response_type": "code",
        "state": state,
        "redirect_uri": redirect_uri(request),
    }))


@login_required
@require_GET
@sensitive_variables()
def callback(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Koneksi TikTok hanya dapat dikelola Super Admin.")
    expected = request.session.pop("tiktok_business_oauth_state", "")
    returned = request.GET.get("state", "")
    auth_code = request.GET.get("code", "") or request.GET.get("auth_code", "")
    try:
        signed_state = signing.loads(returned, salt=STATE_SALT, max_age=600)
    except signing.BadSignature:
        signed_state = {}
    state_valid = (
        bool(expected and secrets.compare_digest(expected, returned))
        or signed_state.get("user_id") == str(request.user.pk)
    )
    if not state_valid or not auth_code:
        return HttpResponseBadRequest("Otorisasi TikTok Business tidak valid atau dibatalkan.")
    try:
        token = api_request("https://business-api.tiktok.com/open_api/v1.3/tt_user/oauth2/token/", json_data={
            "client_id": settings.TIKTOK_BUSINESS_APP_ID,
            "client_secret": settings.TIKTOK_BUSINESS_APP_SECRET,
            "grant_type": "authorization_code",
            "auth_code": auth_code,
            "redirect_uri": redirect_uri(request),
        })
        if not token.get("access_token") or not token.get("refresh_token") or not token.get("open_id"):
            raise TikTokConnectionError("Token TikTok Business tidak lengkap.")
        save_connection({**token, "scope": token.get("scope", ""), "verified_at": timezone.now().isoformat()})
    except TikTokConnectionError as exc:
        logger.warning("TikTok Business OAuth failed: %s", exc)
        return HttpResponseBadRequest(str(exc))
    return redirect("dashboard:tiktok_connection")
