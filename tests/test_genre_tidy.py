import os
import unittest
from collections import namedtuple
from pathlib import Path

# The genre authority tool lives in the package (the 6.0.0 fold of
# scripts/genre_tidy.py, now a launcher); scripts/genre_tidy.py is re-exported
# by these same names via its PEP 562 shim, so either import path works. The
# lattice scan is faked and apply's retag calls run in-process against fixture
# copies, mirroring the integrity modes' approach.
from lattice.modes import genretidy as gt

FakeAD = namedtuple("FakeAD", "artist genre")


class NormTests(unittest.TestCase):
    def test_case_and_whitespace(self):
        self.assertEqual(gt.norm("  The  XX "), "the xx")

    def test_curly_quote_and_dash_fold(self):
        self.assertEqual(gt.norm("Jay‐Z"), gt.norm("Jay-Z"))
        self.assertEqual(gt.norm("Director’s Cut"), gt.norm("Director's Cut"))

    def test_empty_and_none(self):
        self.assertEqual(gt.norm(""), "")
        self.assertEqual(gt.norm(None), "")


class ParseMapTests(unittest.TestCase):
    def test_skips_comments_and_blanks(self):
        entries = gt.parse_map(["# header", "", "  ", "Eminem\tHardcore Hip Hop"])
        self.assertEqual(set(entries), {gt.norm("Eminem")})

    def test_canonical_is_first_column(self):
        entries = gt.parse_map(["Kendrick Lamar\tConscious Hip Hop\tJazz Rap"])
        e = entries[gt.norm("Kendrick Lamar")]
        self.assertEqual(e.canonical, "Conscious Hip Hop")
        self.assertEqual(
            e.allowed_norm,
            frozenset({gt.norm("Conscious Hip Hop"), gt.norm("Jazz Rap")}),
        )

    def test_drops_empty_columns(self):
        entries = gt.parse_map(["Trap Lord\tTrap\t\t "])
        self.assertEqual(
            entries[gt.norm("Trap Lord")].allowed_norm, frozenset({"trap"})
        )

    def test_blanked_genre_means_skip(self):
        entries = gt.parse_map(["Aphex Twin\t"])
        e = entries[gt.norm("Aphex Twin")]
        self.assertEqual(e.canonical, "")
        self.assertEqual(e.allowed_norm, frozenset())

    def test_artist_only_line_no_tab(self):
        entries = gt.parse_map(["Boards of Canada"])
        e = entries[gt.norm("Boards of Canada")]
        self.assertEqual(e.canonical, "")


class IsCompliantTests(unittest.TestCase):
    def setUp(self):
        self.allowed = frozenset({gt.norm("Conscious Hip Hop"), gt.norm("Jazz Rap")})

    def test_in_set_normalized(self):
        self.assertTrue(gt.is_compliant("conscious hip hop", self.allowed))
        self.assertTrue(gt.is_compliant("Jazz Rap", self.allowed))

    def test_outside_set(self):
        self.assertFalse(gt.is_compliant("Pop", self.allowed))

    def test_empty_genre_never_compliant(self):
        self.assertFalse(gt.is_compliant("", self.allowed))
        self.assertFalse(gt.is_compliant(None, self.allowed))


class DirectRetagCallTests(unittest.TestCase):
    """A2 rewiring: apply no longer shells out to retag.py; it calls
    lattice.modes.retag.retag_directory in-process. The old argv-builder pins
    become call-shape pins: the canonical is passed verbatim as ONE genre
    value, and the dry-run flag propagates (writes nothing)."""

    FIXTURES = Path(__file__).parent / "fixtures" / "library"

    def _copied_flac(self, tmp: str) -> Path:
        import shutil

        p = Path(tmp) / "t.flac"
        shutil.copy(
            self.FIXTURES / "Aphex Twin" / "Selected Ambient Works" / "01 - Xtal.flac",
            p,
        )
        return p

    def test_slash_canonical_is_one_genre_value(self):
        # H5: splitting on "/" wrote a multi-value tag that never read back
        # equal to the map (FLAC first-value read; MP3 v2.3 slash-join without
        # spaces), so apply retagged the same albums forever. The canonical is
        # passed verbatim as ONE genre value.
        import tempfile

        from lattice.modes import retag as retag_mod

        with tempfile.TemporaryDirectory() as tmp:
            self._copied_flac(tmp)
            updated, failed, _unchanged = retag_mod.retag_directory(
                tmp,
                ["Emo / Orgcore"],
                dry_run=False,
                log=lambda _msg: None,
            )
            self.assertEqual((updated, failed), (1, 0))
            self.assertEqual(
                retag_mod.read_genres(str(Path(tmp) / "t.flac")), ["Emo / Orgcore"]
            )

    def test_dry_run_propagates_and_writes_nothing(self):
        import tempfile

        from lattice.modes import retag as retag_mod

        with tempfile.TemporaryDirectory() as tmp:
            p = self._copied_flac(tmp)
            lines: list[str] = []
            updated, failed, _unchanged = retag_mod.retag_directory(
                tmp,
                ["Trap"],
                dry_run=True,
                log=lines.append,
            )
            self.assertEqual((updated, failed, _unchanged), (1, 0, 0))
            self.assertEqual(retag_mod.read_genres(str(p)), ["Electronic"])
            self.assertTrue(any("would retag" in line for line in lines))


class ReduceArtistsTests(unittest.TestCase):
    def test_weights_genres_by_album_and_picks_display(self):
        dirs = [
            FakeAD("Freddie Gibbs", "Gangsta Rap"),
            FakeAD("Freddie Gibbs", "Gangsta Rap"),
            FakeAD("Freddie Gibbs", "Hardcore Hip Hop"),
        ]
        reduced = gt.reduce_artists(dirs)
        display, genres = reduced[gt.norm("Freddie Gibbs")]
        self.assertEqual(display, "Freddie Gibbs")
        self.assertEqual(genres["Gangsta Rap"], 2)
        self.assertEqual(genres["Hardcore Hip Hop"], 1)

    def test_spelling_variants_collapse_under_norm(self):
        dirs = [FakeAD("Jay-Z", "East Coast Rap"), FakeAD("Jay‐Z", "Mafioso Rap")]
        reduced = gt.reduce_artists(dirs)
        self.assertEqual(len(reduced), 1)

    def test_skips_empty_artist(self):
        self.assertEqual(gt.reduce_artists([FakeAD("", "Trap")]), {})


class BuildRowsTests(unittest.TestCase):
    def test_single_genre_no_comment(self):
        reduced = gt.reduce_artists([FakeAD("AFI", "Post-Hardcore")])
        rows = gt.build_rows(reduced)
        self.assertEqual(rows, ["AFI\tPost-Hardcore"])

    def test_multi_genre_lists_all_with_comment(self):
        reduced = gt.reduce_artists(
            [FakeAD("Kendrick Lamar", "Conscious Hip Hop")] * 7
            + [FakeAD("Kendrick Lamar", "Jazz Rap")] * 2
        )
        rows = gt.build_rows(reduced)
        self.assertTrue(rows[0].startswith("# Kendrick Lamar:"))
        # The data line lists every genre, most-common first.
        self.assertEqual(rows[1], "Kendrick Lamar\tConscious Hip Hop\tJazz Rap")

    def test_no_genre_artist_flagged(self):
        reduced = gt.reduce_artists([FakeAD("Mystery Act", "")])
        rows = gt.build_rows(reduced)
        self.assertIn("no genre tags found", rows[0])
        # No genres means just the artist (no tab) -> skipped by apply.
        self.assertEqual(rows[1], "Mystery Act")

    def test_round_trips_through_parse_map(self):
        reduced = gt.reduce_artists(
            [FakeAD("Aesop Rock", "Abstract Hip Hop")] * 3
            + [FakeAD("Deftones", "Nu Metal"), FakeAD("Deftones", "Alternative Rock")]
        )
        entries = gt.parse_map(gt.build_rows(reduced))
        self.assertEqual(entries[gt.norm("Aesop Rock")].canonical, "Abstract Hip Hop")
        # Deftones tie (1 each): canonical is whichever the Counter saw first,
        # and both genres survive the round-trip into the allowed set.
        deftones = entries[gt.norm("Deftones")]
        self.assertEqual(deftones.canonical, "Nu Metal")
        self.assertEqual(
            deftones.allowed_norm,
            frozenset({gt.norm("Nu Metal"), gt.norm("Alternative Rock")}),
        )


class CommentRuleTests(unittest.TestCase):
    """M14: a comment is '#' + space/dash/end-of-line, so an artist whose name
    starts with '#' is data and survives the build -> parse round-trip."""

    def test_hash_leading_artist_is_data(self):
        entries = gt.parse_map(["#1 Dad\tIndie Rock"])
        self.assertIn(gt.norm("#1 Dad"), entries)
        self.assertEqual(entries[gt.norm("#1 Dad")].canonical, "Indie Rock")

    def test_marker_and_bare_comments_skipped(self):
        entries = gt.parse_map(["# --- added 2026-07-01 ---", "#", "# a note"])
        self.assertEqual(entries, {})

    def test_generated_comments_still_parse_as_comments(self):
        reduced = gt.reduce_artists(
            [
                FakeAD("Kendrick Lamar", "Conscious Hip Hop"),
                FakeAD("Kendrick Lamar", "Jazz Rap"),
            ]
        )
        rows = gt.build_rows(reduced)
        self.assertTrue(rows[0].startswith("# "))
        self.assertEqual(len(gt.parse_map(rows)), 1)  # comment row not data

    def test_round_trips_hash_artist(self):
        reduced = gt.reduce_artists([FakeAD("#1 Dad", "Indie Rock")])
        entries = gt.parse_map(gt.build_rows(reduced))
        self.assertEqual(entries[gt.norm("#1 Dad")].canonical, "Indie Rock")


class CmdBuildRebuildTests(unittest.TestCase):
    """M15: EXCLUDED (VA) artists must not re-append on every rebuild; a
    rebuild with no library change reports "No new artists"."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.map_path = Path(self._tmp.name) / "genre_map.tsv"

    def tearDown(self):
        self._tmp.cleanup()

    def _build(self, dirs):
        import contextlib
        import io

        orig = gt.scan_album_dirs
        gt.scan_album_dirs = lambda d, q, layout="{artist}/{album}", where=None: dirs
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = gt.run_genre_tidy_build(
                    self._tmp.name,
                    map_path=str(self.map_path),
                    quiet=True,
                )
        finally:
            gt.scan_album_dirs = orig
        return rc, buf.getvalue()

    def test_rebuild_with_va_comp_is_a_noop(self):
        dirs = [
            FakeAD("AFI", "Post-Hardcore"),
            FakeAD("Various Artists", "Pop"),
            FakeAD("Various Artists", "Rock"),
        ]
        rc, _ = self._build(dirs)
        self.assertEqual(rc, 0)
        first = self.map_path.read_text(encoding="utf-8")
        self.assertEqual(first.count("EXCLUDED (compilation)"), 1)
        rc, out = self._build(dirs)
        self.assertEqual(rc, 0)
        self.assertIn("No new artists", out)
        self.assertEqual(self.map_path.read_text(encoding="utf-8"), first)

    def test_multi_genre_count_excludes_other_comment_rows(self):
        # The count used to be "rows starting with #", which also swept up the
        # EXCLUDED and no-genre-tags comments and called them multi-genre.
        rc, out = self._build(
            [
                FakeAD("AFI", "Post-Hardcore"),
                FakeAD("AFI", "Punk"),  # the only genuinely multi-genre artist
                FakeAD("Deftones", "Alternative Metal"),
                FakeAD("Various Artists", "Pop"),
                FakeAD("Various Artists", "Rock"),
                FakeAD("Untagged Act", ""),
            ]
        )
        self.assertEqual(rc, 0)
        self.assertIn("1 artist(s) carry multiple genres", out)

    def test_genuinely_new_artist_still_appends(self):
        self._build([FakeAD("AFI", "Post-Hardcore")])
        rc, out = self._build(
            [FakeAD("AFI", "Post-Hardcore"), FakeAD("Deftones", "Alternative Metal")]
        )
        self.assertEqual(rc, 0)
        self.assertIn("Appended 1 new artist(s)", out)
        entries = gt.parse_map(self.map_path.read_text(encoding="utf-8").splitlines())
        self.assertIn(gt.norm("Deftones"), entries)


class SlashCanonicalConvergenceTests(unittest.TestCase):
    """H5 end-to-end: a slash canonical written through retag must read back
    compliant through lattice's TagBundle (what the scanner feeds apply), so a
    second apply plans zero retags instead of looping forever."""

    def _round_trip(self, src: Path, name: str) -> None:
        import shutil
        import tempfile

        from lattice.modes import retag

        from lattice.tags import get_all_tags

        canonical = "Emo / Orgcore"
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / name
            shutil.copy(src, p)
            self.assertTrue(retag.apply_genres(str(p), [canonical]))
            read_back = get_all_tags(str(p)).genre
            self.assertTrue(
                gt.is_compliant(read_back, frozenset({gt.norm(canonical)})),
                f"read back {read_back!r}, expected compliance with {canonical!r}",
            )

    def test_flac_round_trip_compliant(self):
        fixtures = Path(__file__).parent / "fixtures" / "library"
        self._round_trip(
            fixtures / "Aphex Twin" / "Selected Ambient Works" / "01 - Xtal.flac",
            "t.flac",
        )

    def test_mp3_round_trip_compliant(self):
        fixtures = Path(__file__).parent / "fixtures" / "library"
        self._round_trip(
            fixtures / "Cursive" / "Domestica" / "01 - The Casualty.mp3", "t.mp3"
        )


class CmdApplyTests(unittest.TestCase):
    """M16: a failed retag counts as an error, not a retag; M17: albums in
    formats retag cannot write are skipped with a reason, never invoked."""

    def setUp(self):
        import shutil
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.album = self.root / "Aphex Twin" / "Selected Ambient Works"
        self.album.mkdir(parents=True)
        fixtures = Path(__file__).parent / "fixtures" / "library"
        shutil.copy(
            fixtures / "Aphex Twin" / "Selected Ambient Works" / "01 - Xtal.flac",
            self.album / "01 - Xtal.flac",
        )
        self.map_path = self.root / "genre_map.tsv"
        self.map_path.write_text("Aphex Twin\tIDM\n", encoding="utf-8")
        self.RecAD = namedtuple("RecAD", "path artist genre")

    def tearDown(self):
        for dirpath, _dirs, files in os.walk(self.root):
            for f in files:
                os.chmod(os.path.join(dirpath, f), 0o644)
        self._tmp.cleanup()

    def _apply(self, records):
        import contextlib
        import io

        orig = gt.scan_album_dirs
        gt.scan_album_dirs = lambda d, q, layout="{artist}/{album}", where=None: records
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = gt.run_genre_tidy_apply(
                    str(self.root),
                    dry_run=False,
                    map_path=str(self.map_path),
                    quiet=True,
                )
        finally:
            gt.scan_album_dirs = orig
        return rc, buf.getvalue()

    def _log_text(self) -> str:
        return (self.root / "genre_tidy.log").read_text(encoding="utf-8")

    def test_successful_retag_counted(self):
        rec = self.RecAD(str(self.album), "Aphex Twin", "Ambient")
        rc, out = self._apply([rec])
        self.assertEqual(rc, 0)
        self.assertIn("retagged 1 album(s)", out)
        from lattice.modes import retag

        self.assertEqual(retag.read_genres(str(self.album / "01 - Xtal.flac")), ["IDM"])

    def test_failed_retag_counts_error_not_retagged(self):
        os.chmod(self.album / "01 - Xtal.flac", 0o444)
        rec = self.RecAD(str(self.album), "Aphex Twin", "Ambient")
        rc, out = self._apply([rec])
        # Nonzero on a failed retag, matching retag.py/rerate.py/replaygain.py:
        # apply used to exit 0 no matter how many writes failed.
        self.assertEqual(rc, 1)
        self.assertIn("retagged 0 album(s)", out)
        self.assertIn("1 retag error(s)", out)
        self.assertIn("ERR", self._log_text())

    def test_unwritable_format_skipped_with_reason(self):
        wav_album = self.root / "Field Recordist" / "Tapes"
        wav_album.mkdir(parents=True)
        (wav_album / "01.wav").write_bytes(b"RIFFxxxx")
        self.map_path.write_text("Field Recordist\tAmbient\n", encoding="utf-8")
        rec = self.RecAD(str(wav_album), "Field Recordist", "")
        rc, out = self._apply([rec])
        self.assertEqual(rc, 0)
        self.assertIn("retagged 0 album(s)", out)
        self.assertIn("skipped", out)
        self.assertIn("UNSUPPORTED FORMAT (skipped)", self._log_text())
        # Converges: a second apply reports exactly the same, no retag churn.
        _rc2, out2 = self._apply([rec])
        self.assertEqual(out.replace("\r", ""), out2.replace("\r", ""))


class ExcludedArtistsTests(unittest.TestCase):
    def test_common_va_forms_are_excluded(self):
        for name in ("Various Artists", "various", "VA"):
            self.assertIn(gt.norm(name), gt.EXCLUDED_ARTISTS)

    def test_build_flags_va_as_comment_without_data_row(self):
        reduced = gt.reduce_artists(
            [FakeAD("Various Artists", "Pop"), FakeAD("Various Artists", "Rock")]
        )
        rows = gt.build_rows(reduced)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].startswith("# Various Artists: EXCLUDED"))
        # No enforceable data row, so parse_map yields no entry to apply.
        self.assertNotIn(gt.norm("Various Artists"), gt.parse_map(rows))

    def test_va_data_row_still_not_parsed_as_enforceable(self):
        # Even a hand-written VA row parses (apply hard-skips it separately), but
        # build never emits one; this guards the build side.
        reduced = gt.reduce_artists([FakeAD("Various Artists", "Soundtrack")])
        self.assertFalse(any("\t" in r for r in gt.build_rows(reduced)))


class TsvSanitizeTests(unittest.TestCase):
    """GT2: a tab or newline inside a tag value must not corrupt the TSV. A
    genre "Rock\\tPop" used to become two allowed genres, so a fresh map was
    not a no-op; the sanitized field still matches the raw tag under norm()."""

    def test_tab_in_genre_stays_one_column(self):
        rows = gt.build_rows(gt.reduce_artists([FakeAD("AFI", "Rock\tPop")]))
        entries = gt.parse_map(rows)
        entry = entries[gt.norm("AFI")]
        self.assertEqual(entry.canonical, "Rock Pop")
        self.assertEqual(entry.allowed_norm, frozenset({gt.norm("Rock\tPop")}))
        self.assertTrue(gt.is_compliant("Rock\tPop", entry.allowed_norm))

    def test_newline_in_artist_stays_one_row(self):
        rows = gt.build_rows(gt.reduce_artists([FakeAD("Sig\nur Ros", "Post-Rock")]))
        self.assertTrue(all("\n" not in r for r in rows))
        self.assertIn(gt.norm("Sig\nur Ros"), gt.parse_map(rows))


class ParseMapCollisionTests(unittest.TestCase):
    """GT3: two hand-edited rows that normalize to the same artist used to
    last-win silently; the overwrite warns to stderr now."""

    def test_norm_key_collision_warns(self):
        import contextlib
        import io

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            entries = gt.parse_map(["Jay-Z\tHip Hop", "Jay‐Z\tRap"])
        self.assertIn("normalize to the same artist", err.getvalue())
        self.assertEqual(entries[gt.norm("Jay-Z")].canonical, "Rap")  # last wins

    def test_distinct_rows_do_not_warn(self):
        import contextlib
        import io

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            gt.parse_map(["Jay-Z\tHip Hop", "Nas\tHip Hop"])
        self.assertEqual(err.getvalue(), "")


class LayoutPassthroughTests(unittest.TestCase):
    """GT1: --layout reaches lattice's scanner, so a genre-foldered library
    recovers artist from the right path level for untagged files."""

    def test_scan_album_dirs_passes_layout_through(self):
        from unittest import mock

        seen = {}

        def fake_scan(roots, layout, pbar, where=None):
            seen["layout"] = layout
            return []

        with (
            mock.patch.object(gt, "_scan_album_dirs", fake_scan),
            mock.patch.object(gt, "count_audio_files", lambda roots: 0),
            mock.patch.object(
                gt, "_make_pbar", lambda total, label, quiet: mock.Mock()
            ),
        ):
            gt.scan_album_dirs(Path("/x"), True, "{genre}/{artist}/{album}")
        self.assertEqual(seen["layout"], "{genre}/{artist}/{album}")


class FoldTableDivergenceTests(unittest.TestCase):
    """A3 design point, resolved on purpose: the map key fold is NOT
    lattice.norm's normalize_name. normalize_name additionally strips
    apostrophes/quotes from the key ("Director's Cut" == "Directors Cut");
    re-keying saved maps on it would make build re-append every map artist
    whose name carries a quote as if it were new. The private table stays, and
    this pin documents the divergence so nobody 'simplifies' it away."""

    def test_quote_names_are_distinct_keys_here_but_not_in_norm(self):
        self.assertNotEqual(gt.norm("Director's Cut"), gt.norm("Directors Cut"))
        from lattice.norm import normalize_name

        self.assertEqual(
            normalize_name("Director's Cut"), normalize_name("Directors Cut")
        )

    def test_dash_and_case_folds_still_agree_with_norm(self):
        from lattice.norm import normalize_name

        self.assertEqual(gt.norm("Jay‐Z"), gt.norm("Jay-Z"))
        self.assertEqual(
            gt.norm("Belle & Sebastian"), normalize_name("Belle & Sebastian")
        )
        self.assertEqual(gt.norm("  The  XX "), normalize_name("  The  XX "))


if __name__ == "__main__":
    unittest.main()
