from imj_util.image_prepare import (
    convert_gif_to_png,
    is_gif_image,
    prepare_image_for_upload,
    replace_filename_extension,
)


def test_is_gif_by_extension():
    assert is_gif_image(b"not gif", "page.gif", None)


def test_is_gif_by_magic():
    assert is_gif_image(b"GIF89a\xff", "image.bin", None)


def test_prepare_converts_gif_filename():
    png = convert_gif_to_png(_minimal_gif_bytes())
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    data, name, meta = prepare_image_for_upload(_minimal_gif_bytes(), "comic.gif", "image/gif")
    assert name == "comic.png"
    assert meta["converted_from_gif"] is True
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_prepare_skips_jpeg():
    data, name, meta = prepare_image_for_upload(b"\xff\xd8\xff", "photo.jpg", "image/jpeg")
    assert name == "photo.jpg"
    assert meta == {}


def test_replace_filename_extension():
    assert replace_filename_extension("dir/name.gif", ".png") == "name.png"


def _minimal_gif_bytes() -> bytes:
    from PIL import Image
    import io

    image = Image.new("RGB", (2, 2), color=(255, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="GIF")
    return buffer.getvalue()
