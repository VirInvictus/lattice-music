import contextlib
import io
import os
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from lattice.modes.artwork import (
    _get_image_size,
    compare_art,
    run_art_mismatch_audit,
    run_art_quality_audit,
    run_extract_art,
)


def _png(width, height):
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">L", 13)
        + b"IHDR"
        + struct.pack(">LL", width, height)
    )


def _jpeg(width, height, pad=0):
    # SOI, an optional APP1 padding segment (stands in for a large EXIF/ICC
    # block pushing SOF deep into the file), then a SOF0 (baseline) segment:
    # marker, length, precision, height, width — the shape _get_image_size
    # walks looking for SOFn.
    app1 = b""
    if pad:
        app1 = b"\xff\xe1" + struct.pack(">H", pad + 2) + b"\x00" * pad
    return (
        b"\xff\xd8"
        + app1
        + b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">H", height)
        + struct.pack(">H", width)
    )


class GetImageSizeTests(unittest.TestCase):
    def test_png_dimensions(self):
        self.assertEqual(_get_image_size(_png(800, 600)), (800, 600))

    def test_jpeg_dimensions(self):
        self.assertEqual(_get_image_size(_jpeg(800, 600)), (800, 600))

    def test_jpeg_sof_after_large_app1_segment(self):
        self.assertEqual(_get_image_size(_jpeg(800, 600, pad=10 * 1024)), (800, 600))

    def test_non_image_returns_none(self):
        self.assertIsNone(_get_image_size(b"not an image at all"))

    def test_too_short_returns_none(self):
        self.assertIsNone(_get_image_size(b"\x89PNG"))


class ArtQualityAuditTests(unittest.TestCase):
    def test_low_res_cover_with_deep_sof_is_flagged(self):
        # Regression: the folder-art read was capped at 8 KB, so a cover whose
        # SOF sat past a big APP1 block parsed as None and was never flagged.
        with tempfile.TemporaryDirectory() as tmp:
            album = os.path.join(tmp, "Artist", "Album")
            os.makedirs(album)
            open(os.path.join(album, "track.mp3"), "wb").close()
            with open(os.path.join(album, "cover.jpg"), "wb") as f:
                f.write(_jpeg(300, 300, pad=10 * 1024))
            report = os.path.join(tmp, "report.txt")
            rc = run_art_quality_audit(tmp, report, 500, quiet=True)
            self.assertEqual(rc, 0)
            with open(report, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("Below floor: 1", text)
            self.assertIn("300x300", text)


class ExtractArtDryRunTests(unittest.TestCase):
    """Dry-run over a temp album whose MP3 carries a synthetic embedded cover."""

    def _album_with_embedded_art(self, tmp: str) -> str:
        from mutagen.id3 import APIC, ID3

        fixture = str(
            Path(__file__).parent
            / "fixtures"
            / "library"
            / "Cursive"
            / "Domestica"
            / "01 - The Casualty.mp3"
        )
        album = os.path.join(tmp, "Artist", "Album")
        os.makedirs(album)
        track = os.path.join(album, "track.mp3")
        shutil.copy2(fixture, track)
        id3 = ID3(track)
        id3.add(
            APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,
                desc="Cover",
                data=_jpeg(600, 600),
            )
        )
        id3.save(track)
        return album

    def test_quiet_suppresses_dry_run_lines_and_nothing_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            album = self._album_with_embedded_art(tmp)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                run_extract_art(tmp, quiet=True, dry_run=True)
            self.assertEqual(out.getvalue(), "")
            self.assertFalse(os.path.exists(os.path.join(album, "cover.jpg")))

    def test_dry_run_still_announces_when_not_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            album = self._album_with_embedded_art(tmp)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                run_extract_art(tmp, quiet=False, dry_run=True)
            self.assertIn("[dry-run] Would extract art", out.getvalue())
            self.assertFalse(os.path.exists(os.path.join(album, "cover.jpg")))


class CompareArtTests(unittest.TestCase):
    def _folder(self, td, data, name="cover.jpg"):
        p = Path(td) / name
        p.write_bytes(data)
        return str(p)

    def test_match_same_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            data = _png(400, 400)
            path = self._folder(td, data)
            self.assertEqual(compare_art(data, path), "MATCH")

    def test_same_pixels_different_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._folder(td, _png(400, 400) + b"trailing")
            self.assertEqual(compare_art(_png(400, 400), path), "SAME PIXELS")

    def test_different_image(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._folder(td, _png(400, 400))
            self.assertEqual(compare_art(_png(500, 500), path), "DIFFERENT IMAGE")

    def test_unreadable_dimensions(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._folder(td, b"not-an-image")
            self.assertEqual(compare_art(_png(500, 500), path), "UNREADABLE")

    def test_missing_half_is_irrelevant(self):
        self.assertEqual(compare_art(None, "/x/cover.jpg"), "IRRELEVANT")
        self.assertEqual(compare_art(b"data", None), "IRRELEVANT")


class ArtMismatchAuditTests(unittest.TestCase):
    def test_report_buckets(self):
        with tempfile.TemporaryDirectory() as td:
            # Matched album: the folder cover is byte-identical to the
            # embedded art.
            good = Path(td) / "Good Album"
            good.mkdir()
            good_track = str(good / "01.mp3")
            shutil.copy2(
                Path(__file__).parent
                / "fixtures"
                / "library"
                / "Cursive"
                / "Domestica"
                / "01 - The Casualty.mp3",
                good_track,
            )
            from mutagen.id3 import APIC, ID3

            id3 = ID3(good_track)
            art = _jpeg(600, 600)
            id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=art))
            id3.save(good_track)
            (good / "cover.jpg").write_bytes(art)

            # Drifted album: a real but different image on disk.
            drift = Path(td) / "Drift Album"
            drift.mkdir()
            drift_track = str(drift / "01.mp3")
            shutil.copy2(
                Path(__file__).parent
                / "fixtures"
                / "library"
                / "Cursive"
                / "Domestica"
                / "01 - The Casualty.mp3",
                drift_track,
            )
            id3 = ID3(drift_track)
            id3.add(
                APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,
                    desc="Cover",
                    data=_jpeg(600, 600),
                )
            )
            id3.save(drift_track)
            (drift / "cover.jpg").write_bytes(_jpeg(300, 300))

            out = Path(td) / "mismatch.txt"
            rc = run_art_mismatch_audit([td], str(out), verbose=True, quiet=True)
            self.assertEqual(rc, 0)
            report = out.read_text(encoding="utf-8")
            self.assertIn("ART MISMATCH AUDIT REPORT", report)
            self.assertIn("DIFFERENT IMAGE (1)", report)
            self.assertIn("embedded 600x600", report)
            self.assertIn("300x300 cover.jpg", report)
            self.assertIn("MATCHED (1)", report)


if __name__ == "__main__":
    unittest.main()
