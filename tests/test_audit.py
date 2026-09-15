import contextlib
import io
import os
import shutil
import struct
import unittest
from io import BytesIO
from pathlib import Path
import tempfile
from unittest import mock

from lattice import cli, tui
from lattice.modes import audit as audit_module
from lattice.modes.audit import (
    _album_health,
    audit_id3_junk,
    _audio_regions,
    _cluster_by_duration,
    _DirInfo,
    _fmt_duration,
    _fmt_size,
    _health_grade,
    _layout_depths,
    _loose_key,
    _norm_key,
    _rg_bucket,
    _track_number_findings,
    _trailing_tags_len,
    classify_stray,
    run_album_consistency,
    run_junk_frame_audit,
    run_audio_dupes,
    run_health_score,
    run_stray_audit,
)
from lattice.tags import ReplayGainStatus, TagBundle


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


# =====================================
# Content-hash audio duplicates
# =====================================

# Pseudo-audio with enough variety that unrelated builders never collide.
AUDIO = bytes(range(256)) * 64
OTHER_AUDIO = bytes(range(255, -1, -1)) * 64


def _syncsafe(n: int) -> bytes:
    return bytes(((n >> 21) & 127, (n >> 14) & 127, (n >> 7) & 127, n & 127))


def _id3v2(body: bytes, footer: bool = False) -> bytes:
    flags = 0x10 if footer else 0x00
    out = b"ID3\x04\x00" + bytes([flags]) + _syncsafe(len(body)) + body
    if footer:
        out += b"ID3\x04\x00" + bytes([0x80]) + _syncsafe(len(body))
    return out


def _ape(items: bytes = b"", header: bool = False) -> bytes:
    size = 32 + len(items)
    flags = 0x80000000 if header else 0
    footer = b"APETAGEX" + struct.pack("<IIII", 2000, size, 0, flags) + b"\x00" * 8
    out = items + footer
    if header:
        out = (
            b"APETAGEX" + struct.pack("<IIII", 2000, size, 0, flags) + b"\x00" * 8 + out
        )
    return out


def _mp3(tag: bytes = b"tag", audio: bytes = AUDIO, v1: bool = True) -> bytes:
    out = _id3v2(tag) + audio
    if v1:
        out += b"TAG" + b"\x00" * 125
    return out


def _flac(*blocks: bytes) -> bytes:
    out = b"fLaC"
    for i, body in enumerate(blocks):
        last = 0x80 if i == len(blocks) - 1 else 0x00
        out += bytes([last]) + len(body).to_bytes(3, "big") + body
    return out


def _box(btype: bytes, payload: bytes, large: bool = False) -> bytes:
    if large:
        return (
            struct.pack(">I", 1)
            + btype
            + struct.pack(">Q", 16 + len(payload))
            + payload
        )
    return struct.pack(">I", 8 + len(payload)) + btype + payload


class TrailingTagsTests(unittest.TestCase):
    def test_plain_audio_strips_nothing(self):
        self.assertEqual(_trailing_tags_len(BytesIO(AUDIO), len(AUDIO)), 0)

    def test_id3v1(self):
        data = AUDIO + b"TAG" + b"\x00" * 125
        self.assertEqual(_trailing_tags_len(BytesIO(data), len(data)), 128)

    def test_ape_footer_only(self):
        tag = _ape(items=b"Genre\x00Trash Metal")
        data = AUDIO + tag
        self.assertEqual(_trailing_tags_len(BytesIO(data), len(data)), len(tag))

    def test_ape_with_header(self):
        tag = _ape(items=b"Title\x00Song", header=True)
        data = AUDIO + tag
        self.assertEqual(_trailing_tags_len(BytesIO(data), len(data)), len(tag))

    def test_ape_then_id3v1(self):
        tag = _ape(items=b"x\x00y")
        data = AUDIO + tag + b"TAG" + b"\x00" * 125
        self.assertEqual(_trailing_tags_len(BytesIO(data), len(data)), len(tag) + 128)

    def test_id3v1_like_audio_is_kept(self):
        # "TAG" inside the audio (not end-anchored at 128) is never stripped.
        self.assertEqual(_trailing_tags_len(BytesIO(AUDIO), len(AUDIO)), 0)

    def test_oversized_ape_claim_refused(self):
        size_field = len(AUDIO) * 2  # claims more than exists
        footer = (
            b"APETAGEX" + struct.pack("<IIII", 2000, size_field, 0, 0) + b"\x00" * 8
        )
        data = AUDIO + footer
        self.assertEqual(_trailing_tags_len(BytesIO(data), len(data)), 0)


class AudioRegionsTests(unittest.TestCase):
    def test_mp3_without_id3v2(self):
        self.assertIsNone(_audio_regions(BytesIO(AUDIO), ".mp3", len(AUDIO)))

    def test_mp3_with_tag_and_id3v1(self):
        tag = b"TIT2\x00\x00\x00\x04\x00\x00Song"
        data = _id3v2(tag) + AUDIO + b"TAG" + b"\x00" * 125
        regions = _audio_regions(BytesIO(data), ".mp3", len(data))
        self.assertEqual(regions, [(len(_id3v2(tag)), len(data) - 128)])

    def test_mp3_tag_with_footer(self):
        body = b"TIT2\x00\x00\x00\x04\x00\x00Song"
        data = _id3v2(body, footer=True) + AUDIO
        end = 10 + len(body) + 10
        self.assertEqual(
            _audio_regions(BytesIO(data), ".mp3", len(data)), [(end, len(data))]
        )

    def test_mp3_oversized_tag_claim(self):
        # A header claiming the whole file has no audio region to hash.
        data = b"ID3\x04\x00\x00" + _syncsafe(len(AUDIO) + 10) + AUDIO
        self.assertIsNone(_audio_regions(BytesIO(data), ".mp3", len(data)))

    def test_flac_blocks(self):
        data = _flac(b"STREAMINFO" * 3, b"PIC" * 10) + AUDIO
        start = 4 + (4 + 30) + (4 + 30)
        self.assertEqual(
            _audio_regions(BytesIO(data), ".flac", len(data)), [(start, len(data))]
        )

    def test_flac_bad_magic(self):
        self.assertIsNone(
            _audio_regions(BytesIO(b"OggS" + AUDIO), ".flac", len(AUDIO) + 4)
        )

    def test_flac_truncated_chain(self):
        data = b"fLaC" + bytes([0x00]) + (9999).to_bytes(3, "big") + b"tiny"
        self.assertIsNone(_audio_regions(BytesIO(data), ".flac", len(data)))

    def test_m4a_mdat(self):
        data = (
            _box(b"ftyp", b"\x00" * 8)
            + _box(b"moov", b"\x00" * 16)
            + _box(b"mdat", AUDIO)
        )
        start = (8 + 8) + (8 + 16) + 8
        self.assertEqual(
            _audio_regions(BytesIO(data), ".m4a", len(data)), [(start, len(data))]
        )

    def test_m4a_mdat_largesize(self):
        data = _box(b"ftyp", b"\x00" * 8) + _box(b"mdat", AUDIO, large=True)
        start = (8 + 8) + 16
        self.assertEqual(
            _audio_regions(BytesIO(data), ".m4a", len(data)), [(start, len(data))]
        )

    def test_m4a_without_mdat(self):
        data = _box(b"ftyp", b"\x00" * 8)
        self.assertIsNone(_audio_regions(BytesIO(data), ".m4a", len(data)))

    def test_m4a_malformed_box(self):
        data = _box(b"ftyp", b"\x00" * 8) + b"\x00\x00\x00\x99mdat nonsense"
        self.assertIsNone(_audio_regions(BytesIO(data), ".m4a", len(data)))


class AudioDupesRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.out = self.root / "audio_dupes.txt"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel: str, data: bytes) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def _run(self) -> str:
        rc = run_audio_dupes(str(self.root), str(self.out))
        self.assertEqual(rc, 0)
        return self.out.read_text(encoding="utf-8")

    def test_exact_dupes_across_directories(self):
        self._write("A/Album/01.mp3", _mp3())
        self._write("A Copy/Album/01 - same.mp3", _mp3())
        self._write("B/Album/01.mp3", _mp3(audio=OTHER_AUDIO))
        text = self._run()
        self.assertIn("[EXACT DUPLICATES]    (1 group(s), 2 file(s))", text)
        self.assertIn("A Copy/Album/01 - same.mp3", text)
        self.assertIn("A/Album/01.mp3", text)
        # The retag-immune tiers stay quiet: nothing else shares bytes.
        self.assertIn("[AUDIO-STREAM MATCHES]    (none)", text)
        self.assertIn("[SAMPLED CONTENT MATCHES]    (none)", text)

    def test_retagged_mp3_matches_on_stream(self):
        # Same audio; tag body and length differ; one loses the ID3v1.
        self._write("A/Album/01.mp3", _mp3(tag=b"original"))
        self._write(
            "B/Retagged/01.mp3", _mp3(tag=b"a much longer retagged body!", v1=False)
        )
        text = self._run()
        self.assertIn("[AUDIO-STREAM MATCHES]    (1 group(s), 2 file(s))", text)
        self.assertIn("[EXACT DUPLICATES]    (none)", text)

    def test_retagged_flac_matches_on_stream(self):
        # Metadata block sizes differ, audio identical.
        self._write("A/Album/01.flac", _flac(b"S" * 30, b"P" * 30) + AUDIO)
        self._write("B/Album/01.flac", _flac(b"S" * 60) + AUDIO)
        text = self._run()
        self.assertIn("[AUDIO-STREAM MATCHES]    (1 group(s), 2 file(s))", text)

    def test_distinct_audio_reports_no_groups(self):
        self._write("A/Album/01.mp3", _mp3(audio=AUDIO))
        self._write("B/Album/01.mp3", _mp3(audio=OTHER_AUDIO))
        text = self._run()
        self.assertIn("[EXACT DUPLICATES]    (none)", text)
        self.assertIn("[AUDIO-STREAM MATCHES]    (none)", text)
        self.assertIn("[SAMPLED CONTENT MATCHES]    (none)", text)
        self.assertIn("Files: 2 readable, 0 unreadable", text)

    def test_sampled_pair_for_raw_formats(self):
        # Ogg-style files have no parsed region: identical head and tail
        # samples with differing middles group at the sampled tier. The sample
        # is patched small so the fixtures stay tiny (real 64KB windows need
        # >128KB files for head and tail to be distinct).
        head = b"OggS" + b"\x11" * 92
        tail = b"\x22" * 96
        with mock.patch.object(audit_module, "DUPES_SAMPLE_BYTES", 48):
            self._write("A/Album/01.ogg", head + b"m" * 32 + tail)
            self._write("B/Album/01.ogg", head + b"n" * 32 + tail)
            self._write("C/Album/01.ogg", head + b"n" * 32 + b"\x33" * 96)
            text = self._run()
        self.assertIn("[SAMPLED CONTENT MATCHES]    (1 group(s), 2 file(s))", text)
        self.assertIn("[AUDIO-STREAM MATCHES]    (none)", text)

    def test_unreadable_file_is_listed_and_skipped(self):
        self._write("A/Album/01.mp3", _mp3())
        self._write("A Copy/Album/01.mp3", _mp3())
        locked = self.root / "B/Album/01.mp3"
        self._write("B/Album/01.mp3", _mp3(audio=OTHER_AUDIO))
        os.chmod(locked, 0o000)
        try:
            text = self._run()
        finally:
            os.chmod(locked, 0o644)
        self.assertIn("[UNREADABLE FILES]    (1)", text)
        self.assertIn("B/Album/01.mp3", text)
        self.assertIn("[EXACT DUPLICATES]    (1 group(s), 2 file(s))", text)

    def test_empty_tree(self):
        text = self._run()
        self.assertIn("Files: 0 readable, 0 unreadable", text)
        self.assertIn("[EXACT DUPLICATES]    (none)", text)


class AudioDupesWiringTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_cli_dispatch_writes_the_report(self):
        p = self.root / "A/Album/01.mp3"
        p.parent.mkdir(parents=True)
        p.write_bytes(_mp3())
        out = self.root / "report.txt"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--auditAudioDupes", str(self.root), "--output", str(out)])
        self.assertEqual(rc, 0)
        text = out.read_text(encoding="utf-8")
        self.assertIn("AUDIO DUPLICATE REPORT", text)
        self.assertIn("Files: 1 readable, 0 unreadable", text)

    def test_tui_entry_and_aliases(self):
        sections = {name: items for name, items in tui._MAIN_SECTIONS if name}
        label = "Find duplicate audio (content hash)"
        self.assertIn(label, sections["METADATA"])
        idx = sections["METADATA"].index(label)
        self.assertEqual(tui._MAIN_ALIASES["adupes"], (3, idx))
        self.assertEqual(tui._MAIN_ALIASES["audiodupes"], (3, idx))


# =====================================
# Library health score
# =====================================


def _bundle(**overrides) -> TagBundle:
    fields = dict(
        title="Song",
        artist="Artist",
        trackno=1,
        album="Album",
        genre="Rock",
        bitrate_kbps=256,
    )
    fields.update(overrides)
    return TagBundle(**fields)


def _rg(gain: bool = True) -> ReplayGainStatus:
    return ReplayGainStatus(has_track_gain=gain, has_album_gain=gain)


class HealthGradeTests(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(_health_grade(100), "A")
        self.assertEqual(_health_grade(90), "A")
        self.assertEqual(_health_grade(89), "B")
        self.assertEqual(_health_grade(75), "B")
        self.assertEqual(_health_grade(74), "C")
        self.assertEqual(_health_grade(50), "C")
        self.assertEqual(_health_grade(49), "D")
        self.assertEqual(_health_grade(0), "D")


class AlbumHealthTests(unittest.TestCase):
    def _score(
        self,
        bundles,
        rg,
        *,
        cover=True,
        cover_res=(1000, 1000),
        embedded=False,
        min_kbps=192,
        min_res=500,
    ):
        return _album_health(bundles, rg, cover, cover_res, embedded, min_kbps, min_res)

    def test_full_score_has_no_notes(self):
        bundles = {"a.mp3": _bundle(), "b.mp3": _bundle(trackno=2)}
        rg = {"a.mp3": _rg(), "b.mp3": _rg()}
        score, notes = self._score(bundles, rg)
        self.assertEqual((score, notes), (100, []))

    def test_missing_tag_fields_deduct_four_each(self):
        bundles = {"a.mp3": _bundle(title=None, genre=None)}
        rg = {"a.mp3": _rg()}
        score, notes = self._score(bundles, rg)
        self.assertEqual(score, 92)
        self.assertIn("tags -8", notes[0])
        self.assertIn("a.mp3: title, genre", notes[0])

    def test_tag_deduction_caps_at_forty(self):
        bundles = {
            f"{i}.mp3": _bundle(title=None, artist=None, trackno=None, genre=None)
            for i in range(20)
        }
        rg = {f"{i}.mp3": _rg() for i in range(20)}
        score, _ = self._score(bundles, rg)
        # 80 missing fields would be -320 uncapped; capped at -40.
        self.assertEqual(score, 60)

    def test_replaygain_is_proportional(self):
        bundles = {"a.mp3": _bundle(), "b.mp3": _bundle(trackno=2)}
        rg = {"a.mp3": _rg(), "b.mp3": _rg(gain=False)}
        score, notes = self._score(bundles, rg)
        self.assertEqual(score, 85)
        self.assertIn("replaygain -15 (1/2 tracks tagged)", notes[0])

    def test_no_art_deducts_twenty(self):
        bundles = {"a.mp3": _bundle()}
        rg = {"a.mp3": _rg()}
        score, notes = self._score(bundles, rg, cover=False, embedded=False)
        self.assertEqual(score, 80)
        self.assertIn("art -20 (no art found)", notes[0])

    def test_embedded_only_deducts_twelve(self):
        bundles = {"a.mp3": _bundle()}
        rg = {"a.mp3": _rg()}
        score, notes = self._score(bundles, rg, cover=False, embedded=True)
        self.assertEqual(score, 88)
        self.assertIn("art -12 (embedded only; no folder cover)", notes[0])

    def test_small_cover_deducts_eight(self):
        bundles = {"a.mp3": _bundle()}
        rg = {"a.mp3": _rg()}
        score, notes = self._score(bundles, rg, cover_res=(400, 400))
        self.assertEqual(score, 92)
        self.assertIn("art -8 (folder cover 400x400 < 500px)", notes[0])

    def test_bitrate_deducts_two_per_file_capped_at_ten(self):
        bundles = {f"{i}.mp3": _bundle(bitrate_kbps=128) for i in range(9)}
        rg = {f"{i}.mp3": _rg() for i in range(9)}
        score, notes = self._score(bundles, rg)
        self.assertEqual(score, 90)
        self.assertIn("bitrate -10", notes[0])
        self.assertIn("0.mp3 128kbps", notes[0])

    def test_floor_zero(self):
        # Every bucket maxed: tags -40, replaygain -30, art -20, bitrate -10.
        bundles = {
            f"{i}.mp3": _bundle(
                title=None, artist=None, trackno=None, genre=None, bitrate_kbps=128
            )
            for i in range(5)
        }
        rg = {f"{i}.mp3": _rg(gain=False) for i in range(5)}
        score, _ = self._score(bundles, rg, cover=False, embedded=False)
        self.assertEqual(score, 0)


class HealthScoreRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.out = self.root / "health.txt"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, rel: str, data: bytes = b"audio") -> str:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return str(p)

    def _patch_reads(self, bundles_by_dir, rg_gain=True, embedded=False):
        """Patch the tag and ReplayGain readers (and embedded-art probe) so a
        tree of dummy files scores deterministically without real audio."""

        def fake_tags(paths, pbar=None):
            out = {}
            for p in paths:
                parent = os.path.dirname(p)
                out[p] = bundles_by_dir[parent][os.path.basename(p)]
            return out

        def fake_map(fn, paths, pbar=None, workers=None):
            return {p: _rg(rg_gain) for p in paths}

        return (
            mock.patch.object(audit_module, "read_tags_concurrent", fake_tags),
            mock.patch.object(audit_module, "map_concurrent", fake_map),
            mock.patch.object(audit_module, "_has_embedded_art", lambda d: embedded),
        )

    def test_full_score_album(self):
        p = self._write("A/Album/01.mp3")
        self._write("A/Album/cover.jpg", b"\xff\xd8fake")
        bundles_by_dir = {os.path.dirname(p): {"01.mp3": _bundle()}}
        patches = self._patch_reads(bundles_by_dir)
        with patches[0], patches[1], patches[2]:
            rc = run_health_score(
                str(self.root),
                str(self.out),
                min_kbps=192,
                min_res=500,
                verbose=True,
                quiet=True,
            )
        self.assertEqual(rc, 0)
        text = self.out.read_text(encoding="utf-8")
        self.assertIn("Grades: A 1  B 0  C 0  D 0    Mean: 100.0", text)
        self.assertIn("[FULL-SCORE ALBUMS]    (1 album(s))", text)
        self.assertIn("A/Album", text)

    def test_deductions_listed_with_notes(self):
        p = self._write("A/Album/01.mp3")
        bundles_by_dir = {
            os.path.dirname(p): {"01.mp3": _bundle(title=None, genre=None)}
        }
        patches = self._patch_reads(bundles_by_dir, rg_gain=False, embedded=True)
        with patches[0], patches[1], patches[2]:
            rc = run_health_score(
                str(self.root), str(self.out), min_kbps=192, min_res=500, quiet=True
            )
        self.assertEqual(rc, 0)
        text = self.out.read_text(encoding="utf-8")
        # tags -8, replaygain -30, embedded-only art -12 => 50 (C).
        self.assertIn("  50 C  A/Album/  (1 files)", text)
        self.assertIn("tags -8", text)
        self.assertIn("replaygain -30 (0/1 tracks tagged)", text)
        self.assertIn("art -12 (embedded only; no folder cover)", text)
        self.assertIn("[FULL-SCORE ALBUMS]    0 (list with --verbose)", text)

    def test_small_cover_penalized(self):
        # A 24-byte minimal PNG header with IHDR declaring 100x100.
        png = (
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13)
            + b"IHDR"
            + struct.pack(">II", 100, 100)
        )
        p = self._write("A/Album/01.mp3")
        self._write("A/Album/cover.png", png)
        bundles_by_dir = {os.path.dirname(p): {"01.mp3": _bundle()}}
        patches = self._patch_reads(bundles_by_dir)
        with patches[0], patches[1], patches[2]:
            rc = run_health_score(
                str(self.root), str(self.out), min_kbps=192, min_res=500, quiet=True
            )
        self.assertEqual(rc, 0)
        text = self.out.read_text(encoding="utf-8")
        self.assertIn("art -8 (folder cover 100x100 < 500px)", text)

    def test_empty_tree(self):
        (self.root / "empty").mkdir()
        rc = run_health_score(
            str(self.root / "empty"),
            str(self.out),
            min_kbps=192,
            min_res=500,
            quiet=True,
        )
        self.assertEqual(rc, 0)
        self.assertIn("Albums: 0", self.out.read_text(encoding="utf-8"))


class HealthScoreWiringTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_cli_dispatch_on_fixture_library(self):
        src = Path(__file__).parent / "fixtures" / "library" / "Cursive" / "Domestica"
        dst = self.root / "Cursive" / "Domestica"
        dst.mkdir(parents=True)
        for f in sorted(src.glob("*.mp3"))[:2]:
            shutil.copy(f, dst / f.name)
        out = self.root / "report.txt"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["--healthScore", str(self.root), "--output", str(out)])
        self.assertEqual(rc, 0)
        text = out.read_text(encoding="utf-8")
        self.assertIn("LIBRARY HEALTH REPORT", text)
        self.assertIn("Albums: 1", text)

    def test_tui_entry_and_aliases(self):
        sections = {name: items for name, items in tui._MAIN_SECTIONS if name}
        label = "Library health score"
        self.assertIn(label, sections["METADATA"])
        idx = sections["METADATA"].index(label)
        self.assertEqual(tui._MAIN_ALIASES["health"], (3, idx))


class TrackNumberFindingsTests(unittest.TestCase):
    def test_gap_is_found(self):
        self.assertEqual(
            _track_number_findings([1, 2, 5], 3),
            ["missing track numbers: 3, 4"],
        )

    def test_duplicates_are_found(self):
        self.assertEqual(
            _track_number_findings([1, 1, 2], 3),
            ["duplicate track numbers: 1"],
        )

    def test_untagged_files_are_counted(self):
        self.assertEqual(
            _track_number_findings([1, 2], 4),
            ["2 of 4 file(s) carry no track number"],
        )

    def test_complete_run_is_clean(self):
        self.assertEqual(_track_number_findings([1, 2, 3], 3), [])


class AlbumConsistencyTests(unittest.TestCase):
    def _tree(self, td: str):
        # Album A: mixed codecs, a track gap, divergent years.
        # Album B: fully consistent.
        mixed = Path(td) / "Album A"
        mixed.mkdir()
        (mixed / "01.flac").write_bytes(b"")
        (mixed / "02.mp3").write_bytes(b"")
        clean = Path(td) / "Album B"
        clean.mkdir()
        (clean / "01.flac").write_bytes(b"")
        (clean / "02.flac").write_bytes(b"")
        return mixed, clean

    def _bundles(self, mixed, clean):
        def bundle(trackno, year):
            return TagBundle(title="t", trackno=trackno, year=year)

        return {
            str(mixed / "01.flac"): bundle(1, 1985),
            str(mixed / "02.mp3"): bundle(5, 1986),
            str(clean / "01.flac"): bundle(1, 1990),
            str(clean / "02.flac"): bundle(2, 1990),
        }

    def _run(self, td, mixed, clean, **kw):
        bundles = self._bundles(mixed, clean)
        with mock.patch.object(
            audit_module, "read_tags_concurrent", return_value=bundles
        ):
            out = Path(td) / "consistency.txt"
            rc = run_album_consistency([td], str(out), quiet=True, **kw)
            return rc, out.read_text(encoding="utf-8")

    def test_findings_land_in_their_sections(self):
        with tempfile.TemporaryDirectory() as td:
            mixed, clean = self._tree(td)
            rc, report = self._run(td, mixed, clean)
            self.assertEqual(rc, 0)
            self.assertIn("ALBUM CONSISTENCY REPORT", report)
            self.assertIn("MIXED CODECS (1)", report)
            self.assertIn(".flac + .mp3", report)
            self.assertIn("missing track numbers: 2, 3, 4", report)
            self.assertIn("divergent years: 1985, 1986", report)
            # Album B has no findings, so it only shows in the counts.
            self.assertNotIn("Album B", report.split("CLEAN")[0])

    def test_verbose_lists_clean_albums(self):
        with tempfile.TemporaryDirectory() as td:
            mixed, clean = self._tree(td)
            _rc, report = self._run(td, mixed, clean, verbose=True)
            self.assertIn("CLEAN (1)", report)
            self.assertIn("Album B", report)


class AuditId3JunkTests(unittest.TestCase):
    MP3_SRC = (
        Path(__file__).parent
        / "fixtures"
        / "library"
        / "Cursive"
        / "Domestica"
        / "01 - The Casualty.mp3"
    )

    def _mp3(self, td, frames=()):
        from mutagen.id3 import ID3

        p = Path(td) / "t.mp3"
        shutil.copy(self.MP3_SRC, p)
        id3 = ID3(str(p))
        for frame in frames:
            id3.add(frame)
        id3.save(str(p), v2_version=3)
        return p

    def test_obsolete_frame_is_flagged(self):
        from mutagen.id3 import TYER

        with tempfile.TemporaryDirectory() as td:
            p = self._mp3(td, [TYER(encoding=3, text="2003")])
            findings = audit_id3_junk(str(p))
            self.assertIn("TYER", findings["obsolete"])

    def test_empty_text_frame_is_flagged(self):
        from mutagen.id3 import ID3, TIT2

        with tempfile.TemporaryDirectory() as td:
            p = self._mp3(td, [])
            # Overwrite the title with whitespace-only text.
            id3 = ID3(str(p))
            id3.add(TIT2(encoding=3, text=["  "]))
            id3.save(str(p), v2_version=3)
            findings = audit_id3_junk(str(p))
            self.assertTrue(any("TIT2" in k for k in findings["garbage"]))

    def test_nonstandard_frame_is_reported_not_judged(self):
        from mutagen.id3 import TCMP

        with tempfile.TemporaryDirectory() as td:
            p = self._mp3(td, [TCMP(encoding=3, text="1")])
            findings = audit_id3_junk(str(p))
            self.assertIn("TCMP", findings["nonstandard"])

    def test_clean_fixture_has_no_findings(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._mp3(td, [])
            self.assertIsNone(audit_id3_junk(str(p)))


class RunJunkFrameAuditTests(unittest.TestCase):
    def test_end_to_end_report(self):
        from mutagen.id3 import ID3, TYER

        mp3_src = AuditId3JunkTests.MP3_SRC
        with tempfile.TemporaryDirectory() as td:
            album = Path(td) / "Album"
            album.mkdir()
            track = album / "01.mp3"
            shutil.copy(mp3_src, track)
            id3 = ID3(str(track))
            id3.add(TYER(encoding=3, text="2003"))
            id3.save(str(track), v2_version=3)
            out = Path(td) / "junk.txt"
            rc = run_junk_frame_audit([td], str(out), quiet=True)
            self.assertEqual(rc, 0)
            report = out.read_text(encoding="utf-8")
            self.assertIn("JUNK ID3 FRAME AUDIT", report)
            self.assertIn("OBSOLETE FRAMES (ID3v2.3 leftovers) (1)", report)
            self.assertIn("TYER", report)


if __name__ == "__main__":
    unittest.main()
