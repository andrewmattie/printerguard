"""Decode bounded JPEGs in a worker; never return decoder error messages."""
from io import BytesIO

MAX_DIMENSION = 4096
MAX_PIXELS = 12_000_000


def verify_jpeg(content: bytes) -> str | None:
    """Return a fixed rejection reason, or None after a complete JPEG decode.

    The caller enforces the encoded-byte limit before dispatching this work.
    Pillow is provided by the supported Home Assistant Core installation.
    Do not change Pillow's process-wide limits or truncated-image setting.
    """
    from PIL import Image

    try:
        with Image.open(BytesIO(content), formats=['JPEG']) as image:
            width, height = image.size
            pixels = width * height
            pillow_limit = Image.MAX_IMAGE_PIXELS
            if pillow_limit is not None and pixels > pillow_limit:
                return 'frame_pixel_limit_exceeded'
            if width <= 0 or height <= 0 or max(width, height) > MAX_DIMENSION:
                return 'frame_dimensions_exceeded'
            if pixels > MAX_PIXELS:
                return 'frame_pixel_limit_exceeded'
            image.verify()
        # verify() does not decode JPEG entropy data. Reopen and load it too.
        with Image.open(BytesIO(content), formats=['JPEG']) as image:
            image.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        return 'frame_pixel_limit_exceeded'
    except (OSError, ValueError, SyntaxError):
        return 'invalid_jpeg_data'
    return None
