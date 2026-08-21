from django.contrib import admin

from .models import (
    IncomingCarryover,
    IncomingMonthClose,
    IncomingMonthlyActual,
    IncomingPlan,
    ProjectionRule,
    ProjectionScenario,
    SalesProjection,
)


admin.site.register(ProjectionScenario)
admin.site.register(ProjectionRule)
admin.site.register(SalesProjection)
admin.site.register(IncomingPlan)
admin.site.register(IncomingMonthClose)
admin.site.register(IncomingMonthlyActual)
admin.site.register(IncomingCarryover)
