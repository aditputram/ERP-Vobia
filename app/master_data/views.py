import csv

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from imports.services.master_parser import CANONICAL_HEADERS

from .models import (
    Category,
    MarketplaceProductMapping,
    Product,
    SKU,
    Supplier,
    Warehouse,
)


@login_required
def overview(request):
    query = request.GET.get("q", "").strip()
    products = Product.objects.select_related(
        "status",
        "category",
        "subcategory",
    ).annotate(sku_count=Count("variants__skus", distinct=True))
    if query:
        products = products.filter(
            Q(name__icontains=query)
            | Q(article__icontains=query)
            | Q(parent_sku__icontains=query)
            | Q(code__icontains=query)
            | Q(variants__skus__sku__icontains=query)
        ).distinct()

    sku_quality = {
        "missing_cogs": SKU.objects.filter(current_master_cogs__isnull=True).count(),
        "missing_retail": SKU.objects.filter(current_retail_price__isnull=True).count(),
        "missing_both": SKU.objects.filter(
            current_master_cogs__isnull=True,
            current_retail_price__isnull=True,
        ).count(),
        "inactive": SKU.objects.filter(is_active=False).count(),
    }
    context = {
        "counts": {
            "products": Product.objects.count(),
            "skus": SKU.objects.count(),
            "categories": Category.objects.count(),
            "suppliers": Supplier.objects.count(),
            "warehouses": Warehouse.objects.count(),
        },
        "sku_quality": sku_quality,
        "products": products,
        "query": query,
    }
    return render(request, "master_data/overview.html", context)


@login_required
def export_bank_data(request):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="VOBIA-Bank-Data-{timezone.localdate():%Y-%m-%d}.csv"'
    )
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(CANONICAL_HEADERS)

    mappings = {}
    for mapping in MarketplaceProductMapping.objects.filter(is_active=True).order_by(
        "product_id", "source", "-created_at"
    ):
        mappings.setdefault((mapping.product_id, mapping.source), mapping.marketplace_product_code)

    skus = SKU.objects.select_related(
        "product_variant__product__status",
        "product_variant__product__category",
        "product_variant__product__subcategory",
    ).order_by("sku")
    for sku in skus:
        variant = sku.product_variant
        product = variant.product
        writer.writerow(
            [
                "Vobia",
                sku.sku,
                product.parent_sku,
                product.article,
                product.category.name,
                product.subcategory.name if product.subcategory else "",
                "" if variant.name == "Default" else variant.name,
                sku.size,
                product.status.name,
                sku.current_master_cogs if sku.current_master_cogs is not None else "",
                sku.current_retail_price if sku.current_retail_price is not None else "",
                mappings.get((product.id, MarketplaceProductMapping.Source.SHOPEE), ""),
                mappings.get((product.id, MarketplaceProductMapping.Source.TIKTOK), ""),
            ]
        )
    return response
