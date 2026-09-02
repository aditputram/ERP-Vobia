"""Superadmin-managed, read-only TikTok OAuth connection."""
import fcntl
import hashlib
import json
import os
import secrets
import tempfile
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET


SCOPES = "user.info.basic,user.info.profile,user.info.stats,video.list"
CACHE_SECONDS = 3600


class TikTokConnectionError(Exception):
    pass


def runtime_allowed(request):
    if settings.USE_SQLITE:
        return request.META.get("REMOTE_ADDR") in {"127.0.0.1", "::1"}
    return settings.TIKTOK_LIVE_ENABLED and request.is_secure()


def store_path():
    return Path(settings.TIKTOK_CONNECTION_DIR) / "tiktok.json"


def report_path(start, end, kind="report"):
    return store_path().parent / f"tiktok-{kind}-{start.isoformat()}-{end.isoformat()}.json"


def query_path(video_ids):
    digest = hashlib.sha256(",".join(video_ids).encode()).hexdigest()[:24]
    return store_path().parent / f"tiktok-videos-v2-{digest}.json"


def business_query_path(video_ids):
    digest = hashlib.sha256(",".join(video_ids).encode()).hexdigest()[:24]
    return store_path().parent / f"tiktok-business-videos-{digest}.json"


def write_cache(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise TikTokConnectionError("Lokasi penyimpanan cache TikTok tidak aman.")
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tiktok-cache-")
    try:
        with os.fdopen(descriptor, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump({
                "schema": 1,
                "cached_at": timezone.now().isoformat(),
                "value": value,
            }, handle, cls=DjangoJSONEncoder)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_cache(path):
    try:
        with path.open() as handle:
            cached = json.load(handle)
        return cached if cached.get("schema") == 1 else None
    except (OSError, ValueError, AttributeError):
        return None


def cache_fresh(cached):
    try:
        age = (timezone.now() - datetime.fromisoformat(cached["cached_at"])).total_seconds()
        return 0 <= age < CACHE_SECONDS
    except (TypeError, KeyError, ValueError):
        return False


def cached_value(cached):
    if not cached:
        return None
    value = cached.get("value")
    if isinstance(value, dict) and isinstance(value.get("fetched_at"), str):
        try:
            value["fetched_at"] = datetime.fromisoformat(value["fetched_at"])
        except ValueError:
            pass
    return value


def cached_fetch(path, fetcher, *, force=False, fetch=True):
    cached = load_cache(path)
    if not force and cache_fresh(cached):
        return cached_value(cached), ""
    if not fetch:
        return cached_value(cached), "" if cached else "Snapshot TikTok periode ini belum tersedia."
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        with (path.parent / (path.name.split("-202", 1)[0] + ".lock")).open("a+") as lock:
            os.fchmod(lock.fileno(), 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return cached_value(cached), "Sinkronisasi TikTok sedang berjalan. Snapshot terakhir tetap ditampilkan."
            value = fetcher()
            write_cache(path, value)
            return value, ""
    except TikTokConnectionError as exc:
        return cached_value(cached), str(exc)
    except Exception:
        return cached_value(cached), "Refresh TikTok belum berhasil. Snapshot terakhir tetap ditampilkan."


def redirect_uri(request):
    return request.build_absolute_uri("/marketing/tiktok/callback/")


def status_only():
    path = store_path()
    if not path.exists():
        return None
    with path.open() as handle:
        saved = json.load(handle)
    return {key: saved.get(key) for key in ("open_id", "display_name", "username", "scope", "verified_at", "expires_at")}


def save_connection(payload):
    path = store_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise TikTokConnectionError("Lokasi penyimpanan koneksi tidak aman.")
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tiktok-")
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
def api_request(url, *, data=None, json_data=None, token=""):
    body = json.dumps(json_data).encode() if json_data is not None else urlencode(data).encode() if data is not None else None
    headers = {"Accept": "application/json"}
    if json_data is not None:
        headers["Content-Type"] = "application/json"
    elif data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urlopen(Request(url, data=body, headers=headers), timeout=15) as response:
            payload = json.loads(response.read(1024 * 1024))
    except Exception:
        raise TikTokConnectionError("TikTok belum dapat dihubungi atau menolak permintaan.") from None
    if not isinstance(payload, dict):
        raise TikTokConnectionError("Respons TikTok tidak valid atau izin belum disetujui.")
    error = payload.get("error")
    if isinstance(error, dict) and error.get("code") not in {None, "ok"}:
        raise TikTokConnectionError("Respons TikTok tidak valid atau izin belum disetujui.")
    return payload


def load_connection():
    with store_path().open() as handle:
        return json.load(handle)


@sensitive_variables()
def access_token():
    saved = load_connection()
    try:
        expires_at = datetime.fromisoformat(saved["expires_at"])
    except (KeyError, TypeError, ValueError):
        expires_at = timezone.now()
    if expires_at > timezone.now() + timedelta(minutes=5):
        return saved["access_token"]
    refreshed = api_request("https://open.tiktokapis.com/v2/oauth/token/", data={
        "client_key": settings.TIKTOK_CLIENT_KEY,
        "client_secret": settings.TIKTOK_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": saved["refresh_token"],
    })
    if not refreshed.get("access_token") or not refreshed.get("refresh_token"):
        raise TikTokConnectionError("Token TikTok perlu dihubungkan ulang.")
    now = timezone.now()
    saved.update(refreshed)
    saved["verified_at"] = now.isoformat()
    saved["expires_at"] = (now + timedelta(seconds=int(refreshed.get("expires_in", 0)))).isoformat()
    save_connection(saved)
    return saved["access_token"]


@sensitive_variables()
def fetch_report(start, end):
    token = access_token()
    fields = "open_id,display_name,username,avatar_url,bio_description,profile_deep_link,is_verified,follower_count,following_count,likes_count,video_count"
    profile = api_request("https://open.tiktokapis.com/v2/user/info/?" + urlencode({"fields": fields}), token=token).get("data", {}).get("user", {})
    video_fields = "id,create_time,cover_image_url,share_url,video_description,duration,title,like_count,comment_count,share_count,view_count"
    videos, cursor = [], None
    for _ in range(20):
        body = {"max_count": 20}
        if cursor is not None:
            body["cursor"] = cursor
        data = api_request("https://open.tiktokapis.com/v2/video/list/?" + urlencode({"fields": video_fields}), json_data=body, token=token).get("data", {})
        page = data.get("videos", [])
        if not isinstance(page, list):
            raise TikTokConnectionError("Daftar video TikTok tidak dapat dibaca.")
        stop = False
        for video in page:
            try:
                published = datetime.fromtimestamp(int(video["create_time"]), tz=dt_timezone.utc)
            except (KeyError, TypeError, ValueError, OSError):
                continue
            if published.date() < start:
                stop = True
                continue
            if published.date() <= end:
                likes = max(0, int(video.get("like_count") or 0))
                comments = max(0, int(video.get("comment_count") or 0))
                shares = max(0, int(video.get("share_count") or 0))
                views = max(0, int(video.get("view_count") or 0))
                engagement = likes + comments + shares
                videos.append({**video, "published": published.isoformat(), "likes": likes, "comments": comments,
                               "shares": shares, "views": views, "engagement": engagement,
                               "er": engagement / views * 100 if views else None})
        if stop or not data.get("has_more"):
            break
        cursor = data.get("cursor")
        if cursor is None:
            break
    views = sum(item["views"] for item in videos)
    engagement = sum(item["engagement"] for item in videos)
    business = None
    business_error = ""
    try:
        from . import tiktok_business
        if tiktok_business.store_path().exists():
            business = tiktok_business.fetch_report(start, end)
            for video in videos:
                insight = business["videos"].get(str(video.get("id")))
                if insight:
                    video["business"] = insight
    except TikTokConnectionError as exc:
        business_error = str(exc)
        business = None
    account_views = business.get("views") if business else views
    account_engagement = business.get("engagement") if business else engagement
    return {"profile": profile, "videos": videos, "views": account_views, "engagement": account_engagement,
            "er": account_engagement / account_views * 100 if account_engagement is not None and account_views else None,
            "display_views": views, "display_engagement": engagement, "date_from": start, "date_to": end,
            "fetched_at": timezone.now(), "business": business, "business_error": business_error}


def business_only_report(start, end):
    from . import tiktok_business

    business = tiktok_business.fetch_report(start, end)
    videos = []
    for item in business["videos"].values():
        try:
            published = datetime.fromtimestamp(int(item["create_time"]), tz=dt_timezone.utc).isoformat()
        except (KeyError, TypeError, ValueError, OSError):
            published = ""
        likes = max(0, int(item.get("likes") or 0))
        comments = max(0, int(item.get("comments") or 0))
        shares = max(0, int(item.get("shares") or 0))
        views = max(0, int(item.get("video_views") or 0))
        engagement = likes + comments + shares
        videos.append({
            "id": str(item.get("item_id") or ""), "published": published,
            "share_url": item.get("share_url") or "", "video_description": item.get("caption") or "",
            "likes": likes, "comments": comments, "shares": shares, "views": views,
            "engagement": engagement, "er": engagement / views * 100 if views else None,
            "business": item,
        })
    return {
        "profile": {}, "videos": videos, "views": business.get("views"),
        "engagement": business.get("engagement"),
        "er": business["engagement"] / business["views"] * 100
        if business.get("engagement") is not None and business.get("views") else None,
        "display_views": None, "display_engagement": None, "date_from": start, "date_to": end,
        "fetched_at": timezone.now(), "business": business, "business_error": "",
        "display_error": "Login Kit sedang tidak tersedia; laporan akun tetap memakai Accounts API.",
    }


def fetch_available_report(start, end):
    from . import tiktok_business

    has_display = store_path().exists()
    has_business = tiktok_business.store_path().exists()
    if not has_display and not has_business:
        raise TikTokConnectionError("Hubungkan akun TikTok terlebih dahulu.")
    if has_display:
        try:
            return fetch_report(start, end)
        except TikTokConnectionError as exc:
            if not has_business:
                raise exc
        except Exception:
            if not has_business:
                raise TikTokConnectionError("Data TikTok belum dapat dibaca; detail rahasia tidak ditampilkan.") from None
    if has_business:
        try:
            return business_only_report(start, end)
        except TikTokConnectionError as exc:
            raise exc
        except Exception:
            raise TikTokConnectionError("Data TikTok belum dapat dibaca; detail rahasia tidak ditampilkan.") from None
    raise TikTokConnectionError("Data TikTok belum dapat dibaca; detail rahasia tidak ditampilkan.")


def get_report(start, end, force=False, fetch=True):
    return cached_fetch(
        report_path(start, end),
        lambda: fetch_available_report(start, end),
        force=force, fetch=fetch,
    )


def get_business_profile_report(start, end, force=False, fetch=True):
    from . import tiktok_business

    if not tiktok_business.store_path().exists():
        return None, "Hubungkan TikTok Business terlebih dahulu."
    return cached_fetch(
        report_path(start, end, kind="business-profile"),
        lambda: tiktok_business.fetch_profile_report(start, end),
        force=force, fetch=fetch,
    )


def video_id_from_url(url):
    match = re.search(r"/(?:video|photo)/(\d+)", url or "")
    return match.group(1) if match else ""


@sensitive_variables()
def fetch_videos(video_ids):
    """Read specific linked-account posts without relying on feed pagination."""
    token = access_token()
    fields = "id,create_time,cover_image_url,share_url,video_description,duration,title,like_count,comment_count,share_count,view_count"
    found = {}
    for offset in range(0, len(video_ids), 20):
        payload = api_request(
            "https://open.tiktokapis.com/v2/video/query/?" + urlencode({"fields": fields}),
            json_data={"filters": {"video_ids": video_ids[offset:offset + 20]}}, token=token,
        )
        videos = payload.get("data", {}).get("videos", [])
        if not isinstance(videos, list):
            raise TikTokConnectionError("Data video TikTok tidak dapat dibaca.")
        for video in videos:
            video_id = str(video.get("id") or "")
            if not video_id:
                continue
            likes = max(0, int(video.get("like_count") or 0))
            comments = max(0, int(video.get("comment_count") or 0))
            shares = max(0, int(video.get("share_count") or 0))
            views = max(0, int(video.get("view_count") or 0))
            engagement = likes + comments + shares
            found[video_id] = {**video, "likes": likes, "comments": comments, "shares": shares,
                               "views": views, "engagement": engagement,
                               "er": engagement / views * 100 if views else None}
    return found


def query_videos(video_ids, force=False):
    ids = sorted(dict.fromkeys(str(item) for item in video_ids if item))
    if not ids:
        return {}
    result, error = cached_fetch(query_path(ids), lambda: fetch_videos(ids), force=force)
    if result is not None:
        return result
    raise TikTokConnectionError(error or "Data video TikTok belum dapat dibaca.")


def query_business_videos(video_ids, force=False):
    from . import tiktok_business

    ids = sorted(dict.fromkeys(str(item) for item in video_ids if item))
    if not ids or not tiktok_business.store_path().exists():
        return {}, ""
    result, error = cached_fetch(
        business_query_path(ids),
        lambda: tiktok_business.fetch_video_insights(ids),
        force=force,
    )
    normalized = {
        video_id: {**item, "reach": tiktok_business.nonnegative_int(item.get("reach"))}
        for video_id, item in (result or {}).items()
    }
    return normalized, error


@never_cache
@login_required
@require_GET
def connection(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Koneksi TikTok hanya dapat dikelola Super Admin.")
    error = ""
    try:
        status = status_only()
    except Exception:
        status, error = None, "Status koneksi TikTok tidak dapat dibaca."
    configured = bool(settings.TIKTOK_CLIENT_KEY and settings.TIKTOK_CLIENT_SECRET)
    from . import tiktok_business
    try:
        business_status = tiktok_business.status_only()
    except Exception:
        business_status = None
    business_configured = bool(settings.TIKTOK_BUSINESS_APP_ID and settings.TIKTOK_BUSINESS_APP_SECRET)
    return render(request, "dashboard/tiktok_connection.html", {
        "status": status, "configured": configured, "error": error,
        "business_status": business_status, "business_configured": business_configured,
    })


@never_cache
@login_required
@require_GET
def oauth_start(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Koneksi TikTok hanya dapat dikelola Super Admin.")
    if not runtime_allowed(request) or not settings.TIKTOK_CLIENT_KEY or not settings.TIKTOK_CLIENT_SECRET:
        return HttpResponseForbidden("Konfigurasi aman TikTok belum aktif.")
    state = secrets.token_urlsafe(32)
    request.session["tiktok_oauth_state"] = state
    params = {
        "client_key": settings.TIKTOK_CLIENT_KEY,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": redirect_uri(request),
        "state": state,
    }
    return redirect("https://www.tiktok.com/v2/auth/authorize/?" + urlencode(params))


@never_cache
@login_required
@require_GET
@sensitive_variables()
def callback(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Koneksi TikTok hanya dapat dikelola Super Admin.")
    expected = request.session.pop("tiktok_oauth_state", "")
    returned = request.GET.get("state", "")
    code = request.GET.get("code", "")
    if not expected or not secrets.compare_digest(expected, returned) or not code:
        return render(request, "dashboard/tiktok_connection.html", {"status": status_only(), "configured": True, "error": "Otorisasi TikTok tidak valid atau dibatalkan."}, status=400)
    try:
        token = api_request("https://open.tiktokapis.com/v2/oauth/token/", data={
            "client_key": settings.TIKTOK_CLIENT_KEY,
            "client_secret": settings.TIKTOK_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri(request),
        })
        access_token = token.get("access_token", "")
        profile = api_request(
            "https://open.tiktokapis.com/v2/user/info/?fields=open_id,display_name,username",
            token=access_token,
        ).get("data", {}).get("user", {})
        if not access_token or not token.get("refresh_token") or not profile.get("open_id"):
            raise TikTokConnectionError("Token atau identitas akun TikTok tidak lengkap.")
        now = timezone.now()
        save_connection({
            **token,
            "display_name": profile.get("display_name", ""),
            "username": profile.get("username", ""),
            "verified_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=int(token.get("expires_in", 0)))).isoformat(),
        })
    except TikTokConnectionError as exc:
        return render(request, "dashboard/tiktok_connection.html", {"status": status_only(), "configured": True, "error": str(exc)}, status=400)
    return redirect("dashboard:tiktok_connection")
