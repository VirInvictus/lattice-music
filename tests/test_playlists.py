import tempfile
import unittest
from collections import namedtuple
from pathlib import Path

from lattice.modes.playlists import (
    _evaluate_rule,
    check_playlist,
    run_check_playlists,
    validate_rule,
)

# Only the fields _evaluate_rule reads.
FakeTag = namedtuple(
    "FakeTag", "rating genre artist album title duration_s bitrate_kbps"
)


def tag(**kw):
    base = {
        "rating": None,
        "genre": None,
        "artist": None,
        "album": None,
        "title": None,
        "duration_s": None,
        "bitrate_kbps": None,
    }
    base.update(kw)
    return FakeTag(**base)


class ValidateRuleTests(unittest.TestCase):
    """T6g: a rule that can never evaluate is one error before the walk, not
    one stderr line per track (the TUI paged thousands of identical lines)."""

    def test_valid_rules_pass(self):
        for rule in ("", "rating >= 4", "rating >= 4 AND genre == 'Jazz'"):
            self.assertIsNone(validate_rule(rule), rule)

    def test_syntax_error_is_reported(self):
        self.assertIsNotNone(validate_rule("rating >="))

    def test_unknown_field_is_reported(self):
        err = validate_rule("stars >= 4")
        self.assertIsNotNone(err)
        self.assertIn("stars", err)

    def test_disallowed_construct_is_reported(self):
        self.assertIsNotNone(validate_rule("__import__('os')"))

    def test_division_by_numeric_field_is_valid(self):
        # The dummy metadata is all zeros; dividing by a field is a
        # data-dependent outcome, not a structural error, and real tracks
        # never carry duration/bitrate 0.
        self.assertIsNone(validate_rule("bitrate / duration > 200"))


class RuleEvalTests(unittest.TestCase):
    def test_empty_rule_matches_everything(self):
        self.assertTrue(_evaluate_rule("", tag(), {}))
        self.assertTrue(_evaluate_rule("   ", tag(), {}))

    def test_numeric_comparison(self):
        self.assertTrue(_evaluate_rule("rating >= 4", tag(rating=5.0), {}))
        self.assertFalse(_evaluate_rule("rating >= 4", tag(rating=3.0), {}))

    def test_string_equality_and_membership(self):
        self.assertTrue(_evaluate_rule("genre == 'Jazz'", tag(genre="Jazz"), {}))
        self.assertTrue(_evaluate_rule("'azz' in genre", tag(genre="Jazz"), {}))
        self.assertFalse(_evaluate_rule("genre == 'Rock'", tag(genre="Jazz"), {}))

    def test_boolean_and_or(self):
        t = tag(rating=5.0, genre="Jazz")
        self.assertTrue(_evaluate_rule("rating >= 4 and genre == 'Jazz'", t, {}))
        self.assertFalse(_evaluate_rule("rating >= 4 and genre == 'Rock'", t, {}))
        self.assertTrue(_evaluate_rule("rating < 2 or genre == 'Jazz'", t, {}))

    def test_sql_style_and_or_convenience(self):
        t = tag(rating=5.0, genre="Jazz")
        self.assertTrue(_evaluate_rule("rating >= 4 AND genre == 'Jazz'", t, {}))
        self.assertTrue(_evaluate_rule("rating < 2 OR genre == 'Jazz'", t, {}))

    def test_sql_keywords_inside_string_literals_survive(self):
        # The old str.replace rewrote ' AND ' inside quoted strings too, so
        # this genre could never match.
        t = tag(genre="Drum AND Bass")
        self.assertTrue(_evaluate_rule("genre == 'Drum AND Bass'", t, {}))
        self.assertTrue(_evaluate_rule('genre == "Drum AND Bass"', t, {}))
        t2 = tag(rating=5.0, genre="Drum AND Bass")
        self.assertTrue(
            _evaluate_rule("rating >= 4 AND genre == 'Drum AND Bass'", t2, {})
        )
        self.assertFalse(
            _evaluate_rule("rating < 2 AND genre == 'Drum AND Bass'", t2, {})
        )

    def test_sql_keywords_without_surrounding_spaces(self):
        # Word-bounded matching folds AND/OR even without padding spaces.
        t = tag(rating=5.0, genre="Jazz")
        self.assertTrue(_evaluate_rule("(rating >= 4)AND(genre == 'Jazz')", t, {}))

    def test_chained_comparison(self):
        self.assertTrue(_evaluate_rule("2 <= rating <= 4", tag(rating=3.0), {}))
        self.assertFalse(_evaluate_rule("2 <= rating <= 4", tag(rating=5.0), {}))

    def test_layout_fallback_fields(self):
        self.assertTrue(
            _evaluate_rule("artist == 'Aphex Twin'", tag(), {"artist": "Aphex Twin"})
        )

    # --- security: the old eval() sandbox was escapable; these must NOT run ---

    def test_attribute_access_is_rejected(self):
        # The classic sandbox escape; must be refused, not executed.
        self.assertFalse(
            _evaluate_rule("genre.__class__.__mro__[-1]", tag(genre="x"), {})
        )

    def test_dunder_and_calls_are_rejected(self):
        self.assertFalse(_evaluate_rule("__import__('os')", tag(), {}))
        self.assertFalse(_evaluate_rule("open('/etc/passwd')", tag(), {}))

    def test_unknown_field_is_rejected(self):
        self.assertFalse(_evaluate_rule("bogus == 1", tag(), {}))

    def test_subscript_is_rejected(self):
        self.assertFalse(_evaluate_rule("genre[0] == 'J'", tag(genre="Jazz"), {}))


class CheckPlaylistTests(unittest.TestCase):
    def _pl(self, td: str, name: str = "mix.m3u", lines: list[str] = ()) -> Path:
        p = Path(td) / name
        p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return p

    def test_clean_absolute_playlist(self):
        with tempfile.TemporaryDirectory() as td:
            track = Path(td) / "01.flac"
            track.write_bytes(b"")
            pl = self._pl(
                td, lines=["#EXTM3U", "#EXTINF:120,Artist - Title", str(track)]
            )
            has_header, entries, missing, missing_rows = check_playlist(pl)
            self.assertTrue(has_header)
            self.assertEqual((entries, missing, missing_rows), (1, 0, []))

    def test_missing_targets_are_listed_with_line_numbers(self):
        with tempfile.TemporaryDirectory() as td:
            pl = self._pl(
                td,
                lines=[
                    "#EXTM3U",
                    str(Path(td) / "gone.flac"),
                    str(Path(td) / "also-gone.flac"),
                ],
            )
            has_header, entries, missing, rows = check_playlist(pl)
            self.assertEqual((has_header, entries, missing), (True, 2, 2))
            self.assertEqual([r[0] for r in rows], [2, 3])

    def test_missing_extm3u_header_is_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            track = Path(td) / "01.flac"
            track.write_bytes(b"")
            pl = self._pl(td, lines=[str(track)])
            has_header, entries, missing, _rows = check_playlist(pl)
            self.assertFalse(has_header)
            self.assertEqual((entries, missing), (1, 0))

    def test_relative_entries_resolve_against_the_playlist(self):
        with tempfile.TemporaryDirectory() as td:
            sub = Path(td) / "playlists"
            sub.mkdir()
            track = Path(td) / "Album" / "01.flac"
            track.parent.mkdir()
            track.write_bytes(b"")
            pl = sub / "rel.m3u"
            pl.write_text("../Album/01.flac\n", encoding="utf-8")
            _h, entries, missing, _rows = check_playlist(pl)
            self.assertEqual((entries, missing), (1, 0))

    def test_empty_playlist(self):
        with tempfile.TemporaryDirectory() as td:
            pl = self._pl(td, lines=["#EXTM3U"])
            has_header, entries, missing, _rows = check_playlist(pl)
            self.assertTrue(has_header)
            self.assertEqual((entries, missing), (0, 0))


class RunCheckPlaylistsTests(unittest.TestCase):
    def test_report_and_verbose(self):
        with tempfile.TemporaryDirectory() as td:
            gone = Path(td) / "Album" / "01.flac"
            gone.parent.mkdir()
            gone.write_bytes(b"")
            live = Path(td) / "Album" / "02.flac"
            live.write_bytes(b"")
            (Path(td) / "good.m3u").write_text(f"#EXTM3U\n{live}\n", encoding="utf-8")
            (Path(td) / "stale.m3u").write_text(
                f"#EXTM3U\n{gone}\n{Path(td) / 'vanished.flac'}\n",
                encoding="utf-8",
            )
            out = Path(td) / "check.txt"
            rc = run_check_playlists([td], str(out), quiet=True)
            self.assertEqual(rc, 0)
            report = out.read_text(encoding="utf-8")
            self.assertIn("PLAYLIST CHECK REPORT", report)
            self.assertIn("stale.m3u (1 of 2 missing)", report)
            self.assertIn("vanished.flac", report)
            self.assertNotIn("good.m3u", report)

            out_v = Path(td) / "check_v.txt"
            run_check_playlists([td], str(out_v), verbose=True, quiet=True)
            self.assertIn("CLEAN (1)", out_v.read_text(encoding="utf-8"))
            self.assertIn("good.m3u", out_v.read_text(encoding="utf-8"))

    def test_no_playlists_found(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "check.txt"
            rc = run_check_playlists([td], str(out), quiet=True)
            self.assertEqual(rc, 0)
            self.assertIn("No .m3u", out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
