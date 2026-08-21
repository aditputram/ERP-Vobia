from django.contrib import admin

from .models import SalesOrder, SalesOrderLine, SalesStatusHistory


class SalesOrderLineInline(admin.TabularInline):
    model = SalesOrderLine
    extra = 0
    can_delete = False
    readonly_fields = [field.name for field in SalesOrderLine._meta.fields]


@admin.register(SalesOrder)
class SalesOrderAdmin(admin.ModelAdmin):
    list_display = ("order_datetime", "source", "order_number", "current_status", "is_final", "is_pure_cancelled")
    list_filter = ("source", "current_status", "is_final", "is_pure_cancelled")
    search_fields = ("order_number",)
    readonly_fields = [field.name for field in SalesOrder._meta.fields]
    inlines = (SalesOrderLineInline,)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SalesStatusHistory)
class SalesStatusHistoryAdmin(admin.ModelAdmin):
    list_display = ("observed_at", "order", "previous_status", "normalized_status", "changed_by")
    list_filter = ("normalized_status",)
    readonly_fields = [field.name for field in SalesStatusHistory._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

