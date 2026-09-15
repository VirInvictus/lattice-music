"""Tests for interactive_menu's wiring through vir-tui 2.2.0's
interactive_session(): utils.IN_TUI selects the vir_tui progress box over
captured tqdm output, and the session screen lifecycle is vir-tui's.
Also the cli/tui kwargs-parity pins: the house rule is that both faces
build the same kwargs and call the same modes/* functions directly, and
the c6575dc migration silently broke exactly that (the dead smart
playlist item, the dropped layout kwarg and toggles, the roots[0]
passthrough); these tests fail on any recurrence."""

import inspect
import unittest
from pathlib import Path
from unittest import mock

from lattice import cli, tui, utils
from lattice.modes.audit import (
    run_health_score,
    run_replaygain_audit,
    run_stray_audit,
    run_verify_replaygain,
)
from lattice.modes.integrity import run_flac_mode
from lattice.modes.library import (
    write_ai_library,
    write_ai_wings,
    write_all_wings,
    write_music_library_tree,
)
from lattice.modes.playlists import generate_playlist
from lattice.modes.stats import run_stats


class _FakeSession:
    """Stands in for vir_tui.interactive_session in wiring tests."""

    def __init__(self, screen):
        self.screen = screen

    def __enter__(self):
        return self.screen

    def __exit__(self, *exc):
        return False


class InteractiveMenuWiringTests(unittest.TestCase):
    def tearDown(self):
        utils.IN_TUI = False

    def _run_menu(self, screen):
        seen = {}

        def body():
            seen["in_tui"] = utils.IN_TUI
            return 0

        with (
            mock.patch.object(
                tui, "interactive_session", return_value=_FakeSession(screen)
            ),
            mock.patch.object(tui, "_menu_session", side_effect=body),
        ):
            rc = tui.interactive_menu()
        return rc, seen

    def test_curses_session_selects_tui_progress_semantics(self):
        sentinel = object()
        rc, seen = self._run_menu(sentinel)
        self.assertEqual(rc, 0)
        self.assertTrue(seen["in_tui"])
        self.assertFalse(utils.IN_TUI)

    def test_no_curses_keeps_cli_progress_semantics(self):
        _rc, seen = self._run_menu(None)
        self.assertFalse(seen["in_tui"])
        self.assertFalse(utils.IN_TUI)


class _PromptScript:
    """Answers vir_tui prompts by substring, so each parity case states only
    the values it cares about. An unscripted prompt fails the test rather
    than silently taking a default."""

    def __init__(self, answers):
        self.answers = answers

    def __call__(self, prompt, *args, **kwargs):
        for needle, value in self.answers.items():
            if needle in prompt:
                return value
        raise AssertionError(f"unscripted prompt: {prompt!r}")


def _bound(fn, args, kwargs):
    """Bind a recorded call onto the real mode function's signature so
    positional/keyword shape differences between the faces cancel out."""
    sig = inspect.signature(fn)
    ba = sig.bind(*args, **kwargs)
    ba.apply_defaults()
    return dict(ba.arguments)


class CliTuiKwargsParityTests(unittest.TestCase):
    """Both faces must build the same kwargs for the same mode. Each case
    records the CLI dispatch call and the TUI run_with_capture call with the
    real modes patched out, binds both onto the mode's signature, and
    compares."""

    ROOTS = [str((Path(__file__).parent / "fixtures" / "library").resolve())]
    LAYOUT = "{artist}/{album}"
    OUT = "parity_out.txt"
    RULE = "rating >= 4"

    def _parity(
        self,
        *,
        mode,  # name of the mode function, identical in cli and tui namespaces
        real,  # the real function, for signature binding
        cli_argv,
        selections,  # scripted menu returns, ending with the exit choice
        answers,
        main_menu,  # True: entries in _menu_session; False: _library_submenu
    ):
        prompt = _PromptScript(answers)
        select = "_select_main" if main_menu else "_select_library"
        with (
            mock.patch.object(cli, mode, return_value=0) as cli_mode,
            mock.patch.object(tui, mode) as tui_mode,
            mock.patch.object(tui, "run_with_capture") as capture,
            mock.patch.object(tui, "reset_terminal"),
            mock.patch.object(tui, select, side_effect=list(selections)),
            mock.patch.object(tui, "get_library_roots", return_value=self.ROOTS),
            mock.patch.object(tui, "get_library_root", return_value=self.ROOTS[0]),
            mock.patch.object(tui, "get_layout", return_value=self.LAYOUT),
            mock.patch.object(tui, "ask", side_effect=prompt),
            mock.patch.object(tui, "ask_yn", side_effect=prompt),
            mock.patch.object(tui, "prompt_int", side_effect=prompt),
            mock.patch.object(tui, "prompt_out", side_effect=prompt),
        ):
            cli.main(cli_argv)
            if main_menu:
                tui._menu_session()
            else:
                tui._library_submenu(list(self.ROOTS))

        cli_args, cli_kwargs = cli_mode.call_args
        capture_args, capture_kwargs = capture.call_args
        _title, tui_func, *tui_args = capture_args
        tui_kwargs = dict(capture_kwargs)
        tui_kwargs.pop("footer", None)  # consumed by run_with_capture itself

        # The TUI must target the same mode function the CLI dispatches to.
        self.assertIs(tui_func, tui_mode)
        self.assertEqual(
            _bound(real, cli_args, cli_kwargs), _bound(real, tui_args, tui_kwargs)
        )

    def test_library_tree(self):
        self._parity(
            mode="write_music_library_tree",
            real=write_music_library_tree,
            cli_argv=[
                "--library",
                "--root",
                self.ROOTS[0],
                "--output",
                self.OUT,
                "--layout",
                self.LAYOUT,
            ],
            selections=[(0, 0), tui._SEL_LIB_BACK],
            answers={"Output file": self.OUT, "genres": False, "layout": self.LAYOUT},
            main_menu=False,
        )

    def test_library_tree_with_genres(self):
        # The --genres face and the TUI toggle must agree when on, too.
        self._parity(
            mode="write_music_library_tree",
            real=write_music_library_tree,
            cli_argv=[
                "--library",
                "--root",
                self.ROOTS[0],
                "--output",
                self.OUT,
                "--genres",
                "--layout",
                self.LAYOUT,
            ],
            selections=[(0, 0), tui._SEL_LIB_BACK],
            answers={"Output file": self.OUT, "genres": True, "layout": self.LAYOUT},
            main_menu=False,
        )

    def test_ai_library(self):
        self._parity(
            mode="write_ai_library",
            real=write_ai_library,
            cli_argv=[
                "--ai-library",
                "--root",
                self.ROOTS[0],
                "--output",
                self.OUT,
                "--layout",
                self.LAYOUT,
            ],
            selections=[(0, 1), tui._SEL_LIB_BACK],
            answers={"Output file": self.OUT, "layout": self.LAYOUT},
            main_menu=False,
        )

    def test_all_wings(self):
        self._parity(
            mode="write_all_wings",
            real=write_all_wings,
            cli_argv=[
                "--all-wings",
                "--root",
                self.ROOTS[0],
                "--output",
                "wings_parity",
                "--genres",
                "--paths",
                "--layout",
                self.LAYOUT,
            ],
            selections=[(0, 2), tui._SEL_LIB_BACK],
            answers={
                "Output directory": "wings_parity",
                "genres": True,
                "paths": True,
                "layout": self.LAYOUT,
            },
            main_menu=False,
        )

    def test_ai_wings(self):
        self._parity(
            mode="write_ai_wings",
            real=write_ai_wings,
            cli_argv=[
                "--ai-wings",
                "--root",
                self.ROOTS[0],
                "--output",
                "wings_ai_parity",
                "--layout",
                self.LAYOUT,
            ],
            selections=[(0, 3), tui._SEL_LIB_BACK],
            answers={
                "Output directory": "wings_ai_parity",
                "layout": self.LAYOUT,
            },
            main_menu=False,
        )

    def test_smart_playlist(self):
        # The regression: the menu item called generate_playlist without the
        # required rule and TypeError'd into the error pager on every run.
        self._parity(
            mode="generate_playlist",
            real=generate_playlist,
            cli_argv=[
                "--playlist",
                "--root",
                self.ROOTS[0],
                "--output",
                self.OUT,
                "--rule",
                self.RULE,
                "--layout",
                self.LAYOUT,
            ],
            selections=[(0, 4), tui._SEL_LIB_BACK],
            answers={
                "Output file": self.OUT,
                "rule": self.RULE,
                "layout": self.LAYOUT,
            },
            main_menu=False,
        )

    def test_flac_integrity(self):
        # The FLAC entry is parity-pinned too: prefer + the ffmpeg path (the
        # CLI's --ffmpeg never reached run_flac_mode before) + resume.
        self._parity(
            mode="run_flac_mode",
            real=run_flac_mode,
            cli_argv=[
                "--testFLAC",
                "--root",
                self.ROOTS[0],
                "--output",
                self.OUT,
                "--prefer",
                "ffmpeg",
                "--ffmpeg",
                "/usr/bin/ffmpeg",
            ],
            selections=[(1, 0), tui._SEL_QUIT],
            answers={
                "Output file": self.OUT,
                "Workers": 4,
                "Preferred tool": "ffmpeg",
                "ffmpeg path": "/usr/bin/ffmpeg",
                "Resume": False,
            },
            main_menu=True,
        )

    def test_stats(self):
        self._parity(
            mode="run_stats",
            real=run_stats,
            cli_argv=[
                "--stats",
                "--root",
                self.ROOTS[0],
                "--output",
                self.OUT,
                "--layout",
                self.LAYOUT,
            ],
            selections=[(0, 1), tui._SEL_QUIT],
            answers={"Output file": self.OUT, "layout": self.LAYOUT},
            main_menu=True,
        )

    def test_replaygain_audit(self):
        self._parity(
            mode="run_replaygain_audit",
            real=run_replaygain_audit,
            cli_argv=[
                "--auditReplayGain",
                "--root",
                self.ROOTS[0],
                "--output",
                self.OUT,
                "--verbose",
            ],
            selections=[(3, 4), tui._SEL_QUIT],
            answers={"Output file": self.OUT, "fully-tagged": True},
            main_menu=True,
        )

    def test_verify_replaygain(self):
        self._parity(
            mode="run_verify_replaygain",
            real=run_verify_replaygain,
            cli_argv=[
                "--verifyReplayGain",
                "--root",
                self.ROOTS[0],
                "--output",
                self.OUT,
                "--target-lufs",
                "-14",
                "--tolerance",
                "0.3",
                "--verbose",
            ],
            selections=[(3, 5), tui._SEL_QUIT],
            answers={
                "Output file": self.OUT,
                "Target loudness": -14,
                "Tolerance": "0.3",
                "fully-correct": True,
            },
            main_menu=True,
        )

    def test_stray_audit(self):
        self._parity(
            mode="run_stray_audit",
            real=run_stray_audit,
            cli_argv=[
                "--auditStrays",
                "--root",
                self.ROOTS[0],
                "--output",
                self.OUT,
                "--layout",
                self.LAYOUT,
            ],
            selections=[(3, 6), tui._SEL_QUIT],
            answers={"Output file": self.OUT, "layout": self.LAYOUT},
            main_menu=True,
        )

    def test_health_score(self):
        self._parity(
            mode="run_health_score",
            real=run_health_score,
            cli_argv=[
                "--healthScore",
                "--root",
                self.ROOTS[0],
                "--output",
                self.OUT,
                "--verbose",
            ],
            selections=[(3, 7), tui._SEL_QUIT],
            answers={
                "Output file": self.OUT,
                "bitrate floor": 192,
                "resolution": 500,
                "perfect-score": True,
            },
            main_menu=True,
        )


class RootPassthroughTests(unittest.TestCase):
    """The multi-root contract: a library_roots config reaches the modes as
    the full filtered list (as the CLI passes it), the title says how many,
    and the write modes refuse a multi-root config instead of silently
    picking the first tree."""

    ROOTS = ["/data/Music", "/mnt/usb/Albums"]

    def _session(self, roots, selection, existing=None):
        existing = set(roots) if existing is None else set(existing)
        notified = []
        with (
            mock.patch.object(tui, "get_library_roots", return_value=list(roots)),
            mock.patch.object(
                tui, "get_library_root", return_value=roots[0] if roots else None
            ),
            mock.patch.object(
                tui.os.path, "isdir", side_effect=lambda p: p in existing
            ),
            mock.patch.object(tui, "reset_terminal"),
            mock.patch.object(
                tui, "_select_main", side_effect=[selection, tui._SEL_QUIT]
            ) as sel,
            mock.patch.object(tui, "notify", side_effect=notified.append),
        ):
            rc = tui._menu_session()
        title = sel.call_args[0][0] if sel.call_args else None
        return rc, title, notified

    def test_title_counts_multiple_roots(self):
        rc, title, _notified = self._session(self.ROOTS, tui._SEL_QUIT)
        self.assertEqual(rc, 0)
        self.assertEqual(title, "lattice-music (2 roots)")

    def test_missing_root_stops_the_session(self):
        vanished = ["/gone/Music"]
        rc, _title, notified = self._session(vanished, tui._SEL_QUIT, existing=[])
        self.assertEqual(rc, 1)
        self.assertTrue(any("/gone/Music" in n for n in notified))

    def test_write_mode_refuses_multi_root(self):
        rc, _title, notified = self._session(self.ROOTS, (4, 0))
        self.assertEqual(rc, 0)
        self.assertTrue(any("exactly one library root" in n for n in notified))


if __name__ == "__main__":
    unittest.main()
