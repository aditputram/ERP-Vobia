"""Local-UAT Instagram connection; no business-data writes or scheduled sync."""
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_http_methods


ACCOUNT_ID = "17841401620688121"
USERNAME = "vobia.id"


class ConnectionError(Exception):
    pass


def access_allowed(request):
    if not request.user.is_superuser:
        return False
    if settings.USE_SQLITE:
        return request.META.get("REMOTE_ADDR") in {"127.0.0.1", "::1"}
    return settings.INSTAGRAM_LIVE_ENABLED and request.is_secure()


class TokenForm(forms.Form):
    access_token = forms.CharField(
        label="Access Token Instagram", max_length=4096, strip=True,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "spellcheck": "false"}),
    )
    consent = forms.BooleanField(label="Izinkan ERP mengirim token ke API resmi Meta untuk menguji akun dan membaca Insights.")

    def clean_access_token(self):
        value = self.cleaned_data["access_token"]
        if not re.fullmatch(r"[A-Za-z0-9._~-]{20,4096}", value):
            raise forms.ValidationError("Format token tidak valid. Paste token saja, tanpa kutip atau spasi.")
        return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward an Authorization header to another host.


@sensitive_variables()
def api_get(token, path, params):
    url = "https://graph.instagram.com/v25.0/" + path + "?" + urlencode(params)
    request = Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    try:
        with build_opener(NoRedirect).open(request, timeout=15) as response:
            result = json.loads(response.read(1024 * 1024))
    except HTTPError as exc:
        if exc.code == 429:
            raise ConnectionError("Batas permintaan Meta tercapai. Coba lagi nanti.") from None
        raise ConnectionError("Meta menolak permintaan. Periksa masa berlaku token dan izin akun.") from None
    except Exception:
        raise ConnectionError("API Meta belum dapat dihubungi atau responsnya tidak valid. Coba lagi nanti.") from None
    if not isinstance(result, dict) or "error" in result:
        raise ConnectionError("Respons API Meta tidak valid.")
    return result


@sensitive_variables()
def verify(token):
    profile = api_get(token, "me", {"fields": "user_id,username"})
    if str(profile.get("user_id", "")) != ACCOUNT_ID or profile.get("username", "").lower() != USERNAME:
        raise ConnectionError("Token bukan milik akun @vobia.id yang disetujui. Tidak disimpan.")
    # Probe only; do not equate these values with the agreed total social report.
    insights_ok = False
    try:
        insights = api_get(token, ACCOUNT_ID + "/insights", {"metric": "reach", "period": "day"})
        insights_ok = isinstance(insights.get("data"), list)
    except ConnectionError:
        pass
    return {"username": USERNAME, "account_id": ACCOUNT_ID,
            "verified_at": timezone.now().isoformat(), "insights_ok": insights_ok}


def store_path():
    return Path(getattr(settings, "INSTAGRAM_CONNECTION_DIR", settings.PROJECT_ROOT / "data" / "private_integrations")) / "instagram.json"


@sensitive_variables()
def save_connection(token, status):
    path = store_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ConnectionError("Lokasi penyimpanan koneksi tidak aman.")
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".instagram-")
    try:
        with os.fdopen(descriptor, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump({**status, "access_token": token}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@sensitive_variables()
def status_only():
    path = store_path()
    if not path.exists():
        return None
    with path.open() as handle:
        saved = json.load(handle)
    return {key: saved.get(key) for key in ("username", "account_id", "verified_at", "insights_ok")}


@sensitive_post_parameters()
@never_cache
@login_required
@require_http_methods(["GET", "POST"])
@sensitive_variables()
def connection(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("Koneksi akun hanya dapat dikelola Super Admin.")
    if not access_allowed(request):
        return HttpResponseForbidden("Koneksi Instagram memerlukan HTTPS dan penyimpanan rahasia yang aktif.")
    request.session["active_module"] = "marketing"
    error = ""
    if request.method == "POST":
        # Remove secrets from request before any downstream rendering/debug handler.
        data = request.POST.copy()
        request.POST = request.POST.copy()
        request.POST.pop("access_token", None)
        request._body = b""
        try:
            form = TokenForm(data)
            if not form.is_valid():
                error = "Token atau persetujuan belum valid. Paste ulang token dan centang persetujuan."
            else:
                token = form.cleaned_data["access_token"]
                result = verify(token)
                save_connection(token, result)
                return redirect("dashboard:instagram_connection")
        except ConnectionError as exc:
            error = str(exc)
        except Exception:
            error = "Koneksi belum dapat disimpan. Periksa konfigurasi penyimpanan; token tidak ditampilkan."
    try:
        status = status_only()
    except Exception:
        status = None
        error = error or "Status koneksi tersimpan tidak dapat dibaca. Hubungi pengelola sistem."
    return render(request, "dashboard/instagram_connection.html", {"form": TokenForm(), "status": status, "error": error})
