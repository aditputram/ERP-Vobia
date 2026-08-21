from django.contrib import admin

from .models import StagedTrafficRow, TrafficImportBatch, TrafficImportIssue, TrafficPeriodState, TrafficProductMetric


admin.site.register(TrafficPeriodState)
admin.site.register(TrafficImportBatch)
admin.site.register(StagedTrafficRow)
admin.site.register(TrafficImportIssue)
admin.site.register(TrafficProductMetric)
