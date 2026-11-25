"""Pictures of inventory items, made ready to store (``INVENTORY_PLAN.md``,
"Pictures").

:func:`prepare` takes the bytes of an uploaded image (a phone photo, a
product image from a supplier's page) and returns what the database keeps: a
JPEG no larger than :data:`MAX_SIDE` on its longer side and a thumbnail no
larger than :data:`THUMB_SIDE`. The picture is turned upright by its EXIF
orientation first, and the metadata is not kept (a phone photo's EXIF holds
where it was taken). Transparent areas become white.

Needs Pillow (the ``inventory`` or ``web`` extra).
"""
from __future__ import annotations

import io
from dataclasses import dataclass

MAX_UPLOAD = 30 * 1024 * 1024   # bytes of one upload, before it is read as an image
MAX_PIXELS = 60_000_000         # a 48 MP phone photo is about 48 million
MAX_SIDE = 1600
THUMB_SIDE = 320
QUALITY = 85
THUMB_QUALITY = 80
CONTENT_TYPE = "image/jpeg"


class PictureError(ValueError):
    """An upload that is not a picture, or one too large to take."""


@dataclass(frozen=True)
class Prepared:
    data: bytes
    thumb: bytes
    width: int
    height: int
    content_type: str = CONTENT_TYPE


def _jpeg(image, quality: int) -> bytes:
    out = io.BytesIO()
    image.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
    return out.getvalue()


def prepare(raw: bytes) -> Prepared:
    """The stored form of the image *raw*; :class:`PictureError` when it is
    empty, too large, or not an image Pillow reads."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not raw:
        raise PictureError("the picture is empty")
    if len(raw) > MAX_UPLOAD:
        raise PictureError(f"the picture is larger than {MAX_UPLOAD // (1024 * 1024)} MB")
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.width * source.height > MAX_PIXELS:
                raise PictureError(f"the picture is too large ({source.width} x {source.height})")
            source.load()
            image = ImageOps.exif_transpose(source)
    except PictureError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise PictureError(f"not a picture this app can read ({exc.__class__.__name__})") from exc
    if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P"
                                             and "transparency" in image.info):
        image = image.convert("RGBA")
        flat = Image.new("RGB", image.size, "white")
        flat.paste(image, mask=image.getchannel("A"))
        image = flat
    elif image.mode != "RGB":
        image = image.convert("RGB")
    image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    thumb = image.copy()
    thumb.thumbnail((THUMB_SIDE, THUMB_SIDE), Image.Resampling.LANCZOS)
    return Prepared(data=_jpeg(image, QUALITY), thumb=_jpeg(thumb, THUMB_QUALITY),
                    width=image.width, height=image.height)
