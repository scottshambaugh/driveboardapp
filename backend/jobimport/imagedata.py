"""Helpers for the base64 image data carried by raster job defs."""

import base64
import io
import logging

try:
    from PIL import Image
except ImportError:
    Image = None

log = logging.getLogger("svg_reader")

# What a browser will accept in an <img>, which is how the frontend previews a
# raster. Pillow reads far more than this, so anything else is re-encoded to
# PNG on import. Without that the preview silently never loads, even though the
# backend would have engraved the image just fine.
WEB_FORMATS = frozenset({"PNG", "JPEG", "GIF", "BMP", "WEBP", "ICO"})

# modes PNG cannot store, so they need flattening to RGB first
UNSAVEABLE_MODES = frozenset({"CMYK", "YCbCr", "LAB", "HSV", "F", "I"})


def normalize_image_data(data_uri):
    """Re-encode a raster to PNG unless it is already in a displayable format.

    Returns the data URI unchanged when it needs no conversion, and also when
    it cannot be read at all, so that an unreadable image fails later in the
    pipeline where it is reported rather than here.
    """
    if Image is None or not data_uri:
        return data_uri
    try:
        payload = data_uri.split(",", 1)[-1]
        img = Image.open(io.BytesIO(base64.b64decode(payload)))
        source_format = img.format
        if source_format in WEB_FORMATS:
            return data_uri

        if img.mode in UNSAVEABLE_MODES:
            img = img.convert("RGB")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        log.info(f"re-encoded embedded {source_format} image as PNG so it can be displayed")
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("utf-8")
    except Exception as e:
        log.warning(f"Failed to normalize image data: {e}")
        return data_uri
