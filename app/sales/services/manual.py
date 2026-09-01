from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit.services import record_audit
from inventory.services.fifo import create_expected_return, post_sales_out

from ..models import SalesOrder, SalesOrderLine, SalesStatusHistory


@transaction.atomic
def create_manual_sales(*, source_label, order_number, order_datetime, lines, status, actor, shipped=False):
    source_label = source_label.strip()
    order_number = order_number.strip()
    if not source_label or not order_number:
        raise ValidationError("Source dan No. Pesanan/invoice wajib diisi.")
    lines = list(lines)
    if not lines:
        raise ValidationError("Minimal satu Product wajib diisi.")
    sku_ids = [line["sku"].id for line in lines]
    if len(sku_ids) != len(set(sku_ids)):
        raise ValidationError("SKU yang sama hanya boleh dipilih sekali per transaksi.")
    prepared_lines = []
    for item in lines:
        sku = item["sku"]
        quantity = int(item["quantity"])
        net_unit_price = Decimal(item["net_unit_price"])
        if quantity <= 0 or net_unit_price < 0:
            raise ValidationError("Qty harus positif dan harga net tidak boleh negatif.")
        if sku.current_master_cogs is None or sku.current_retail_price is None:
            raise ValidationError(
                f"Retail Price dan COGS master SKU {sku.sku} wajib tersedia sebelum posting finansial."
            )
        prepared_lines.append((sku, quantity, net_unit_price))
    if SalesOrder.objects.filter(source_label=source_label, order_number=order_number).exists():
        raise ValidationError("No. Pesanan/invoice manual ini sudah ada.")
    is_final = status in {"Selesai", "Retur"}
    order = SalesOrder.objects.create(
        source=SalesOrder.Source.OTHER,
        source_label=source_label,
        order_number=order_number,
        order_datetime=order_datetime,
        shipped_datetime=order_datetime if shipped or is_final else None,
        order_date=timezone.localtime(order_datetime).date() if timezone.is_aware(order_datetime) else order_datetime.date(),
        current_status=status,
        source_status=status,
        is_final=is_final,
        is_pure_cancelled=False,
        import_origin=SalesOrder.ImportOrigin.MANUAL,
        affects_inventory=True,
        first_seen_batch_id="00000000-0000-0000-0000-000000000000",
        latest_batch_id="00000000-0000-0000-0000-000000000000",
    )
    SalesStatusHistory.objects.create(
        order=order,
        normalized_status=status,
        source_status=status,
        import_batch_id="00000000-0000-0000-0000-000000000000",
        changed_by=actor,
    )
    created_lines = []
    for sku, quantity, net_unit_price in prepared_lines:
        master_retail = Decimal(sku.current_retail_price)
        retail_special_case = net_unit_price > master_retail
        retail = net_unit_price if retail_special_case else master_retail
        cogs = Decimal(sku.current_master_cogs)
        gross = (Decimal(quantity) * retail).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        net = (Decimal(quantity) * net_unit_price).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        total_cogs = (Decimal(quantity) * cogs).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        gpm = net - total_cogs
        line = SalesOrderLine.objects.create(
            order=order,
            sku=sku,
            sku_code_snapshot=sku.sku,
            product_status_snapshot=sku.product_variant.product.status.name,
            category_snapshot=sku.product_variant.product.category.name,
            subcategory_snapshot=(sku.product_variant.product.subcategory.name if sku.product_variant.product.subcategory else ""),
            product_name_snapshot=sku.product_variant.product.name,
            variant_name_snapshot=sku.size,
            quantity=quantity,
            net_unit_price=net_unit_price,
            retail_price_snapshot=retail,
            sales_cogs_snapshot=cogs,
            total_gross_sales=gross,
            total_net_sales=net,
            total_cogs=total_cogs,
            gpm=gpm,
            gpm_rate=(gpm / gross).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP) if gross else None,
            is_counted=True,
        )
        if order.shipped_datetime:
            post_sales_out(line, actor)
        if status == "Retur":
            create_expected_return(line)
        record_audit(
            actor=actor,
            action="manual_sale_created",
            entity_type="sales.salesorderline",
            entity_id=line.id,
            after_values={
                "business_key": line.business_key,
                "quantity": quantity,
                "net_sales": str(net),
                "master_retail_price": str(master_retail),
                "retail_price_snapshot": str(retail),
                "retail_price_special_case": retail_special_case,
            },
        )
        if retail_special_case:
            record_audit(
                actor=actor,
                action="retail_price_snapshot_special_case",
                entity_type="sales.salesorderline",
                entity_id=line.id,
                reason="Net Unit Price melebihi Retail Price master saat transaksi manual dibuat.",
                before_values={"master_retail_price": str(master_retail)},
                after_values={
                    "retail_price_snapshot": str(retail),
                    "master_retail_price_changed": False,
                },
            )
        line.retail_price_special_case = retail_special_case
        line.master_retail_price_at_entry = master_retail
        created_lines.append(line)
    return created_lines


def create_manual_sale(*, source_label, order_number, order_datetime, sku, quantity, net_unit_price, status, actor, shipped=False):
    return create_manual_sales(
        source_label=source_label,
        order_number=order_number,
        order_datetime=order_datetime,
        lines=[{"sku": sku, "quantity": quantity, "net_unit_price": net_unit_price}],
        status=status,
        actor=actor,
        shipped=shipped,
    )[0]
