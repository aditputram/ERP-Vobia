from django.urls import path

from . import views
from .instagram import connection
from .instagram_report import dashboard as instagram_dashboard
from .tiktok import callback as tiktok_callback, connection as tiktok_connection, oauth_start as tiktok_oauth_start
from .tiktok_business import callback as tiktok_business_callback, oauth_start as tiktok_business_oauth_start
from .campaigns import campaign_cover, campaign_create, campaign_delete, campaign_detail, campaign_edit, campaign_list
from .partnerships import partnership_create, partnership_delete, partnership_detail, partnership_edit, partnership_list
from .social_sync import scheduled_sync
from rnd import views as rnd_views


app_name = "dashboard"

urlpatterns = [
    path("internal/social-sync/", scheduled_sync, name="scheduled_social_sync"),
    path("marketing/", instagram_dashboard, name="instagram_dashboard"),
    path("marketing/upcoming-collections/", rnd_views.upcoming_collection_list, name="upcoming_collection_list"),
    path("marketing/upcoming-collections/<uuid:collection_id>/", rnd_views.upcoming_collection_detail, name="upcoming_collection_detail"),
    path("marketing/upcoming-collections/products/<uuid:product_id>/recommend/", rnd_views.upcoming_collection_recommend, name="upcoming_collection_recommend"),
    path("marketing/upcoming-collections/products/<uuid:product_id>/files/<slug:file_kind>/", rnd_views.product_file, name="upcoming_collection_product_file"),
    path("marketing/upcoming-collections/products/<uuid:product_id>/official-approve/", rnd_views.upcoming_collection_official_approve, name="upcoming_collection_official_approve"),
    path("marketing/upcoming-collections/<uuid:collection_id>/commercial-approve/", rnd_views.upcoming_collection_commercial_approve, name="upcoming_collection_commercial_approve"),
    path("marketing/instagram/", connection, name="instagram_connection"),
    path("marketing/tiktok/", tiktok_connection, name="tiktok_connection"),
    path("marketing/tiktok/connect/", tiktok_oauth_start, name="tiktok_oauth_start"),
    path("marketing/tiktok/callback/", tiktok_callback, name="tiktok_callback"),
    path("marketing/tiktok/business/connect/", tiktok_business_oauth_start, name="tiktok_business_oauth_start"),
    path("marketing/tiktok/business/callback/", tiktok_business_callback, name="tiktok_business_callback"),
    path("marketing/campaigns/", campaign_list, name="campaign_list"),
    path("marketing/campaigns/new/", campaign_create, name="campaign_create"),
    path("marketing/campaigns/<uuid:campaign_id>/edit/", campaign_edit, name="campaign_edit"),
    path("marketing/campaigns/<uuid:campaign_id>/delete/", campaign_delete, name="campaign_delete"),
    path("marketing/campaigns/<uuid:campaign_id>/cover/", campaign_cover, name="campaign_cover"),
    path("marketing/campaigns/<uuid:campaign_id>/", campaign_detail, name="campaign_detail"),
    path("marketing/partnerships/", partnership_list, name="partnership_list"),
    path("marketing/partnerships/new/", partnership_create, name="partnership_create"),
    path("marketing/partnerships/<uuid:partnership_id>/edit/", partnership_edit, name="partnership_edit"),
    path("marketing/partnerships/<uuid:partnership_id>/delete/", partnership_delete, name="partnership_delete"),
    path("marketing/partnerships/<uuid:partnership_id>/", partnership_detail, name="partnership_detail"),
    path("", views.index, name="index"),
    path("module/<slug:module_slug>/", views.enter_module, name="enter_module"),
    path("guide/", views.guide, name="guide"),
]
