from django.contrib import admin

from .models import PPICRequirement, PPICRequirementRevision, PurchaseOrder, PurchaseOrderLine, PurchaseOrderNumberSequence


admin.site.register(PPICRequirement)
admin.site.register(PPICRequirementRevision)
admin.site.register(PurchaseOrder)
admin.site.register(PurchaseOrderLine)
admin.site.register(PurchaseOrderNumberSequence)
