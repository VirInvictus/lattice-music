"""Tests for `--json`, `--output -`, and `--fail-on-findings` (B2): the
machine shapes. The .txt report stays the default; --json replaces it with the
uniform envelope ({mode, root, findings, payload}) for stats and the
tag-reading audits, and ends a write mode with a JSON run summary; exit-code
discipline holds (refusals 2, findings gate 1 only under --fail-on-findings,
default 0)."""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lattice import cli

FIXTURES = Path(__file__).parent / "fixtures" / "library"
MP3_SRC = FIXTURES / "Cursive" / "Domestica" / "01 - The Casualty.mp3"


def _main(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(argv)
    return rc, buf.getvalue()


class JsonReportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "Music"
        album = self.root / "Art" / "Album"
        album.mkdir(parents=True)
        shutil.copy(
            FIXTURES / "Aphex Twin" / "Selected Ambient Works" / "01 - Xtal.flac",
            album / "01.flac",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, mode_args, out="out.json"):
        out_path = Path(self._tmp.name) / out
        rc, _out = _main(
            [*mode_args, "--json", "--output", str(out_path), str(self.root)]
        )
        return rc, json.loads(out_path.read_text(encoding="utf-8"))

    def test_stats_json_envelope(self):
        rc, doc = self._run(["--stats"])
        self.assertEqual(rc, 0)
        self.assertEqual(doc["mode"], "stats")
        self.assertEqual(doc["findings"], 0)
        self.assertEqual(doc["payload"]["total_files"], 1)
        self.assertIn(".flac", doc["payload"]["formats"])

    def test_tag_audit_json(self):
        rc, doc = self._run(["--auditTags"])
        self.assertEqual(rc, 0)
        self.assertEqual(doc["mode"], "tag_audit")
        # The fixture track has a complete tag block, so no findings.
        self.assertEqual(doc["findings"], 0)
        self.assertEqual(doc["payload"]["items"], [])

    def test_health_score_json(self):
        rc, doc = self._run(["--healthScore"])
        self.assertEqual(rc, 0)
        self.assertEqual(doc["mode"], "health_score")
        self.assertIn("flagged", doc["payload"])

    def test_json_to_stdout_pipes(self):
        rc, out = _main(["--stats", "--json", "--output", "-", str(self.root)])
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertEqual(doc["mode"], "stats")

    def test_fail_on_findings_gates_exit(self):
        # The fixture album is missing nothing, so force a finding: a
        # bitrate floor above the file's real bitrate makes the audit find one.
        rc, _ = _main(
            [
                "--auditBitrate",
                "--min-bitrate",
                "9999",
                "--fail-on-findings",
                "--output",
                str(Path(self._tmp.name) / "br.txt"),
                str(self.root),
            ]
        )
        self.assertEqual(rc, 1)
        # Default stays 0 on the same findings.
        rc, _ = _main(
            [
                "--auditBitrate",
                "--min-bitrate",
                "9999",
                "--output",
                str(Path(self._tmp.name) / "br2.txt"),
                str(self.root),
            ]
        )
        self.assertEqual(rc, 0)

    def test_json_refused_for_unsupported_mode(self):
        rc, _out = _main(["--library", "--json", str(self.root)])
        self.assertEqual(rc, 2)

    def test_fail_on_findings_refused_for_non_audit(self):
        rc, _out = _main(["--stats", "--fail-on-findings", str(self.root)])
        self.assertEqual(rc, 2)


class MissingRootExitClassTests(unittest.TestCase):
    """A library root that does not exist is a usage error (exit 2, one clean
    line on stderr), never a confident-looking empty report on stdout with
    exit 0: that made a typo'd root indistinguishable from an empty library
    and handed --fail-on-findings a false green. An existing root with zero
    audio keeps the true empty report (exit 0)."""

    def _main_both(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_missing_root_is_exit_2_on_stderr(self):
        missing = "/tmp/lattice-no-such-root-xyz"
        rc, out, err = self._main_both(["--stats", missing])
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertIn(missing, err)
        self.assertIn("does not exist", err)

    def test_missing_root_refuses_a_write_mode_too(self):
        rc, _out, err = self._main_both(
            ["--stripRatings", "/tmp/lattice-no-such-root-xyz", "--apply"]
        )
        self.assertEqual(rc, 2)
        self.assertIn("does not exist", err)

    def test_repeated_roots_name_every_missing_path(self):
        rc, _out, err = self._main_both(
            [
                "--stats",
                "--root",
                "/tmp/lattice-no-such-a",
                "--root",
                "/tmp/lattice-no-such-b",
            ]
        )
        self.assertEqual(rc, 2)
        self.assertIn("/tmp/lattice-no-such-a", err)
        self.assertIn("/tmp/lattice-no-such-b", err)

    def test_existing_root_with_zero_audio_stays_exit_0(self):
        with tempfile.TemporaryDirectory() as td:
            rc, out, err = self._main_both(["--stats", td])
            self.assertEqual(rc, 0)
            self.assertIn("No audio", out)
            self.assertEqual(err, "")


class WriteSummaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "Album"
        self.root.mkdir()
        shutil.copy(MP3_SRC, self.root / "01.mp3")

    def tearDown(self):
        self._tmp.cleanup()

    def test_retag_json_summary(self):
        rc, out = _main(["--retag", str(self.root), "Screamo", "--apply", "--json"])
        self.assertEqual(rc, 0)
        doc = json.loads(out[out.index("{") :])
        self.assertEqual(doc["mode"], "retag")
        self.assertFalse(doc["dry_run"])
        self.assertEqual(doc["counts"]["updated"], 1)

    def test_clean_json_summary_dry_run_flag(self):
        rc, out = _main(["--clean", str(self.root), "--json"])
        self.assertEqual(rc, 0)
        doc = json.loads(out[out.index("{") :])
        self.assertEqual(doc["mode"], "clean")
        self.assertTrue(doc["dry_run"])
        self.assertIn("groups", doc["counts"])

    def test_apestrip_json_summary(self):
        rc, out = _main(["--apestrip", str(self.root), "--json"])
        self.assertEqual(rc, 0)
        doc = json.loads(out[out.index("{") :])
        self.assertEqual(doc["mode"], "apestrip")
        self.assertTrue(doc["dry_run"])
        self.assertEqual(doc["counts"]["files_with_ape"], 0)


if __name__ == "__main__":
    unittest.main()
