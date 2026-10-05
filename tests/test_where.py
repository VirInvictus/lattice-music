"""Tests for `--where`, the global selector (B1): the playlist rule engine
promoted into a scan-time TagBundle predicate scoping the library exports,
--stats, and the tag-reading audits, plus album-granular targeting for the
write modes. The album-granular rule is pinned here explicitly: a where
matching ANY track of an album selects the WHOLE album as a write target;
write modes never act on a partial album."""

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from lattice import cli
from lattice.modes.playlists import RuleError, compile_where
from lattice.tags import TagBundle


def _bundle(**overrides) -> TagBundle:
    fields = dict(
        title="Song",
        artist="Artist",
        trackno=1,
        album="Album",
        genre="Rock",
        rating=4.0,
        duration_s=200.0,
        bitrate_kbps=256,
    )
    fields.update(overrides)
    return TagBundle(**fields)


class CompileWhereTests(unittest.TestCase):
    def test_predicate_over_tagbundle_fields(self):
        pred = compile_where("rating >= 4 and genre == 'Jazz'")
        self.assertTrue(pred(_bundle(genre="Jazz")))
        self.assertFalse(pred(_bundle(genre="Rock")))

    def test_sql_and_or_are_accepted(self):
        pred = compile_where("genre == 'Rock' AND rating > 3")
        self.assertTrue(pred(_bundle()))

    def test_invalid_rule_raises_rule_error(self):
        with self.assertRaises(RuleError):
            compile_where("genre ==")
        with self.assertRaises(RuleError):
            compile_where("os.getcwd()")  # calls are not in the grammar

    def test_unknown_field_raises(self):
        with self.assertRaises(RuleError):
            compile_where("composer == 'X'")


class ScannerWhereTests(unittest.TestCase):
    """Track-granularity scoping in _scan_album_dirs: non-matching tracks stay
    out of the aggregation, and an album with no matching tracks drops out."""

    FIXTURES = Path(__file__).parent / "fixtures" / "library"

    def _scan(self, tmp, where=None):
        from lattice.utils import _make_pbar

        root = Path(tmp) / "Art"
        root.mkdir()
        album = root / "Album"
        album.mkdir()
        shutil.copy(
            self.FIXTURES / "Aphex Twin" / "Selected Ambient Works" / "01 - Xtal.flac",
            album / "01.flac",
        )
        pbar = _make_pbar(2, "Scanning", quiet=True)
        try:
            from lattice.modes.library import _scan_album_dirs
            from lattice.utils import as_roots

            return _scan_album_dirs(
                as_roots(str(tmp)), "{artist}/{album}", pbar, where=where
            )
        finally:
            pbar.close()

    def test_matching_album_is_kept(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            dirs = self._scan(tmp, where=compile_where("genre == 'Electronic'"))
            self.assertEqual(len(dirs), 1)
            self.assertEqual(dirs[0].artist, "Aphex Twin")

    def test_non_matching_album_drops_out_whole(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            dirs = self._scan(tmp, where=compile_where("genre == 'Polka'"))
            self.assertEqual(dirs, [])


class StatsWhereTests(unittest.TestCase):
    FIXTURES = Path(__file__).parent / "fixtures" / "library"

    def test_scoped_report_counts_only_matches(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Art"
            (root / "Album").mkdir(parents=True)
            shutil.copy(
                self.FIXTURES
                / "Aphex Twin"
                / "Selected Ambient Works"
                / "01 - Xtal.flac",
                root / "Album" / "01.flac",
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                report = cli_stats(tmp, where=compile_where("genre == 'Polka'"))
            self.assertIn("--where scoped: 0 of 1 files", report)
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                report2 = cli_stats(tmp, where=compile_where("genre == 'Electronic'"))
            self.assertIn("--where scoped: 1 of 1 files", report2)


def cli_stats(root, *, where):
    from lattice.modes.stats import run_stats

    return run_stats(root, None, quiet=True, where=where)


class WriteTargetingTests(unittest.TestCase):
    """The album-granular rule, pinned per write mode: any matching track
    selects the whole album; no partial-album writes."""

    FIXTURES = Path(__file__).parent / "fixtures" / "library"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        album = self.root / "Art" / "Album"
        album.mkdir(parents=True)
        shutil.copy(
            self.FIXTURES / "Aphex Twin" / "Selected Ambient Works" / "01 - Xtal.flac",
            album / "01.flac",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_genre_tidy_apply_where_excludes_album(self):
        from lattice.modes import genretidy as gt

        (self.root / "genre_map.tsv").write_text(
            "Aphex Twin\tPolka\n", encoding="utf-8"
        )
        seen = {}
        orig = gt.scan_album_dirs

        def spy(directory, quiet, layout="{artist}/{album}", where=None):
            seen["where"] = where
            return orig(directory, quiet, layout, where=where)

        gt.scan_album_dirs = spy
        try:
            rc = gt.run_genre_tidy_apply(
                str(self.root),
                map_path=str(self.root / "genre_map.tsv"),
                quiet=True,
                where=compile_where("genre == 'Polka'"),
            )
        finally:
            gt.scan_album_dirs = orig
        self.assertEqual(rc, 0)
        # The album's genre is Electronic; the Polka-only where excludes it,
        # so nothing is retagged even though the map would retag it.
        self.assertIsNotNone(seen["where"])
        from mutagen.flac import FLAC

        self.assertEqual(
            FLAC(str(self.root / "Art" / "Album" / "01.flac")).get("genre"),
            ["Electronic"],
        )

    def test_replaygain_where_scopes_the_worklist(self):
        from lattice.modes import replaygain as rg

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = rg.run_replaygain(
                str(self.root),
                dry_run=True,
                where=compile_where("genre == 'Polka'"),
            )
        self.assertEqual(rc, 0)
        self.assertIn("No albums match the --where rule", buf.getvalue())
        self.assertFalse((self.root / "replaygain.log").exists())
        # The matching rule keeps the album on the worklist.
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2), contextlib.redirect_stderr(buf2):
            rc2 = rg.run_replaygain(
                str(self.root),
                dry_run=True,
                where=compile_where("genre == 'Electronic'"),
            )
        self.assertEqual(rc2, 0)
        log2 = (self.root / "replaygain.log").read_text(encoding="utf-8")
        self.assertIn("would scan 1 of 1 album", log2)

    def test_cli_where_with_unsupported_mode_is_refused(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--testFLAC", "--where", "rating >= 4", str(self.root)])
        self.assertEqual(rc, 2)

    def test_cli_where_invalid_rule_is_exit_1(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--stats", "--where", "genre ==", str(self.root)])
        self.assertEqual(rc, 1)

    def test_cli_where_scopes_stats(self):
        out = Path(self._tmp.name) / "stats.txt"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(
                [
                    "--stats",
                    "--where",
                    "genre == 'Electronic'",
                    "--output",
                    str(out),
                    str(self.root),
                ]
            )
        self.assertEqual(rc, 0)
        text = out.read_text(encoding="utf-8")
        self.assertIn("--where scoped: 1 of 1 files", text)


if __name__ == "__main__":
    unittest.main()
