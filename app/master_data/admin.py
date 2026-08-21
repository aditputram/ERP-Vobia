from django.contrib import admin

from .models import (
    Category,
    MarketplaceProductMapping,
    MarketplaceSKUMapping,
    Product,
    ProductStatus,
    ProductVariant,
    SKU,
    SKUValueHistory,
    Subcategory,
    Supplier,
    Warehouse,
)


@admin.register(ProductStatus)
class ProductStatusAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name")


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name")


@admin.register(Subcategory)
class SubcategoryAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "category", "is_active")
    list_filter = ("category", "is_active")
    search_fields = ("code", "name")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "status", "category", "subcategory", "is_active")
    list_filter = ("status", "category", "is_active")
    search_fields = ("code", "parent_sku", "article", "name")
    autocomplete_fields = ("status", "category", "subcategory")


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = ("product", "name", "color", "is_active")
    list_filter = ("is_active",)
    search_fields = ("product__name", "name", "color")
    autocomplete_fields = ("product",)


@admin.register(SKU)
class SKUAdmin(admin.ModelAdmin):
    list_display = (
        "sku",
        "product_variant",
        "size",
        "current_retail_price",
        "current_master_cogs",
        "is_active",
    )
    list_filter = ("is_active", "product_variant__product__status", "product_variant__product__category")
    search_fields = ("sku", "product_variant__product__name", "size")
    autocomplete_fields = ("product_variant",)


@admin.register(MarketplaceProductMapping)
class MarketplaceProductMappingAdmin(admin.ModelAdmin):
    list_display = ("source", "marketplace_product_code", "product", "is_active")
    list_filter = ("source", "is_active")
    search_fields = ("marketplace_product_code", "product__name")
    autocomplete_fields = ("product",)


@admin.register(MarketplaceSKUMapping)
class MarketplaceSKUMappingAdmin(admin.ModelAdmin):
    list_display = ("source", "marketplace_sku_id", "marketplace_seller_sku", "sku", "confirmed_by", "is_active")
    list_filter = ("source", "is_active")
    search_fields = ("marketplace_sku_id", "marketplace_seller_sku", "sku__sku", "product_name_evidence")
    autocomplete_fields = ("sku", "confirmed_by")


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "contact_name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name", "contact_name")


@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name")


@admin.register(SKUValueHistory)
class SKUValueHistoryAdmin(admin.ModelAdmin):
    list_display = ("effective_at", "sku", "retail_price", "master_cogs", "product_status", "changed_by")
    list_filter = ("product_status",)
    search_fields = ("sku__sku", "changed_by__username")
    readonly_fields = (
        "id",
        "sku",
        "effective_at",
        "retail_price",
        "master_cogs",
        "product_status",
        "source_batch_id",
        "changed_by",
        "changes",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
