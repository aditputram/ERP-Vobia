from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import render

from .models import Category, Product, SKU, Supplier, Warehouse


@login_required
def overview(request):
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
        "recent_skus": SKU.objects.select_related(
            "product_variant__product__status",
            "product_variant__product__category",
        ).filter(Q(is_active=True) | Q(is_active=False))[:10],
    }
    return render(request, "master_data/overview.html", context)

