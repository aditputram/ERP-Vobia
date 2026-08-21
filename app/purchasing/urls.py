from django.urls import path

from . import views


app_name = "purchasing"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("tracking/", views.tracking, name="tracking"),
    path("<uuid:po_id>/", views.po_detail, name="po_detail"),
    path("<uuid:po_id>/release/", views.po_release, name="po_release"),
    path("<uuid:po_id>/cancel/", views.po_cancel, name="po_cancel"),
    path("<uuid:po_id>/print/", views.po_print, name="po_print"),
]
