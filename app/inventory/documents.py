from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _qty(value):
    return f"{int(value):,}".replace(",", ".")


def build_inbound_receipt_pdf(*, po, receipts):
    receipts = list(receipts)
    output = BytesIO()
    page_size = landscape(A4)
    styles = getSampleStyleSheet()
    body = ParagraphStyle("InboundBody", parent=styles["BodyText"], fontName="Helvetica", fontSize=8, leading=10, textColor=colors.HexColor("#171717"))
    small = ParagraphStyle("InboundSmall", parent=body, fontSize=7, leading=9)
    title = ParagraphStyle("InboundTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=19, leading=22, textColor=colors.HexColor("#171717"))
    right = ParagraphStyle("InboundRight", parent=body, alignment=TA_RIGHT)
    center = ParagraphStyle("InboundCenter", parent=body, alignment=TA_CENTER)
    document = SimpleDocTemplate(output, pagesize=page_size, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=18 * mm, bottomMargin=16 * mm, title=f"Laporan Inbound {po.po_number}", author="Vobia Space")

    received_total = sum((row.received_qty for row in receipts), 0)
    ordered_total = sum((line.ordered_qty for line in po.lines.all()), 0)
    outstanding = max(ordered_total - received_total, 0)
    date_totals = {}
    for row in receipts:
        date_totals[row.inbound_date] = date_totals.get(row.inbound_date, 0) + row.received_qty

    story = [
        Table([[Paragraph("VOBIA SPACE", body), Paragraph("WAREHOUSE", right)], [Paragraph("LAPORAN PENERIMAAN BARANG", title), ""]], colWidths=(180 * mm, 80 * mm)),
        Spacer(1, 7 * mm),
    ]
    meta = Table(
        [["NO. PURCHASE ORDER", "VENDOR", "TOTAL PO", "TOTAL DITERIMA", "OUTSTANDING"], [Paragraph(po.po_number, body), Paragraph(po.supplier.name, body), Paragraph(f"{_qty(ordered_total)} pcs", center), Paragraph(f"{_qty(received_total)} pcs", center), Paragraph(f"{_qty(outstanding)} pcs", center)]],
        colWidths=(55 * mm, 85 * mm, 40 * mm, 40 * mm, 40 * mm),
    )
    meta.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#171717")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, 0), 7), ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#bdbdb8")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story.extend([meta, Spacer(1, 6 * mm), Paragraph("RINGKASAN TANGGAL PENERIMAAN", styles["Heading3"])])
    date_table = Table([["TANGGAL DITERIMA", "QUANTITY"]] + [[date.strftime("%d/%m/%Y"), f"{_qty(qty)} pcs"] for date, qty in sorted(date_totals.items())], colWidths=(65 * mm, 45 * mm))
    date_table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#d7f75b")), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("ALIGN", (1, 1), (1, -1), "RIGHT"), ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#bdbdb8")), ("FONTSIZE", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story.extend([date_table, Spacer(1, 7 * mm), Paragraph("DETAIL PENERIMAAN", styles["Heading3"])])

    detail_rows = [["TANGGAL", "NO. DO", "SKU", "PRODUCT", "VARIANT", "SIZE", "QTY", "WAREHOUSE", "DITERIMA OLEH"]]
    for row in receipts:
        sku = row.po_line.sku
        detail_rows.append([row.inbound_date.strftime("%d/%m/%Y"), row.delivery_activity.delivery_order.number if row.delivery_activity_id and row.delivery_activity.delivery_order_id else "-", Paragraph(sku.sku, small), Paragraph(sku.product_variant.product.name, small), Paragraph(sku.product_variant.name, small), sku.size or "-", _qty(row.received_qty), Paragraph(row.warehouse.name, small), Paragraph(row.recorded_by.get_full_name() or row.recorded_by.username, small)])
    detail_rows.append(["", "", "", "", "", "TOTAL", _qty(received_total), "", ""])
    detail = Table(detail_rows, colWidths=(22 * mm, 29 * mm, 30 * mm, 48 * mm, 34 * mm, 15 * mm, 18 * mm, 32 * mm, 32 * mm), repeatRows=1)
    detail.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#171717")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 7), ("GRID", (0, 0), (-1, -2), 0.4, colors.HexColor("#c8c8c3")), ("ROWBACKGROUNDS", (0, 1), (-1, -2), (colors.white, colors.HexColor("#f4f4f1"))), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (5, 1), (6, -1), "RIGHT"), ("SPAN", (0, -1), (4, -1)), ("SPAN", (7, -1), (8, -1)), ("LINEABOVE", (5, -1), (6, -1), 1, colors.HexColor("#171717")), ("FONTNAME", (5, -1), (6, -1), "Helvetica-Bold"), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4)]))
    story.append(detail)

    def footer(pdf, doc):
        pdf.saveState()
        pdf.setFont("Helvetica", 7)
        pdf.setFillColor(colors.HexColor("#666666"))
        pdf.drawString(14 * mm, 9 * mm, f"Vobia Space - {po.po_number}")
        pdf.drawRightString(page_size[0] - 14 * mm, 9 * mm, f"Halaman {doc.page}")
        pdf.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def build_inbound_checklist_pdf(*, po, deliveries):
    deliveries = list(deliveries)
    output = BytesIO()
    page_size = landscape(A4)
    styles = getSampleStyleSheet()
    body = ParagraphStyle("ChecklistBody", parent=styles["BodyText"], fontName="Helvetica", fontSize=8, leading=10)
    small = ParagraphStyle("ChecklistSmall", parent=body, fontSize=7, leading=9)
    title = ParagraphStyle("ChecklistTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=19, leading=22)
    right = ParagraphStyle("ChecklistRight", parent=body, alignment=TA_RIGHT)
    document = SimpleDocTemplate(output, pagesize=page_size, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=18 * mm, bottomMargin=16 * mm, title=f"Checklist Penerimaan {po.po_number}", author="Vobia Space")
    total_remaining = sum((row["remaining"] for row in deliveries), 0)
    story = [Table([[Paragraph("VOBIA SPACE", body), Paragraph("WAREHOUSE", right)], [Paragraph("CHECKLIST PENERIMAAN BARANG", title), ""]], colWidths=(180 * mm, 80 * mm)), Spacer(1, 7 * mm)]
    meta = Table([["NO. PURCHASE ORDER", "VENDOR", "TOTAL MENUNGGU", "TANGGAL PENGECEKAN"], [Paragraph(po.po_number, body), Paragraph(po.supplier.name, body), f"{_qty(total_remaining)} pcs", "____ / ____ / ______"]], colWidths=(60 * mm, 95 * mm, 50 * mm, 55 * mm))
    meta.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#171717")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 8), ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#bdbdb8")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (2, 1), (-1, 1), "CENTER"), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story.extend([meta, Spacer(1, 7 * mm)])
    rows = [["NO. DO", "TGL KIRIM", "SKU", "ARTICLE", "VARIANT", "SIZE", "QTY DIKIRIM", "SUDAH DITERIMA", "QTY DICEK", "CATATAN"]]
    for row in deliveries:
        delivery = row["delivery"]
        sku = delivery.po_line.sku
        rows.append([delivery.delivery_order.number, delivery.activity_date.strftime("%d/%m/%Y"), Paragraph(sku.sku, small), Paragraph(sku.product_variant.product.name, small), Paragraph(sku.product_variant.name, small), sku.size or "-", _qty(row["shipped"]), _qty(row["received"]), "", ""])
    rows.append(["", "", "", "", "", "TOTAL", _qty(sum((row["shipped"] for row in deliveries), 0)), _qty(sum((row["received"] for row in deliveries), 0)), "", ""])
    detail = Table(rows, colWidths=(30 * mm, 22 * mm, 28 * mm, 44 * mm, 31 * mm, 14 * mm, 21 * mm, 24 * mm, 21 * mm, 35 * mm), repeatRows=1)
    detail.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#171717")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 7), ("GRID", (0, 0), (-1, -2), 0.4, colors.HexColor("#c8c8c3")), ("ROWBACKGROUNDS", (0, 1), (-1, -2), (colors.white, colors.HexColor("#f4f4f1"))), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (5, 1), (8, -1), "RIGHT"), ("SPAN", (0, -1), (4, -1)), ("SPAN", (8, -1), (9, -1)), ("LINEABOVE", (5, -1), (7, -1), 1, colors.HexColor("#171717")), ("FONTNAME", (5, -1), (7, -1), "Helvetica-Bold"), ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
    story.extend([detail, Spacer(1, 10 * mm), Table([["Diperiksa oleh:", "Mengetahui:"], ["\n\n____________________________", "\n\n____________________________"]], colWidths=(130 * mm, 130 * mm))])

    def footer(pdf, doc):
        pdf.saveState()
        pdf.setFont("Helvetica", 7)
        pdf.setFillColor(colors.HexColor("#666666"))
        pdf.drawString(14 * mm, 9 * mm, f"Vobia Space - {po.po_number}")
        pdf.drawRightString(page_size[0] - 14 * mm, 9 * mm, f"Halaman {doc.page}")
        pdf.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def build_inbound_queue_pdf(*, deliveries):
    output = BytesIO()
    page_size = landscape(A4)
    styles = getSampleStyleSheet()
    body = ParagraphStyle("QueueBody", parent=styles["BodyText"], fontName="Helvetica", fontSize=7, leading=9)
    title = ParagraphStyle("QueueTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=19, leading=22)
    right = ParagraphStyle("QueueRight", parent=body, alignment=TA_RIGHT)
    rows = [["PO", "VENDOR", "NO. DO", "JENIS", "TGL KIRIM", "SKU", "ARTICLE", "SIZE", "QTY MENUNGGU", "QTY DICEK", "CATATAN"]]
    total = 0
    for shipment in deliveries:
        for row in shipment["rows"]:
            if not row["remaining"]:
                continue
            delivery = row["delivery"]
            sku = delivery.po_line.sku
            total += row["remaining"]
            rows.append([Paragraph(delivery.production_order.po.po_number, body), Paragraph(delivery.production_order.po.supplier.name, body), delivery.delivery_order.number, row["kind_label"], delivery.activity_date.strftime("%d/%m/%Y"), Paragraph(sku.sku, body), Paragraph(sku.product_variant.product.name, body), sku.size or "-", _qty(row["remaining"]), "", ""])
    rows.append(["", "", "", "", "", "", "", "TOTAL", _qty(total), "", ""])
    document = SimpleDocTemplate(output, pagesize=page_size, leftMargin=10 * mm, rightMargin=10 * mm, topMargin=14 * mm, bottomMargin=14 * mm, title="Checklist Pengiriman Menunggu Penerimaan", author="Vobia Space")
    story = [Table([[Paragraph("VOBIA SPACE", body), Paragraph("WAREHOUSE", right)], [Paragraph("PENGIRIMAN MENUNGGU PENERIMAAN", title), ""]], colWidths=(190 * mm, 85 * mm)), Spacer(1, 6 * mm)]
    detail = Table(rows, colWidths=(31 * mm, 31 * mm, 27 * mm, 23 * mm, 20 * mm, 25 * mm, 38 * mm, 12 * mm, 23 * mm, 20 * mm, 30 * mm), repeatRows=1)
    detail.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#171717")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 6.5), ("GRID", (0, 0), (-1, -2), 0.4, colors.HexColor("#c8c8c3")), ("ROWBACKGROUNDS", (0, 1), (-1, -2), (colors.white, colors.HexColor("#f4f4f1"))), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (7, 1), (9, -1), "RIGHT"), ("SPAN", (0, -1), (6, -1)), ("SPAN", (9, -1), (10, -1)), ("LINEABOVE", (7, -1), (8, -1), 1, colors.HexColor("#171717")), ("FONTNAME", (7, -1), (8, -1), "Helvetica-Bold"), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story.extend([detail, Spacer(1, 8 * mm), Table([["Tanggal pengecekan: ____ / ____ / ______", "Diperiksa oleh: ____________________________", "Mengetahui: ____________________________"]], colWidths=(90 * mm, 95 * mm, 90 * mm))])

    def footer(pdf, doc):
        pdf.saveState(); pdf.setFont("Helvetica", 7); pdf.setFillColor(colors.HexColor("#666666")); pdf.drawString(10 * mm, 7 * mm, "Vobia Space - Warehouse Inbound"); pdf.drawRightString(page_size[0] - 10 * mm, 7 * mm, f"Halaman {doc.page}"); pdf.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def build_inbound_filtered_receipts_pdf(*, receipts, query=""):
    receipts = list(receipts)
    output = BytesIO()
    page_size = landscape(A4)
    styles = getSampleStyleSheet()
    body = ParagraphStyle("ReceiptListBody", parent=styles["BodyText"], fontName="Helvetica", fontSize=7, leading=9)
    title = ParagraphStyle("ReceiptListTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=19, leading=22)
    right = ParagraphStyle("ReceiptListRight", parent=body, alignment=TA_RIGHT)
    rows = [["PO", "VENDOR", "TGL DITERIMA", "NO. DO", "SKU", "ARTICLE", "SIZE", "QTY", "WAREHOUSE", "DITERIMA OLEH"]]
    total = 0
    for receipt in receipts:
        sku = receipt.po_line.sku
        total += receipt.received_qty
        rows.append([Paragraph(receipt.po_line.po.po_number, body), Paragraph(receipt.po_line.po.supplier.name, body), receipt.inbound_date.strftime("%d/%m/%Y"), receipt.delivery_activity.delivery_order.number if receipt.delivery_activity_id and receipt.delivery_activity.delivery_order_id else "-", Paragraph(sku.sku, body), Paragraph(sku.product_variant.product.name, body), sku.size or "-", _qty(receipt.received_qty), Paragraph(receipt.warehouse.name, body), Paragraph(receipt.recorded_by.get_full_name() or receipt.recorded_by.username, body)])
    rows.append(["", "", "", "", "", "", "TOTAL", _qty(total), "", ""])
    document = SimpleDocTemplate(output, pagesize=page_size, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=16 * mm, bottomMargin=14 * mm, title="Pengiriman Sudah Diterima", author="Vobia Space")
    filter_label = query or "Semua PO dan artikel"
    story = [Table([[Paragraph("VOBIA SPACE", body), Paragraph("WAREHOUSE", right)], [Paragraph("PENGIRIMAN SUDAH DITERIMA", title), ""]], colWidths=(185 * mm, 80 * mm)), Paragraph(f"Filter: {filter_label}", body), Spacer(1, 6 * mm)]
    detail = Table(rows, colWidths=(33 * mm, 37 * mm, 23 * mm, 30 * mm, 30 * mm, 47 * mm, 14 * mm, 17 * mm, 30 * mm, 29 * mm), repeatRows=1)
    detail.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#171717")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 6.5), ("GRID", (0, 0), (-1, -2), 0.4, colors.HexColor("#c8c8c3")), ("ROWBACKGROUNDS", (0, 1), (-1, -2), (colors.white, colors.HexColor("#f4f4f1"))), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (6, 1), (7, -1), "RIGHT"), ("SPAN", (0, -1), (5, -1)), ("SPAN", (8, -1), (9, -1)), ("LINEABOVE", (6, -1), (7, -1), 1, colors.HexColor("#171717")), ("FONTNAME", (6, -1), (7, -1), "Helvetica-Bold"), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story.append(detail)

    def footer(pdf, doc):
        pdf.saveState(); pdf.setFont("Helvetica", 7); pdf.setFillColor(colors.HexColor("#666666")); pdf.drawString(12 * mm, 7 * mm, "Vobia Space - Warehouse Inbound"); pdf.drawRightString(page_size[0] - 12 * mm, 7 * mm, f"Halaman {doc.page}"); pdf.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
