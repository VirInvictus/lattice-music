import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# replaygain.py lives in scripts/ (outside the lattice package); it shells out to
# rsgain to write ReplayGain tags. The pure helpers are tested directly and the
# rsgain call is mocked, so these tests never invoke rsgain or mutate a library.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import replaygain
import lattice.tags as tags_mod
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TXXX

from lattice.config import AUDIO_EXTENSIONS as AUDIO_EXTS

FIXTURE = Path(__file__).parent / "fixtures" / "library"
MP3_SRC = FIXTURE / "Cursive" / "Domestica" / "01 - The Casualty.mp3"
FLAC_SRC = FIXTURE / "Aphex Twin" / "Selected Ambient Works" / "01 - Xtal.flac"


class CoverageLabelTests(unittest.TestCase):
    def test_none(self):
        self.assertEqual(replaygain.coverage_label(0, 0, 5), "none")

    def test_partial(self):
        self.assertEqual(replaygain.coverage_label(3, 0, 5), "partial")

    def test_no_album_gain(self):
        self.assertEqual(replaygain.coverage_label(5, 0, 5), "no-album-gain")

    def test_no_album_gain_when_album_incomplete(self):
        self.assertEqual(replaygain.coverage_label(5, 3, 5), "no-album-gain")

    def test_ok(self):
        self.assertEqual(replaygain.coverage_label(5, 5, 5), "ok")


class FindAlbumDirsTests(unittest.TestCase):
    def test_finds_audio_dirs_and_prunes_hidden(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Album A").mkdir()
            (root / "Album A" / "01.mp3").write_bytes(b"x")
            (root / "Album A" / "cover.jpg").write_bytes(b"x")  # not audio
            (root / "Empty").mkdir()  # no audio -> excluded
            (root / ".hidden").mkdir()
            (root / ".hidden" / "01.flac").write_bytes(b"x")  # pruned

            found = replaygain.find_album_dirs(str(root), AUDIO_EXTS)

            names = {os.path.basename(d) for d, _ in found}
            self.assertEqual(names, {"Album A"})
            (_dir, files) = found[0]
            self.assertEqual(files, ["01.mp3"])


class AlbumCoverageTests(unittest.TestCase):
    def test_aggregates_from_reader(self):
        # Fake reader: first two files fully tagged, third bare.
        def fake_read(path):
            n = os.path.basename(path)
            full = n in ("a", "b")
            return mock.Mock(has_track_gain=full, has_album_gain=full)

        n_total, n_track, n_album, label = replaygain.album_coverage(
            fake_read, "/x", ["a", "b", "c"]
        )
        self.assertEqual((n_total, n_track, n_album), (3, 2, 2))
        self.assertEqual(label, "partial")


class ReadGainStringsTests(unittest.TestCase):
    def test_mp3_txxx(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "t.mp3")
            shutil.copy(MP3_SRC, p)
            tags = ID3(p)
            tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text=["-6.66 dB"]))
            tags.add(TXXX(encoding=3, desc="REPLAYGAIN_ALBUM_GAIN", text=["-7.00 dB"]))
            tags.save(p)
            self.assertEqual(replaygain.read_gain_strings(p), ("-6.66 dB", "-7.00 dB"))

    def test_flac_vorbis(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "t.flac")
            shutil.copy(FLAC_SRC, p)
            f = FLAC(p)
            f["replaygain_track_gain"] = "-5.00 dB"
            f["replaygain_album_gain"] = "-4.00 dB"
            f.save()
            self.assertEqual(replaygain.read_gain_strings(p), ("-5.00 dB", "-4.00 dB"))

    def test_untagged_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "t.flac")
            shutil.copy(FLAC_SRC, p)
            self.assertEqual(replaygain.read_gain_strings(p), (None, None))

    def test_m4a_freeform_bytes_decode(self):
        # R3: MP4FreeForm is a bytes subclass; the read-back must decode it
        # instead of logging repr(b'-6.66 dB'). The walk lives in
        # lattice.tags.read_replaygain_values now, so that is what gets mocked.
        from mutagen.mp4 import MP4FreeForm

        fake = mock.Mock(
            tags={
                "----:com.apple.iTunes:replaygain_track_gain": [
                    MP4FreeForm(b"-6.66 dB")
                ],
                "----:com.apple.iTunes:replaygain_album_gain": [
                    MP4FreeForm(b"-7.00 dB")
                ],
            }
        )
        with mock.patch.object(tags_mod, "MutagenFile", return_value=fake):
            self.assertEqual(
                replaygain.read_gain_strings("/x.m4a"), ("-6.66 dB", "-7.00 dB")
            )


class ScanAlbumTests(unittest.TestCase):
    def test_builds_rsgain_easy_command_single_thread(self):
        with mock.patch.object(replaygain.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            rc, _out, _err = replaygain.scan_album("/music/Album", ["01.mp3"], 1, None)
        self.assertEqual(rc, 0)
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["rsgain", "easy", "-q", "/music/Album"])

    def test_threads_add_multithread_flag_in_easy_mode(self):
        with mock.patch.object(replaygain.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            replaygain.scan_album("/music/Album", ["01.mp3"], 4, None)
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["rsgain", "easy", "-q", "-m", "4", "/music/Album"])

    def test_target_lufs_uses_custom_mode_with_explicit_files(self):
        with mock.patch.object(replaygain.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            # threads is ignored in custom mode (-m there means max-peak, not threads).
            replaygain.scan_album("/music/Album", ["01.mp3", "02.flac"], 4, -14.0)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["rsgain", "custom", "-q"])
        # writes RG2 tags, album gain, positive-gain clip protection, the target.
        for flag in ("-a", "-s", "i", "-c", "p"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("-l") + 1], "-14")
        # explicit file list, no -m thread flag, no directory.
        self.assertIn("/music/Album/01.mp3", argv)
        self.assertIn("/music/Album/02.flac", argv)
        self.assertNotIn("-m", argv)
        self.assertNotIn("/music/Album", argv)

    def test_target_lufs_formats_without_trailing_zero(self):
        with mock.patch.object(replaygain.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            replaygain.scan_album("/a", ["x.mp3"], 1, -16.5)
        argv = run.call_args[0][0]
        self.assertEqual(argv[argv.index("-l") + 1], "-16.5")


class RsgainSupportTests(unittest.TestCase):
    """M7: custom mode rejects its whole file list over one unsupported file
    (verified live: raw ADTS .aac -> "File list is not valid"), so the custom
    branch filters to rsgain-taggable extensions."""

    def test_split_partitions_by_extension(self):
        sup, unsup = replaygain.split_rsgain_supported(["01.flac", "02.aac", "03.mp3"])
        self.assertEqual(sup, ["01.flac", "03.mp3"])
        self.assertEqual(unsup, ["02.aac"])

    def test_custom_argv_excludes_unsupported(self):
        with mock.patch.object(replaygain.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            replaygain.scan_album("/m/A", ["01.flac", "02.aac"], 1, -14.0)
        argv = run.call_args[0][0]
        self.assertIn("/m/A/01.flac", argv)
        self.assertNotIn("/m/A/02.aac", argv)


class NestedParentTests(unittest.TestCase):
    """M9: rsgain easy scans recursively, so an album dir with audio in a
    subdir would rescan the nested album; such parents are detected."""

    def test_parent_with_audio_subdir_detected(self):
        albums = [
            ("/m/Album", ["t.mp3"]),
            ("/m/Album/CD1", ["01.mp3"]),
            ("/m/Other", ["x.mp3"]),
        ]
        self.assertEqual(replaygain.find_nested_parents(albums), {"/m/Album"})

    def test_sibling_string_prefix_is_not_nesting(self):
        albums = [("/m/Album", ["t.mp3"]), ("/m/Album 2", ["x.mp3"])]
        self.assertEqual(replaygain.find_nested_parents(albums), set())

    def test_flat_leaves_yield_nothing(self):
        albums = [("/m/A/Alb1", ["t.mp3"]), ("/m/B/Alb2", ["x.mp3"])]
        self.assertEqual(replaygain.find_nested_parents(albums), set())


class _MainHarness(unittest.TestCase):
    """Shared fixture/driver for main() tests (dry-run and apply paths)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _mp3(self, rel, tagged=False):
        p = Path(self.root, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(MP3_SRC, p)
        if tagged:
            tags = ID3(p)
            tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text=["-6.0 dB"]))
            tags.add(TXXX(encoding=3, desc="REPLAYGAIN_ALBUM_GAIN", text=["-6.5 dB"]))
            tags.save(p)
        return p

    def _run_main(self, argv):
        old = sys.argv
        sys.argv = ["replaygain.py", *argv]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = replaygain.main()
        finally:
            sys.argv = old
        return rc, buf.getvalue()

    def _log_text(self):
        return Path(self.root, "replaygain.log").read_text(encoding="utf-8")


class MainDryRunTests(_MainHarness):
    """M8: the dry-run must predict apply's scan set (--skip-tagged honored);
    M9: nested parents are reported, not scanned."""

    def test_dry_run_honors_skip_tagged(self):
        self._mp3("Tagged/01.mp3", tagged=True)
        self._mp3("Bare/01.mp3")
        rc, out = self._run_main([self.root, "--dry-run", "--skip-tagged"])
        self.assertEqual(rc, 0)
        self.assertIn("Would scan 1 of 2 album(s)", out)
        self.assertIn("1 skipped as fully tagged", out)
        log_text = Path(self.root, "replaygain.log").read_text(encoding="utf-8")
        self.assertIn("would skip", log_text)

    def test_dry_run_reports_nested_parent(self):
        self._mp3("Album/loose.mp3")
        self._mp3("Album/CD1/01.mp3")
        rc, out = self._run_main([self.root, "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("Would scan 1 of 2 album(s)", out)
        self.assertIn("1 nested parent(s) skipped", out)
        log_text = Path(self.root, "replaygain.log").read_text(encoding="utf-8")
        self.assertIn("NESTED ALBUM (skipped)", log_text)


class MainApplyTests(_MainHarness):
    """R2/R4/R6: apply-path accounting. rsgain is never invoked (scan_album
    and the PATH check are mocked); files stay inside the temp tree."""

    def _which(self):
        return mock.patch.object(
            replaygain.shutil, "which", return_value="/usr/bin/rsgain"
        )

    def test_noop_album_reported_distinctly(self):
        # R2: rc=0 with "No files were scanned" (or nothing read back) must not
        # count as a scanned album.
        self._mp3("Album/01.mp3")
        with (
            self._which(),
            mock.patch.object(
                replaygain,
                "scan_album",
                return_value=(0, "No files were scanned\n", ""),
            ),
        ):
            rc, _out = self._run_main([self.root, "--yes"])
        self.assertEqual(rc, 0)
        log_text = self._log_text()
        self.assertIn("NO-OP", log_text)
        self.assertNotIn("  scanned ", log_text)
        self.assertIn("no-ops: 1", log_text)
        self.assertIn("albums scanned: 0", log_text)

    def test_written_gains_count_as_scanned(self):
        p = self._mp3("Album/01.mp3")

        def fake_scan(dirpath, files, threads, target):
            tags = ID3(p)
            tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text=["-6.0 dB"]))
            tags.add(TXXX(encoding=3, desc="REPLAYGAIN_ALBUM_GAIN", text=["-6.5 dB"]))
            tags.save(p)
            return (0, "", "")

        with (
            self._which(),
            mock.patch.object(replaygain, "scan_album", side_effect=fake_scan),
        ):
            rc, _out = self._run_main([self.root, "--yes"])
        self.assertEqual(rc, 0)
        log_text = self._log_text()
        self.assertIn("scanned Album", log_text)
        self.assertIn("no-ops: 0", log_text)

    def test_skip_everything_still_logs_run(self):
        # R4: a run that writes nothing is still a run; the log records it.
        self._mp3("Tagged/01.mp3", tagged=True)
        with (
            self._which(),
            mock.patch.object(replaygain, "scan_album") as sa,
        ):
            rc, _out = self._run_main([self.root, "--yes", "--skip-tagged"])
        sa.assert_not_called()
        self.assertEqual(rc, 0)
        log_text = self._log_text()
        self.assertIn("RG RUN START [APPLY]", log_text)
        self.assertIn("nothing to scan", log_text)

    def test_declined_prompt_logs_abort(self):
        self._mp3("Album/01.mp3")
        with (
            self._which(),
            mock.patch.object(replaygain, "scan_album") as sa,
            mock.patch.object(sys.stdin, "isatty", return_value=True),
            mock.patch("builtins.input", return_value="n"),
        ):
            rc, out = self._run_main([self.root])
        sa.assert_not_called()
        self.assertEqual(rc, 0)
        self.assertIn("Aborted.", out)
        self.assertIn("aborted by user at confirmation", self._log_text())

    def test_unopenable_log_is_a_clean_error(self):
        # R6: a bad --log path is a validated error, not a traceback.
        self._mp3("Album/01.mp3")
        bad = os.path.join(self.root, "no-such-dir", "rg.log")
        with self._which():
            rc, out = self._run_main([self.root, "--yes", "--log", bad])
        self.assertEqual(rc, 1)
        self.assertIn("cannot open log file", out)


if __name__ == "__main__":
    unittest.main()
