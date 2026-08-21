from django.urls import path

from . import views


app_name = "imports"

urlpatterns = [
    path("master/", views.master_import_list, name="master_list"),
    path("master/upload/", views.master_import_upload, name="master_upload"),
    path("master/<uuid:batch_id>/", views.master_import_detail, name="master_detail"),
    path("master/<uuid:batch_id>/approve/", views.master_import_approve, name="master_approve"),
    path("sales/", views.sales_import_list, name="sales_list"),
    path("sales/upload/", views.sales_import_upload, name="sales_upload"),
    path("sales/<uuid:batch_id>/", views.sales_import_detail, name="sales_detail"),
    path("sales/<uuid:batch_id>/approve/", views.sales_import_approve, name="sales_approve"),
]
