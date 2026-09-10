from django.urls import path

from . import views


app_name = "finance"
urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("accounts/", views.account_list, name="accounts"),
    path("journals/", views.journal_list, name="journals"),
    path("journals/new/", views.journal_create, name="journal_create"),
    path("journals/<uuid:entry_id>/", views.journal_detail, name="journal_detail"),
    path("journals/<uuid:entry_id>/approve/", views.journal_approve, name="journal_approve"),
    path("reports/trial-balance/", views.trial_balance, name="trial_balance"),
    path("reports/balance-sheet/", views.balance_sheet, name="balance_sheet"),
    path("reports/profit-loss/", views.profit_loss, name="profit_loss"),
]
