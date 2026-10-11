"""Tests for --stripRatings, the rating-tag stripper (v6.4.0). The brain is
modes/stripratings.py: per-container deletion of nonzero rating tags only,
driven by the same rating-source predicates and decoders lattice.tags uses,
so a stripped file always reads back rating-less. These tests pin the brain
against fixture copies (the committed Cursive MP3 carries a WMP POPM 255, the
Aphex Twin FLACs carry Vorbis ratings), the dispatch contract at the CLI
surface (dry-run default, --apply opts in, the root-list guard fires), and
the ingest stage-0 wiring."""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from mutagen.apev2 import TEXT, APEv2, APEValue
from mutagen.flac import FLAC
from mutagen.id3 import ID3, POPM, TXXX

from lattice import cli
from lattice.modes.stripratings import (
    FilePlan,
    _plan_asf,
    _plan_mp4,
    run_stripratings,
    strip_ratings_from_file,
)
from lattice.tags import get_all_tags

FIXTURES = Path(__file__).parent / "fixtures" / "library"
MP3_SRC = FIXTURES / "Cursive" / "Domestica" / "01 - The Casualty.mp3"
FLAC_SRC = FIXTURES / "Aphex Twin" / "Selected Ambient Works" / "01 - Xtal.flac"


class _FakeAudio:
    """The slice of a mutagen container object the _plan_* branches touch."""

    def __init__(self, tags):
        self.tags = tags
        self.saved = False

    def save(self):
        self.saved = True


class StripRatingsBrainTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.mp3 = self.root / "t.mp3"
        shutil.copy(MP3_SRC, self.mp3)

    def tearDown(self):
        self._tmp.cleanup()

    def test_plan_and_apply_strip_every_rating_source(self):
        id3 = ID3(str(self.mp3))
        id3.add(TXXX(encoding=3, desc="FMPS_RATING", text=["0.8"]))
        id3.add(TXXX(encoding=3, desc="Media type", text=["CD"]))
        id3.add(POPM(email="counter@example", rating=0, count=9))
        id3.save(str(self.mp3))
        ape = APEv2()
        ape["Rating"] = APEValue("4", TEXT)
        ape["Label"] = APEValue("X", TEXT)
        ape.save(str(self.mp3))

        plan = strip_ratings_from_file(str(self.mp3), apply=False)
        self.assertEqual(len(plan.removals), 3, plan.removals)
        self.assertTrue(any(r.startswith("POPM") for r in plan.removals))
        self.assertTrue(any("FMPS_RATING" in r for r in plan.removals))
        self.assertTrue(any(r.startswith("APEv2:") for r in plan.removals))
        self.assertIsNone(plan.error)

        res = strip_ratings_from_file(str(self.mp3), apply=True)
        self.assertEqual(len(res.removals), 3)
        self.assertIsNone(res.error)

        # The reader decodes no rating after the strip; keepers survive.
        bundle = get_all_tags(str(self.mp3))
        self.assertIsNone(bundle.rating)
        self.assertEqual(bundle.title, "The Casualty")
        self.assertEqual(bundle.genre, "Rock")
        id3 = ID3(str(self.mp3))
        self.assertEqual(id3.getall("TXXX:Media type")[0].text, ["CD"])
        counters = [f for f in id3.getall("POPM") if f.rating == 0 and f.count == 9]
        self.assertEqual(len(counters), 1)
        ape = APEv2(str(self.mp3))
        self.assertEqual(list(ape.keys()), ["Label"])

    def test_mp3_saves_use_the_house_v23_convention(self):
        strip_ratings_from_file(str(self.mp3), apply=True)
        self.assertEqual(ID3(str(self.mp3)).version, (2, 3, 0))

    def test_idempotent(self):
        strip_ratings_from_file(str(self.mp3), apply=True)
        second = strip_ratings_from_file(str(self.mp3), apply=True)
        self.assertEqual(second.removals, [])
        self.assertIsNone(second.error)

    def test_flac_vorbis_rating_key_goes(self):
        flac = self.root / "t.flac"
        shutil.copy(FLAC_SRC, flac)
        self.assertIsNotNone(get_all_tags(str(flac)).rating)
        plan = strip_ratings_from_file(str(flac), apply=True)
        self.assertEqual(len(plan.removals), 1, plan.removals)
        self.assertIsNone(get_all_tags(str(flac)).rating)
        tags = FLAC(str(flac))
        self.assertIn("title", tags)
        self.assertIn("genre", tags)

    def test_vorbis_zero_shell_stays(self):
        flac = self.root / "t.flac"
        shutil.copy(FLAC_SRC, flac)
        tags = FLAC(str(flac))
        tags["rating"] = ["0"]
        tags.save()
        plan = strip_ratings_from_file(str(flac), apply=True)
        self.assertEqual(plan.removals, [])
        self.assertEqual(FLAC(str(flac))["rating"], ["0"])

    def test_mp4_branch_strips_rate_leaves_the_advisory(self):
        audio = _FakeAudio(
            {"\xa9nam": ["T"], "\xa9gen": ["G"], "rate": ["80"], "\xa9rtng": [1]}
        )
        fp = FilePlan(path="fake.m4a")
        _plan_mp4("fake.m4a", audio, fp, apply=True)
        self.assertEqual(fp.removals, ["rate='80'"])
        self.assertNotIn("rate", audio.tags)
        self.assertIn("\xa9rtng", audio.tags)
        self.assertTrue(audio.saved)

    def test_mp4_branch_keeps_zero_shell(self):
        audio = _FakeAudio({"rate": ["0"]})
        fp = FilePlan(path="fake.m4a")
        _plan_mp4("fake.m4a", audio, fp, apply=True)
        self.assertEqual(fp.removals, [])
        self.assertIn("rate", audio.tags)
        self.assertFalse(audio.saved)

    def test_asf_branch_strips_wm_rating(self):
        audio = _FakeAudio({"WM/Genre": ["Metal"], "WM/Rating": ["80"]})
        fp = FilePlan(path="fake.wma")
        _plan_asf("fake.wma", audio, fp, apply=True)
        self.assertEqual(fp.removals, ["WM/Rating='80'"])
        self.assertNotIn("WM/Rating", audio.tags)
        self.assertIn("WM/Genre", audio.tags)

    def test_nonzero_threshold_follows_the_reader(self):
        # A non-numeric rating value ("love") is skipped by the reader and so
        # by the stripper: the frame matches the rating-desc predicate but a
        # value that decodes to nothing is never a finding.
        strip_ratings_from_file(str(self.mp3), apply=True)
        id3 = ID3(str(self.mp3))
        id3.add(TXXX(encoding=3, desc="LOVE RATING", text=["L"]))
        id3.save(str(self.mp3))
        plan = strip_ratings_from_file(str(self.mp3), apply=True)
        self.assertEqual(plan.removals, [])
        self.assertIsNotNone(ID3(str(self.mp3)).getall("TXXX:LOVE RATING"))

    def test_advisory_keys_are_not_ratings(self):
        # "content rating" is Explicit/Clean metadata, not stars; the shared
        # predicates exclude it on both sides (the live library carried four
        # Vorbis `content rating` keys the substring match once read as 1
        # star).
        from lattice.tags import (
            is_asf_rating_key,
            is_id3_rating_desc,
            is_mp4_rating_key,
            is_vorbis_rating_key,
        )

        self.assertFalse(is_vorbis_rating_key("content rating"))
        self.assertFalse(is_id3_rating_desc("Content Rating"))
        self.assertFalse(is_mp4_rating_key("----:com.apple.itunes:content rating"))
        self.assertFalse(is_asf_rating_key("wm/parentalrating"))
        flac = self.root / "t.flac"
        shutil.copy(FLAC_SRC, flac)
        strip_ratings_from_file(str(flac), apply=True)
        tags = FLAC(str(flac))
        tags["content rating"] = ["1"]
        tags.save()
        plan = strip_ratings_from_file(str(flac), apply=False)
        self.assertEqual(plan.removals, [])
        self.assertEqual(FLAC(str(flac))["content rating"], ["1"])


class StripRatingsRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "Music"
        album = self.root / "Art" / "Album"
        album.mkdir(parents=True)
        shutil.copy(MP3_SRC, album / "01.mp3")
        shutil.copy(FLAC_SRC, album / "02.flac")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, **kwargs):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_stripratings(str(self.root), **kwargs)
        return rc, buf.getvalue()

    def test_dry_run_reports_and_writes_nothing(self):
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("[DRY RUN]", out)
        self.assertIn("2 file(s)", out)
        self.assertIn("POPM", out)
        self.assertFalse((self.root / "stripratings.log").exists())
        self.assertIsNotNone(
            get_all_tags(str(self.root / "Art" / "Album" / "01.mp3")).rating
        )

    def test_apply_strips_and_logs(self):
        rc, _out = self._run(dry_run=False, assume_yes=True)
        self.assertEqual(rc, 0)
        self.assertIsNone(
            get_all_tags(str(self.root / "Art" / "Album" / "01.mp3")).rating
        )
        self.assertIsNone(
            get_all_tags(str(self.root / "Art" / "Album" / "02.flac")).rating
        )
        log = (self.root / "stripratings.log").read_text(encoding="utf-8")
        self.assertIn("Stripping rating tags from 2 file(s)", log)
        self.assertIn("removed POPM", log)
        self.assertIn("-> stripped 2 file(s); 0 error(s).", log)

    def test_json_summary(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_stripratings(str(self.root), json_mode=True)
        self.assertEqual(rc, 0)
        doc = json.loads(buf.getvalue()[buf.getvalue().index("{") :])
        self.assertEqual(doc["mode"], "stripratings")
        self.assertTrue(doc["dry_run"])
        self.assertEqual(doc["counts"]["files_with_ratings"], 2)
        self.assertEqual(doc["counts"]["removed"], 2)

    def test_clean_tree_is_a_noop(self):
        for p in (self.root / "Art" / "Album").iterdir():
            strip_ratings_from_file(str(p), apply=True)
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("No rating tags found", out)


class StripRatingsDispatchTests(unittest.TestCase):
    """CLI wiring pins, the ApestripDispatchTests shape."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.track = self.root / "t.mp3"
        shutil.copy(MP3_SRC, self.track)

    def tearDown(self):
        self._tmp.cleanup()

    def _main(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return cli.main(argv)

    def test_dry_run_is_the_default(self):
        rc = self._main(["--stripRatings", str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIsNotNone(get_all_tags(str(self.track)).rating)
        self.assertFalse((self.root / "stripratings.log").exists())

    def test_apply_strips_and_logs(self):
        rc = self._main(["--stripRatings", str(self.root), "--apply"])
        self.assertEqual(rc, 0)
        self.assertIsNone(get_all_tags(str(self.track)).rating)
        log = (self.root / "stripratings.log").read_text(encoding="utf-8")
        self.assertIn("removed POPM", log)

    def test_dry_run_flag_beats_apply(self):
        rc = self._main(["--stripRatings", str(self.root), "--apply", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIsNotNone(get_all_tags(str(self.track)).rating)

    def test_multiple_roots_are_rejected(self):
        other = Path(self._tmp.name) / "Other"
        other.mkdir()
        shutil.copy(MP3_SRC, other / "t.mp3")
        rc = self._main(
            [
                "--stripRatings",
                "--root",
                str(self.root),
                "--root",
                str(other),
                "--apply",
            ]
        )
        self.assertEqual(rc, 2)
        self.assertIsNotNone(get_all_tags(str(self.track)).rating)

    def test_mode_flags_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self._main(["--stripRatings", "--apestrip", str(self.root)])

    def test_json_is_supported(self):
        rc, out = self._main_capture(["--stripRatings", str(self.root), "--json"])
        self.assertEqual(rc, 0)
        doc = json.loads(out[out.index("{") :])
        self.assertEqual(doc["mode"], "stripratings")

    def _main_capture(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(argv)
        return rc, buf.getvalue()


class RatingProtectionTests(unittest.TestCase):
    """The v6.4.1 guard: a real run refuses any root at or under a
    rating-protected root, except through a staging inbox. Born the hard way:
    a 10,946-file --apply once stripped a hand-rated library."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.protected = Path(self._tmp.name) / "Music"
        self.track = self.protected / "Art" / "Album" / "01.mp3"
        self.track.parent.mkdir(parents=True)
        shutil.copy(MP3_SRC, self.track)

    def tearDown(self):
        self._tmp.cleanup()

    def test_apply_under_protected_root_refuses(self):
        rc, out = self._run(
            str(self.protected), dry_run=False, protected_roots=[str(self.protected)]
        )
        self.assertEqual(rc, 2)
        self.assertIn("rating-protected", out)
        self.assertIsNotNone(get_all_tags(str(self.track)).rating)

    def test_apply_in_a_subdirectory_is_refused_too(self):
        rc, _out = self._run(
            str(self.track.parent),
            dry_run=False,
            protected_roots=[str(self.protected)],
        )
        self.assertEqual(rc, 2)
        self.assertIsNotNone(get_all_tags(str(self.track)).rating)

    def test_dry_run_under_protected_root_still_previews(self):
        rc, out = self._run(
            str(self.protected), dry_run=True, protected_roots=[str(self.protected)]
        )
        self.assertEqual(rc, 0)
        self.assertIn("Rating tags found", out)
        self.assertIsNotNone(get_all_tags(str(self.track)).rating)

    def test_staging_inbox_inside_a_protected_root_stays_strippable(self):
        inbox = self.protected / "Unfiltered" / "New"
        inbox.mkdir(parents=True)
        inbox_track = inbox / "02.mp3"
        shutil.copy(MP3_SRC, inbox_track)
        rc, _out = self._run(
            str(inbox), dry_run=False, protected_roots=[str(self.protected)]
        )
        self.assertEqual(rc, 0)
        self.assertIsNone(get_all_tags(str(inbox_track)).rating)
        # The filed library beside it is untouched.
        self.assertIsNotNone(get_all_tags(str(self.track)).rating)

    def test_unprotected_roots_strip_normally(self):
        rc, _out = self._run(
            str(self.protected), dry_run=False, protected_roots=["/mnt/Elsewhere"]
        )
        self.assertEqual(rc, 0)
        self.assertIsNone(get_all_tags(str(self.track)).rating)

    def _run(self, root, *, dry_run, protected_roots):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = run_stripratings(
                root, dry_run=dry_run, assume_yes=True, protected_roots=protected_roots
            )
        return rc, buf.getvalue() + err.getvalue()


if __name__ == "__main__":
    unittest.main()
