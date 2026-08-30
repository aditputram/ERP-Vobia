from django.urls import path

from .views import (
    InitialSuperadminSetupView,
    LocalLoginView,
    LocalLogoutView,
    VobiaPasswordChangeView,
    user_create,
    user_edit,
    user_list,
)


app_name = "accounts"

urlpatterns = [
    path("setup/", InitialSuperadminSetupView.as_view(), name="initial_setup"),
    path("login/", LocalLoginView.as_view(), name="login"),
    path("logout/", LocalLogoutView.as_view(), name="logout"),
    path("password/change/", VobiaPasswordChangeView.as_view(), name="password_change"),
    path("users/", user_list, name="user_list"),
    path("users/new/", user_create, name="user_create"),
    path("users/<uuid:user_id>/edit/", user_edit, name="user_edit"),
]
