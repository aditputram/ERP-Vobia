from django.urls import path

from . import views


app_name = "sales"

urlpatterns = [
    # Landing page for the Sales module selected from the ERP module hub.
    path("", views.dashboard, name="dashboard"),
    path("planning-builder/", views.planning_builder, name="planning_builder"),
    path("planning-builder/filter-options/", views.planning_filter_options, name="planning_filter_options"),
    path("product-performance/", views.product_performance, name="product_performance"),
    path("pareto-analysis/", views.pareto, name="pareto"),
    path("transactions/", views.transactions, name="transactions"),
    path("input-transaction/", views.input_transaction, name="input_transaction"),
]
