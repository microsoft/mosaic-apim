from dataclasses import dataclass
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from mosaic_api.errors import DirectoryError

PHOTO_MAX_BYTES = 64 * 1024
PHOTO_MAX_EDGE = 256


@dataclass(frozen=True)
class DirectoryPhoto:
    content: bytes
    content_type: str


def validate_photo(content: bytes, content_type: str) -> DirectoryPhoto:
    """Decode only small static JPEG/PNG images; never trust the upstream MIME type alone."""

    media_type = content_type.partition(";")[0].strip().lower()
    formats = {"image/jpeg": "JPEG", "image/png": "PNG"}
    if media_type not in formats or not content or len(content) > PHOTO_MAX_BYTES:
        raise DirectoryError("Microsoft Graph returned an invalid or oversized photo")
    try:
        with Image.open(BytesIO(content), formats=[formats[media_type]]) as image:
            if (
                image.format != formats[media_type]
                or not all(0 < edge <= PHOTO_MAX_EDGE for edge in image.size)
                or getattr(image, "is_animated", False)
            ):
                raise ValueError("Unsupported photo dimensions or format")
            image.verify()
        # JPEG verification alone does not decode the pixels or detect a truncated scan.
        with Image.open(BytesIO(content), formats=[formats[media_type]]) as image:
            image.load()
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
    ) as exc:
        raise DirectoryError("Microsoft Graph returned an invalid or oversized photo") from exc
    return DirectoryPhoto(content=content, content_type=media_type)
