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
import json
import os
import re
import unittest
from pathlib import Path
from unittest import mock

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


class ColorTests(unittest.TestCase):
    CSI = re.compile(r"\x1b\[[0-9;]*m")

    class _TTY(io.StringIO):
        def isatty(self):
            return True

    def test_colored_render_strips_to_the_plain_render(self):
        # The invariant behind the visible-length column math: ANSI codes
        # never change the layout, only decorate it.
        for dest in ("clean", "auditBitrate", "retag", "health", "diff_snapshot"):
            colored = help_mod.render_mode(dest, color=True)
            self.assertEqual(
                self.CSI.sub("", colored),
                help_mod.render_mode(dest, color=False),
            )
        self.assertEqual(
            self.CSI.sub("", help_mod.render_index(color=True)),
            help_mod.render_index(color=False),
        )

    def test_colored_output_uses_the_cpython_argparse_theme(self):
        idx = help_mod.render_index(color=True)
        self.assertIn("\x1b[1;34mAUDITS (read-only reports)\x1b[0m", idx)
        self.assertIn("\x1b[1;36m--library\x1b[0m", idx)
        self.assertIn("\x1b[1;32m-h\x1b[0m", idx)
        self.assertIn("\x1b[1;35mlattice\x1b[0m", idx)
        page = help_mod.render_mode("retag", color=True)
        self.assertIn("\x1b[1;36m--retag\x1b[0m", page)
        self.assertIn("\x1b[1;33mGENRE\x1b[0m", page)

    def test_piped_help_carries_no_ansi_codes(self):
        # StringIO stdout is never a TTY, so auto-detection must go plain:
        # this is the README-embedding and `| cat` face.
        for argv in (["--help"], ["help", "clean"], ["--clean", "--help"]):
            rc, out = _main(argv)
            self.assertEqual(rc, 0)
            self.assertNotIn("\x1b", out)

    def test_can_color_env_contract(self):
        # FORCE_COLOR wins, NO_COLOR and PYTHON_COLORS=0 suppress, and the
        # fallback is the stream's TTY-ness.
        pipe = io.StringIO()
        with mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}):
            self.assertTrue(help_mod._can_color(pipe))
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertFalse(help_mod._can_color(self._TTY()))
        with mock.patch.dict(os.environ, {"PYTHON_COLORS": "0"}):
            self.assertFalse(help_mod._can_color(self._TTY()))
        self.assertFalse(help_mod._can_color(pipe))
        self.assertTrue(help_mod._can_color(self._TTY()))

    def test_legend_names_the_per_mode_pages(self):
        idx = help_mod.render_index(color=False)
        self.assertIn("Every mode above has a full help page:", idx)
        self.assertIn("lattice help MODE", idx)


if __name__ == "__main__":
    unittest.main()


class ProbeTruthTests(unittest.TestCase):
    """The AI-grokability probe's findings, fixed: the contradictions are
    gone, the where-mode list and exit codes are stated, and the machine
    surface exists."""

    def test_stats_example_uses_a_real_grammar_field(self):
        # 'format' is not in the rule grammar; the old example could only
        # fail at runtime with a RuleError
        stats = next(m for m in help_mod.MODES if m.dest == "stats")
        self.assertNotIn("format ==", stats.example)
        for field in help_mod.RULE_FIELDS:
            pass
        self.assertIn("genre ==", stats.example)

    def test_rule_fields_exclude_format_and_help_says_so(self):
        source = Path(help_mod.__file__).read_text()
        self.assertNotIn("format ==", source)
        self.assertIn("no format field", source)

    def test_diff_example_matches_usage_order(self):
        page = next(m for m in help_mod.MODES if m.dest == "diff_snapshot")
        # usage: SNAPSHOT then ROOT; the example must read the same order
        self.assertLess(page.usage.index("SNAPSHOT"), page.usage.index("[ROOT]"))
        self.assertLess(
            page.example.index("before-reorg.tsv"), page.example.rindex("~/Music")
        )

    def test_where_entry_names_every_mode_flag(self):
        parser = cli.build_parser()
        flags = {}
        for action in parser._actions:
            if action.option_strings:
                flags[action.dest] = action.option_strings[0]
        where_text = help_mod.SHARED["where"].text
        for dest in cli._WHERE_MODES:
            self.assertIn(flags[dest], where_text, dest)
        self.assertIn("exit 2", where_text)

    def test_json_entry_names_the_envelope_keys(self):
        text = help_mod.SHARED["json"].text
        for key in ("mode", "root", "findings", "payload", "dry_run", "counts"):
            self.assertIn(key, text, key)

    def test_index_carries_exit_codes_and_the_machine_surface(self):
        rc, index = _main(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("exit codes:", index)
        self.assertIn("lattice help --json", index)

    def test_help_json_is_valid_complete_deterministic_plain(self):
        rc, first = _main(["help", "--json"])
        self.assertEqual(rc, 0)
        import json as jsonlib

        data = jsonlib.loads(first)
        self.assertEqual(data["tool"], "lattice")
        self.assertEqual(len(data["modes"]), len(help_mod.MODES))
        self.assertNotIn("format", data["rule_fields"])
        self.assertEqual(sorted(data["exit_codes"]), ["0", "1", "130", "2"])
        self.assertEqual(first, _main(["help", "--json"])[1])
        self.assertNotIn("\x1b", first)

    def test_help_dash_dash_mode_is_a_per_mode_form(self):
        rc, out = _main(["--help", "stats"])
        self.assertEqual(rc, 0)
        self.assertIn("--stats", out)
        rc, err_buf = _err_main(["--help", "wat"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown help topic", err_buf)


def _err_main(argv):
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = cli.main(argv)
    return rc, buf.getvalue()


class AuditTruthTests(unittest.TestCase):
    """The 12-agent audit's findings, pinned: the exit legend tells the
    truth (integrity modes exit 1 on CORRUPT without a flag), the two
    dry-run-promise exceptions are labeled, the grammar carries its real
    operators and units, and help --json is a complete agent surface."""

    def test_exit_legend_names_the_integrity_and_failure_classes(self):
        rc, index = _main(["--help"])
        self.assertEqual(rc, 0)
        self.assertIn("integrity scans on any CORRUPT file", index)
        # the old, falsified legend must not come back
        self.assertNotIn("only with --fail-on-findings); 2 usage", index)
        data = json.loads(_main(["help", "--json"])[1])
        self.assertIn("CORRUPT", data["exit_codes"]["1"])

    def test_integrity_pages_carry_tiers_and_exits(self):
        for dest in ("testFLAC", "testMP3", "testOpus", "testWAV", "testWMA"):
            mode = next(m for m in help_mod.MODES if m.dest == dest)
            detail = " ".join(mode.detail.split())
            self.assertIn("CORRUPT", detail, dest)
            self.assertIn("exits 1", detail.lower(), dest)
            self.assertIn("CORRUPT", detail, dest)  # every shape names it

    def test_write_promise_exceptions_are_labeled(self):
        extract = next(m for m in help_mod.MODES if m.dest == "extractArt")
        self.assertIn("WRITES by default", extract.summary)
        build = next(m for m in help_mod.MODES if m.dest == "genre_tidy_build")
        self.assertIn("no dry-run/apply gate", build.detail)

    def test_grammar_carries_operators_units_and_scale(self):
        data = json.loads(_main(["help", "--json"])[1])
        grammar = data["rule_grammar"]
        self.assertEqual(grammar["fields_with_units"]["duration"], "seconds")
        self.assertEqual(grammar["fields_with_units"]["bitrate"], "kbps")
        self.assertIn("in", grammar["operators"])
        playlist = next(m for m in help_mod.MODES if m.dest == "playlist")
        self.assertIn("not in", playlist.detail)

    def test_help_json_carries_own_options_and_shared(self):
        data = json.loads(_main(["help", "--json"])[1])
        clean = next(m for m in data["modes"] if m["dest"] == "clean")
        self.assertTrue(any(o["flag"] == "--all" for o in clean["own_options"]))
        self.assertIn("apply", clean["shared"])
        self.assertTrue(all("shared" in m for m in data["modes"]))

    def test_rsgain_requirement_is_apply_scoped(self):
        mode = next(m for m in help_mod.MODES if m.dest == "replaygain")
        detail = " ".join(mode.detail.split())
        self.assertIn("Requires rsgain (exit 2 without it)", detail)
