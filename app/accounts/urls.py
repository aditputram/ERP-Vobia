from django.urls import path

from .views import (
    InitialSuperadminSetupView,
    LocalLoginView,
    LocalLogoutView,
    VobiaPasswordChangeView,
)


app_name = "accounts"

urlpatterns = [
    path("setup/", InitialSuperadminSetupView.as_view(), name="initial_setup"),
    path("login/", LocalLoginView.as_view(), name="login"),
    path("logout/", LocalLogoutView.as_view(), name="logout"),
    path("password/change/", VobiaPasswordChangeView.as_view(), name="password_change"),
]
