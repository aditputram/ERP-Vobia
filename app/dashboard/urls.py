from django.urls import path

from . import views
from .instagram import connection
from .instagram_report import dashboard as instagram_dashboard
from .campaigns import campaign_create, campaign_detail, campaign_edit, campaign_list


app_name = "dashboard"

urlpatterns = [
    path("marketing/", instagram_dashboard, name="instagram_dashboard"),
    path("marketing/instagram/", connection, name="instagram_connection"),
    path("marketing/campaigns/", campaign_list, name="campaign_list"),
    path("marketing/campaigns/new/", campaign_create, name="campaign_create"),
    path("marketing/campaigns/<uuid:campaign_id>/edit/", campaign_edit, name="campaign_edit"),
    path("marketing/campaigns/<uuid:campaign_id>/", campaign_detail, name="campaign_detail"),
    path("", views.index, name="index"),
    path("module/<slug:module_slug>/", views.enter_module, name="enter_module"),
    path("guide/", views.guide, name="guide"),
]
