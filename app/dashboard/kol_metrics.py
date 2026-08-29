import json
import re
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


FIELDS = ("views", "likes", "comments", "saves", "shares")
KEYS = {
    "views": ("playCount", "video_view_count", "videoViewCount"),
    "likes": ("diggCount", "like_count", "likeCount"),
    "comments": ("commentCount", "comment_count"),
    "saves": ("collectCount", "save_count"),
    "shares": ("shareCount", "share_count"),
}


def _hosts(platform):
    return {"instagram.com", "www.instagram.com"} if platform == "INSTAGRAM" else {"tiktok.com", "www.tiktok.com", "vm.tiktok.com"}


class SafeRedirect(HTTPRedirectHandler):
    def __init__(self, allowed):
        self.allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme != "https" or (urlparse(newurl).hostname or "").lower() not in self.allowed:
            raise ValueError("Redirect link tidak aman.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_public_metrics(url, platform):
    allowed = _hosts(platform)
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in allowed:
        raise ValueError("Link tidak sesuai platform.")
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36"})
    with build_opener(SafeRedirect(allowed)).open(request, timeout=10) as response:
        html = response.read(4_000_001)
    if len(html) > 4_000_000:
        raise ValueError("Halaman terlalu besar.")
    text = html.decode("utf-8", "replace")
    values = {}
    for field, keys in KEYS.items():
        value = None
        for key in keys:
            match = re.search(rf'["\\]{re.escape(key)}["\\]\s*:\s*["\\]?(\d+)', text)
            if match:
                value = int(match.group(1))
                break
        if value is not None:
            values[field] = value
    if platform == "INSTAGRAM":
        description = re.search(r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\']([^"\']+)', text, re.I)
        if description:
            for field, label in (("likes", "likes"), ("comments", "comments")):
                match = re.search(rf'([\d,.]+[KMB]?)\s+{label}', description.group(1), re.I)
                if match and field not in values:
                    raw = match.group(1).replace(",", "")
                    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(raw[-1:].upper(), 1)
                    values[field] = round(float(raw[:-1] if multiplier > 1 else raw) * multiplier)
    if not values:
        raise ValueError("Metrik publik belum dapat dibaca; isi manual.")
    return {field: values.get(field) for field in FIELDS}
