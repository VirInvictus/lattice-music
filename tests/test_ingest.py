"""Tests for `--ingest`, the ritual packaged (B4): one command sequences the
genreMap -> apestrip -> clean --all stages plus the post-state digest. Ingest
sequences; it never re-implements, so these tests pin the sequencing contract:
dry-run default, per-stage logs left in the stage modes' hands, the single
top-level summary pointing into them, and the exit code carrying any stage
failure."""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lattice import cli, tui
from lattice.modes.ingest import run_ingest

FIXTURES = Path(__file__).parent / "fixtures" / "library"
FLAC_SRC = FIXTURES / "Aphex Twin" / "Selected Ambient Works" / "01 - Xtal.flac"
MP3_SRC = FIXTURES / "Cursive" / "Domestica" / "01 - The Casualty.mp3"


def _main(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(argv)
    return rc, buf.getvalue()


class _Tree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "Music"
        album = self.root / "Art" / "Album"
        album.mkdir(parents=True)
        shutil.copy(FLAC_SRC, album / "01.flac")

    def tearDown(self):
        self._tmp.cleanup()


class IngestDryRunTests(_Tree):
    def test_dry_run_writes_plans_and_summary_not_moves(self):
        rc, out = _main(["--ingest", str(self.root)])
        self.assertEqual(rc, 0)
        # Nothing moved.
        self.assertTrue((self.root / "Art" / "Album" / "01.flac").exists())
        # The per-stage logs exist as [DRY] plans (each stage keeps its own).
        manifest = self.root / "genre_foldermap.manifest.tsv"
        self.assertFalse(manifest.exists())  # dry runs write no manifest
        cleanup = (self.root / "cleanup.log").read_text(encoding="utf-8")
        self.assertIn("CLEANUP RUN START [DRY RUN]", cleanup)
        # The summary points into the stage logs.
        summary = (self.root / "ingest_summary.txt").read_text(encoding="utf-8")
        self.assertIn("Mode: DRY RUN", summary)
        self.assertIn("genreMap: ok", summary)
        self.assertIn("health digest: skipped (dry run", summary)
        self.assertIn("Per-stage records", summary)

    def test_summary_is_the_top_level_record(self):
        rc, _out = _main(["--ingest", str(self.root)])
        summary = (self.root / "ingest_summary.txt").read_text(encoding="utf-8")
        self.assertIn("genre_foldermap.manifest.tsv", summary)
        self.assertIn("apestrip.log", summary)
        self.assertIn("cleanup.log", summary)


class IngestApplyTests(_Tree):
    def test_apply_runs_the_stages_and_the_digest(self):
        rc, _out = _main(["--ingest", str(self.root), "--apply"])
        self.assertEqual(rc, 0)
        # The genreMap stage moved the album under its genre; the clean stage
        # ran (its APPLY log exists); the digest ran after.

        moved = self.root / "Electronic" / "Art" / "Album" / "01.flac"
        self.assertTrue(moved.exists(), "the genreMap stage did not move the album")
        cleanup = (self.root / "cleanup.log").read_text(encoding="utf-8")
        self.assertIn("CLEANUP RUN START [APPLY]", cleanup)
        digest = self.root / "ingest_health.txt"
        self.assertTrue(digest.exists())
        self.assertIn("LIBRARY HEALTH DIGEST", digest.read_text(encoding="utf-8"))
        summary = (self.root / "ingest_summary.txt").read_text(encoding="utf-8")
        self.assertIn("Mode: APPLY", summary)
        self.assertNotIn("declined", summary)

    def test_snapshot_baseline_produces_a_diff_report(self):
        baseline = Path(self._tmp.name) / "before.tsv"
        rc, _out = _main(["--snapshot", "--output", str(baseline), str(self.root)])
        self.assertEqual(rc, 0)
        rc, _out = _main(
            ["--ingest", str(self.root), "--apply", "--baseline", str(baseline)]
        )
        self.assertEqual(rc, 0)
        diff = self.root / "ingest_diff.txt"
        self.assertTrue(diff.exists())
        # The ritual moved the album, so the diff sees it.
        text = diff.read_text(encoding="utf-8")
        self.assertIn("MOVED", text)

    def test_json_summary_ends_the_run(self):
        rc, out = _main(["--ingest", str(self.root), "--json"])
        self.assertEqual(rc, 0)
        doc = json.loads(out[out.index("{") :])
        self.assertEqual(doc["mode"], "ingest")
        self.assertTrue(doc["dry_run"])
        self.assertIn("genreMap", doc["counts"]["stages"])


class IngestWiringTests(unittest.TestCase):
    def test_tui_entry_and_alias(self):
        sections = {name: items for name, items in tui._MAIN_SECTIONS if name}
        self.assertEqual(
            sections["MAINTENANCE"][-1],
            "Import ritual: genreMap + apestrip + clean (ingest)",
        )
        self.assertEqual(tui._MAIN_ALIASES["ingest"], (4, 8))

    def test_declined_stage_is_recorded_not_fatal(self):
        # _confirm is only consulted on a TTY; force it to decline and pin
        # the recorded-skip contract.
        import sys
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "Music"
            album = root / "Art" / "Album"
            album.mkdir(parents=True)
            shutil.copy(FLAC_SRC, album / "01.flac")
            with (
                mock.patch.object(sys, "stdin", create=True) as fake_stdin,
                mock.patch("builtins.input", return_value="n"),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                fake_stdin.isatty.return_value = True
                rc = run_ingest(str(root), apply=True)
            self.assertEqual(rc, 0)
            summary = (root / "ingest_summary.txt").read_text(encoding="utf-8")
            self.assertIn("declined by user; skipped", summary)
            # Nothing moved: every stage was declined.
            self.assertTrue((album / "01.flac").exists())


if __name__ == "__main__":
    unittest.main()
