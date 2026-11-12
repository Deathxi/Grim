"""Strip identifying metadata from images Grim reuploads for someone else.

PNG, JPEG, GIF and WebP are cleaned by rewriting the container and keeping only
the chunks needed to draw the picture, so pixels are never re-compressed. The
one exception is a still image whose EXIF orientation is not upright: it has to
be rotated, so it is re-encoded (PNG and WebP losslessly, JPEG at quality 95).
"""

from __future__ import annotations

import struct
from io import BytesIO

from PIL import Image, ImageOps

FORMAT_EXTENSIONS = {"PNG": ".png", "JPEG": ".jpg", "GIF": ".gif", "WEBP": ".webp"}

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_KEEP = {
    b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS",
    b"gAMA", b"cHRM", b"sRGB", b"iCCP", b"sBIT", b"cICP",
    b"acTL", b"fcTL", b"fdAT",
}

GIF_KEEP_APPS = {b"NETSCAPE2.0", b"ANIMEXTS1.0"}

WEBP_KEEP = {b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ANIM", b"ANMF", b"ICCP"}
WEBP_FLAG_XMP = 0x04
WEBP_FLAG_EXIF = 0x08


class ImageCleanError(Exception):
    """The image could not be cleaned. Nothing unclean is ever returned."""


def _png(data: bytes) -> bytes:
    if not data.startswith(PNG_SIGNATURE):
        raise ImageCleanError("not a PNG")
    out = [PNG_SIGNATURE]
    pos = len(PNG_SIGNATURE)
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        end = pos + 12 + length
        if end > len(data):
            raise ImageCleanError("truncated PNG")
        if kind in PNG_KEEP:
            out.append(data[pos:end])
        pos = end
        if kind == b"IEND":
            return b"".join(out)
    raise ImageCleanError("PNG has no IEND")


def _jpeg(data: bytes) -> bytes:
    if data[:2] != b"\xff\xd8":
        raise ImageCleanError("not a JPEG")
    out = [b"\xff\xd8"]
    pos = 2
    size = len(data)
    while pos + 2 <= size:
        if data[pos] != 0xFF:
            raise ImageCleanError("bad JPEG marker")
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker == 0xD9:
            out.append(b"\xff\xd9")
            return b"".join(out)
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if pos + 4 > size:
            raise ImageCleanError("truncated JPEG")
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        end = pos + 2 + length
        if length < 2 or end > size:
            raise ImageCleanError("truncated JPEG")
        payload = data[pos + 4:end]
        keep = False
        if marker in (0xDB, 0xC4, 0xDD) or (0xC0 <= marker <= 0xCF and marker not in (0xC8, 0xCC)):
            keep = True
        elif marker == 0xDA:
            keep = True
        elif marker == 0xE0:
            keep = payload.startswith(b"JFIF\x00")
        elif marker == 0xE2:
            keep = payload.startswith(b"ICC_PROFILE\x00")
        elif marker == 0xEE:
            keep = payload.startswith(b"Adobe")
        if keep:
            out.append(data[pos:end])
        pos = end
        if marker == 0xDA:
            start = pos
            while pos + 1 < size:
                if data[pos] == 0xFF and data[pos + 1] != 0x00 and not 0xD0 <= data[pos + 1] <= 0xD7:
                    break
                pos += 1
            out.append(data[start:pos])
    raise ImageCleanError("JPEG has no end marker")


def _skip_sub_blocks(data: bytes, pos: int) -> int:
    while True:
        if pos >= len(data):
            raise ImageCleanError("truncated GIF")
        length = data[pos]
        pos += 1 + length
        if length == 0:
            return pos


def _gif(data: bytes) -> bytes:
    if data[:6] not in (b"GIF87a", b"GIF89a") or len(data) < 13:
        raise ImageCleanError("not a GIF")
    flags = data[10]
    pos = 13
    if flags & 0x80:
        pos += 3 * (2 << (flags & 0x07))
    out = [data[:pos]]
    while pos < len(data):
        block = data[pos]
        if block == 0x3B:
            out.append(b"\x3b")
            return b"".join(out)
        if block == 0x21:
            label = data[pos + 1]
            end = _skip_sub_blocks(data, pos + 2)
            keep = label == 0xF9
            if label == 0xFF:
                ident_len = data[pos + 2] if pos + 2 < len(data) else 0
                keep = ident_len == 11 and data[pos + 3:pos + 14] in GIF_KEEP_APPS
            if keep:
                out.append(data[pos:end])
            pos = end
        elif block == 0x2C:
            if pos + 10 > len(data):
                raise ImageCleanError("truncated GIF")
            local = data[pos + 9]
            end = pos + 10
            if local & 0x80:
                end += 3 * (2 << (local & 0x07))
            end = _skip_sub_blocks(data, end + 1)
            out.append(data[pos:end])
            pos = end
        else:
            raise ImageCleanError("bad GIF block")
    raise ImageCleanError("GIF has no trailer")


def _webp(data: bytes) -> bytes:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ImageCleanError("not a WebP")
    chunks = []
    pos = 12
    while pos + 8 <= len(data):
        kind = data[pos:pos + 4]
        length = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        end = pos + 8 + length
        if end > len(data):
            raise ImageCleanError("truncated WebP")
        payload = data[pos + 8:end]
        if kind in WEBP_KEEP:
            if kind == b"VP8X" and len(payload) >= 1:
                payload = bytes([payload[0] & ~(WEBP_FLAG_EXIF | WEBP_FLAG_XMP)]) + payload[1:]
            chunk = kind + struct.pack("<I", len(payload)) + payload
            chunks.append(chunk + (b"\x00" if len(payload) % 2 else b""))
        pos = end + (length % 2)
    body = b"WEBP" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


_STRIPPERS = {"PNG": _png, "JPEG": _jpeg, "GIF": _gif, "WEBP": _webp}


def _needs_rotation(picture: Image.Image) -> bool:
    if getattr(picture, "is_animated", False):
        return False
    try:
        return int(picture.getexif().get(0x0112, 1)) not in (0, 1)
    except Exception:
        return False


def _reencode_upright(data: bytes, image_format: str) -> bytes:
    with Image.open(BytesIO(data)) as picture:
        profile = picture.info.get("icc_profile")
        upright = ImageOps.exif_transpose(picture)
        upright.load()
    options: dict = {}
    if profile:
        options["icc_profile"] = profile
    if image_format == "JPEG":
        options.update(quality=95)
    elif image_format == "WEBP":
        options.update(lossless=True, exact=True)
    out = BytesIO()
    upright.save(out, format=image_format, **options)
    return out.getvalue()


def metadata_left(data: bytes) -> list[str]:
    """What Pillow still sees as metadata. Empty means clean."""
    found = []
    with Image.open(BytesIO(data)) as picture:
        if len(picture.getexif()):
            found.append("exif")
        for key in ("exif", "xmp", "comment", "photoshop", "XML:com.adobe.xmp", "DateTime"):
            if picture.info.get(key):
                found.append(key)
        if picture.format == "PNG":
            found.extend(f"png:{key}" for key in getattr(picture, "text", {}) or {})
    return found


def clean_image(data: bytes) -> tuple[bytes, str]:
    """The one door every image Grim reuploads goes through. Returns (bytes, extension)."""
    try:
        with Image.open(BytesIO(data)) as picture:
            picture.verify()
            image_format = picture.format
    except Exception as error:
        raise ImageCleanError("not an image") from error
    if image_format not in FORMAT_EXTENSIONS:
        raise ImageCleanError("unsupported format")
    return strip_image_metadata(data, image_format), FORMAT_EXTENSIONS[image_format]


def strip_image_metadata(data: bytes, image_format: str) -> bytes:
    """Return the image without EXIF, GPS, XMP, IPTC, comments, or timestamps."""
    stripper = _STRIPPERS.get(image_format)
    if stripper is None:
        raise ImageCleanError("unsupported format")
    try:
        with Image.open(BytesIO(data)) as picture:
            rotate = _needs_rotation(picture)
        cleaned = _reencode_upright(data, image_format) if rotate else data
        cleaned = stripper(cleaned)
        with Image.open(BytesIO(cleaned)) as check:
            check.verify()
        if metadata_left(cleaned):
            raise ImageCleanError("metadata survived")
    except ImageCleanError:
        raise
    except Exception as error:
        raise ImageCleanError(type(error).__name__) from error
    return cleaned
