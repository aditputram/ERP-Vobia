from django.urls import path

from . import views


app_name = "chat"

urlpatterns = [
    path("", views.inbox, name="inbox"),
    path("push/config/", views.push_config, name="push_config"),
    path("push/subscribe/", views.push_subscribe, name="push_subscribe"),
    path("push/unsubscribe/", views.push_unsubscribe, name="push_unsubscribe"),
    path("direct/<uuid:user_id>/", views.start_direct, name="start_direct"),
    path("attachment/<uuid:message_id>/", views.attachment, name="attachment"),
    path("<uuid:thread_id>/", views.inbox, name="thread"),
]
