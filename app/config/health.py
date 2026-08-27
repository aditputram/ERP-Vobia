"""Endpoint pemeriksaan kesehatan untuk hosting.

Render (dan alat pemantauan) memanggil alamat ini untuk tahu apakah aplikasi
masih hidup. Sengaja tanpa login dan tanpa data sensitif: hanya memastikan
aplikasi menyala dan database masih bisa dihubungi.
"""

from django.db import connection
from django.http import JsonResponse


def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 - status database sengaja tidak dibocorkan detailnya
        return JsonResponse({"status": "degraded", "database": "unreachable"}, status=503)
    return JsonResponse({"status": "ok"})
