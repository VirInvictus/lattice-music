"""Tests for `--health`, the one-walk digest (B3): one screen of finding
counts across the existing lenses, each file's tags read once and fed to every
lens. The derivation principle is pinned structurally: the digest calls the
same lens code the audits expose (_tag_findings, _rg_bucket, _album_health,
classify_stray, check_playlist), so the two faces cannot drift. The digest
always exits 0 (the audits are the gates)."""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lattice import cli, tui

FIXTURES = Path(__file__).parent / "fixtures" / "library"
FLAC_SRC = FIXTURES / "Aphex Twin" / "Selected Ambient Works" / "01 - Xtal.flac"


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


class HealthDigestTests(_Tree):
    def test_digest_is_one_screen_and_exits_zero(self):
        # Explicit --output everywhere: the mode's CWD default would litter
        # the checkout (the other audit tests follow the same rule).
        rc, out = _main(
            ["--health", "--output", str(Path(self._tmp.name) / "d.txt"), str(self.root)]
        )
        self.assertEqual(rc, 0)
        self.assertIn("LIBRARY HEALTH DIGEST", out)
        self.assertIn("Walked once: 1 files in 1 albums", out)
        # Every lens row carries its pointer to the full-report mode.
        for pointer in (
            "--auditTags",
            "--auditBitrate",
            "--auditReplayGain",
            "--missingArt",
            "--auditStrays",
            "--checkPlaylists",
            "--duplicates",
            "--healthScore",
        ):
            self.assertIn(pointer, out)

    def test_counts_reflect_the_tree(self):
        rc, out = _main(
            ["--health", "--output", str(Path(self._tmp.name) / "d.txt"), str(self.root)]
        )
        self.assertEqual(rc, 0)
        # The fixture track is fully tagged, RG-bare, and has no folder art:
        # the digest's numbers must agree with the audits' rules.
        self.assertIn("Tags:       0 file(s) with incomplete tags", out)
        self.assertIn("ReplayGain: 1 missing", out)
        self.assertIn("Art:        1 album(s) without any cover", out)

    def test_worst_albums_listed_with_grades(self):
        rc, out = _main(
            ["--health", "--output", str(Path(self._tmp.name) / "d.txt"), str(self.root)]
        )
        self.assertIn("mean", out)
        self.assertRegex(out, r"\s+[A-D]\s+Art/Album/")

    def test_json_envelope_and_still_exit_zero(self):
        out_path = Path(self._tmp.name) / "digest.json"
        rc, _out = _main(
            ["--health", "--json", "--output", str(out_path), str(self.root)]
        )
        self.assertEqual(rc, 0)
        doc = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(doc["mode"], "health")
        self.assertEqual(doc["payload"]["files"], 1)
        self.assertEqual(doc["payload"]["albums"], 1)
        self.assertEqual(doc["payload"]["tag_incomplete"], 0)
        self.assertEqual(doc["payload"]["replaygain"]["missing"], 1)
        self.assertEqual(doc["payload"]["albums_without_art"], 1)

    def test_digest_lenses_share_the_audit_classifiers(self):
        # The drift-proofing pin: the digest's tag lens IS the audit's
        # classifier, and its RG buckets ARE the audit's buckets.
        from lattice.modes.audit import _rg_bucket, _tag_findings, run_health
        from lattice.tags import TagBundle

        self.assertEqual(
            _tag_findings(TagBundle(title="x", artist="a", trackno=1, genre="g")),
            [],
        )
        self.assertEqual(
            _tag_findings(TagBundle(title="x")), ["artist", "tracknumber", "genre"]
        )
        self.assertEqual(_rg_bucket(0, 0, 3), "MISSING")

        # And the digest entry point is the same function the TUI dispatches.
        self.assertTrue(callable(run_health))


class HealthWiringTests(unittest.TestCase):
    def test_health_is_in_the_house_flag_group_and_tui_menu(self):

        from lattice import cli as cli_mod

        parser = cli_mod.build_parser()
        args = parser.parse_args(["--health"])
        self.assertTrue(args.health)
        sections = {name: items for name, items in tui._MAIN_SECTIONS if name}
        self.assertEqual(sections["METADATA"][-1], "Library health digest (health)")
        self.assertEqual(tui._MAIN_ALIASES["digest"], (3, 11))

    def test_json_accepted_but_fail_on_findings_refused(self):
        with tempfile.TemporaryDirectory() as td:
            rc, _ = _main(["--health", "--json", "--output", f"{td}/d.json", td])
            self.assertEqual(rc, 0)
            rc, _ = _main(["--health", "--fail-on-findings", td])
            self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
