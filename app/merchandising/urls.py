from django.urls import path

from . import views


app_name = "merchandising"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("projection/", views.projection, name="projection"),
    path("projection/<uuid:projection_id>/approve/", views.approve_projection, name="approve_projection"),
    path("projection/<uuid:projection_id>/incoming/", views.make_incoming, name="make_incoming"),
    path("incoming/<uuid:plan_id>/approve/", views.approve_incoming, name="approve_incoming"),
    path("incoming/close/", views.close_incoming, name="close_incoming"),
]
