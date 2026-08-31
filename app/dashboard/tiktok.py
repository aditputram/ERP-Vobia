"""Superadmin-managed, read-only TikTok OAuth connection."""
import json
import os
import secrets
import tempfile
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET


SCOPES = "user.info.basic,user.info.profile,user.info.stats,video.list"


class TikTokConnectionError(Exception):
    pass


def runtime_allowed(request):
    if settings.USE_SQLITE:
        return request.META.get("REMOTE_ADDR") in {"127.0.0.1", "::1"}
    return settings.TIKTOK_LIVE_ENABLED and request.is_secure()


def store_path():
    return Path(settings.TIKTOK_CONNECTION_DIR) / "tiktok.json"


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
    return {"profile": profile, "videos": videos, "views": views, "engagement": engagement,
            "er": engagement / views * 100 if views else None, "date_from": start, "date_to": end,
            "fetched_at": timezone.now()}


def get_report(start, end):
    if not store_path().exists():
        return None, "Hubungkan akun TikTok terlebih dahulu."
    try:
        return fetch_report(start, end), ""
    except TikTokConnectionError as exc:
        return None, str(exc)
    except Exception:
        return None, "Data TikTok belum dapat dibaca; detail rahasia tidak ditampilkan."


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
