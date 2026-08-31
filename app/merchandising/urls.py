from django.urls import path

from . import views


app_name = "merchandising"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("projection/", views.projection, name="projection"),
    path("planning-builder/", views.planning_builder, name="planning_builder"),
    path("planning-builder/filter-options/", views.planning_filter_options, name="planning_filter_options"),
    path("planning-builder/scenario/<uuid:scenario_id>/edit/", views.edit_scenario, name="edit_scenario"),
    path("planning-builder/scenario/<uuid:scenario_id>/delete/", views.delete_scenario, name="delete_scenario"),
    path("planning-builder/scenario/<uuid:scenario_id>/draft/", views.update_scenario_draft, name="update_scenario_draft"),
    path("planning-builder/scenario/<uuid:scenario_id>/revise/", views.revise_scenario, name="revise_scenario"),
    path("planning-builder/scenario/<uuid:scenario_id>/draft/items/delete/", views.delete_scenario_draft_items, name="delete_scenario_draft_items"),
    path("projection/<uuid:projection_id>/approve/", views.approve_projection, name="approve_projection"),
    path("projection/<uuid:projection_id>/incoming/", views.make_incoming, name="make_incoming"),
    path("incoming/<uuid:plan_id>/approve/", views.approve_incoming, name="approve_incoming"),
    path("incoming/close/", views.close_incoming, name="close_incoming"),
]
