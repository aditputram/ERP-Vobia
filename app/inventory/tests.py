import csv
import io
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from audit.models import AuditEvent
from master_data.models import Category, Product, ProductStatus, ProductVariant, SKU, Supplier, Warehouse
from purchasing.models import PurchaseOrder, PurchaseOrderLine
from production.models import ProductionStage
from production.services import ensure_production_order
from sales.models import SalesOrder, SalesOrderLine

from .models import ExpectedReturn, FIFOAllocation, FIFOLayer, FIFOOpeningImportBatch, FIFOOpeningSnapshot, InboundReceipt, InventoryException, InventoryMovement, PhysicalReturnReceipt, QCFollowUp
from .services.aging import po_aging_snapshot, refresh_po_close
from .services.fifo import (
    inventory_balance,
    complete_qc_rework,
    create_expected_return,
    post_adjustment,
    post_opening,
    post_sales_out,
    record_inbound,
    record_physical_return,
    record_qc,
    record_re_qc,
    undo_unallocated_inbound,
)
from .services.opening_import import approve_opening_import, create_opening_import


class InventoryWorkflowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="warehouse", password="test-password")
        status = ProductStatus.objects.create(code="ACTIVE", name="Active")
        category = Category.objects.create(code="APPAREL", name="Apparel")
        product = Product.objects.create(code="P-1", parent_sku="PARENT-1", name="Product", status=status, category=category)
        variant = ProductVariant.objects.create(product=product, name="Black", color="Black")
        self.sku = SKU.objects.create(
            sku="SKU-1",
            product_variant=variant,
            current_retail_price=Decimal("200000"),
            current_master_cogs=Decimal("100000"),
        )
        self.supplier = Supplier.objects.create(code="SUP-1", name="Supplier")
        self.warehouse = Warehouse.objects.get(code="MAIN")
        self.po = PurchaseOrder.objects.create(
            po_number="PO-VOB-09/26-001",
            sequence=1,
            supplier=self.supplier,
            need_month=date(2026, 9, 1),
            status=PurchaseOrder.Status.RELEASED,
            source=PurchaseOrder.Source.MANUAL_NEW_PRODUCT,
            created_by=self.user,
            released_by=self.user,
            released_at=timezone.now(),
        )
        self.po_line = PurchaseOrderLine.objects.create(
            po=self.po,
            sku=self.sku,
            ordered_qty=Decimal("10"),
            cogs_snapshot=Decimal("120000"),
        )
        self.production_order = ensure_production_order(self.po, actor=self.user)
        ProductionStage.objects.filter(
            production_order=self.production_order,
            stage=ProductionStage.Stage.TRIM,
        ).update(
            status=ProductionStage.Status.COMPLETE,
            progress_percent=100,
            completed_qty=Decimal("10"),
            actual_start_date=date(2026, 9, 1),
            actual_end_date=date(2026, 9, 4),
            updated_by=self.user,
        )

    def _sales_line(self, number="ORDER-1", quantity=8, order_date=date(2026, 9, 10)):
        order = SalesOrder.objects.create(
            source=SalesOrder.Source.SHOPEE,
            order_number=number,
            order_datetime=timezone.make_aware(datetime.combine(order_date, datetime.min.time())),
            order_date=order_date,
            current_status="Selesai",
            source_status="Selesai",
            is_final=True,
            first_seen_batch_id="11111111-1111-1111-1111-111111111111",
            latest_batch_id="11111111-1111-1111-1111-111111111111",
        )
        return SalesOrderLine.objects.create(
            order=order,
            sku=self.sku,
            quantity=quantity,
            net_unit_price=Decimal("180000"),
            retail_price_snapshot=Decimal("200000"),
            sales_cogs_snapshot=Decimal("100000"),
            total_gross_sales=Decimal("1600000"),
            total_net_sales=Decimal("1440000"),
            total_cogs=Decimal("800000"),
            gpm=Decimal("640000"),
            gpm_rate=Decimal("0.4"),
        )

    def test_qc_then_partial_inbound_creates_actual_layer_only_after_receipt(self):
        inspected_at = timezone.make_aware(datetime(2026, 9, 5, 10, 0))
        record_qc(
            po_line=self.po_line,
            inspected_at=inspected_at,
            qty_inspected=10,
            qty_passed=8,
            qty_failed=2,
            actor=self.user,
            disposition="REWORK",
        )
        self.assertEqual(InventoryMovement.objects.count(), 0)
        _, movement = record_inbound(
            po_line=self.po_line,
            inbound_date=date(2026, 9, 6),
            received_qty=5,
            warehouse=self.warehouse,
            reference="GRN-001",
            actor=self.user,
        )
        self.assertEqual(movement.movement_type, InventoryMovement.MovementType.INCOMING)
        self.assertEqual(movement.created_fifo_layer.remaining_qty, Decimal("5"))
        self.assertEqual(movement.allocated_cost, Decimal("600000"))
        with self.assertRaises(ValidationError):
            record_inbound(
                po_line=self.po_line,
                inbound_date=date(2026, 9, 7),
                received_qty=4,
                warehouse=self.warehouse,
                reference="GRN-002",
                actor=self.user,
            )

    def test_unallocated_inbound_can_be_undone_with_audit_trail(self):
        record_qc(
            po_line=self.po_line,
            inspected_at=timezone.make_aware(datetime(2026, 9, 5, 10, 0)),
            qty_inspected=10,
            qty_passed=10,
            qty_failed=0,
            actor=self.user,
        )
        receipt, movement = record_inbound(
            po_line=self.po_line,
            inbound_date=date(2026, 9, 6),
            received_qty=5,
            warehouse=self.warehouse,
            reference="GRN-UNDO",
            actor=self.user,
        )
        receipt_id = receipt.id
        movement_id = movement.id
        layer_id = movement.created_fifo_layer.id

        undo_unallocated_inbound(
            receipt=receipt,
            actor=self.user,
            reason="Tanggal penerimaan salah.",
        )

        self.assertFalse(InboundReceipt.objects.filter(pk=receipt_id).exists())
        self.assertFalse(InventoryMovement.objects.filter(pk=movement_id).exists())
        self.assertFalse(FIFOLayer.objects.filter(pk=layer_id).exists())
        self.assertTrue(
            AuditEvent.objects.filter(
                action="inbound_receipt_undone",
                entity_id=str(receipt_id),
            ).exists()
        )

    def test_rework_follow_up_re_qc_and_inbound_flow(self):
        inspection = record_qc(
            po_line=self.po_line,
            inspected_at=timezone.make_aware(datetime(2026, 9, 5, 10, 0)),
            qty_inspected=10,
            qty_passed=8,
            qty_failed=2,
            disposition="REWORK",
            notes="Jahitan perlu dirapikan.",
            actor=self.user,
        )
        follow_up = QCFollowUp.objects.get(source_inspection=inspection)
        self.assertEqual(follow_up.status, QCFollowUp.Status.AWAITING_REWORK)
        self.assertEqual(follow_up.open_qty, Decimal("2"))

        complete_qc_rework(
            follow_up=follow_up,
            activity_date=date(2026, 9, 6),
            notes="Jahitan sudah diperbaiki.",
            actor=self.user,
        )
        follow_up.refresh_from_db()
        self.assertEqual(follow_up.status, QCFollowUp.Status.READY_RE_QC)

        record_re_qc(
            follow_up=follow_up,
            activity_date=date(2026, 9, 7),
            qty_passed=1,
            failed_disposition="REWORK",
            notes="Satu pcs masih perlu diperbaiki.",
            actor=self.user,
        )
        follow_up.refresh_from_db()
        self.assertEqual(follow_up.status, QCFollowUp.Status.AWAITING_REWORK)
        self.assertEqual(follow_up.open_qty, Decimal("1"))
        self.assertEqual(follow_up.resolved_passed_qty, Decimal("1"))

        complete_qc_rework(
            follow_up=follow_up,
            activity_date=date(2026, 9, 8),
            notes="Perbaikan kedua selesai.",
            actor=self.user,
        )
        record_re_qc(
            follow_up=follow_up,
            activity_date=date(2026, 9, 9),
            qty_passed=1,
            failed_disposition="",
            notes="Lolos.",
            actor=self.user,
        )
        follow_up.refresh_from_db()
        self.assertEqual(follow_up.status, QCFollowUp.Status.RESOLVED)
        self.assertEqual(follow_up.open_qty, Decimal("0"))
        self.assertEqual(follow_up.resolved_passed_qty, Decimal("2"))
        self.assertEqual(follow_up.events.count(), 5)

        _, movement = record_inbound(
            po_line=self.po_line,
            inbound_date=date(2026, 9, 10),
            received_qty=10,
            warehouse=self.warehouse,
            reference="GRN-RE-QC",
            actor=self.user,
        )
        self.assertEqual(movement.quantity, Decimal("10"))

    def test_old_qc_follow_up_url_redirects_to_production_monitoring(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("production:qc_follow_up"))
        self.assertRedirects(response, reverse("production:monitoring"), fetch_redirect_response=False)

    def test_production_page_is_separate_from_warehouse(self):
        self.client.force_login(self.user)
        production_response = self.client.get(reverse("production:dashboard"))
        warehouse_response = self.client.get(reverse("inventory:overview"))
        self.assertEqual(production_response.status_code, 200)
        self.assertContains(production_response, "Production Dashboard")
        self.assertNotContains(production_response, "PO-VOB-09/26-001")
        self.assertContains(production_response, "Production Plan")
        self.assertNotContains(warehouse_response, "Catat QC")
        self.assertEqual(warehouse_response.context["selected_warehouse"].code, "MAIN")
        turnover_response = self.client.get(reverse("inventory:turnover"))
        self.assertEqual(turnover_response.context["selected_warehouse"].code, "MAIN")

    def test_inventory_summary_date_filter_reconstructs_ending_stock_and_fifo(self):
        post_opening(sku=self.sku, quantity=10, unit_cost=100000, actor=self.user, warehouse=self.warehouse)
        record_qc(
            po_line=self.po_line,
            inspected_at=timezone.make_aware(datetime(2026, 8, 4, 10, 0)),
            qty_inspected=3,
            qty_passed=3,
            qty_failed=0,
            actor=self.user,
        )
        record_inbound(
            po_line=self.po_line,
            inbound_date=date(2026, 8, 5),
            received_qty=3,
            warehouse=self.warehouse,
            reference="GRN-AUG-001",
            actor=self.user,
        )
        post_sales_out(self._sales_line(quantity=4, order_date=date(2026, 8, 10)), self.user)
        self.client.force_login(self.user)

        before_inbound = self.client.get(reverse("inventory:overview"), {"as_of_date": "2026-08-04"})
        after_inbound = self.client.get(reverse("inventory:overview"), {"as_of_date": "2026-08-06"})
        after_sale = self.client.get(reverse("inventory:overview"), {"as_of_date": "2026-08-10"})
        future = self.client.get(reverse("inventory:overview"), {"as_of_date": "2030-12-31"})

        self.assertEqual(before_inbound.context["balances"][0]["balance"], Decimal("10"))
        self.assertEqual(before_inbound.context["balances"][0]["fifo_value"], Decimal("1000000"))
        self.assertEqual(after_inbound.context["balances"][0]["balance"], Decimal("13"))
        self.assertEqual(after_inbound.context["balances"][0]["incoming_qty"], Decimal("3"))
        self.assertEqual(after_inbound.context["balances"][0]["fifo_value"], Decimal("1360000"))
        self.assertEqual(after_sale.context["balances"][0]["balance"], Decimal("9"))
        self.assertEqual(after_sale.context["balances"][0]["outgoing_qty"], Decimal("4"))
        self.assertEqual(after_sale.context["balances"][0]["fifo_value"], Decimal("960000"))
        self.assertContains(after_sale, 'name="as_of_date"')
        self.assertContains(after_sale, 'value="2026-08-10"')
        self.assertEqual(future.context["as_of_date"], date(2030, 12, 31))
        self.assertNotContains(future, 'max="')

    def test_inventory_summary_can_aggregate_by_parent_sku(self):
        second_sku = SKU.objects.create(
            sku="SKU-2",
            product_variant=self.sku.product_variant,
            size="L",
            current_retail_price=Decimal("200000"),
            current_master_cogs=Decimal("100000"),
        )
        post_opening(sku=self.sku, quantity=10, unit_cost=100000, actor=self.user, warehouse=self.warehouse)
        post_opening(sku=second_sku, quantity=4, unit_cost=100000, actor=self.user, warehouse=self.warehouse)
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("inventory:overview"),
            {"as_of_date": "2026-07-31", "sku_type": "parent"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sku_type"], "parent")
        self.assertEqual(len(response.context["balances"]), 1)
        parent = response.context["balances"][0]
        self.assertEqual(parent["parent_sku"], "PARENT-1")
        self.assertEqual(parent["sku_count"], 2)
        self.assertEqual(parent["balance"], Decimal("14"))
        self.assertEqual(parent["fifo_value"], Decimal("1400000"))
        self.assertContains(response, "2 SKU")

    def test_inventory_turnover_filters_product_and_size(self):
        self.sku.size = "M"
        self.sku.save(update_fields=["size"])
        other_product = Product.objects.create(
            code="P-2",
            name="Other Product",
            status=self.sku.product_variant.product.status,
            category=self.sku.product_variant.product.category,
        )
        other_sku = SKU.objects.create(
            sku="SKU-2",
            product_variant=ProductVariant.objects.create(product=other_product, name="Black"),
            size="L",
            current_master_cogs=Decimal("100000"),
        )
        post_opening(sku=self.sku, quantity=10, unit_cost=100000, actor=self.user, warehouse=self.warehouse)
        post_opening(sku=other_sku, quantity=4, unit_cost=100000, actor=self.user, warehouse=self.warehouse)
        post_sales_out(self._sales_line(quantity=2), self.user)
        self.client.force_login(self.user)

        response = self.client.get(reverse("inventory:turnover"), {
            "product": str(self.sku.product_variant.product_id),
            "size": "M",
        })

        self.assertContains(response, 'name="product"')
        self.assertContains(response, 'name="size"')
        self.assertContains(response, 'placeholder="Cari produk..."')
        self.assertContains(response, "data-auto-search")
        self.assertContains(response, "Clear All")
        self.assertContains(response, "Balance Value")
        self.assertNotContains(response, ">Apply</button>")
        self.assertEqual(response.context["size_options"], [{"value": "M", "label": "M"}])
        self.assertEqual(response.context["page"].object_list[-1]["running_balance"], Decimal("8"))
        self.assertEqual(response.context["page"].object_list[-1]["running_value"], Decimal("800000"))
        self.assertContains(response, "SKU-1")
        self.assertNotContains(response, "SKU-2")

    def test_inventory_turnover_parent_sku_summarizes_child_skus(self):
        self.sku.size = "M"
        self.sku.save(update_fields=["size"])
        sibling = SKU.objects.create(
            sku="SKU-1-L",
            product_variant=self.sku.product_variant,
            size="L",
            current_master_cogs=Decimal("100000"),
        )
        post_opening(sku=self.sku, quantity=10, unit_cost=100000, actor=self.user, warehouse=self.warehouse)
        post_opening(sku=sibling, quantity=4, unit_cost=100000, actor=self.user, warehouse=self.warehouse)
        post_sales_out(self._sales_line(quantity=2), self.user)
        self.client.force_login(self.user)

        response = self.client.get(reverse("inventory:turnover"), {
            "sku_type": "parent",
            "product": str(self.sku.product_variant.product_id),
        })
        rows = response.context["page"].object_list

        self.assertEqual(response.context["sku_type"], "parent")
        self.assertEqual(rows[0]["parent_sku"], "PARENT-1")
        self.assertEqual(rows[0]["quantity"], Decimal("14"))
        self.assertEqual(rows[0]["running_value"], Decimal("1400000"))
        self.assertEqual(rows[-1]["running_balance"], Decimal("12"))
        self.assertEqual(rows[-1]["running_value"], Decimal("1200000"))
        self.assertContains(response, "Parent SKU")

    def test_fifo_oldest_first_short_exception_and_no_fake_cost(self):
        post_opening(sku=self.sku, quantity=5, unit_cost=100000, actor=self.user, warehouse=self.warehouse)
        record_qc(
            po_line=self.po_line,
            inspected_at=timezone.make_aware(datetime(2026, 9, 5, 10, 0)),
            qty_inspected=10,
            qty_passed=10,
            qty_failed=0,
            actor=self.user,
        )
        record_inbound(
            po_line=self.po_line,
            inbound_date=date(2026, 9, 6),
            received_qty=10,
            warehouse=self.warehouse,
            reference="GRN-001",
            actor=self.user,
        )
        movement = post_sales_out(self._sales_line(quantity=17), self.user)
        allocations = list(FIFOAllocation.objects.filter(outbound_movement=movement).order_by("layer__receipt_date"))
        self.assertEqual([row.allocated_qty for row in allocations], [Decimal("5"), Decimal("10")])
        self.assertEqual(movement.allocated_cost, Decimal("1700000"))
        exception = InventoryException.objects.get(movement=movement)
        self.assertEqual(exception.quantity, Decimal("2"))
        self.assertEqual(inventory_balance(self.sku), Decimal("-2"))

    def test_sellable_return_restores_original_fifo_layer_and_non_sellable_does_not(self):
        post_opening(sku=self.sku, quantity=10, unit_cost=100000, actor=self.user, warehouse=self.warehouse)
        sales_line = self._sales_line(quantity=8)
        post_sales_out(sales_line, self.user)
        _, movement = record_physical_return(
            sales_line=sales_line,
            received_date=date(2026, 9, 15),
            quantity=3,
            warehouse=self.warehouse,
            condition=PhysicalReturnReceipt.Condition.SELLABLE,
            actor=self.user,
        )
        self.assertEqual(movement.allocated_cost, Decimal("300000"))
        allocation = FIFOAllocation.objects.get(outbound_movement__sales_line=sales_line)
        self.assertEqual(allocation.returned_qty, Decimal("3"))
        self.assertEqual(allocation.layer.remaining_qty, Decimal("5"))
        self.assertEqual(inventory_balance(self.sku), Decimal("5"))

        _, no_movement = record_physical_return(
            sales_line=sales_line,
            received_date=date(2026, 9, 16),
            quantity=1,
            warehouse=self.warehouse,
            condition=PhysicalReturnReceipt.Condition.DAMAGED,
            actor=self.user,
        )
        self.assertIsNone(no_movement)
        self.assertEqual(inventory_balance(self.sku), Decimal("5"))

    def test_return_log_filters_order_and_receives_multiple_skus(self):
        first_line = self._sales_line(number="RETURN-ORDER-1", quantity=4)
        first_line.order.current_status = "Retur"
        first_line.order.source_label = "Shopee Vobia"
        first_line.order.save(update_fields=["current_status", "source_label"])
        create_expected_return(first_line)

        second_sku = SKU.objects.create(
            sku="SKU-RETURN-2",
            product_variant=self.sku.product_variant,
            size="L",
            current_retail_price=Decimal("200000"),
            current_master_cogs=Decimal("100000"),
        )
        second_line = SalesOrderLine.objects.create(
            order=first_line.order,
            sku=second_sku,
            quantity=2,
            net_unit_price=Decimal("180000"),
            retail_price_snapshot=Decimal("200000"),
            sales_cogs_snapshot=Decimal("100000"),
            total_gross_sales=Decimal("400000"),
            total_net_sales=Decimal("360000"),
            total_cogs=Decimal("200000"),
            gpm=Decimal("160000"),
            gpm_rate=Decimal("0.4"),
        )
        create_expected_return(second_line)

        other_line = self._sales_line(number="RETURN-ORDER-TIKTOK", quantity=1)
        other_line.order.current_status = "Retur"
        other_line.order.source = SalesOrder.Source.TIKTOK
        other_line.order.source_label = "TikTok Vobia"
        other_line.order.save(update_fields=["current_status", "source", "source_label"])
        create_expected_return(other_line)

        self.client.force_login(self.user)
        url = (
            f"{reverse('inventory:return_log')}?source=Shopee%20Vobia"
            "&order_number=RETURN-ORDER-1"
        )
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_order"], first_line.order)
        self.assertEqual(len(response.context["return_rows"]), 2)
        self.assertContains(response, "RETURN-ORDER-1")
        self.assertNotContains(response, "RETURN-ORDER-TIKTOK")
        self.assertContains(response, "SKU-1")
        self.assertContains(response, "SKU-RETURN-2")
        self.assertContains(response, "Quantity Sales")
        self.assertContains(response, "Expected Return")
        self.assertContains(response, f'name="quantity_{first_line.id}"')
        self.assertContains(response, "Receive Sales Return")
        self.assertNotContains(response, "Waiting Inspection")

        response = self.client.post(
            url,
            {
                "received_date": "2026-09-20",
                "warehouse": str(self.warehouse.id),
                "condition": PhysicalReturnReceipt.Condition.DAMAGED,
                "notes": "Diterima dari marketplace.",
                f"quantity_{first_line.id}": "2",
                f"quantity_{second_line.id}": "2",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(PhysicalReturnReceipt.objects.filter(sales_line__order=first_line.order).count(), 2)
        first_line.expected_return.refresh_from_db()
        second_line.expected_return.refresh_from_db()
        self.assertEqual(first_line.expected_return.status, ExpectedReturn.Status.PARTIALLY_RECEIVED)
        self.assertEqual(second_line.expected_return.status, ExpectedReturn.Status.RECEIVED)
        self.assertFalse(InventoryMovement.objects.filter(movement_type=InventoryMovement.MovementType.RETURN_IN).exists())

        receipt = PhysicalReturnReceipt.objects.filter(sales_line=first_line).get()
        response = self.client.post(
            url,
            {
                "action": "update_follow_up",
                "receipt_id": str(receipt.id),
                "follow_up_status": PhysicalReturnReceipt.FollowUpStatus.SUBMITTED,
            },
        )
        self.assertEqual(response.status_code, 302)
        receipt.refresh_from_db()
        self.assertEqual(receipt.follow_up_status, PhysicalReturnReceipt.FollowUpStatus.SUBMITTED)
        response = self.client.get(url)
        self.assertContains(response, "Tindak Lanjut Retur Bermasalah")
        self.assertContains(response, "Diajukan ke Marketplace")

    def test_po_aging_closes_only_after_full_inbound_and_layer_depletion(self):
        record_qc(
            po_line=self.po_line,
            inspected_at=timezone.make_aware(datetime(2026, 9, 5, 10, 0)),
            qty_inspected=10,
            qty_passed=10,
            qty_failed=0,
            actor=self.user,
        )
        record_inbound(
            po_line=self.po_line,
            inbound_date=date(2026, 9, 6),
            received_qty=10,
            warehouse=self.warehouse,
            reference="GRN-001",
            actor=self.user,
        )
        sale = self._sales_line(quantity=10, order_date=date(2026, 9, 10))
        post_sales_out(sale, self.user)
        as_of = date.today()
        snapshot = refresh_po_close(self.po.id, as_of)
        self.po.refresh_from_db()
        self.assertEqual(snapshot["po_remaining_qty"], Decimal("0"))
        self.assertEqual(self.po.close_date, as_of)

        _, _ = record_physical_return(
            sales_line=sale,
            received_date=as_of,
            quantity=1,
            warehouse=self.warehouse,
            condition=PhysicalReturnReceipt.Condition.SELLABLE,
            actor=self.user,
        )
        self.po.refresh_from_db()
        self.assertIsNone(self.po.close_date)
        self.assertEqual(po_aging_snapshot(self.po, as_of)["po_remaining_qty"], Decimal("1"))

    def test_traceable_adjustment_resolves_fifo_short_with_historical_evidence(self):
        post_opening(sku=self.sku, quantity=2, unit_cost=100000, actor=self.user, warehouse=self.warehouse)
        sale = self._sales_line(quantity=5, order_date=date(2026, 9, 10))
        movement = post_sales_out(sale, self.user)
        exception = InventoryException.objects.get(movement=movement)
        adjustment = post_adjustment(
            sku=self.sku,
            movement_date=date(2026, 9, 9),
            direction=InventoryMovement.Direction.IN,
            quantity=3,
            unit_cost=Decimal("110000"),
            actor=self.user,
            warehouse=self.warehouse,
            reason="Inbound lama terlewat dari sumber",
            evidence_reference="SJ-HIST-001",
            exception=exception,
        )
        exception.refresh_from_db()
        movement.refresh_from_db()
        self.assertEqual(exception.status, InventoryException.Status.RESOLVED)
        self.assertEqual(exception.resolution_movement, adjustment)
        self.assertEqual(movement.allocated_cost, Decimal("530000"))
        self.assertEqual(inventory_balance(self.sku), Decimal("0"))

    def test_zero_opening_still_freezes_snapshot_without_movement(self):
        self.assertIsNone(post_opening(sku=self.sku, quantity=0, unit_cost=100000, actor=self.user))
        self.assertEqual(self.sku.fifo_opening_snapshot.opening_qty, Decimal("0"))
        self.assertEqual(InventoryMovement.objects.count(), 0)

    def test_negative_opening_is_part_of_official_balance_without_fake_layer(self):
        post_opening(sku=self.sku, quantity=-3, unit_cost=100000, actor=self.user)
        self.assertEqual(inventory_balance(self.sku), Decimal("-3"))
        self.assertEqual(FIFOLayer.objects.count(), 0)
        self.assertTrue(InventoryException.objects.filter(code=InventoryException.Code.NEGATIVE_OPENING).exists())

    def test_po_wip_legacy_qc_allows_only_post_cutover_outstanding_inbound(self):
        self.po.source = PurchaseOrder.Source.LEGACY_WIP
        self.po.migration_cutoff_date = date(2026, 7, 31)
        self.po.migration_evidence_reference = "PO-WIP-AUDIT-001"
        self.po.save()
        self.po_line.ordered_qty = Decimal("10")
        self.po_line.received_before_cutover_qty = Decimal("4")
        self.po_line.qc_passed_before_cutover_qty = Decimal("10")
        self.po_line.full_clean()
        self.po_line.save()
        receipt, _ = record_inbound(
            po_line=self.po_line,
            inbound_date=date(2026, 8, 2),
            received_qty=6,
            warehouse=self.warehouse,
            reference="GRN-WIP-001",
            actor=self.user,
        )
        self.assertEqual(receipt.received_qty, Decimal("6"))
        with self.assertRaises(ValidationError):
            record_inbound(
                po_line=self.po_line,
                inbound_date=date(2026, 8, 3),
                received_qty=1,
                warehouse=self.warehouse,
                reference="GRN-WIP-002",
                actor=self.user,
            )

    def test_bulk_opening_import_previews_and_commits_atomically(self):
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["SKU", "Opening Qty", "Frozen Unit COGS", "Opening Inventory COGS", "Cutover Date", "Layer Key"])
        writer.writerow(["SKU-1", "10", "100000", "1000000", "2026-07-31", "OPENING|20260731|SKU-1"])
        uploaded = SimpleUploadedFile("fifo-opening.csv", output.getvalue().encode(), content_type="text/csv")
        with tempfile.TemporaryDirectory() as directory, override_settings(PRIVATE_UPLOAD_ROOT=Path(directory)):
            batch = create_opening_import(uploaded, self.user)
            self.assertEqual(batch.status, FIFOOpeningImportBatch.Status.READY)
            self.assertEqual(batch.total_rows, 1)
            approve_opening_import(batch.id, self.user)
        batch.refresh_from_db()
        self.assertEqual(batch.status, FIFOOpeningImportBatch.Status.COMMITTED)
        self.assertEqual(FIFOOpeningSnapshot.objects.count(), 1)
        self.assertEqual(FIFOLayer.objects.get().remaining_qty, Decimal("10"))
