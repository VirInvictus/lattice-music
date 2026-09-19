"""Tests for the lyrics write mode: the mode brain (test_lyrics.py) via an
injected fake LRCLIB fetcher — the suite's first network-seam stub — plus the
stray-audit pin that keeps --auditStrays from flagging our own sidecars."""

import contextlib
import io
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from mutagen.id3 import ID3, TALB, TIT2, TPE1

from lattice import cli
from lattice.modes.audit import SIDECAR_IGNORE_EXT
from lattice.modes.lyrics import run_lyrics
from lattice.tags import get_all_tags

FIXTURES = Path(__file__).parent / "fixtures" / "library"
MP3_SRC = FIXTURES / "Cursive" / "Domestica" / "01 - The Casualty.mp3"

SYNCED = "[00:12.00]first line\n[00:20.50]second line\n"


def _exact(synced=SYNCED, instrumental=False):
    """An LRCLIB /api/get-shaped record."""
    return {
        "instrumental": instrumental,
        "plainLyrics": "first line\nsecond line",
        "syncedLyrics": synced,
    }


def _fake_fetch(exact=None, search=None, calls=None):
    """A stand-in for lyrics._fetch_json: /api/get answers `exact` (None =
    the endpoint's 404 no-match), /api/search answers the `search` list.
    Appends every URL to `calls` when given."""

    def fetch(url):
        if calls is not None:
            calls.append(url)
        if "/api/get" in url:
            if exact is None:
                raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
            return exact
        if "/api/search" in url:
            return list(search or [])
        raise AssertionError(f"unexpected url: {url}")

    return fetch


def _tagged_mp3(path, artist="Cursive", title="The Casualty", album="Domestica"):
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(MP3_SRC, path)
    id3 = ID3(str(path))
    id3.setall("TIT2", [TIT2(encoding=3, text=[title])])
    id3.setall("TPE1", [TPE1(encoding=3, text=[artist])])
    id3.setall("TALB", [TALB(encoding=3, text=[album])])
    id3.save(str(path))


def _untagged_mp3(path):
    shutil.copy(MP3_SRC, path)
    ID3(str(path)).delete(str(path), delete_v1=True, delete_v2=True)


def _duration(path):
    return get_all_tags(str(path)).duration_s


class _Run:
    """run_lyrics with stdout captured and a fake fetcher wired in."""

    def __init__(self, fetch, tmp):
        self.fetch = fetch
        self.tmp = tmp
        self.out = io.StringIO()

    def __call__(self, *args, **kwargs):
        kwargs.setdefault("assume_yes", True)  # non-TTY convention anyway
        kwargs.setdefault("sleep_s", 0)
        with contextlib.redirect_stdout(self.out):
            return run_lyrics(str(self.tmp), *args, fetch=self.fetch, **kwargs)


class LyricsModeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.track = self.root / "Album" / "01 - The Casualty.mp3"
        _tagged_mp3(self.track)
        self.sidecar = self.root / "Album" / "01 - The Casualty.lrc"

    def tearDown(self):
        self._tmp.cleanup()

    def test_apply_writes_sidecar_and_logs(self):
        run = _Run(_fake_fetch(exact=_exact()), self.root)
        rc = run(dry_run=False)
        self.assertEqual(rc, 0)
        self.assertEqual(self.sidecar.read_text(encoding="utf-8"), SYNCED)
        log = (self.root / "lyrics.log").read_text(encoding="utf-8")
        self.assertIn("wrote 01 - The Casualty.lrc (LRCLIB)", log)
        self.assertIn("-> wrote 1 sidecar(s); 0 write error(s).", log)

    def test_dry_run_looks_up_but_writes_nothing(self):
        calls = []
        run = _Run(_fake_fetch(exact=_exact(), calls=calls), self.root)
        rc = run()  # dry-run is the default
        self.assertEqual(rc, 0)
        self.assertFalse(self.sidecar.exists())
        self.assertFalse((self.root / "lyrics.log").exists())
        # The lookup pass really ran: one exact-hit request, and the preview's
        # hit count is a real count, not a guess.
        self.assertEqual(len(calls), 1)
        self.assertIn("1 match(es)", run.out.getvalue())
        self.assertIn("Dry run: no files modified.", run.out.getvalue())

    def test_skip_existing(self):
        self.sidecar.write_text("keep me\n", encoding="utf-8")
        calls = []
        run = _Run(_fake_fetch(exact=_exact(), calls=calls), self.root)
        rc = run(dry_run=False)
        self.assertEqual(rc, 0)
        self.assertEqual(self.sidecar.read_text(encoding="utf-8"), "keep me\n")
        self.assertEqual(calls, [])  # skip happens before any LRCLIB request
        self.assertIn("1 already sided", run.out.getvalue())

    def test_force_overwrites(self):
        self.sidecar.write_text("stale\n", encoding="utf-8")
        run = _Run(_fake_fetch(exact=_exact()), self.root)
        rc = run(dry_run=False, force=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.sidecar.read_text(encoding="utf-8"), SYNCED)

    def test_instrumental_is_reported_not_written(self):
        run = _Run(_fake_fetch(exact=_exact(instrumental=True)), self.root)
        rc = run(dry_run=False)
        self.assertEqual(rc, 0)
        self.assertFalse(self.sidecar.exists())
        self.assertIn("[inst]", run.out.getvalue())

    def test_plain_only_is_reported_not_written(self):
        run = _Run(_fake_fetch(exact=_exact(synced="")), self.root)
        rc = run(dry_run=False)
        self.assertEqual(rc, 0)
        self.assertFalse(self.sidecar.exists())
        self.assertIn("[plain]", run.out.getvalue())

    def test_not_found_is_reported(self):
        run = _Run(_fake_fetch(exact=None, search=[]), self.root)
        rc = run(dry_run=False)
        self.assertEqual(rc, 0)
        self.assertFalse(self.sidecar.exists())
        self.assertIn("[miss]", run.out.getvalue())
        self.assertIn("Nothing to write.", run.out.getvalue())

    def test_search_fallback_prefers_closest_duration(self):
        dur = _duration(self.track)
        far = _exact(synced="[00:01.00]far\n")
        far["duration"] = dur - 10
        near = _exact(synced="[00:01.00]near\n")
        near["duration"] = dur + 0.4
        run = _Run(_fake_fetch(exact=None, search=[far, near]), self.root)
        rc = run(dry_run=False)
        self.assertEqual(rc, 0)
        self.assertEqual(self.sidecar.read_text(encoding="utf-8"), "[00:01.00]near\n")

    def test_network_error_is_isolated_per_file(self):
        other = self.root / "Album" / "02 - Boom.mp3"
        _tagged_mp3(other, title="Boom")

        def fetch(url):
            if "track_name=Boom" in url:
                raise urllib.error.URLError("connection refused")
            return _exact()

        run = _Run(fetch, self.root)
        rc = run(dry_run=False)
        self.assertEqual(rc, 1)  # a hard error fails the run
        self.assertTrue(self.sidecar.exists())  # the healthy file still wrote
        self.assertIn("connection refused", run.out.getvalue())

    def test_untagged_file_skips_the_network(self):
        untagged = self.root / "Album" / "03 - Untagged.mp3"
        _untagged_mp3(untagged)
        calls = []
        run = _Run(_fake_fetch(exact=_exact(), calls=calls), self.root)
        rc = run(dry_run=False)
        self.assertEqual(rc, 0)
        self.assertFalse((self.root / "Album" / "03 - Untagged.lrc").exists())
        self.assertTrue(all("Untagged" not in c for c in calls))
        self.assertIn("[untagged]", run.out.getvalue())

    def test_sleep_paces_between_lookups_only(self):
        extra = self.root / "Album" / "02 - The Recluse.mp3"
        _tagged_mp3(extra, title="The Recluse")
        with mock.patch("lattice.modes.lyrics.time.sleep") as sleep:
            run = _Run(_fake_fetch(exact=_exact()), self.root)
            rc = run(sleep_s=0.25)
        self.assertEqual(rc, 0)
        self.assertEqual(
            sleep.call_args_list, [mock.call(0.25)]
        )  # between the two lookups, never after the last one

    def test_existing_sidecar_member_of_audit_ignore_set(self):
        # Cross-mode contract: --auditStrays must not flag the sidecars this
        # mode writes as import junk.
        self.assertIn(".lrc", SIDECAR_IGNORE_EXT)


class LyricsDispatchTests(unittest.TestCase):
    """The package-surface contract: dry-run default, --apply opts in, the
    multi-root guard fires, and the mode flag stays mutually exclusive."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.track = self.root / "t.mp3"
        _tagged_mp3(self.track)

    def tearDown(self):
        self._tmp.cleanup()

    def _main(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return cli.main(argv)

    def test_dry_run_is_the_default(self):
        rc = self._main(["--lyrics", str(self.root), "--lyrics-sleep", "0"])
        self.assertEqual(rc, 0)
        self.assertFalse((self.root / "t.lrc").exists())
        self.assertFalse((self.root / "lyrics.log").exists())

    def test_apply_writes_and_logs(self):
        rc = self._main(["--lyrics", str(self.root), "--apply", "--lyrics-sleep", "0"])
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / "t.lrc").exists())
        log = (self.root / "lyrics.log").read_text(encoding="utf-8")
        self.assertIn("wrote t.lrc (LRCLIB)", log)

    def test_dry_run_flag_beats_apply(self):
        rc = self._main(
            ["--lyrics", str(self.root), "--apply", "--dry-run", "--lyrics-sleep", "0"]
        )
        self.assertEqual(rc, 0)
        self.assertFalse((self.root / "t.lrc").exists())
        self.assertFalse((self.root / "lyrics.log").exists())

    def test_multiple_roots_are_rejected(self):
        other = Path(self._tmp.name) / "Other"
        other.mkdir()
        rc = self._main(
            [
                "--lyrics",
                "--root",
                str(self.root),
                "--root",
                str(other),
                "--apply",
                "--lyrics-sleep",
                "0",
            ]
        )
        self.assertEqual(rc, 2)
        self.assertFalse((self.root / "t.lrc").exists())

    def test_mode_flags_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self._main(["--lyrics", "--apestrip", str(self.root)])


if __name__ == "__main__":
    unittest.main()
