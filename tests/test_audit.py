import unittest
from pathlib import Path
import tempfile

from lattice.modes.audit import (
    _cluster_by_duration,
    _DirInfo,
    _fmt_duration,
    _fmt_size,
    _layout_depths,
    _loose_key,
    _norm_key,
    _rg_bucket,
    classify_stray,
    run_stray_audit,
)
from lattice.tags import TagBundle


class NormKeyTests(unittest.TestCase):
    def test_empty_inputs(self):
        self.assertEqual(_norm_key(None), "")
        self.assertEqual(_norm_key(""), "")

    def test_dash_variants_fold(self):
        # U+2010 hyphen and ASCII hyphen collapse to the same key.
        self.assertEqual(_norm_key("Jay‐Z"), _norm_key("Jay-Z"))

    def test_curly_apostrophe_folds(self):
        self.assertEqual(_norm_key("Director’s"), "director's")

    def test_whitespace_collapsed_and_lowered(self):
        self.assertEqual(_norm_key("  Hello   World "), "hello world")


class LooseKeyTests(unittest.TestCase):
    def test_strips_trailing_parenthetical(self):
        self.assertEqual(_loose_key("Domestica (Deluxe Edition)"), "domestica")

    def test_strips_feat_clause(self):
        self.assertEqual(_loose_key("Song feat. Someone"), "song")

    def test_strips_multiple_trailing_parens(self):
        self.assertEqual(_loose_key("Album (Remastered) (2009)"), "album")


class FmtSizeTests(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(_fmt_size(512), "512 B")

    def test_kb(self):
        self.assertEqual(_fmt_size(2048), "2.0 KB")

    def test_mb(self):
        self.assertEqual(_fmt_size(5 * 1024 * 1024), "5.0 MB")

    def test_gb(self):
        self.assertEqual(_fmt_size(3 * 1024**3), "3.0 GB")


class FmtDurationTests(unittest.TestCase):
    def test_none(self):
        self.assertEqual(_fmt_duration(None), "--:--")

    def test_minutes_seconds(self):
        self.assertEqual(_fmt_duration(125), "2:05")


_FNAME: str = "track.mp3"


def _entry(path, dur):
    info = _DirInfo(
        path=path,
        artist="",
        album="",
        norm_artist="",
        norm_album="",
        loose_album="",
        total_bytes=0,
        formats={},
        fmt_bitrate={},
        files=[],
    )
    return (info, _FNAME, TagBundle(duration_s=dur))


class ClusterByDurationTests(unittest.TestCase):
    def test_single_cluster_within_delta(self):
        entries = [_entry("/A", 100.0), _entry("/B", 101.0)]
        clusters = _cluster_by_duration(entries, delta=2.0)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]), 2)

    def test_split_when_beyond_delta(self):
        # Two copies 100s apart, so neither cluster has 2 entries.
        entries = [_entry("/A", 100.0), _entry("/B", 200.0)]
        self.assertEqual(_cluster_by_duration(entries, delta=2.0), [])

    def test_studio_and_live_surface_separately(self):
        entries = [
            _entry("/A", 100.0),
            _entry("/B", 101.0),
            _entry("/C", 240.0),
            _entry("/D", 241.0),
        ]
        clusters = _cluster_by_duration(entries, delta=2.0)
        self.assertEqual(len(clusters), 2)

    def test_same_directory_not_a_cluster(self):
        # Two entries within delta but in one directory: not cross-library.
        entries = [_entry("/A", 100.0), _entry("/A", 101.0)]
        self.assertEqual(_cluster_by_duration(entries, delta=2.0), [])

    def test_durationless_entries_cluster_together(self):
        entries = [_entry("/A", None), _entry("/B", None)]
        clusters = _cluster_by_duration(entries, delta=2.0)
        self.assertEqual(len(clusters), 1)


class ReplayGainBucketTests(unittest.TestCase):
    def test_missing_when_no_track_gain(self):
        self.assertEqual(_rg_bucket(0, 0, 5), "MISSING")

    def test_partial_when_some_tracks_bare(self):
        self.assertEqual(_rg_bucket(3, 0, 5), "PARTIAL")

    def test_no_album_gain_when_all_track_but_no_album(self):
        self.assertEqual(_rg_bucket(5, 0, 5), "NO_ALBUM_GAIN")

    def test_no_album_gain_when_album_incomplete(self):
        self.assertEqual(_rg_bucket(5, 3, 5), "NO_ALBUM_GAIN")

    def test_ok_when_fully_tagged(self):
        self.assertEqual(_rg_bucket(5, 5, 5), "OK")

    def test_single_track_fully_tagged_is_ok(self):
        self.assertEqual(_rg_bucket(1, 1, 1), "OK")


class LayoutDepthsTests(unittest.TestCase):
    def test_default_layout(self):
        self.assertEqual(_layout_depths("{artist}/{album}"), (1, 2))

    def test_genre_layout(self):
        self.assertEqual(_layout_depths("{genre}/{artist}/{album}"), (2, 3))

    def test_missing_album_is_an_error(self):
        with self.assertRaises(ValueError):
            _layout_depths("{artist}")


class ClassifyStrayTests(unittest.TestCase):
    """rel_parts are components below the library root; the default layout
    ({artist}/{album}) has artist depth 1 and album depth 2."""

    def test_placed_album_track(self):
        self.assertEqual(classify_stray(("Artist", "Album", "01.flac"), 1, 2), "placed")

    def test_placed_disc_subfolder(self):
        # A multi-disc album lives deeper than album depth, but an ancestor
        # sits at album depth, so it is placed.
        self.assertEqual(
            classify_stray(("Artist", "Album", "CD1", "01.flac"), 1, 2), "placed"
        )

    def test_loose_track_beside_albums(self):
        self.assertEqual(classify_stray(("Artist", "loose.flac"), 1, 2), "loose")

    def test_root_level_audio_is_wrong_depth(self):
        self.assertEqual(classify_stray(("00 - Stray.flac",), 1, 2), "wrong-depth")

    def test_flat_strays_on_a_genre_library(self):
        # {genre}/{artist}/{album}: a flat Artist/Album stray's files sit one
        # slot short of the album depth, in a folder at the artist slot.
        # Depth alone cannot tell them from genuinely loose tracks, so they
        # share the loose bucket (the report's section note says so).
        self.assertEqual(classify_stray(("Artist", "Album", "01.flac"), 2, 3), "loose")
        self.assertEqual(
            classify_stray(("Genre", "Artist", "loose.flac"), 2, 3), "loose"
        )
        self.assertEqual(classify_stray(("Genre", "loose.flac"), 2, 3), "wrong-depth")

    def test_hidden_dir_wins_over_everything(self):
        self.assertEqual(classify_stray((".testing", "Copy", "01.mp3"), 1, 2), "hidden")

    def test_hidden_file(self):
        self.assertEqual(classify_stray(("Artist", ".01.flac"), 1, 2), "hidden")


class StrayAuditRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._touch("Artist/Album/01.flac")  # placed
        self._touch("Artist/Album/cover.jpg")  # image, never junk
        self._touch("Artist/Album/cleanup.log")  # sidecar, never junk
        self._touch("Artist/Album/notes.doc")  # unknown extension -> junk
        self._touch("Artist/loose.flac")  # loose
        self._touch("00 - Root Stray.flac")  # wrong-depth
        self._touch(".testing/Copy/01.mp3")  # hidden
        self.out = self.root / "stray_audit.txt"

    def tearDown(self):
        self._tmp.cleanup()

    def _touch(self, rel: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")

    def test_reports_every_bucket_and_junk(self):
        rc = run_stray_audit(str(self.root), str(self.out), layout="{artist}/{album}")
        self.assertEqual(rc, 0)
        text = self.out.read_text(encoding="utf-8")
        self.assertIn("Scanned: 4 audio", text)
        self.assertIn("== WRONG-DEPTH AUDIO (1) ==", text)
        self.assertIn("00 - Root Stray.flac", text)
        self.assertIn("== LOOSE TRACKS (1) ==", text)
        self.assertIn("Artist/loose.flac", text)
        self.assertIn("== HIDDEN-DIR AUDIO (1) ==", text)
        self.assertIn(".testing", text)
        self.assertIn("== NON-AUDIO FILES IN ALBUM FOLDERS (1) ==", text)
        self.assertIn("notes.doc", text)
        # Placed audio and recognized sidecars are never reported.
        self.assertNotIn("01.flac", text.replace(".testing", ""))
        self.assertNotIn("cover.jpg", text)
        self.assertNotIn("cleanup.log", text)

    def test_clean_tree_reports_zeros(self):
        empty = self.root / "empty"
        empty.mkdir()
        out = self.root / "empty_audit.txt"
        rc = run_stray_audit(str(empty), str(out))
        self.assertEqual(rc, 0)
        self.assertIn("Strays: 0", out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
