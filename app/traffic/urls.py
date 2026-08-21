from django.urls import path

from . import views


app_name = "traffic"
urlpatterns = [
    path("", views.overview, name="overview"),
    path("<uuid:batch_id>/", views.detail, name="detail"),
    path("<uuid:batch_id>/approve/", views.approve, name="approve"),
    path("complete/", views.complete, name="complete"),
    path("reopen/", views.reopen, name="reopen"),
]
