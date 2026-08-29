"""Read-only Instagram reporting with private, bounded local-UAT snapshots."""
import fcntl
import json
import math
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import urlparse

from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_http_methods

from .instagram import ACCOUNT_ID, USERNAME, ConnectionError, api_get, store_path


ACCOUNT_METRICS = {
    "reach": "Reach", "views": "Impression", "total_interactions": "Total Engagement",
    "accounts_engaged": "Accounts Engaged", "profile_views": "Profile Visit",
    "website_clicks": "Click Website", "likes": "Likes", "comments": "Comments",
    "shares": "Shares", "saves": "Saves", "replies": "Replies", "reposts": "Reposts",
    "profile_links_taps": "Profile Links Taps",
}
MEDIA_METRICS = ("views", "reach", "total_interactions", "likes", "comments", "shares", "saved")
DEMOGRAPHICS = {
    "follower_demographics": "Followers",
    "engaged_audience_demographics": "Akun berinteraksi",
    "reached_audience_demographics": "Akun dijangkau",
}
DIMENSIONS = {"age": "Usia", "gender": "Gender", "country": "Negara", "city": "Kota"}
CACHE_SECONDS = 3600


class PeriodForm(forms.Form):
    period = forms.ChoiceField(label="Periode", choices=[(str(days), f"{days} days") for days in (7, 14, 30, 60, 90)] + [("custom", "Custom")])
    date_from = forms.DateField(required=False, label="Dari tanggal", widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(required=False, label="Sampai tanggal", widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, data=None, *args, **kwargs):
        if data is not None:
            data = data.copy()
            data.setdefault("period", "custom" if "date_from" in data or "date_to" in data else "7")
            if data.get("period") != "custom":
                data.pop("date_from", None)
                data.pop("date_to", None)
        super().__init__(data, *args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        today = timezone.now().date()
        preset = cleaned.get("period")
        if preset and preset != "custom":
            cleaned["date_from"] = today - timedelta(days=int(preset))
            cleaned["date_to"] = today - timedelta(days=1)
        start, end = cleaned.get("date_from"), cleaned.get("date_to")
        if preset == "custom":
            for key in ("date_from", "date_to"):
                if not cleaned.get(key) and key not in self.errors:
                    self.add_error(key, "Isi tanggal untuk periode Custom.")
        if start and end:
            if start > end:
                raise forms.ValidationError("Tanggal awal harus sebelum atau sama dengan tanggal akhir.")
            if (end - start).days >= 90:
                raise forms.ValidationError("Pilih maksimal 90 hari per laporan.")
            if end >= today or start < today - timedelta(days=90):
                raise forms.ValidationError("Pilih hari lengkap sebelum hari ini, dalam 90 hari terakhir.")
        return cleaned


def numeric(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return value
    return None


def metric_values(payload, names):
    result = {name: None for name in names}
    for item in payload.get("data", []):
        if not isinstance(item, dict) or item.get("name") not in result:
            continue
        total = item.get("total_value", {})
        value = total.get("value") if isinstance(total, dict) else None
        values = item.get("values", [])
        # Only lifetime media objects use values here; never sum unique account series.
        if value is None and item.get("period") == "lifetime" and len(values) == 1:
            value = values[0].get("value")
        result[item["name"]] = numeric(value)
    return result


def rate(engagement, views):
    return engagement / views * 100 if engagement is not None and views not in (None, 0) else None


def breakdown_rows(payload):
    rows = []
    for item in payload.get("data", []):
        for group in item.get("total_value", {}).get("breakdowns", []):
            for row in group.get("results", []):
                value = numeric(row.get("value"))
                if value is not None:
                    label = " / ".join(str(part) for part in row.get("dimension_values", []))
                    rows.append({"label": label, "value": value})
    total = sum(row["value"] for row in rows)
    for row in rows:
        row["percent"] = row["value"] / total * 100 if total else 0
    return sorted(rows, key=lambda row: row["value"], reverse=True)


def safe_permalink(value):
    parsed = urlparse(value or "")
    return value if parsed.scheme == "https" and parsed.hostname in {"www.instagram.com", "instagram.com"} else ""


@sensitive_variables()
def fetch_report(start, end):
    with store_path().open() as handle:
        token = json.load(handle)["access_token"]
    profile = api_get(token, "me", {"fields": "user_id,username,followers_count,media_count"})
    if str(profile.get("user_id")) != ACCOUNT_ID or profile.get("username", "").lower() != USERNAME:
        raise ConnectionError("Akun token tidak cocok dengan @vobia.id. Snapshot tidak diganti.")
    warnings = []

    def optional(path, params):
        try:
            return api_get(token, path, params)
        except ConnectionError:
            return {}

    period = {"period": "day", "metric_type": "total_value", "since": start.isoformat(), "until": (end + timedelta(days=1)).isoformat()}
    account = ACCOUNT_ID + "/insights"
    totals = metric_values(optional(account, {**period, "metric": ",".join(ACCOUNT_METRICS)}), ACCOUNT_METRICS)
    # A retired/unsupported metric must not take down every otherwise-valid KPI.
    missing = [name for name, value in totals.items() if value is None]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = pool.map(lambda name: (name, metric_values(optional(account, {**period, "metric": name}), [name])[name]), missing)
        totals.update(dict(results))
    if all(totals[key] is None for key in ("reach", "views", "total_interactions")):
        raise ConnectionError("Insights periode ini tidak tersedia. Periksa token, izin, atau batas API; snapshot lama dipertahankan.")
    missing = [ACCOUNT_METRICS[name] for name, value in totals.items() if value is None]
    if missing:
        warnings.append("Metrik belum tersedia (bukan nol): " + ", ".join(missing))
    follow_rows = breakdown_rows(optional(account, {**period, "metric": "follows_and_unfollows", "breakdown": "follow_type"}))
    changes = {row["label"]: row["value"] for row in follow_rows}
    follows, unfollows = changes.get("FOLLOWER"), changes.get("NON_FOLLOWER")

    def demographic(pair):
        metric, dimension = pair
        payload = optional(account, {"metric": metric, "period": "lifetime", "metric_type": "total_value", "timeframe": "last_30_days", "breakdown": dimension})
        return {"audience": DEMOGRAPHICS[metric], "dimension": DIMENSIONS[dimension], "rows": breakdown_rows(payload)}
    pairs = [(metric, dimension) for metric in DEMOGRAPHICS for dimension in DIMENSIONS]
    with ThreadPoolExecutor(max_workers=4) as pool:
        demographics = list(pool.map(demographic, pairs))
    if any(not item["rows"] for item in demographics):
        warnings.append("Sebagian demografi kosong/tidak tersedia dari Meta; bisa terkait ambang privasi atau izin API.")

    # Fetch the whole paginated library before filtering publication dates. Never trust a next URL with credentials.
    media, seen_ids, seen_cursors = [], set(), set()
    cursor = None
    library_complete = False
    for _ in range(20):
        params = {"fields": "id,caption,media_type,media_product_type,timestamp,permalink", "limit": 100}
        if cursor:
            params["after"] = cursor
        payload = optional(ACCOUNT_ID + "/media", params)
        if not isinstance(payload.get("data"), list):
            warnings.append("Daftar konten gagal diambil atau hanya sebagian; jangan dianggap lengkap.")
            break
        for item in payload["data"]:
            media_id = str(item.get("id", ""))
            if not media_id.isdigit() or media_id in seen_ids:
                continue
            seen_ids.add(media_id)
            try:
                published = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")).astimezone(dt_timezone.utc)
            except (KeyError, ValueError, TypeError):
                continue
            if start <= published.date() <= end:
                media.append({"id": media_id, "caption": str(item.get("caption", ""))[:240], "kind": item.get("media_product_type") or item.get("media_type"), "published": published.isoformat(), "permalink": safe_permalink(item.get("permalink"))})
        paging = payload.get("paging", {})
        if not paging.get("next"):
            library_complete = True
            break
        cursor = paging.get("cursors", {}).get("after")
        if not cursor or cursor in seen_cursors:
            warnings.append("Pagination konten tidak lengkap; hanya data yang berhasil diambil ditampilkan.")
            break
        seen_cursors.add(cursor)
    if not library_complete:
        warnings.append("Cakupan konten belum lengkap (batas keamanan maksimal 20 halaman API per refresh).")

    def content_metrics(item):
        path = item["id"] + "/insights"
        values = metric_values(optional(path, {"metric": ",".join(MEDIA_METRICS)}), MEDIA_METRICS)
        for name, value in list(values.items()):
            if value is None:
                values[name] = metric_values(optional(path, {"metric": name}), [name])[name]
        extras = ("ig_reels_avg_watch_time", "ig_reels_video_view_total_time") if item["kind"] == "REELS" else ("profile_visits", "follows")
        for name in extras:
            values.update(metric_values(optional(path, {"metric": name}), [name]))
        values["er"] = rate(values["total_interactions"], values["views"])
        for name in ("ig_reels_avg_watch_time", "ig_reels_video_view_total_time"):
            raw = values.get(name)
            values[name + "_seconds"] = raw / 1000 if raw is not None else None
        comments, cursor, available, complete = [], None, False, False
        for _ in range(20):
            params = {"fields": "id,from,text,timestamp,like_count", "limit": 100}
            if cursor:
                params["after"] = cursor
            payload = optional(item["id"] + "/comments", params)
            if not isinstance(payload.get("data"), list):
                break
            available = True
            for comment in payload["data"]:
                author = comment.get("from") if isinstance(comment.get("from"), dict) else {}
                comments.append({
                    "id": str(comment.get("id", ""))[:80],
                    "username": str(author.get("username", "Instagram user"))[:80],
                    "text": str(comment.get("text", ""))[:2000],
                    "timestamp": str(comment.get("timestamp", ""))[:40],
                    "like_count": numeric(comment.get("like_count")),
                })
            paging = payload.get("paging", {})
            if not paging.get("next"):
                complete = True
                break
            cursor = paging.get("cursors", {}).get("after")
            if not cursor:
                break
        return {**item, "metrics": values, "partial": any(values.get(name) is None for name in MEDIA_METRICS),
                "comments": comments, "comments_available": available, "comments_complete": complete}
    with ThreadPoolExecutor(max_workers=4) as pool:
        contents = list(pool.map(content_metrics, media))
    contents.sort(key=lambda item: item["published"], reverse=True)
    return {"schema": 1, "date_from": start.isoformat(), "date_to": end.isoformat(), "fetched_at": timezone.now().isoformat(),
            "profile": {"username": USERNAME, "followers": numeric(profile.get("followers_count")), "media_count": numeric(profile.get("media_count"))},
            "totals": totals, "er": rate(totals["total_interactions"], totals["views"]),
            "follows": follows, "unfollows": unfollows, "net_follows": follows - unfollows if follows is not None and unfollows is not None else None,
            "demographics": demographics, "contents": contents, "warnings": warnings, "library_complete": library_complete}


def report_path(start, end):
    return store_path().parent / ("report-" + start.isoformat() + "-" + end.isoformat() + ".json")


def write_snapshot(path, snapshot):
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".report-")
    try:
        with os.fdopen(descriptor, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(snapshot, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_snapshot(path):
    try:
        with path.open() as handle:
            snapshot = json.load(handle)
        return snapshot if snapshot.get("schema") == 1 else None
    except (OSError, ValueError, AttributeError):
        return None


def fresh(snapshot):
    try:
        age = (timezone.now() - datetime.fromisoformat(snapshot["fetched_at"])).total_seconds()
        return 0 <= age < CACHE_SECONDS
    except (TypeError, KeyError, ValueError):
        return False


@sensitive_variables()
def get_report(start, end, force=False):
    path = report_path(start, end)
    cached = load_snapshot(path)
    if not force and fresh(cached):
        return cached, ""
    if not store_path().exists():
        return cached, "Hubungkan akun Instagram terlebih dahulu."
    try:
        # ponytail: one account-wide lock; split by account if more accounts are added.
        with (path.parent / "report.lock").open("a+") as lock:
            os.fchmod(lock.fileno(), 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return cached, "Sinkronisasi sedang berjalan. Buka ulang halaman sebentar lagi."
            # A short refresh cooldown also protects Meta against double submits.
            attempted = os.fstat(lock.fileno()).st_mtime
            lock.seek(0)
            if lock.read() == path.name and timezone.now().timestamp() - attempted < 30:
                return cached, "Tunggu 30 detik antar-refresh agar tidak membebani API Meta."
            lock.seek(0)
            lock.truncate()
            lock.write(path.name)
            lock.flush()
            snapshot = fetch_report(start, end)
            write_snapshot(path, snapshot)
            return snapshot, ""
    except ConnectionError as exc:
        return cached, str(exc)
    except Exception:
        return cached, "Refresh belum berhasil. Snapshot lama dipertahankan; detail rahasia tidak ditampilkan."


@never_cache
@login_required
@require_http_methods(["GET", "POST"])
def dashboard(request):
    if not request.user.is_superuser or not settings.USE_SQLITE or request.META.get("REMOTE_ADDR") not in {"127.0.0.1", "::1"}:
        return HttpResponseForbidden("Dashboard Instagram hanya tersedia untuk Super Admin pada local UAT.")
    request.session["active_module"] = "marketing"
    data = request.POST if request.method == "POST" else request.GET
    form = PeriodForm(data or {"period": "7"})
    report, error = None, ""
    if form.is_valid():
        start, end = form.cleaned_data["date_from"], form.cleaned_data["date_to"]
        report, error = get_report(start, end, force=request.method == "POST")
    main_keys = ("reach", "views", "total_interactions", "accounts_engaged", "profile_views", "website_clicks")
    cards = [{"name": ACCOUNT_METRICS[key], "api": key, "value": report["totals"].get(key)} for key in main_keys] if report else []
    details = [{"name": label, "value": report["totals"].get(key)} for key, label in ACCOUNT_METRICS.items() if key not in main_keys] if report else []
    if report:
        report = dict(report)
        report["fetched_display"] = datetime.fromisoformat(report["fetched_at"])
    return render(request, "dashboard/instagram_report.html", {"form": form, "report": report, "error": error, "cards": cards, "details": details})
