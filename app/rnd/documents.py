from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image
from django.core.exceptions import ValidationError
from django.utils import timezone
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle


APPROVAL_STAMP_PATH = Path(__file__).resolve().parent / "assets" / "approval-stamp.png"


def _source_reader(field):
    field.open("rb")
    try:
        source = BytesIO(field.read())
    finally:
        field.close()

    if source.getvalue().startswith(b"%PDF"):
        try:
            return PdfReader(source)
        except Exception as exc:
            raise ValidationError(f"PDF {field.name.rsplit('/', 1)[-1]} tidak dapat dibaca.") from exc

    try:
        image = Image.open(source)
        converted = BytesIO()
        image.convert("RGB").save(converted, "PDF", resolution=150)
        converted.seek(0)
        return PdfReader(converted)
    except Exception as exc:
        raise ValidationError(f"Gambar {field.name.rsplit('/', 1)[-1]} tidak dapat dibaca.") from exc


def _pdf_literal(value):
    encoded = str(value).encode("latin-1", errors="replace")
    encoded = bytes(character if character >= 32 else ord("?") for character in encoded)
    return encoded.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _date_label(value):
    return timezone.localtime(value).strftime("%d %b %Y").upper()


def _approval_stamp_reader():
    if not APPROVAL_STAMP_PATH.exists():
        raise ValidationError("Cap approval Vobia tidak tersedia.")
    with Image.open(APPROVAL_STAMP_PATH) as source:
        stamp = source.convert("RGBA")
        alpha_box = stamp.getchannel("A").getbbox()
        return ImageReader(stamp.crop(alpha_box) if alpha_box else stamp)


def _add_font(resources, resource_name, base_font):
    fonts = resources.get("/Font")
    if fonts is None:
        fonts = DictionaryObject()
        resources[NameObject("/Font")] = fonts
    else:
        fonts = fonts.get_object()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject(base_font),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        }
    )
    fonts[NameObject(resource_name)] = font


def _text_command(font, size, x, y, value):
    return b"BT " + font.encode("ascii") + f" {size:g} Tf 1 0 0 1 {x:g} {y:g} Tm (".encode() + _pdf_literal(value) + b") Tj ET\n"


def _add_approval_mark(page, *, approved_by):
    width = float(page.mediabox.width)
    height = float(page.mediabox.height)
    scale_x = width / 841.89
    scale_y = height / 595.276

    overlay = BytesIO()
    pdf = canvas.Canvas(overlay, pagesize=(width, height), pageCompression=1)
    pdf.scale(scale_x, scale_y)
    pdf.setFillColorRGB(0, 0, 0)
    pdf.setFont("Helvetica-Bold", 7)
    pdf.drawCentredString(777, 509, approved_by[:28])
    pdf.drawImage(_approval_stamp_reader(), 756, 494, width=42, height=42, mask="auto")
    pdf.save()

    overlay.seek(0)
    page.merge_page(PdfReader(overlay).pages[0], over=True)


def _stamp_page(page, *, writer, submitted_at, revision, approved_at=None, approved_by=""):
    width = float(page.mediabox.width)
    height = float(page.mediabox.height)
    scale_x = width / 841.89
    scale_y = height / 595.276
    resources = page.get("/Resources")
    if resources is None:
        resources = DictionaryObject()
        page[NameObject("/Resources")] = resources
    else:
        resources = resources.get_object()
    _add_font(resources, "/VobiaStampBold", "/Helvetica-Bold")
    _add_font(resources, "/VobiaStampRegular", "/Helvetica")

    commands = [
        b"q\n0 g\n" + f"{scale_x:g} 0 0 {scale_y:g} 0 0 cm\n".encode(),
        _text_command("/VobiaStampBold", 8, 637, 537, _date_label(submitted_at)),
        _text_command("/VobiaStampBold", 8, 637, 519, f"{revision:03d}"),
    ]

    if approved_at:
        commands.append(_text_command("/VobiaStampBold", 8, 637, 501, _date_label(approved_at)))

    commands.append(b"Q\n")
    stamp = DecodedStreamObject()
    stamp.set_data(b"".join(commands))
    stamp_reference = writer._add_object(stamp)
    if "/Contents" in page:
        existing_contents = page.raw_get("/Contents")
        resolved_contents = existing_contents.get_object()
        if isinstance(resolved_contents, ArrayObject):
            page[NameObject("/Contents")] = ArrayObject((*resolved_contents, stamp_reference))
        else:
            page[NameObject("/Contents")] = ArrayObject((existing_contents, stamp_reference))
    else:
        page[NameObject("/Contents")] = stamp_reference

    if approved_at:
        _add_approval_mark(page, approved_by=approved_by)


def _bom_reader(*, product, submitted_at=None, approved_at=None, approved_by=""):
    page_size = (841.89, 595.276)
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=page_size,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=46 * mm,
        bottomMargin=16 * mm,
    )
    body_style = ParagraphStyle(
        "BomBody",
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#151515"),
    )
    header_style = ParagraphStyle(
        "BomHeader",
        parent=body_style,
        fontName="Helvetica-Bold",
        textColor=colors.white,
        alignment=TA_CENTER,
    )
    table_data = [
        [
            Paragraph("MATERIAL", header_style),
            Paragraph("KEBUTUHAN", header_style),
            Paragraph("EOM / SATUAN", header_style),
        ]
    ]
    materials = list(product.materials.all())
    if materials:
        table_data.extend(
            [
                Paragraph(escape(material.material), body_style),
                Paragraph(f"{material.requirement:.1f}".replace(".", ","), body_style),
                Paragraph(escape(material.eom), body_style),
            ]
            for material in materials
        )
    else:
        table_data.append(
            [
                Paragraph("Belum ada material", body_style),
                Paragraph("-", body_style),
                Paragraph("-", body_style),
            ]
        )

    table = Table(
        table_data,
        colWidths=(151 * mm, 45 * mm, 65 * mm),
        repeatRows=1,
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.black),
                ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#1d1d1d")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), (colors.white, colors.HexColor("#f3f3f0"))),
            ]
        )
    )

    def draw_header(pdf, _document):
        width, height = page_size
        pdf.saveState()
        pdf.setFillColor(colors.black)
        pdf.rect(18 * mm, height - 16 * mm, width - 36 * mm, 10 * mm, stroke=0, fill=1)
        pdf.setFillColor(colors.white)
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(22 * mm, height - 12.5 * mm, "BILL OF MATERIAL")
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawRightString(width - 22 * mm, height - 12.2 * mm, "VOBIA")

        pdf.setFillColor(colors.HexColor("#151515"))
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(18 * mm, height - 24 * mm, "COLLECTION")
        pdf.drawString(18 * mm, height - 31 * mm, "PRODUCT")
        pdf.setFont("Helvetica", 8)
        pdf.drawString(42 * mm, height - 24 * mm, product.collection.name[:45])
        pdf.drawString(42 * mm, height - 31 * mm, product.name[:45])

        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(118 * mm, height - 24 * mm, "SUBMITTED DATE")
        pdf.drawString(118 * mm, height - 31 * mm, "REV")
        pdf.drawString(177 * mm, height - 24 * mm, "APPROVAL DATE")
        pdf.setFont("Helvetica", 8)
        if submitted_at:
            pdf.drawString(148 * mm, height - 24 * mm, _date_label(submitted_at))
            pdf.drawString(148 * mm, height - 31 * mm, f"{product.document_revision:03d}")
        if approved_at:
            pdf.drawString(207 * mm, height - 24 * mm, _date_label(approved_at))
            pdf.setFont("Helvetica-Bold", 7)
            pdf.drawCentredString(264 * mm, height - 31 * mm, approved_by[:28])
            pdf.drawImage(
                _approval_stamp_reader(),
                256 * mm,
                height - 38 * mm,
                width=16 * mm,
                height=16 * mm,
                mask="auto",
            )
        pdf.restoreState()

    document.build([table], onFirstPage=draw_header, onLaterPages=draw_header)
    output.seek(0)
    return PdfReader(output)


def build_combined_document(*, product, submitted_at=None, approved_at=None, approved_by=""):
    if not product.mockup or not product.technical_drawing:
        raise ValidationError("Mockup dan Technical Drawing wajib tersedia sebelum Submit Approval.")

    readers = [_source_reader(field) for field in (product.mockup, product.technical_drawing)]
    writer = PdfWriter()
    for reader in readers:
        if not reader.pages:
            raise ValidationError("Dokumen yang di-upload tidak memiliki halaman.")
        for index, page in enumerate(reader.pages):
            writer.add_page(page)
            if index == 0 and submitted_at:
                _stamp_page(
                    writer.pages[-1],
                    writer=writer,
                    submitted_at=submitted_at,
                    revision=product.document_revision,
                    approved_at=approved_at,
                    approved_by=approved_by,
                )

    bom_reader = _bom_reader(
        product=product,
        submitted_at=submitted_at,
        approved_at=approved_at,
        approved_by=approved_by,
    )
    for page in bom_reader.pages:
        writer.add_page(page)

    output = BytesIO()
    writer.write(output)
    return output.getvalue()
