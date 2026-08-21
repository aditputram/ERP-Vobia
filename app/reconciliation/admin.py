from django.contrib import admin

from .models import ReconciliationIssue, ReconciliationRun


admin.site.register(ReconciliationRun)
admin.site.register(ReconciliationIssue)
