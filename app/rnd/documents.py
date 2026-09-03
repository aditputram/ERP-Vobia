from io import BytesIO
from pathlib import Path

from PIL import Image
from django.core.exceptions import ValidationError
from django.utils import timezone
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


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
    if not APPROVAL_STAMP_PATH.exists():
        raise ValidationError("Cap approval Vobia tidak tersedia.")

    width = float(page.mediabox.width)
    height = float(page.mediabox.height)
    scale_x = width / 841.89
    scale_y = height / 595.276

    with Image.open(APPROVAL_STAMP_PATH) as source:
        stamp = source.convert("RGBA")
        alpha_box = stamp.getchannel("A").getbbox()
        if alpha_box:
            stamp = stamp.crop(alpha_box)
        stamp_reader = ImageReader(stamp)

        overlay = BytesIO()
        pdf = canvas.Canvas(overlay, pagesize=(width, height), pageCompression=1)
        pdf.scale(scale_x, scale_y)
        pdf.setFillColorRGB(0, 0, 0)
        pdf.setFont("Helvetica-Bold", 7)
        pdf.drawCentredString(777, 509, approved_by[:28])
        pdf.drawImage(stamp_reader, 756, 494, width=42, height=42, mask="auto")
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
        page[NameObject("/Contents")] = ArrayObject((page.raw_get("/Contents"), stamp_reference))
    else:
        page[NameObject("/Contents")] = stamp_reference

    if approved_at:
        _add_approval_mark(page, approved_by=approved_by)


def build_combined_document(*, product, submitted_at=None, approved_at=None, approved_by=""):
    if not product.mockup or not product.technical_drawing:
        raise ValidationError("MDR dan Technical Drawing wajib tersedia sebelum Submit Approval.")

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

    output = BytesIO()
    writer.write(output)
    return output.getvalue()
