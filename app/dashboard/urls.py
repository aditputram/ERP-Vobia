from django.urls import path

from . import views


app_name = "dashboard"

urlpatterns = [
    path("", views.index, name="index"),
    path("module/<slug:module_slug>/", views.enter_module, name="enter_module"),
    path("guide/", views.guide, name="guide"),
]
