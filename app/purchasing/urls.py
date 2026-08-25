from django.urls import path

from . import views


app_name = "purchasing"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("requirements/", views.requirements, name="requirements"),
    path("po-generator/", views.overview, name="generator"),
    path("purchase-orders/", views.purchase_orders, name="purchase_orders"),
    path("vendors/", views.vendors, name="vendors"),
    path("vendors/<uuid:supplier_id>/delete/", views.vendor_delete, name="vendor_delete"),
    path("tracking/", views.tracking, name="tracking"),
    path("wip-migration/", views.po_wip_import_list, name="po_wip_list"),
    path("wip-migration/upload/", views.po_wip_import_upload, name="po_wip_upload"),
    path("wip-migration/<uuid:batch_id>/", views.po_wip_import_detail, name="po_wip_detail"),
    path("wip-migration/<uuid:batch_id>/approve/", views.po_wip_import_approve, name="po_wip_approve"),
    path("<uuid:po_id>/", views.po_detail, name="po_detail"),
    path("<uuid:po_id>/release/", views.po_release, name="po_release"),
    path("<uuid:po_id>/cancel/", views.po_cancel, name="po_cancel"),
    path("<uuid:po_id>/revise-vendor/", views.po_revise_vendor, name="po_revise_vendor"),
    path("<uuid:po_id>/delete-draft/", views.po_delete_draft, name="po_delete_draft"),
    path("<uuid:po_id>/print/", views.po_print, name="po_print"),
]
