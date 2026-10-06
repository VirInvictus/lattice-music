"""Tests for the two-level help surface (lattice/help.py + the cli.py
interception). Two layers are pinned:

1. The registry cannot drift from the parser or from cli.py's enforcement
   sets: every mode flag has an entry, every option a page lists exists on
   the parsed namespace, and where/json/fail-on-findings applicability is
   exactly the _WHERE_MODES / _JSON_MODES / _FAIL_MODES sets.
2. The interception behaves: `lattice help` and bare --help print the
   index (exit 0), `lattice help MODE` and `MODE --help` print that mode's
   page, and an unknown topic is a usage error (exit 2)."""

import contextlib
import io
import unittest

from lattice import cli
from lattice import help as help_mod


def _main(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(argv)
    return rc, buf.getvalue()


def _parser_mode_dests():
    parser = cli.build_parser()
    dests = set()
    for group in parser._mutually_exclusive_groups:
        dests.update(action.dest for action in group._group_actions)
    return dests


class RegistryCompletenessTests(unittest.TestCase):
    def test_every_parser_mode_flag_has_a_registry_entry(self):
        registered = {m.dest for m in help_mod.MODES}
        self.assertEqual(_parser_mode_dests(), registered)

    def test_every_registry_flag_parses(self):
        parser = cli.build_parser()
        for mode in help_mod.MODES:
            token = mode.flag.split()[0]
            # --diff consumes the snapshot path; everything else is a
            # bare switch.
            argv = [token] + (["snapshot.tsv"] if token == "--diff" else [])
            args = parser.parse_args(argv)
            self.assertTrue(
                getattr(args, mode.dest),
                f"{mode.flag} did not set dest {mode.dest}",
            )

    def test_every_listed_option_dest_exists_on_the_namespace(self):
        parser = cli.build_parser()
        args = parser.parse_args([])
        for mode in help_mod.MODES:
            for opt in mode.own:
                self.assertTrue(
                    hasattr(args, opt.dest),
                    f"{mode.flag} lists nonexistent option dest {opt.dest}",
                )
            for dest in mode.shared:
                self.assertTrue(
                    hasattr(args, help_mod.SHARED[dest].dest),
                    f"{mode.flag} lists nonexistent shared dest {dest}",
                )

    def test_where_json_fail_applicability_matches_the_enforcement_sets(self):
        for mode in help_mod.MODES:
            self.assertEqual(
                "where" in mode.shared,
                mode.dest in cli._WHERE_MODES,
                f"{mode.flag}: --where applicability drifted from _WHERE_MODES",
            )
            self.assertEqual(
                "json" in mode.shared,
                mode.dest in cli._JSON_MODES,
                f"{mode.flag}: --json applicability drifted from _JSON_MODES",
            )
            self.assertEqual(
                "fail_on_findings" in mode.shared,
                mode.dest in cli._FAIL_MODES,
                f"{mode.flag}: --fail-on-findings applicability drifted "
                "from _FAIL_MODES",
            )

    def test_categories_cover_every_mode_and_every_mode_renders(self):
        category_keys = {key for key, _ in help_mod.CATEGORIES}
        for mode in help_mod.MODES:
            self.assertIn(mode.category, category_keys)
            page = help_mod.render_mode(mode.dest)
            self.assertIn((mode.display or mode.flag).split()[0], page)
            self.assertIn("usage:", page)
            self.assertIn("example:", page)

    def test_index_lists_every_category_and_every_mode(self):
        index = help_mod.render_index()
        for _, heading in help_mod.CATEGORIES:
            self.assertIn(heading, index)
        for mode in help_mod.MODES:
            self.assertIn(mode.display or mode.flag, index)

    def test_no_em_dashes_in_any_user_visible_help_text(self):
        for mode in help_mod.MODES:
            page = help_mod.render_mode(mode.dest)
            self.assertNotIn("\u2014", page)
            self.assertNotIn("\u2013", page)
        self.assertNotIn("\u2014", help_mod.render_index())


class TopicResolutionTests(unittest.TestCase):
    def test_topic_matching_is_case_and_dash_insensitive(self):
        self.assertEqual(help_mod.resolve_topic("clean"), "clean")
        self.assertEqual(help_mod.resolve_topic("auditTags"), "auditTags")
        self.assertEqual(help_mod.resolve_topic("audit-tags"), "auditTags")
        self.assertEqual(help_mod.resolve_topic("audittags"), "auditTags")
        self.assertEqual(
            help_mod.resolve_topic("--genreTidy-build"), "genre_tidy_build"
        )
        self.assertEqual(help_mod.resolve_topic("genre_tidy_build"), "genre_tidy_build")
        self.assertIsNone(help_mod.resolve_topic("bogus"))
        self.assertIsNone(help_mod.resolve_topic(""))


class HelpPathTests(unittest.TestCase):
    def test_bare_help_flag_prints_the_index_and_exits_zero(self):
        for argv in (["--help"], ["-h"], ["help"]):
            rc, out = _main(argv)
            self.assertEqual(rc, 0)
            self.assertIn("usage: lattice MODE [ROOT] [options]", out)
            self.assertIn("LIBRARY TREES & EXPORTS", out)
            self.assertIn("--auditBitrate", out)

    def test_help_topic_prints_the_mode_page(self):
        rc, out = _main(["help", "clean"])
        self.assertEqual(rc, 0)
        self.assertIn("--clean: consolidate fragmented album folders", out)
        self.assertIn("--normalize-tags", out)
        self.assertIn("--apply", out)

    def test_help_topic_accepts_flag_spellings_and_any_case(self):
        rc, out = _main(["help", "--genreTidy-Build"])
        self.assertEqual(rc, 0)
        self.assertIn("--genreTidy-build", out)

    def test_mode_flag_with_help_prints_that_modes_page(self):
        rc, out = _main(["--auditBitrate", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("--auditBitrate: files below a bitrate floor", out)
        self.assertIn("--min-bitrate", out)
        self.assertNotIn("LIBRARY TREES & EXPORTS", out)

    def test_retag_page_covers_the_positionals(self):
        rc, out = _main(["--retag", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("lattice --retag DIR GENRE [GENRE...]", out)
        self.assertIn("--strip-junk", out)

    def test_two_mode_flags_fall_back_to_the_index(self):
        rc, out = _main(["--clean", "--apestrip", "--help"])
        self.assertEqual(rc, 0)
        self.assertIn("LIBRARY TREES & EXPORTS", out)

    def test_unknown_topic_is_a_usage_error(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = cli.main(["help", "bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown help topic", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
