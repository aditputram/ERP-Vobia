from django.urls import path

from . import views


app_name = "inventory"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("turnover/", views.turnover, name="turnover"),
    path("inbound/", views.inbound, name="inbound"),
    path("returns/", views.return_log, name="return_log"),
    path("outbound/", views.outbound, name="outbound"),
    path("production/", views.production, name="production"),
    path("opening/", views.opening_import_list, name="opening_list"),
    path("opening/upload/", views.opening_import_upload, name="opening_upload"),
    path("opening/<uuid:batch_id>/", views.opening_import_detail, name="opening_detail"),
    path("opening/<uuid:batch_id>/approve/", views.opening_import_approve, name="opening_approve"),
]
