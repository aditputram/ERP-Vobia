from django.urls import path

from . import views


app_name = "sales"

urlpatterns = [
    # Landing page for the Sales module selected from the ERP module hub.
    path("", views.dashboard, name="dashboard"),
    path("planning-builder/", views.planning_builder, name="planning_builder"),
    path("planning-builder/filter-options/", views.planning_filter_options, name="planning_filter_options"),
    path("forecast/", views.forecast, name="forecast"),
    path("forecast-recommendation/", views.forecast_recommendation, name="forecast_recommendation"),
    path("product-performance/", views.product_performance, name="product_performance"),
    path("pivot-analysis/", views.product_performance, {"pivot_only": True}, name="pivot_analysis"),
    path("traffic-analysis/", views.traffic_analysis, name="traffic_analysis"),
    path("pareto-analysis/", views.pareto, name="pareto"),
    path("transactions/", views.transactions, name="transactions"),
    path("input-transaction/", views.input_transaction, name="input_transaction"),
]
