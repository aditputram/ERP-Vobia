from django.contrib import admin

from .models import (
    PPICProductionMaterial,
    PPICProductionCost,
    PPICProductionPlan,
    PPICProductionPlanLine,
    PPICRequirement,
    PPICRequirementRevision,
    POWIPImportBatch,
    POWIPImportIssue,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderLine,
    PurchaseOrderNumberSequence,
    StagedPOWIPRow,
)


admin.site.register(PPICRequirement)
admin.site.register(PPICRequirementRevision)
admin.site.register(PPICProductionPlan)
admin.site.register(PPICProductionPlanLine)
admin.site.register(PPICProductionMaterial)
admin.site.register(PPICProductionCost)
admin.site.register(PurchaseOrder)
admin.site.register(PurchaseOrderItem)
admin.site.register(PurchaseOrderLine)
admin.site.register(PurchaseOrderNumberSequence)
admin.site.register(POWIPImportBatch)
admin.site.register(StagedPOWIPRow)
admin.site.register(POWIPImportIssue)
