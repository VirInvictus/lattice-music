"""Tests for --snapshot / --diff: the evidence half for every writer. The
diff contract is structural (moved / retagged / resized / added / removed),
pinned end to end against temp trees with the tag layer mocked where a
retag has to be simulated between two snapshots."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import lattice.modes.library as library_mod
from lattice.modes.library import (
    _load_snapshot,
    diff_snapshot,
    write_snapshot,
)
from lattice.tags import TagBundle

FIXTURE = Path(__file__).parent / "fixtures" / "library"
FLAC_SRC = FIXTURE / "Aphex Twin" / "Selected Ambient Works" / "01 - Xtal.flac"


class SnapshotRoundTripTests(unittest.TestCase):
    def test_snapshot_then_load_is_lossless(self):
        with tempfile.TemporaryDirectory() as td:
            track = Path(td) / "Album" / "01.flac"
            track.parent.mkdir(parents=True)
            track.write_bytes(b"st-xyz")
            out = Path(td) / "snap.tsv"
            write_snapshot([td], str(out), quiet=True)
            rows = _load_snapshot(str(out))
            self.assertIsNotNone(rows)
            self.assertIn(str(track), rows)
            row = rows[str(track)]
            self.assertEqual(row["size"], 6)
            self.assertEqual(row["rg"], (0, 0))

    def test_non_snapshot_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            junk = Path(td) / "not-a-snapshot.tsv"
            junk.write_text("a\tb\tc\n", encoding="utf-8")
            self.assertIsNone(_load_snapshot(str(junk)))


class DiffTests(unittest.TestCase):
    def _tree(self, td: str) -> Path:
        album = Path(td) / "Album"
        album.mkdir(parents=True, exist_ok=True)
        (album / "01.flac").write_bytes(b"aaaa")
        (album / "02.flac").write_bytes(b"bb")
        return album

    def _snap(self, td: str, name: str = "snap.tsv") -> Path:
        out = Path(td) / name
        write_snapshot([td], str(out), quiet=True)
        return out

    def _diff(self, td: str, snap: Path, verbose: bool = False) -> tuple[int, str]:
        out = Path(td) / "diff.txt"
        rc = diff_snapshot([td], str(snap), str(out), quiet=True)
        return rc, out.read_text(encoding="utf-8")

    def test_unchanged_tree_diffs_clean(self):
        with tempfile.TemporaryDirectory() as td:
            self._tree(td)
            snap = self._snap(td)
            rc, report = self._diff(td, snap)
            self.assertEqual(rc, 0)
            self.assertIn("No differences.", report)

    def test_move_is_not_removal_plus_addition(self):
        with tempfile.TemporaryDirectory() as td:
            album = self._tree(td)
            snap = self._snap(td)
            os.rename(album / "02.flac", album / "renamed.flac")
            _rc, report = self._diff(td, snap)
            self.assertIn("MOVED (1)", report)
            self.assertIn("-> Album/renamed.flac", report)
            self.assertNotIn("REMOVED", report)
            self.assertNotIn("ADDED", report)

    def test_added_and_removed_leftovers(self):
        with tempfile.TemporaryDirectory() as td:
            album = self._tree(td)
            snap = self._snap(td)
            (album / "01.flac").unlink()
            (album / "03.flac").write_bytes(b"new")
            _rc, report = self._diff(td, snap)
            self.assertIn("ADDED (1)", report)
            self.assertIn("REMOVED (1)", report)
            self.assertIn("03.flac", report)

    def test_resize_is_reported_with_sizes(self):
        with tempfile.TemporaryDirectory() as td:
            album = self._tree(td)
            snap = self._snap(td)
            (album / "01.flac").write_bytes(b"a" * 40)
            _rc, report = self._diff(td, snap)
            self.assertIn("RESIZED (1)", report)
            self.assertIn("(4 -> 40 bytes)", report)

    def test_retagged_shows_field_changes(self):
        with tempfile.TemporaryDirectory() as td:
            self._tree(td)
            snap = self._snap(td)
            # Simulate a retag pass: the second walk sees different tags.
            retagged = TagBundle(
                title="t",
                trackno=1,
                genre="Metal",
                year=1985,
            )
            real = library_mod.get_all_tags

            def fake(path):
                if path.endswith("01.flac"):
                    return retagged
                return real(path)

            with mock.patch.object(library_mod, "get_all_tags", side_effect=fake):
                _rc, report = self._diff(td, snap)
            self.assertIn("RETAGGED (1)", report)
            self.assertIn("genre (none) -> Metal", report)
            self.assertIn("year (none) -> 1985", report)

    def test_diff_against_a_non_snapshot_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            self._tree(td)
            junk = Path(td) / "junk.tsv"
            junk.write_text("nope\n", encoding="utf-8")
            out = Path(td) / "diff.txt"
            rc = diff_snapshot([td], str(junk), str(out), quiet=True)
            self.assertEqual(rc, 2)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
