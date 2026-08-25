from django.contrib import admin

from .models import ProductionActivity, ProductionOrder, ProductionStage, ProductionTrial


@admin.register(ProductionOrder)
class ProductionOrderAdmin(admin.ModelAdmin):
    list_display = ("po", "updated_at")
    search_fields = ("po__po_number", "po__supplier__name")


@admin.register(ProductionStage)
class ProductionStageAdmin(admin.ModelAdmin):
    list_display = ("production_order", "stage", "status", "completed_qty", "progress_percent", "updated_at")
    list_filter = ("stage", "status")


@admin.register(ProductionTrial)
class ProductionTrialAdmin(admin.ModelAdmin):
    list_display = ("production_order", "revision", "status", "target_trial_date", "trial_date", "decided_at")
    list_filter = ("status",)


@admin.register(ProductionActivity)
class ProductionActivityAdmin(admin.ModelAdmin):
    list_display = ("production_order", "action", "stage", "actor", "occurred_at")
    list_filter = ("action", "stage")
