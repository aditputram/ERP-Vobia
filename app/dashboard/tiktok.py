"""Superadmin-managed, read-only TikTok OAuth connection."""
import json
import os
import secrets
import tempfile
from datetime import timedelta
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
def api_request(url, *, data=None, token=""):
    body = urlencode(data).encode() if data is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
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
    return render(request, "dashboard/tiktok_connection.html", {"status": status, "configured": configured, "error": error})


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
