from django.urls import path

from . import views


app_name = "master_data"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("export/", views.export_bank_data, name="export_bank_data"),
]
