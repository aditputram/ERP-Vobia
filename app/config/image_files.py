from hashlib import sha256
from io import BytesIO
from pathlib import Path

from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError


class ImageProcessingError(ValueError):
    pass


def optimized_upload(uploaded, *, max_edge=1600, max_pixels=40_000_000, quality=82):
    """Return a display-ready WebP while keeping upload validation in the form."""
    try:
        with Image.open(uploaded) as source:
            if source.width * source.height > max_pixels:
                raise ImageProcessingError("Resolusi gambar terlalu besar untuk diproses.")
            image = ImageOps.exif_transpose(source)
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            has_alpha = image.mode in {"RGBA", "LA"} or "transparency" in image.info
            image = image.convert("RGBA" if has_alpha else "RGB")
            output = BytesIO()
            image.save(output, "WEBP", quality=quality, method=4)
    except ImageProcessingError:
        raise
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ImageProcessingError("Gambar tidak dapat diproses.") from exc
    finally:
        uploaded.seek(0)
    return ContentFile(output.getvalue(), name=f"{Path(uploaded.name).stem[:220]}.webp")


def card_image(field, *, namespace, object_id, max_edge=960, quality=78):
    """Open a persistent card-size derivative, creating it once for legacy originals."""
    storage = field.storage
    digest = sha256(field.name.encode("utf-8")).hexdigest()[:16]
    derivative_name = f"derived/{namespace}/{object_id}-{digest}-w{max_edge}.webp"
    if storage.exists(derivative_name):
        return storage.open(derivative_name, "rb"), derivative_name

    try:
        with field.open("rb") as source_file:
            with Image.open(source_file) as source:
                image = ImageOps.exif_transpose(source)
                image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                has_alpha = image.mode in {"RGBA", "LA"} or "transparency" in image.info
                image = image.convert("RGBA" if has_alpha else "RGB")
                output = BytesIO()
                image.save(output, "WEBP", quality=quality, method=4)
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        return storage.open(field.name, "rb"), field.name

    saved_name = storage.save(derivative_name, ContentFile(output.getvalue()))
    return storage.open(saved_name, "rb"), saved_name
