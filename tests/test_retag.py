import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# retag.py lives in scripts/ (outside the lattice package). It writes tags, so
# it is exercised against copies of the committed fixture files. The .flac case
# covers the shared Vorbis branch (.flac/.ogg/.opus); the .mp3 case locks in the
# v4.6.0 "deadbeef trap" fix. (.m4a/MP4 has no fixture and is not covered here.)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import retag
from mutagen.apev2 import APENoHeaderError, APEv2
from mutagen.id3 import ID3, TCON, TXXX, ID3NoHeaderError

FIXTURES = Path(__file__).parent / "fixtures" / "library"
FLAC_SRC = FIXTURES / "Aphex Twin" / "Selected Ambient Works" / "01 - Xtal.flac"
MP3_SRC = FIXTURES / "Cursive" / "Domestica" / "01 - The Casualty.mp3"


class ApplyGenresFlacTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "track.flac")
        shutil.copy(FLAC_SRC, self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_round_trip_single(self):
        self.assertTrue(retag.apply_genres(self.path, ["Ambient"]))
        self.assertEqual(retag.read_genres(self.path), ["Ambient"])

    def test_round_trip_multi(self):
        # Vorbis comments natively carry repeated keys.
        self.assertTrue(retag.apply_genres(self.path, ["Ambient", "IDM"]))
        self.assertEqual(retag.read_genres(self.path), ["Ambient", "IDM"])

    def test_overwrites_existing(self):
        retag.apply_genres(self.path, ["Old"])
        retag.apply_genres(self.path, ["New"])
        self.assertEqual(retag.read_genres(self.path), ["New"])


class ApplyGenresMp3Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "track.mp3")
        shutil.copy(MP3_SRC, self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def _id3(self) -> ID3:
        try:
            return ID3(self.path)
        except ID3NoHeaderError:
            return ID3()

    def test_round_trip_sets_tcon(self):
        self.assertTrue(retag.apply_genres(self.path, ["Trip Hop"]))
        self.assertEqual(retag.read_genres(self.path), ["Trip Hop"])
        self.assertEqual(self._id3()["TCON"].text, ["Trip Hop"])

    def test_clears_every_stale_genre_spot(self):
        # Seed the three override spots that survived the old EasyID3 path and
        # kept players like deadbeef showing a stale genre, plus one qualified
        # TXXX frame that must be preserved.
        ape = APEv2()  # a non-empty APE tag, so the save actually persists
        ape["Genre"] = "StaleApe"
        ape.save(self.path)

        tags = self._id3()
        tags.setall("TCON", [TCON(encoding=3, text=["StaleTcon"])])
        tags.add(TXXX(encoding=3, desc="GENRE", text=["StaleTxxx"]))
        tags.add(TXXX(encoding=3, desc="AB:GENRE", text=["KeepMe"]))
        tags.save(self.path, v1=2)

        self.assertTrue(retag.apply_genres(self.path, ["Clean Genre"]))

        self.assertEqual(retag.read_genres(self.path), ["Clean Genre"])
        with self.assertRaises(APENoHeaderError):
            APEv2(self.path)  # APEv2 tag fully removed
        tags = ID3(self.path)
        self.assertEqual(tags["TCON"].text, ["Clean Genre"])
        self.assertNotIn("TXXX:GENRE", tags)  # bare custom genre frame gone
        self.assertIn("TXXX:AB:GENRE", tags)  # qualified frame preserved

    def test_stale_id3v1_genre_defeats_noop(self):
        # TCON already matches, but the trailing ID3v1 genre byte disagrees —
        # one of the hidden spots the write exists to refresh. is_noop must
        # say a write is needed, and the write must converge to a real noop.
        tags = self._id3()
        tags.setall("TCON", [TCON(encoding=3, text=["Metal"])])
        tags.save(self.path, v2_version=3, v1=2)  # v1 genre byte = Metal
        data = bytearray(Path(self.path).read_bytes())
        self.assertEqual(data[-128:-125], b"TAG")
        data[-1] = 0  # genre byte 0 = Blues
        Path(self.path).write_bytes(data)

        self.assertFalse(retag.is_noop(self.path, ["Metal"]))
        self.assertTrue(retag.apply_genres(self.path, ["Metal"]))
        self.assertTrue(retag.is_noop(self.path, ["Metal"]))

    def test_absent_id3v1_stays_noop(self):
        # No v1 tag at all means nothing stale to clear; a matching TCON must
        # still be a noop (a write would only mint a v1 tag nobody asked for).
        tags = self._id3()
        tags.setall("TCON", [TCON(encoding=3, text=["Metal"])])
        tags.save(self.path, v2_version=3, v1=0)  # strip any ID3v1
        self.assertTrue(retag.is_noop(self.path, ["Metal"]))


class FailureReportingTests(unittest.TestCase):
    """M16: per-file failures go to stderr and main exits nonzero, so a caller
    (genre_tidy's apply) can tell a failed album from a retagged one."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        for dirpath, _dirs, files in os.walk(self.root):
            for f in files:
                os.chmod(os.path.join(dirpath, f), 0o644)
        self._tmp.cleanup()

    def _run_main(self, argv):
        import contextlib
        import io

        old = sys.argv
        sys.argv = ["retag.py", *argv]
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = retag.main()
        finally:
            sys.argv = old
        return rc, out.getvalue(), err.getvalue()

    def test_apply_failure_prints_to_stderr_and_returns_false(self):
        import contextlib
        import io

        p = os.path.join(self.root, "t.flac")
        shutil.copy(FLAC_SRC, p)
        os.chmod(p, 0o444)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ok = retag.apply_genres(p, ["X"])
        self.assertFalse(ok)
        self.assertIn("Failed to tag", err.getvalue())

    def test_main_exits_nonzero_when_a_file_fails(self):
        good = os.path.join(self.root, "a.flac")
        bad = os.path.join(self.root, "b.flac")
        shutil.copy(FLAC_SRC, good)
        shutil.copy(FLAC_SRC, bad)
        os.chmod(bad, 0o444)
        rc, out, err = self._run_main([self.root, "IDM"])
        self.assertEqual(rc, 1)
        self.assertIn("1 file(s) failed", out)
        self.assertIn("Failed to tag", err)
        self.assertEqual(retag.read_genres(good), ["IDM"])  # others still done

    def test_main_exits_zero_on_full_success(self):
        p = os.path.join(self.root, "a.flac")
        shutil.copy(FLAC_SRC, p)
        rc, _out, _err = self._run_main([self.root, "IDM"])
        self.assertEqual(rc, 0)

    def test_mixed_album_reports_unsupported_file(self):
        # M17: the unwritable sibling is named, not silently skipped.
        shutil.copy(FLAC_SRC, os.path.join(self.root, "01.flac"))
        Path(self.root, "02.wav").write_bytes(b"RIFFxxxx")
        rc, out, _err = self._run_main([self.root, "IDM", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("skip (unsupported): 02.wav", out)
        self.assertIn("would retag 01.flac", out)


class ApplyGenresGuardTests(unittest.TestCase):
    def test_unsupported_extension_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "notes.txt")
            Path(p).write_text("x")
            self.assertFalse(retag.apply_genres(p, ["Whatever"]))

    def test_read_genres_never_raises_on_garbage(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "broken.flac")
            Path(p).write_bytes(b"not audio")
            self.assertEqual(retag.read_genres(p), [])

    def test_unreadable_vorbis_family_reports_failure(self):
        # RT2: mutagen.File returning None used to fail *silently*; the
        # failure must print like every other failure path.
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "broken.flac")
            Path(p).write_bytes(b"not audio")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                ok = retag.apply_genres(p, ["X"])
            self.assertFalse(ok)
            self.assertIn("Failed to tag", err.getvalue())


class ReadGenresWmaTests(unittest.TestCase):
    """RT3: easy=True has no ASF wrapper, so .wma read back [] and dry-runs
    always showed the old genre as empty; WM/Genre is read directly now.
    (No WMA fixture exists, so the container is faked.)"""

    def test_wma_reads_wm_genre_attribute(self):
        from unittest import mock

        with mock.patch.object(retag, "ASF", return_value={"WM/Genre": ["Rock"]}):
            self.assertEqual(retag.read_genres("/x.wma"), ["Rock"])

    def test_wma_without_genre_reads_empty(self):
        from unittest import mock

        with mock.patch.object(retag, "ASF", return_value={}):
            self.assertEqual(retag.read_genres("/x.wma"), [])


class NoopGuardTests(unittest.TestCase):
    """RT1: a file already carrying exactly the target genre is not rewritten
    (no APEv2 delete, no v2.4->v2.3 re-save, no minted ID3v1), so direct use is
    byte-idempotent; the hidden MP3 genre spots still force the write."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.mp3 = os.path.join(self.root, "a.mp3")
        shutil.copy(MP3_SRC, self.mp3)

    def tearDown(self):
        self._tmp.cleanup()

    def _run_main(self, argv):
        import contextlib
        import io

        old = sys.argv
        sys.argv = ["retag.py", *argv]
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = retag.main()
        finally:
            sys.argv = old
        return rc, out.getvalue(), err.getvalue()

    def test_second_run_is_byte_identical(self):
        rc, out, _err = self._run_main([self.root, "Clean Genre"])
        self.assertEqual(rc, 0)
        self.assertIn("retagged a.mp3", out)
        before = Path(self.mp3).read_bytes()
        rc, out, _err = self._run_main([self.root, "Clean Genre"])
        self.assertEqual(rc, 0)
        self.assertIn("unchanged a.mp3", out)
        self.assertEqual(Path(self.mp3).read_bytes(), before)

    def test_matching_tcon_with_stray_ape_still_written(self):
        self._run_main([self.root, "Clean Genre"])
        ape = APEv2()
        ape["Genre"] = "StaleApe"
        ape.save(self.mp3)
        self.assertFalse(retag.is_noop(self.mp3, ["Clean Genre"]))
        rc, out, _err = self._run_main([self.root, "Clean Genre"])
        self.assertEqual(rc, 0)
        self.assertIn("retagged a.mp3", out)
        with self.assertRaises(APENoHeaderError):
            APEv2(self.mp3)

    def test_matching_tcon_with_bare_txxx_genre_still_written(self):
        self._run_main([self.root, "Clean Genre"])
        tags = ID3(self.mp3)
        tags.add(TXXX(encoding=3, desc="GENRE", text=["Stale"]))
        tags.save(self.mp3)
        self.assertFalse(retag.is_noop(self.mp3, ["Clean Genre"]))

    def test_dry_run_predicts_unchanged(self):
        self._run_main([self.root, "Clean Genre"])
        _rc, out, _err = self._run_main([self.root, "Clean Genre", "--dry-run"])
        self.assertIn("unchanged a.mp3", out)
        self.assertNotIn("would retag", out)


if __name__ == "__main__":
    unittest.main()


class StripJunkTests(unittest.TestCase):
    """--strip-junk: the convert-or-drop companion to --auditJunkFrames
    (P2-2, seeded by the real finding: three MP3s carrying five obsolete
    v2.3 frames each). The fixture MP3 is clean, so the junk frames are
    seeded raw (translate=False, exactly how the audit sees them)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "track.mp3")
        shutil.copy(MP3_SRC, self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def _seed(self):
        from mutagen.id3 import TALB, TDRC, TYER, TPE1

        tags = ID3(self.path, translate=False)
        tags.add(TYER(encoding=3, text="2000"))
        tags.add(TPE1(encoding=3, text="Artist"))
        tags.add(TALB(encoding=3, text="Album"))
        tags.add(TDRC(encoding=3, text="2000-01-01"))
        tags.save(self.path, v2_version=3)

    def _findings(self):
        from lattice.modes.audit import audit_id3_junk

        return audit_id3_junk(self.path)

    def test_clean_fixture_is_a_noop(self):
        changed, summary = retag.strip_junk_frames(self.path)
        self.assertFalse(changed)
        self.assertEqual(summary, "")

    def test_obsolete_frame_is_converted_and_saved(self):
        self._seed()
        self.assertIsNotNone(self._findings())  # the seed is really junky
        changed, summary = retag.strip_junk_frames(self.path)
        self.assertTrue(changed)
        self.assertIn("junk frame(s) removed", summary)
        after = ID3(self.path, translate=False)
        self.assertNotIn("TYER", after)  # folded into TDRC by update_to_v24
        self.assertEqual(after["TPE1"].text, ["Artist"])  # real frames survive

    def test_strip_is_idempotent(self):
        self._seed()
        self.assertTrue(retag.strip_junk_frames(self.path)[0])
        changed, _ = retag.strip_junk_frames(self.path)
        self.assertFalse(changed)
        self.assertIsNone(self._findings())

    def _run_main(self, argv):
        import contextlib
        import io

        old = sys.argv
        sys.argv = ["retag.py", *argv]
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = retag.main()
        finally:
            sys.argv = old
        return rc, out.getvalue(), err.getvalue()

    def test_main_strip_junk_refuses_genre_arguments(self):
        with self.assertRaises(SystemExit):
            self._run_main([os.path.dirname(self.path), "--strip-junk", "Some Genre"])

    def test_main_strip_junk_runs_and_exits_zero(self):
        self._seed()
        rc, _out, _err = self._run_main([os.path.dirname(self.path), "--strip-junk"])
        self.assertEqual(rc, 0)
        self.assertIsNone(self._findings())
