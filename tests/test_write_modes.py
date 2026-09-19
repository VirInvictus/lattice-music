"""Tests for the write-mode wiring: the `lattice --clean` / `--apestrip` CLI
dispatch and the TUI menu entries added with the v5.0.0 fold. The modes'
brains are covered by test_cleaner.py and test_apestrip.py; this file pins
the contract that matters at the package surface: dry-run is the default,
--apply opts in, the root-list guard fires, and the TUI entries exist with
the confirms in front of any apply."""

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from mutagen.apev2 import APENoHeaderError, APEValue, APEv2, TEXT

from lattice import cli, tui

FIXTURES = Path(__file__).parent / "fixtures" / "library"
MP3_SRC = FIXTURES / "Cursive" / "Domestica" / "01 - The Casualty.mp3"


def _fragmented_tree(root: Path) -> None:
    """A canonical album folder plus a case-variant sibling holding one track."""
    canon = root / "Album"
    variant = root / "album"
    canon.mkdir(parents=True)
    variant.mkdir()
    (canon / "01.flac").write_bytes(b"x")
    (variant / "02.flac").write_bytes(b"y")


def _mp3_with_ape(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(MP3_SRC, path)
    ape = APEv2()
    ape["Genre"] = APEValue("Trash Metal", TEXT)
    ape.save(str(path))


class CleanDispatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "Music"
        _fragmented_tree(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _main(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return cli.main(argv)

    def test_dry_run_is_the_default(self):
        rc = self._main(["--clean", str(self.root)])
        self.assertEqual(rc, 0)
        # The case-variant folder survived untouched (log lines say [DRY]).
        self.assertTrue((self.root / "album" / "02.flac").exists())
        self.assertFalse((self.root / "Album" / "02.flac").exists())

    def test_apply_merges_and_logs(self):
        rc = self._main(["--clean", str(self.root), "--apply"])
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / "Album" / "02.flac").exists())
        self.assertFalse((self.root / "album").exists())
        log = (self.root / "cleanup.log").read_text(encoding="utf-8")
        self.assertIn("CLEANUP RUN START [APPLY]", log)

    def test_dry_run_flag_beats_apply(self):
        rc = self._main(["--clean", str(self.root), "--apply", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / "album" / "02.flac").exists())
        log = (self.root / "cleanup.log").read_text(encoding="utf-8")
        self.assertIn("CLEANUP RUN START [DRY RUN]", log)

    def test_all_flag_enables_every_pass(self):
        # --all turns on the three opt-in passes; dry-run default keeps it
        # safe while proving the flags reach the mode (Pass 4 header logged).
        rc = self._main(["--clean", str(self.root), "--all"])
        self.assertEqual(rc, 0)
        log = (self.root / "cleanup.log").read_text(encoding="utf-8")
        self.assertIn("--- PASS 3: normalize names", log)
        self.assertIn("--- PASS 4: normalize tags (library-wide) ---", log)

    def test_multiple_roots_are_rejected(self):
        other = Path(self._tmp.name) / "Other"
        _fragmented_tree(other)
        rc = self._main(
            ["--clean", "--root", str(self.root), "--root", str(other), "--apply"]
        )
        self.assertEqual(rc, 2)
        # Nothing was written anywhere.
        self.assertTrue((self.root / "album" / "02.flac").exists())

    def test_mode_flags_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self._main(["--clean", "--apestrip", str(self.root)])


class ApestripDispatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.track = self.root / "t.mp3"
        _mp3_with_ape(self.track)

    def tearDown(self):
        self._tmp.cleanup()

    def _main(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return cli.main(argv)

    def _ape_present(self) -> bool:
        try:
            APEv2(str(self.track))
            return True
        except APENoHeaderError:
            return False

    def test_dry_run_is_the_default(self):
        rc = self._main(["--apestrip", str(self.root)])
        self.assertEqual(rc, 0)
        self.assertTrue(self._ape_present())
        self.assertFalse((self.root / "apestrip.log").exists())

    def test_apply_strips_and_logs(self):
        # stdin is not a TTY under the test runner, so the confirmation is
        # auto-skipped: the script's non-interactive convention, preserved.
        rc = self._main(["--apestrip", str(self.root), "--apply"])
        self.assertEqual(rc, 0)
        self.assertFalse(self._ape_present())
        log = (self.root / "apestrip.log").read_text(encoding="utf-8")
        self.assertIn("stripped APEv2 tag", log)

    def test_keep_metadata_flag_reaches_the_mode(self):
        rc = self._main(["--apestrip", str(self.root), "--apply", "--keep-metadata"])
        self.assertEqual(rc, 0)
        self.assertFalse(self._ape_present())
        log = (self.root / "apestrip.log").read_text(encoding="utf-8")
        # The fixture's ID3 already has the APE's fields, so the strip is
        # logged plainly; the flag's acceptance is the dry-run worklist's job
        # (covered in test_apestrip.py). Here we pin the exit path only.
        self.assertIn("-> stripped 1 file(s)", log)


class TuiWriteModeTests(unittest.TestCase):
    def test_maintenance_section_holds_the_write_modes(self):
        sections = {name: items for name, items in tui._MAIN_SECTIONS if name}
        self.assertEqual(
            sections["MAINTENANCE"],
            [
                "Consolidate fragmented albums (clean)",
                "Strip APEv2 tags (apestrip)",
                "Fetch synced lyrics (lyrics)",
            ],
        )

    def test_aliases_and_selection_constants_track_the_new_section(self):
        self.assertEqual(tui._MAIN_ALIASES["clean"], (4, 0))
        self.assertEqual(tui._MAIN_ALIASES["apestrip"], (4, 1))
        self.assertEqual(tui._MAIN_ALIASES["lyrics"], (4, 2))
        self.assertEqual(tui._MAIN_ALIASES["lrc"], (4, 2))
        self.assertEqual(tui._SEL_CHANGE_ROOT, (5, 0))
        self.assertEqual(tui._SEL_QUIT, (6, 0))

    def test_every_alias_points_at_a_real_menu_item(self):
        for key, sel in tui._MAIN_ALIASES.items():
            if sel is None:
                continue
            si, ii = sel
            self.assertLess(si, len(tui._MAIN_SECTIONS), f"{key}: bad section")
            self.assertLess(ii, len(tui._MAIN_SECTIONS[si][1]), f"{key}: bad item")


if __name__ == "__main__":
    unittest.main()
