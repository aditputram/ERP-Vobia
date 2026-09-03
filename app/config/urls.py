from django.contrib import admin
from django.shortcuts import render
from django.urls import include, path

from .health import healthz

admin.site.site_header = "Vobia ERP Administration"
admin.site.site_title = "Vobia ERP"
admin.site.index_title = "Administrasi data"

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("terms/", lambda request: render(request, "legal/terms.html"), name="terms"),
    path("privacy/", lambda request: render(request, "legal/privacy.html"), name="privacy"),
    path("admin/", admin.site.urls),
    path("account/", include("accounts.urls")),
    path("imports/", include("imports.urls")),
    path("sales/", include("sales.urls")),
    path("traffic/", include("traffic.urls")),
    path("merchandising/", include("merchandising.urls")),
    path("purchasing/", include("purchasing.urls")),
    path("production/", include("production.urls")),
    path("inventory/", include("inventory.urls")),
    path("reconciliation/", include("reconciliation.urls")),
    path("master-data/", include("master_data.urls")),
    path("rnd/", include("rnd.urls")),
    path("", include("dashboard.urls")),
]
