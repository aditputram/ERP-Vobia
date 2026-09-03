from django.urls import path

from . import views


app_name = "rnd"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("designing/", views.designing, name="designing"),
    path("designing/<uuid:design_id>/", views.design_detail, name="design_detail"),
    path("designing/<uuid:design_id>/file/", views.design_file, name="design_file"),
    path("designing/<uuid:design_id>/recommend/", views.design_recommend, name="design_recommend"),
    path(
        "designing/<uuid:design_id>/cancel-recommendation/",
        views.design_unrecommend,
        name="design_unrecommend",
    ),
    path("collections/new/", views.collection_create, name="collection_create"),
    path("collections/<uuid:collection_id>/", views.collection_detail, name="collection_detail"),
    path("collections/<uuid:collection_id>/delete/", views.collection_delete, name="collection_delete"),
    path("collections/<uuid:collection_id>/handover/", views.collection_handover, name="collection_handover"),
    path("products/<uuid:product_id>/", views.product_detail, name="product_detail"),
    path("products/<uuid:product_id>/submit-approval/", views.product_submit_approval, name="product_submit"),
    path("products/<uuid:product_id>/approve/", views.product_approve, name="product_approve"),
    path(
        "products/<uuid:product_id>/move-to-costing/",
        views.product_move_to_costing,
        name="product_move_to_costing",
    ),
    path(
        "products/<uuid:product_id>/finalize/",
        views.product_finalize,
        name="product_finalize",
    ),
    path(
        "products/<uuid:product_id>/request-revision/",
        views.product_request_revision,
        name="product_request_revision",
    ),
    path(
        "products/<uuid:product_id>/revisions/<int:revision>/file/",
        views.product_revision_file,
        name="product_revision_file",
    ),
    path("products/<uuid:product_id>/files/<slug:file_kind>/", views.product_file, name="product_file"),
]
