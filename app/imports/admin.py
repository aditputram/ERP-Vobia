from django.contrib import admin

from .models import ImportValidationIssue, MasterImportBatch, RawFile, StagedMasterRow


@admin.register(RawFile)
class RawFileAdmin(admin.ModelAdmin):
    list_display = (
        "uploaded_at",
        "dataset_type",
        "original_filename",
        "byte_size",
        "uploaded_by",
    )
    search_fields = ("original_filename", "checksum_sha256")
    readonly_fields = (
        "id",
        "dataset_type",
        "original_filename",
        "storage_path",
        "checksum_sha256",
        "byte_size",
        "detected_format",
        "uploaded_at",
        "uploaded_by",
        "source_metadata",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MasterImportBatch)
class MasterImportBatchAdmin(admin.ModelAdmin):
    list_display = ("created_at", "raw_file", "status", "total_rows", "blocking_issue_count", "warning_count")
    list_filter = ("status", "parser_version")
    readonly_fields = [field.name for field in MasterImportBatch._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StagedMasterRow)
class StagedMasterRowAdmin(admin.ModelAdmin):
    list_display = ("batch", "row_number", "sku", "article", "proposed_action")
    list_filter = ("proposed_action", "product_status", "category")
    search_fields = ("sku", "parent_sku", "article")
    readonly_fields = [field.name for field in StagedMasterRow._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ImportValidationIssue)
class ImportValidationIssueAdmin(admin.ModelAdmin):
    list_display = ("batch", "staged_row", "severity", "code", "field_name", "is_blocking")
    list_filter = ("severity", "is_blocking", "code")
    readonly_fields = [field.name for field in ImportValidationIssue._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

