import struct
import unittest
import zlib
from io import BytesIO

from PIL import Image, ImageChops, ImageOps

from image_clean import (
    ImageCleanError,
    clean_image,
    clean_image_report,
    inspect_metadata,
    metadata_left,
    strip_image_metadata,
)

MAKE = "SecretCam"
MODEL = "Model-XYZ-9000"
STAMP = "2026:01:02 03:04:05"
XMP = (
    b'<?xpacket begin="\xef\xbb\xbf"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
    b"<rdf:RDF><rdf:Description creator=\"Noct Private\"/></rdf:RDF></x:xmpmeta><?xpacket end=\"w\"?>"
)
LEAKS = (
    b"Exif", b"GPS", MAKE.encode(), MODEL.encode(), b"2026:01:02", b"Noct Private",
    b"adobe:ns:meta", b"Taken at home", b"secret-comment", b"Author",
)


def sample_image(size=(8, 4), mode="RGB"):
    picture = Image.new(mode, size)
    pixels = picture.load()
    for x in range(size[0]):
        for y in range(size[1]):
            value = (x * 30 % 256, y * 60 % 256, (x + y) * 20 % 256)
            pixels[x, y] = value if mode == "RGB" else value + (200,)
    return picture


def exif_with_gps(orientation=None):
    exif = Image.Exif()
    exif[0x010F] = MAKE
    exif[0x0110] = MODEL
    exif[0x0132] = STAMP
    exif[0x013B] = "Author"
    exif[0x9003] = STAMP
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (40.0, 44.0, 54.0)
    gps[3] = "W"
    gps[4] = (73.0, 59.0, 9.0)
    if orientation:
        exif[0x0112] = orientation
    return exif


def encode(picture, image_format, **options):
    out = BytesIO()
    picture.save(out, format=image_format, **options)
    return out.getvalue()


def png_chunk(kind, payload):
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def add_png_chunks(data, chunks):
    index = data.rindex(b"\x00\x00\x00\x00IEND")
    return data[:index] + b"".join(chunks) + data[index:]


def dirty_png(orientation=None, image=None):
    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    info.add_text("Comment", "Taken at home")
    info.add_itxt("Description", "secret-comment", lang="en")
    data = encode(image or sample_image(), "PNG", pnginfo=info, exif=exif_with_gps(orientation))
    return add_png_chunks(data, [png_chunk(b"tIME", struct.pack(">HBBBBB", 2026, 1, 2, 3, 4, 5))])


def dirty_jpeg(orientation=None, image=None):
    return encode(
        image or sample_image(),
        "JPEG",
        quality=90,
        exif=exif_with_gps(orientation),
        comment=b"secret-comment",
        xmp=XMP,
    )


def dirty_gif(animated=False):
    frames = [sample_image((8, 8)).convert("P"), sample_image((8, 8)).transpose(Image.Transpose.ROTATE_90).convert("P")]
    options = dict(comment=b"secret-comment")
    if animated:
        options.update(save_all=True, append_images=frames[1:], duration=[70, 130], loop=3)
    data = encode(frames[0], "GIF", **options)
    xmp_ext = b"\x21\xff\x0bXMP DataXMP" + bytes([len(XMP[:255])]) + XMP[:255] + b"\x00"
    return data[:-1] + xmp_ext + b"\x3b"


def dirty_webp(animated=False, orientation=None, lossless=True):
    frames = [sample_image(), sample_image().transpose(Image.Transpose.ROTATE_180)]
    options = dict(
        lossless=lossless,
        quality=100 if lossless else 80,
        exif=exif_with_gps(orientation),
        xmp=XMP,
    )
    if animated:
        options.update(save_all=True, append_images=frames[1:], duration=[60, 90], loop=2)
    return encode(frames[0], "WEBP", **options)


def pixels(data, frame=0):
    with Image.open(BytesIO(data)) as picture:
        picture.seek(frame)
        return picture.convert("RGBA").copy()


def same_pixels(a, b):
    return ImageChops.difference(a, b).getbbox() is None


class StripMetadataTests(unittest.TestCase):
    def assertClean(self, data):
        for leak in LEAKS:
            self.assertNotIn(leak, data)
        self.assertEqual(metadata_left(data), [])
        with Image.open(BytesIO(data)) as picture:
            self.assertEqual(len(picture.getexif()), 0)

    def test_fixtures_really_carry_metadata(self):
        for data in (dirty_png(), dirty_jpeg(), dirty_gif(), dirty_webp()):
            self.assertTrue(any(leak in data for leak in LEAKS))
        with Image.open(BytesIO(dirty_jpeg())) as picture:
            gps = picture.getexif().get_ifd(0x8825)
            self.assertEqual(gps[1], "N")
            self.assertEqual(picture.getexif()[0x010F], MAKE)

    def test_png_loses_exif_text_and_time_but_keeps_every_pixel(self):
        original = dirty_png()
        cleaned = strip_image_metadata(original, "PNG")
        self.assertClean(cleaned)
        self.assertNotIn(b"tIME", cleaned)
        self.assertNotIn(b"eXIf", cleaned)
        self.assertNotIn(b"tEXt", cleaned)
        self.assertNotIn(b"iTXt", cleaned)
        self.assertTrue(same_pixels(pixels(original), pixels(cleaned)))

    def test_png_with_transparency_and_palette_keeps_pixels(self):
        rgba = dirty_png(image=sample_image(mode="RGBA"))
        self.assertTrue(same_pixels(pixels(rgba), pixels(strip_image_metadata(rgba, "PNG"))))
        palette = encode(sample_image().convert("P"), "PNG", exif=exif_with_gps())
        cleaned = strip_image_metadata(palette, "PNG")
        self.assertClean(cleaned)
        self.assertTrue(same_pixels(pixels(palette), pixels(cleaned)))

    def test_jpeg_loses_exif_gps_xmp_and_comment_without_recompressing(self):
        original = dirty_jpeg()
        cleaned = strip_image_metadata(original, "JPEG")
        self.assertClean(cleaned)
        self.assertNotIn(b"\xff\xe1", cleaned[:200])
        self.assertTrue(same_pixels(pixels(original), pixels(cleaned)))
        self.assertLess(len(cleaned), len(original))

    def test_jpeg_keeps_its_colour_profile(self):
        from PIL import ImageCms

        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        original = encode(sample_image(), "JPEG", exif=exif_with_gps(), icc_profile=profile)
        cleaned = strip_image_metadata(original, "JPEG")
        self.assertClean(cleaned)
        with Image.open(BytesIO(cleaned)) as picture:
            self.assertEqual(picture.info.get("icc_profile"), profile)

    def test_gif_loses_comment_and_xmp_but_keeps_frames_timing_and_loop(self):
        original = dirty_gif(animated=True)
        cleaned = strip_image_metadata(original, "GIF")
        self.assertClean(cleaned)
        with Image.open(BytesIO(original)) as before, Image.open(BytesIO(cleaned)) as after:
            self.assertEqual(after.n_frames, before.n_frames)
            self.assertEqual(after.info.get("loop"), before.info.get("loop"))
            for index in range(before.n_frames):
                before.seek(index)
                after.seek(index)
                self.assertEqual(after.info["duration"], before.info["duration"])
                self.assertTrue(same_pixels(before.convert("RGBA"), after.convert("RGBA")))
        still = strip_image_metadata(dirty_gif(), "GIF")
        self.assertClean(still)

    def test_webp_loses_exif_and_xmp_for_still_and_animated(self):
        for animated in (False, True):
            original = dirty_webp(animated=animated)
            cleaned = strip_image_metadata(original, "WEBP")
            self.assertClean(cleaned)
            self.assertNotIn(b"EXIF", cleaned)
            self.assertNotIn(b"XMP ", cleaned)
            with Image.open(BytesIO(original)) as before, Image.open(BytesIO(cleaned)) as after:
                self.assertEqual(getattr(after, "n_frames", 1), getattr(before, "n_frames", 1))
                self.assertEqual(after.size, before.size)
            for frame in range(2 if animated else 1):
                self.assertTrue(same_pixels(pixels(original, frame), pixels(cleaned, frame)))

    def test_lossy_webp_is_not_recompressed(self):
        original = dirty_webp(lossless=False)
        cleaned = strip_image_metadata(original, "WEBP")
        self.assertClean(cleaned)
        self.assertTrue(same_pixels(pixels(original), pixels(cleaned)))

    def test_orientation_is_applied_before_the_tag_is_dropped(self):
        upright_source = sample_image((8, 4))
        cases = {
            "JPEG": dirty_jpeg(orientation=6, image=upright_source),
            "PNG": dirty_png(orientation=6, image=upright_source),
            "WEBP": dirty_webp(orientation=6),
        }
        for image_format, data in cases.items():
            with Image.open(BytesIO(data)) as picture:
                expected = ImageOps.exif_transpose(picture).convert("RGBA")
            self.assertEqual(expected.size, (4, 8))
            cleaned = strip_image_metadata(data, image_format)
            self.assertClean(cleaned)
            result = pixels(cleaned)
            self.assertEqual(result.size, (4, 8))
            if image_format == "JPEG":
                diff = ImageChops.difference(result, expected).convert("L")
                self.assertLess(diff.getextrema()[1], 40)
            else:
                self.assertTrue(same_pixels(result, expected))

    def test_clean_image_detects_format_and_rejects_the_rest(self):
        cleaned, extension = clean_image(dirty_jpeg())
        self.assertEqual(extension, ".jpg")
        self.assertClean(cleaned)
        for data in (b"", b"not an image", dirty_png()[:40]):
            with self.assertRaises(ImageCleanError):
                clean_image(data)
        bmp = encode(sample_image(), "BMP")
        with self.assertRaises(ImageCleanError):
            clean_image(bmp)

    def test_trailing_bytes_after_the_image_are_dropped(self):
        for data, image_format in ((dirty_png(), "PNG"), (dirty_jpeg(), "JPEG"), (dirty_gif(), "GIF")):
            padded = data + b"APPENDED-SECRET-PAYLOAD"
            self.assertNotIn(b"APPENDED-SECRET", strip_image_metadata(padded, image_format))

    def test_a_clean_image_stays_valid_when_cleaned_twice(self):
        for data, image_format in ((dirty_png(), "PNG"), (dirty_jpeg(), "JPEG"), (dirty_gif(), "GIF"), (dirty_webp(), "WEBP")):
            once = strip_image_metadata(data, image_format)
            self.assertEqual(strip_image_metadata(once, image_format), once)

    def test_inspect_names_what_the_file_carries(self):
        jpeg = inspect_metadata(dirty_jpeg(), "JPEG")
        for label in ("GPS", "device", "timestamp", "author", "XMP", "comments"):
            self.assertIn(label, jpeg)
        self.assertEqual(jpeg[:3], ["GPS", "device", "timestamp"])
        self.assertIn("timestamp", inspect_metadata(dirty_png(), "PNG"))
        self.assertIn("comments", inspect_metadata(dirty_png(), "PNG"))
        self.assertIn("GPS", inspect_metadata(dirty_png(), "PNG"))
        self.assertIn("XMP", inspect_metadata(dirty_gif(), "GIF"))
        self.assertIn("comments", inspect_metadata(dirty_gif(), "GIF"))
        webp = inspect_metadata(dirty_webp(), "WEBP")
        self.assertIn("GPS", webp)
        self.assertIn("XMP", webp)
        self.assertIn("other", inspect_metadata(dirty_png() + b"tail-bytes", "PNG"))

    def test_clean_files_report_nothing_and_cleaned_files_report_nothing(self):
        for data, image_format in ((dirty_png(), "PNG"), (dirty_jpeg(), "JPEG"), (dirty_gif(), "GIF"), (dirty_webp(), "WEBP")):
            cleaned = strip_image_metadata(data, image_format)
            self.assertEqual(inspect_metadata(cleaned, image_format), [])
            report = clean_image_report(data)
            self.assertTrue(report.removed)
            self.assertEqual(clean_image_report(cleaned).removed, ())
        plain = encode(sample_image(), "PNG")
        self.assertEqual(clean_image_report(plain).removed, ())

    def test_orientation_only_is_reported_as_orientation(self):
        exif = Image.Exif()
        exif[0x0112] = 6
        data = encode(sample_image(), "JPEG", exif=exif)
        self.assertEqual(inspect_metadata(data, "JPEG"), ["orientation"])


if __name__ == "__main__":
    unittest.main()
