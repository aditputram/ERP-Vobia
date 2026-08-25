from django.urls import path

from . import views


app_name = "production"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("planning/", views.planning, name="planning"),
    path("monitoring/", views.monitoring, name="monitoring"),
    path("rejected-goods/", views.rejected_goods, name="rejected_goods"),
    path("activity/", views.activity, name="activity"),
    path("delivery-order-preview/", views.delivery_order_preview, name="delivery_order_preview"),
    path("activity/<uuid:activity_id>/correct/", views.activity_correction, name="activity_correction"),
    path("qc-follow-up/", views.qc_follow_up, name="qc_follow_up"),
    path("trial-approval/", views.trial_approval, name="trial_approval"),
    path("quality-control/", views.quality_control, name="quality_control"),
    path("<uuid:production_id>/approve-cogs/", views.approve_cogs_finalization, name="approve_cogs_finalization"),
    path("<uuid:production_id>/", views.detail, name="detail"),
]
