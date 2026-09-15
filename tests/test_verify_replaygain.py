"""Tests for --verifyReplayGain: the rsgain -O scan parser, the promoted
ReplayGain value reader, the bucket math, and the mode's run contract with
rsgain mocked (the integrity modes' house split: the subprocess engine is
never invoked; the brain around it is)."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import lattice.modes.audit as audit_mod
from lattice.modes.audit import (
    parse_rsgain_scan,
    rsgain_verify_command,
    run_verify_replaygain,
    _verify_bucket,
)
from lattice.tags import read_replaygain_values

FIXTURE = Path(__file__).parent / "fixtures" / "library"
FLAC_SRC = FIXTURE / "Aphex Twin" / "Selected Ambient Works" / "01 - Xtal.flac"

# A real-shaped rsgain custom -l -18 -a -O -s s page (columns per the 09-13
# probe; tabs between cells).
_TSV = (
    "Filename\tLoudness (LUFS)\tGain (dB)\tPeak\tPeak (dB)\tPeak Type\t"
    "Clipping Adjustment?\n"
    "01.flac\t-27.09\t9.09\t0.062500\t-24.08\tSample\tN\n"
    "02.flac\t-26.50\t8.50\t0.062500\t-24.08\tSample\tY\n"
    "Album\t-27.09\t9.09\t0.062500\t-24.08\tSample\tN\n"
)


class RsgainOutputParserTests(unittest.TestCase):
    def test_parses_files_and_album_row(self):
        rows, album = parse_rsgain_scan(_TSV)
        self.assertEqual(set(rows), {"01.flac", "02.flac"})
        self.assertAlmostEqual(rows["01.flac"]["loudness"], -27.09)
        self.assertFalse(rows["01.flac"]["clip"])
        self.assertTrue(rows["02.flac"]["clip"])
        self.assertIsNotNone(album)
        self.assertAlmostEqual(album["loudness"], -27.09)
        self.assertFalse(album["clip"])

    def test_no_album_row(self):
        text = "\n".join(_TSV.splitlines()[:-1])
        rows, album = parse_rsgain_scan(text)
        self.assertEqual(len(rows), 2)
        self.assertIsNone(album)

    def test_garbage_text_yields_nothing(self):
        rows, album = parse_rsgain_scan("rsgain: error: file list is not valid\n")
        self.assertEqual(rows, {})
        self.assertIsNone(album)

    def test_unparseable_row_is_skipped(self):
        text = _TSV.replace("01.flac\t-27.09", "01.flac\t[loudness]")
        rows, _album = parse_rsgain_scan(text)
        self.assertEqual(set(rows), {"02.flac"})


class VerifyBucketTests(unittest.TestCase):
    def test_within_tolerance_is_ok(self):
        # stored 9.09, measured -27.09 at target -18: expected 9.09, exact.
        self.assertEqual(_verify_bucket(9.09, -27.09, -18.0, 0.5), "OK")

    def test_tolerance_edge_is_ok(self):
        # Exactly at the tolerance is still OK; a hair over is OFF.
        self.assertEqual(_verify_bucket(9.59, -27.09, -18.0, 0.5), "OK")
        self.assertEqual(_verify_bucket(9.60, -27.09, -18.0, 0.5), "OFF")

    def test_missing_value_is_ungauged(self):
        self.assertEqual(_verify_bucket(None, -27.09, -18.0, 0.5), "UNGAUGED")


class ReadReplayGainValuesTests(unittest.TestCase):
    def test_vorbis_strings(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "t.flac")
            shutil.copy(FLAC_SRC, p)
            from mutagen.flac import FLAC

            f = FLAC(p)
            f["replaygain_track_gain"] = "-5.00 dB"
            f["replaygain_album_gain"] = "-4.00 dB"
            f.save()
            self.assertEqual(read_replaygain_values(p), (-5.0, -4.0))

    def test_r128_integer_is_q78(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "t.flac")
            shutil.copy(FLAC_SRC, p)
            from mutagen.flac import FLAC

            f = FLAC(p)
            # 2327 / 256 == 9.0898... dB; the opus write convention read
            # through a Vorbis container for the unit.
            f["r128_track_gain"] = "2327"
            f["r128_album_gain"] = "-1792"
            f.save()
            track, album = read_replaygain_values(p)
            self.assertAlmostEqual(track, 2327 / 256.0)
            self.assertAlmostEqual(album, -7.0)

    def test_untagged_is_none_pair(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "t.flac")
            shutil.copy(FLAC_SRC, p)
            self.assertEqual(read_replaygain_values(p), (None, None))

    def test_unreadable_file_is_none_pair(self):
        self.assertEqual(read_replaygain_values("/no/such/file.flac"), (None, None))


class CommandTests(unittest.TestCase):
    def test_scan_only_command_shape(self):
        cmd = rsgain_verify_command(["/m/Album/01.flac", "/m/Album/02.flac"], -18.0)
        self.assertEqual(
            cmd,
            [
                "rsgain",
                "custom",
                "-l",
                "-18.0",
                "-a",
                "-O",
                "-q",
                "-s",
                "s",
                "/m/Album/01.flac",
                "/m/Album/02.flac",
            ],
        )


class RunVerifyReplayGainTests(unittest.TestCase):
    """The mode end to end with rsgain's output canned: the tags on disk say
    one thing, the canned measurement another, and the report must say which
    rows are off, ungauged, clip-adjusted, and OK."""

    def _album(self, td: str) -> list[Path]:
        files = []
        for name in ("01.flac", "02.flac", "03.flac"):
            p = Path(td) / "Album" / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"")
            files.append(p)
        return files

    def _run(self, td, out, stored_map, tsv=_TSV, **kw):
        def values(path):
            return stored_map.get(str(path), (None, None))

        def run_proc(cmd, capture_output, text):
            assert cmd[:2] == ["rsgain", "custom"]
            # -s s is the contract: the command must never carry a write mode.
            assert "-s" in cmd and "s" == cmd[cmd.index("-s") + 1]
            return mock.Mock(returncode=0, stdout=tsv, stderr="")

        with (
            mock.patch.object(audit_mod, "read_replaygain_values", side_effect=values),
            mock.patch.object(
                audit_mod.shutil, "which", return_value="/usr/bin/rsgain"
            ),
            mock.patch.object(audit_mod.subprocess, "run", side_effect=run_proc),
        ):
            return run_verify_replaygain([td], str(out), quiet=True, **kw)

    def test_off_ungauged_and_clip_land_in_their_sections(self):
        tsv = (
            "Filename\tLoudness (LUFS)\tGain (dB)\tPeak\tPeak (dB)\tPeak Type\t"
            "Clipping Adjustment?\n"
            "01.flac\t-27.09\t9.09\t0.062500\t-24.08\tSample\tN\n"
            "02.flac\t-26.50\t8.50\t0.062500\t-24.08\tSample\tY\n"
            "03.flac\t-27.00\t9.00\t0.062500\t-24.08\tSample\tN\n"
            "Album\t-27.09\t9.09\t0.062500\t-24.08\tSample\tN\n"
        )
        with tempfile.TemporaryDirectory() as td:
            files = self._album(td)
            out = Path(td) / "verify.txt"
            # 01: exact match at -18 -> fine. 02: clip-adjusted (exempt from
            # the comparison, reported separately). 03: no track gain tag.
            # The album gain (9.09 from file 01) matches the album row.
            stored = {
                str(files[0]): (9.09, 9.09),
                str(files[1]): (3.0, 9.09),
                str(files[2]): (None, 9.09),
            }
            rc = self._run(td, out, stored, tsv=tsv)
            self.assertEqual(rc, 0)
            report = out.read_text(encoding="utf-8")
            self.assertIn("REPLAYGAIN VERIFICATION REPORT", report)
            self.assertIn("Target: -18.0 LUFS", report)
            self.assertIn("UNTAGGED / NOT MEASURED (1)", report)
            self.assertIn("(no track gain tag)", report)
            self.assertIn("CLIP-ADJUSTED (reported, never OFF) (1)", report)
            # Nothing is actually wrong, so the OFF section stays out.
            self.assertNotIn("GAIN OFF BY", report)
            self.assertIn("OK: 0", report)

    def test_wrong_gain_is_reported_with_numbers(self):
        tsv = _TSV  # 01 measured -27.09 -> expected gain 9.09 at -18
        with tempfile.TemporaryDirectory() as td:
            files = self._album(td)
            out = Path(td) / "verify.txt"
            stored = {
                str(files[0]): (3.09, 9.09),
                str(files[1]): (8.50, 9.09),
                str(files[2]): (9.00, 9.09),
            }
            self._run(td, out, stored, tsv=tsv)
            report = out.read_text(encoding="utf-8")
            self.assertIn("GAIN OFF BY > 0.5 dB", report)
            self.assertIn("stored +3.09 dB vs expected +9.09 dB", report)
            self.assertIn("off by -6.00 dB", report)

    def test_all_correct_album_is_ok_and_verbose_lists_it(self):
        tsv = (
            "Filename\tLoudness (LUFS)\tGain (dB)\tClipping Adjustment?\n"
            "01.flac\t-27.09\t9.09\tN\n"
            "02.flac\t-26.50\t8.50\tN\n"
            "03.flac\t-27.00\t9.00\tN\n"
            "Album\t-27.09\t9.09\tN\n"
        )
        with tempfile.TemporaryDirectory() as td:
            files = self._album(td)
            out = Path(td) / "verify.txt"
            stored = {
                str(p): (9.09 if i == 0 else 8.5 if i == 1 else 9.0, 9.09)
                for i, p in enumerate(files)
            }
            rc = self._run(td, out, stored, tsv=tsv, verbose=True)
            self.assertEqual(rc, 0)
            report = out.read_text(encoding="utf-8")
            self.assertIn("OK: 1", report)
            self.assertIn("OK (1)", report)
            self.assertIn("Album", report)

    def test_missing_rsgain_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "verify.txt"
            with mock.patch.object(audit_mod.shutil, "which", return_value=None):
                rc = run_verify_replaygain([td], str(out), quiet=True)
            self.assertEqual(rc, 2)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
