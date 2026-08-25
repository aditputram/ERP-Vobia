from django.contrib import admin

from .models import (
    PPICRequirement,
    PPICRequirementRevision,
    POWIPImportBatch,
    POWIPImportIssue,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderNumberSequence,
    StagedPOWIPRow,
)


admin.site.register(PPICRequirement)
admin.site.register(PPICRequirementRevision)
admin.site.register(PurchaseOrder)
admin.site.register(PurchaseOrderLine)
admin.site.register(PurchaseOrderNumberSequence)
admin.site.register(POWIPImportBatch)
admin.site.register(StagedPOWIPRow)
admin.site.register(POWIPImportIssue)
