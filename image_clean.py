"""Strip identifying metadata from images Grim reuploads for someone else.

PNG, JPEG, GIF and WebP are cleaned by rewriting the container and keeping only
the chunks needed to draw the picture, so pixels are never re-compressed. The
one exception is a still image whose EXIF orientation is not upright: it has to
be rotated, so it is re-encoded (PNG and WebP losslessly, JPEG at quality 95).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
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


METADATA_ORDER = ("GPS", "device", "timestamp", "author", "software", "camera settings", "XMP", "IPTC", "comments", "orientation", "other")
METADATA_DETAIL = {
    "GPS": "GPS / location coordinates",
    "device": "Device make, model, or serial number",
    "timestamp": "Date and time taken or edited",
    "author": "Author, owner, copyright, or description",
    "software": "Editing software",
    "camera settings": "Camera settings (exposure, lens, and similar)",
    "XMP": "XMP data (edit history, author, and similar)",
    "IPTC": "IPTC / Photoshop data (captions, credits, and similar)",
    "comments": "Embedded comments or text",
    "orientation": "Rotation flag",
    "other": "Extra data hidden in the file",
}

_EXIF_DEVICE = {0x010F, 0x0110, 0xA431, 0xA432, 0xA433, 0xA434, 0xA435, 0xA430, 0xC62F}
_EXIF_TIME = {0x0132, 0x9003, 0x9004, 0x9290, 0x9291, 0x9292, 0xA003}
_EXIF_AUTHOR = {0x013B, 0x8298, 0x010E, 0x9286, 0x9C9B, 0x9C9C, 0x9C9D, 0x9C9E, 0x9C9F}
_EXIF_SOFTWARE = {0x0131, 0x013C}
_EXIF_STRUCTURAL = {0x8769, 0x8825, 0xA005, 0x0112, 0x0100, 0x0101, 0x011A, 0x011B, 0x0128, 0x0213}


def _exif_labels(exif: Image.Exif) -> set[str]:
    found: set[str] = set()
    tags: dict = dict(exif)
    try:
        tags.update(exif.get_ifd(0x8769))
    except Exception:
        pass
    try:
        gps = exif.get_ifd(0x8825)
    except Exception:
        gps = {}
    if gps:
        found.add("GPS")
    for tag in tags:
        if tag in _EXIF_DEVICE:
            found.add("device")
        elif tag in _EXIF_TIME:
            found.add("timestamp")
        elif tag in _EXIF_AUTHOR:
            found.add("author")
        elif tag in _EXIF_SOFTWARE:
            found.add("software")
        elif tag == 0x0112:
            found.add("orientation")
        elif tag not in _EXIF_STRUCTURAL:
            found.add("camera settings")
    return found


def _exif_from_bytes(payload: bytes) -> set[str]:
    try:
        exif = Image.Exif()
        exif.load(payload)
        return _exif_labels(exif) or {"other"}
    except Exception:
        return {"other"}


def _ordered(found: set[str]) -> list[str]:
    return [label for label in METADATA_ORDER if label in found]


def _inspect_png(data: bytes) -> set[str]:
    found: set[str] = set()
    pos = len(PNG_SIGNATURE)
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        end = pos + 12 + length
        payload = data[pos + 8:pos + 8 + length]
        if kind == b"tIME":
            found.add("timestamp")
        elif kind == b"eXIf":
            found |= _exif_from_bytes(payload)
        elif kind in (b"tEXt", b"zTXt", b"iTXt"):
            keyword = payload.split(b"\x00", 1)[0].decode("latin-1").lower()
            if keyword in ("author", "copyright"):
                found.add("author")
            elif keyword in ("creation time", "date:create", "date:modify"):
                found.add("timestamp")
            elif keyword == "software":
                found.add("software")
            elif keyword.startswith("xml:com.adobe.xmp"):
                found.add("XMP")
            else:
                found.add("comments")
        pos = end
        if kind == b"IEND":
            if data[pos:].strip(b"\x00"):
                found.add("other")
            break
    return found


def _inspect_jpeg(data: bytes) -> set[str]:
    found: set[str] = set()
    pos, size = 2, len(data)
    while pos + 2 <= size:
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker == 0xD9:
            if data[pos + 2:].strip(b"\x00"):
                found.add("other")
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if pos + 4 > size:
            break
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        end = pos + 2 + length
        payload = data[pos + 4:end]
        if marker == 0xE1 and payload.startswith(b"Exif\x00"):
            found |= _exif_from_bytes(payload)
        elif marker == 0xE1 and payload.startswith(b"http://ns.adobe.com/xap"):
            found.add("XMP")
        elif marker == 0xED:
            found.add("IPTC")
        elif marker == 0xFE:
            found.add("comments")
        elif 0xE0 <= marker <= 0xEF and not (
            (marker == 0xE0 and payload.startswith(b"JFIF\x00"))
            or (marker == 0xE2 and payload.startswith(b"ICC_PROFILE\x00"))
            or (marker == 0xEE and payload.startswith(b"Adobe"))
        ):
            found.add("other")
        pos = end
        if marker == 0xDA:
            while pos + 1 < size:
                if data[pos] == 0xFF and data[pos + 1] != 0x00 and not 0xD0 <= data[pos + 1] <= 0xD7:
                    break
                pos += 1
    return found


def _inspect_gif(data: bytes) -> set[str]:
    found: set[str] = set()
    flags = data[10]
    pos = 13
    if flags & 0x80:
        pos += 3 * (2 << (flags & 0x07))
    while pos < len(data):
        block = data[pos]
        if block == 0x3B:
            if data[pos + 1:].strip(b"\x00"):
                found.add("other")
            break
        if block == 0x21:
            label = data[pos + 1]
            end = _skip_sub_blocks(data, pos + 2)
            if label == 0xFE or label == 0x01:
                found.add("comments")
            elif label == 0xFF:
                ident = data[pos + 3:pos + 14]
                if ident.startswith(b"XMP Data"):
                    found.add("XMP")
                elif ident not in GIF_KEEP_APPS:
                    found.add("other")
            pos = end
        elif block == 0x2C:
            local = data[pos + 9]
            end = pos + 10
            if local & 0x80:
                end += 3 * (2 << (local & 0x07))
            pos = _skip_sub_blocks(data, end + 1)
        else:
            break
    return found


def _inspect_webp(data: bytes) -> set[str]:
    found: set[str] = set()
    pos = 12
    while pos + 8 <= len(data):
        kind = data[pos:pos + 4]
        length = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        payload = data[pos + 8:pos + 8 + length]
        if kind == b"EXIF":
            found |= _exif_from_bytes(payload)
        elif kind == b"XMP ":
            found.add("XMP")
        elif kind not in WEBP_KEEP:
            found.add("other")
        pos += 8 + length + (length % 2)
    return found


_INSPECTORS = {"PNG": _inspect_png, "JPEG": _inspect_jpeg, "GIF": _inspect_gif, "WEBP": _inspect_webp}


def inspect_metadata(data: bytes, image_format: str) -> list[str]:
    """Short labels for the identifying metadata in the file, e.g. ["GPS", "device"]."""
    inspector = _INSPECTORS.get(image_format)
    if inspector is None:
        return []
    try:
        return _ordered(inspector(data))
    except Exception:
        return ["other"]


@dataclass(frozen=True)
class CleanedImage:
    data: bytes
    extension: str
    removed: tuple[str, ...]


def clean_image_report(data: bytes) -> CleanedImage:
    """Clean an image and report which kinds of metadata it carried."""
    try:
        with Image.open(BytesIO(data)) as picture:
            picture.verify()
            image_format = picture.format
    except Exception as error:
        raise ImageCleanError("not an image") from error
    if image_format not in FORMAT_EXTENSIONS:
        raise ImageCleanError("unsupported format")
    removed = tuple(inspect_metadata(data, image_format))
    cleaned = strip_image_metadata(data, image_format)
    return CleanedImage(cleaned, FORMAT_EXTENSIONS[image_format], removed)


def clean_image(data: bytes) -> tuple[bytes, str]:
    """The one door every image Grim reuploads goes through. Returns (bytes, extension)."""
    result = clean_image_report(data)
    return result.data, result.extension


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
