from io import BytesIO

from mosaic_api.integrations.graph import DirectoryPhoto
from PIL import Image


def photo_image(format: str = "PNG", *, size: tuple[int, int] = (96, 96)) -> DirectoryPhoto:
    output = BytesIO()
    with Image.new("RGB", size, (30, 100, 170)) as image:
        image.save(output, format=format)
    return DirectoryPhoto(
        content=output.getvalue(),
        content_type="image/jpeg" if format == "JPEG" else f"image/{format.lower()}",
    )


def corrupted_png() -> bytes:
    content = bytearray(photo_image().content)
    content[content.index(b"IDAT") + 4] ^= 1
    return bytes(content)
