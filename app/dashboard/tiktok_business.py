"""Read-only TikTok Accounts API OAuth connection."""
import json
import logging
import os
import secrets
import tempfile
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
            "comment.list.manage",
            "video.publish",
            "video.upload",
            "biz.spark.auth",
            "discovery.search.words",
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
        permissions = api_request(
            "https://business-api.tiktok.com/open_api/v1.3/tt_user/token_info/get/",
            token=token["access_token"],
        )
        save_connection({**token, "scope": permissions.get("scope", token.get("scope", "")), "verified_at": timezone.now().isoformat()})
    except TikTokConnectionError as exc:
        logger.warning("TikTok Business OAuth failed: %s", exc)
        return HttpResponseBadRequest(str(exc))
    return redirect("dashboard:tiktok_connection")
