from django.urls import path

from . import views


app_name = "reconciliation"
urlpatterns = [path("", views.overview, name="overview"), path("<uuid:run_id>/", views.detail, name="detail")]
