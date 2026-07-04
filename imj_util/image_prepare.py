from __future__ import annotations

import io
from pathlib import PurePosixPath

from PIL import Image

GIF_MAGIC = (b"GIF87a", b"GIF89a")


def prepare_image_for_upload(
    image_bytes: bytes,
    filename: str,
    content_type: str | None = None,
) -> tuple[bytes, str, dict]:
    """Convert unsupported GIF to PNG for GigaChat file upload."""
    if not is_gif_image(image_bytes, filename, content_type):
        return image_bytes, filename, {}

    png_bytes = convert_gif_to_png(image_bytes)
    return (
        png_bytes,
        replace_filename_extension(filename, ".png"),
        {
            "converted_from_gif": True,
            "original_filename": filename,
            "original_size_bytes": len(image_bytes),
            "converted_size_bytes": len(png_bytes),
        },
    )


def is_gif_image(image_bytes: bytes, filename: str, content_type: str | None = None) -> bool:
    if content_type and "gif" in content_type.lower():
        return True
    if filename.lower().endswith(".gif"):
        return True
    return len(image_bytes) >= 6 and image_bytes[:6] in GIF_MAGIC


def convert_gif_to_png(image_bytes: bytes) -> bytes:
    with Image.open(io.BytesIO(image_bytes)) as image:
        if getattr(image, "n_frames", 1) > 1:
            image.seek(0)
        if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
            frame = image.convert("RGBA")
        else:
            frame = image.convert("RGB")
        buffer = io.BytesIO()
        frame.save(buffer, format="PNG")
        return buffer.getvalue()


def replace_filename_extension(filename: str, new_extension: str) -> str:
    suffix = new_extension if new_extension.startswith(".") else f".{new_extension}"
    stem = PurePosixPath(filename).stem or "image"
    return f"{stem}{suffix}"
