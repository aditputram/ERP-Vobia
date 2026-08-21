from django.contrib import admin

from .models import (
    ExpectedReturn,
    FIFOAllocation,
    FIFOOpeningSnapshot,
    FIFOLayer,
    InboundReceipt,
    InventoryException,
    InventoryMovement,
    PhysicalReturnReceipt,
    QCInspection,
)


admin.site.register(QCInspection)
admin.site.register(InboundReceipt)
admin.site.register(InventoryMovement)
admin.site.register(FIFOLayer)
admin.site.register(FIFOAllocation)
admin.site.register(FIFOOpeningSnapshot)
admin.site.register(InventoryException)
admin.site.register(ExpectedReturn)
admin.site.register(PhysicalReturnReceipt)
