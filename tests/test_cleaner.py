import re
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# The cleaner brain lives in the lattice package now (promoted out of scripts/,
# which launches it as a shim). These tests exercise the package modules: the
# pure name/tag rules via lattice.norm, the Run virtual filesystem and the four
# passes via lattice.modes.clean, against tempfile trees.
from lattice import norm
from lattice.modes import clean


class NormalizeNameTests(unittest.TestCase):
    def test_case_and_whitespace_collapse(self):
        self.assertEqual(norm.normalize_name("  The   Album "), "the album")

    def test_dash_variants_fold(self):
        self.assertEqual(norm.normalize_name("Jay‐Z"), norm.normalize_name("Jay-Z"))
        self.assertEqual(norm.normalize_name("A–B"), norm.normalize_name("A-B"))

    def test_curly_quote_folds(self):
        self.assertEqual(
            norm.normalize_name("You’re Gonna Miss It"),
            norm.normalize_name("You're Gonna Miss It"),
        )

    def test_apostrophe_present_vs_absent(self):
        # The 2026-05-25 found-bug fix: apostrophes are stripped so these merge.
        self.assertEqual(
            norm.normalize_name("Director's Cut"),
            norm.normalize_name("Directors Cut"),
        )


def _make_dir(root: Path, name: str, files: dict[str, bytes]) -> Path:
    d = root / name
    d.mkdir(parents=True)
    for fname, content in files.items():
        (d / fname).write_bytes(content)
    return d


def _png(w: int, h: int, pad: int = 0) -> bytes:
    """A minimal byte blob with a valid PNG signature + IHDR so _get_image_size
    reads (w, h); `pad` varies the byte length to force a size mismatch."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">LL", w, h)
        + b"\x00" * pad
    )


class ConsolidateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.log = self.root / "cleanup.log"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, dry_run=False):
        return clean.Run(self.root, self.log, dry_run=dry_run)

    def test_find_groups_matches_normalized_siblings(self):
        _make_dir(self.root, "Album", {"a.flac": b"a"})
        _make_dir(self.root, "album", {"b.flac": b"b"})
        _make_dir(self.root, "Different", {"c.flac": b"c"})
        run = self._run()
        groups = clean.find_groups(self.root, run)
        run.close()
        self.assertEqual(len(groups), 1)
        self.assertEqual({p.name for p in groups[0]}, {"Album", "album"})

    def test_hidden_dirs_ignored(self):
        _make_dir(self.root, "Album", {"a.flac": b"a"})
        _make_dir(self.root, ".album", {"b.flac": b"b"})
        run = self._run()
        groups = clean.find_groups(self.root, run)
        run.close()
        self.assertEqual(groups, [])

    def test_non_colliding_merge_into_larger(self):
        canon = _make_dir(self.root, "Album", {"01.flac": b"x", "02.flac": b"y"})
        src = _make_dir(self.root, "album", {"03.flac": b"z"})
        run = self._run()
        clean.consolidate_group([canon, src], "test", run)
        run.close()
        self.assertTrue((canon / "03.flac").exists())
        self.assertFalse(src.exists())

    def test_audio_collision_different_size_kept_as_fragment(self):
        canon = _make_dir(self.root, "Album", {"01.flac": b"x" * 100, "02.flac": b"y"})
        src = _make_dir(self.root, "album", {"01.flac": b"x" * 200})
        run = self._run()
        clean.consolidate_group([canon, src], "test", run)
        run.close()
        self.assertEqual((canon / "01.flac").read_bytes(), b"x" * 100)
        self.assertTrue((canon / "01.from-fragment.flac").exists())
        self.assertEqual((canon / "01.from-fragment.flac").read_bytes(), b"x" * 200)
        self.assertFalse(src.exists())

    def test_wma_collision_different_size_kept_as_fragment(self):
        # H2: .wma is audio; a differing-size collision must keep both copies,
        # never fall into the DROP NON-AUDIO branch.
        canon = _make_dir(self.root, "Album", {"01.wma": b"x" * 100, "02.flac": b"y"})
        src = _make_dir(self.root, "album", {"01.wma": b"x" * 200})
        run = self._run()
        clean.consolidate_group([canon, src], "test", run)
        run.close()
        self.assertEqual((canon / "01.wma").read_bytes(), b"x" * 100)
        self.assertTrue((canon / "01.from-fragment.wma").exists())
        self.assertEqual((canon / "01.from-fragment.wma").read_bytes(), b"x" * 200)

    def test_audio_collision_identical_size_dropped(self):
        canon = _make_dir(self.root, "Album", {"01.flac": b"x" * 100, "02.flac": b"y"})
        src = _make_dir(self.root, "album", {"01.flac": b"x" * 100})
        run = self._run()
        clean.consolidate_group([canon, src], "test", run)
        run.close()
        self.assertFalse((canon / "01.from-fragment.flac").exists())
        self.assertFalse(src.exists())

    def test_audio_collision_same_size_different_bytes_kept(self):
        # C1: "identical" means size + sampled bytes, not size alone; a
        # same-size re-encode must survive as a fragment, never be dropped.
        canon = _make_dir(self.root, "Album", {"01.flac": b"x" * 100, "02.flac": b"y"})
        src = _make_dir(self.root, "album", {"01.flac": b"z" * 100})
        run = self._run()
        clean.consolidate_group([canon, src], "test", run)
        run.close()
        self.assertEqual((canon / "01.flac").read_bytes(), b"x" * 100)
        self.assertTrue((canon / "01.from-fragment.flac").exists())
        self.assertEqual((canon / "01.from-fragment.flac").read_bytes(), b"z" * 100)

    def test_non_audio_non_image_collision_dropped(self):
        # Non-image non-audio (.nfo) keeps canonical's copy, drops the source.
        canon = _make_dir(self.root, "Album", {"info.nfo": b"A" * 50, "01.flac": b"z"})
        src = _make_dir(self.root, "album", {"info.nfo": b"B" * 99})
        run = self._run()
        clean.consolidate_group([canon, src], "test", run)
        run.close()
        self.assertEqual((canon / "info.nfo").read_bytes(), b"A" * 50)
        self.assertFalse(any(canon.glob("*from-fragment*")))
        self.assertFalse(src.exists())

    def test_dry_run_changes_nothing(self):
        canon = _make_dir(self.root, "Album", {"01.flac": b"x", "02.flac": b"y"})
        src = _make_dir(self.root, "album", {"03.flac": b"z"})
        run = self._run(dry_run=True)
        clean.consolidate_group([canon, src], "test", run)
        run.close()
        self.assertTrue((src / "03.flac").exists())
        self.assertFalse((canon / "03.flac").exists())


class _TreeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.log = self.root / "cleanup.log"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, dry_run=False):
        return clean.Run(self.root, self.log, dry_run=dry_run)


class CanonicalRenderTests(unittest.TestCase):
    def test_unicode_dash_to_ascii(self):
        self.assertEqual(
            norm.canonical_render("Drive‐By Truckers"), "Drive-By Truckers"
        )

    def test_curly_apostrophe_to_straight(self):
        self.assertEqual(norm.canonical_render("You’re"), "You're")

    def test_case_preserved_whitespace_collapsed(self):
        self.assertEqual(norm.canonical_render("  The   XX "), "The XX")

    def test_curly_double_quotes_preserved(self):
        # Straight " is forbidden on Windows/NTFS, so curly double quotes stay.
        self.assertEqual(
            norm.canonical_render("Damian “Jr. Gong” Marley"),
            "Damian “Jr. Gong” Marley",
        )

    def test_ellipsis_preserved(self):
        # "..." would end a name in dots, which NTFS rejects; keep the glyph.
        self.assertEqual(norm.canonical_render("Rooms…"), "Rooms…")

    def test_en_dash_preserved(self):
        # En-dashes in ranges are correct; canonical_render must not fold them.
        self.assertEqual(norm.canonical_render("Works 85–92"), "Works 85–92")

    def test_em_dash_preserved(self):
        self.assertEqual(norm.canonical_render("peace — reworks"), "peace — reworks")

    def test_already_normal_unchanged(self):
        self.assertEqual(
            norm.canonical_render("Damian Jr. Gong Marley"),
            "Damian Jr. Gong Marley",
        )


class IsLegalNameTests(unittest.TestCase):
    def test_rejects_trailing_dot_or_space(self):
        self.assertFalse(norm.is_legal_name("Rooms..."))
        self.assertFalse(norm.is_legal_name("Album "))

    def test_rejects_windows_forbidden_chars(self):
        self.assertFalse(norm.is_legal_name('a"b'))
        self.assertFalse(norm.is_legal_name("a:b"))
        self.assertFalse(norm.is_legal_name("a/b"))

    def test_accepts_normal_names(self):
        self.assertTrue(norm.is_legal_name("Drive-By Truckers"))
        self.assertTrue(norm.is_legal_name("Get Rich or Die Tryin'"))
        self.assertTrue(norm.is_legal_name("85–92"))


class GetImageSizeTests(unittest.TestCase):
    def test_png(self):
        self.assertEqual(clean._get_image_size(_png(640, 480)), (640, 480))

    def test_jpeg(self):
        data = (
            b"\xff\xd8\xff\xc0\x00\x11\x08" + struct.pack(">HH", 300, 200) + b"\x00" * 8
        )
        self.assertEqual(clean._get_image_size(data), (200, 300))

    def test_garbage_none(self):
        self.assertIsNone(clean._get_image_size(b"not an image"))


class DryRunFidelityTests(unittest.TestCase):
    """A dry-run must predict the real run's rmdir decisions and counts (#1)."""

    def _scenario(self, dry):
        tmp = tempfile.mkdtemp()
        try:
            root = Path(tmp)
            canon = _make_dir(root, "Album", {"01.flac": b"x", "02.flac": b"y"})
            src = _make_dir(root, "album", {"03.flac": b"z"})
            run = clean.Run(root, root / "log", dry_run=dry)
            clean.consolidate_group([canon, src], "t", run)
            run.close()
            return dict(run.stats)
        finally:
            shutil.rmtree(tmp)

    def test_dry_stats_match_real(self):
        self.assertEqual(self._scenario(dry=True), self._scenario(dry=False))


class SurvivorRenameTests(_TreeCase):
    def test_unicode_canonical_renamed_to_ascii(self):
        canon = _make_dir(
            self.root, "Drive‐By Truckers", {"a.flac": b"1", "b.flac": b"2"}
        )
        src = _make_dir(self.root, "Drive-By Truckers", {"c.flac": b"3"})
        run = self._run()
        clean.consolidate_group([canon, src], "artists", run)
        run.close()
        self.assertTrue((self.root / "Drive-By Truckers").is_dir())
        self.assertFalse((self.root / "Drive‐By Truckers").exists())
        self.assertEqual(run.stats["renamed"], 1)

    def test_already_normalized_no_rename(self):
        canon = _make_dir(self.root, "Album", {"a.flac": b"1", "b.flac": b"2"})
        src = _make_dir(self.root, "album", {"c.flac": b"3"})
        run = self._run()
        clean.consolidate_group([canon, src], "t", run)
        run.close()
        self.assertEqual(run.stats["renamed"], 0)

    def _rename_scenario(self, dry):
        base = self.root / ("dry" if dry else "apply")
        canon = _make_dir(base, "Drive‐By Truckers", {"a.flac": b"1", "b.flac": b"2"})
        src = _make_dir(base, "Drive-By Truckers", {"c.flac": b"3"})
        run = clean.Run(
            base, base / "log", dry_run=dry, normalize_tags=True, artist_depth=1
        )
        clean.consolidate_group([canon, src], "artists", run)
        run.close()
        return dict(run.stats), {name for name, _ in run.tag_targets}

    def test_dry_run_predicts_survivor_rename_and_tag_targets(self):
        # H3: in a dry-run the merged-away ASCII source still exists on disk, so
        # the rename guard must consult run.removed or the preview misses the
        # survivor rename (and records the wrong tag-authority name).
        dry_stats, dry_targets = self._rename_scenario(dry=True)
        apply_stats, apply_targets = self._rename_scenario(dry=False)
        self.assertEqual(apply_stats["renamed"], 1)
        self.assertEqual(dry_stats, apply_stats)
        self.assertEqual(dry_targets, apply_targets)


class RenamedSurvivorDryRunParityTests(unittest.TestCase):
    """Pass 1's survivor rename must not hide the artist from Passes 2-4 in a
    dry-run: the old name lands in run.removed, but the bytes survive under
    the new name, so the preview must still report the album consolidation,
    name normalization, and tag scanning an apply run performs."""

    def _build(self, base: Path) -> None:
        _make_dir(base, "Drive‐By Truckers/Album", {"01.flac": b"x", "02.flac": b"y"})
        _make_dir(base, "Drive‐By Truckers/album", {"03.flac": b"z"})
        # The ascii variant merges away; same.flac is an identical-bytes
        # collision that gets dropped, so Pass 4 must not preview-scan it.
        _make_dir(base, "Drive-By Truckers/Album", {"same.flac": b"x"})
        (base / "Drive‐By Truckers" / "Album" / "same.flac").write_bytes(b"x")

    def _stats_from_log(self, log_path: Path) -> dict[str, int]:
        text = log_path.read_text(encoding="utf-8")
        block = text.split("--- SUMMARY ---")[-1].split("CLEANUP RUN END")[0]
        stats = {}
        for line in block.splitlines():
            m = re.search(r"(\w+): (\d+)\s*$", line)
            if m:
                stats[m.group(1)] = int(m.group(2))
        return stats

    def _run_main(self, base: Path, dry: bool) -> dict[str, int]:
        argv = [
            "clean.py",
            str(base),
            "--normalize-names",
            "--normalize-filenames",
            "--normalize-tags",
        ]
        if dry:
            argv.append("--dry-run")
        with mock.patch.object(sys, "argv", argv):
            self.assertEqual(clean.main(), 0)
        return self._stats_from_log(base / "cleanup.log")

    def test_dry_run_stats_match_apply(self):
        with tempfile.TemporaryDirectory() as td:
            dry_base = Path(td) / "dry"
            apply_base = Path(td) / "apply"
            self._build(dry_base)
            self._build(apply_base)
            dry_stats = self._run_main(dry_base, dry=True)
            apply_stats = self._run_main(apply_base, dry=False)
            # The apply run really did rename the survivor and merge under it.
            merged = apply_base / "Drive-By Truckers" / "Album"
            self.assertTrue((merged / "03.flac").is_file())
            self.assertFalse((apply_base / "Drive‐By Truckers").exists())
            # 2 groups: the artist variants and the Album/album variants the
            # old dry-run never saw once the survivor was renamed.
            self.assertEqual(apply_stats["groups"], 2)
            self.assertEqual(dry_stats, apply_stats)


class CoverResolutionTests(_TreeCase):
    def test_higher_res_replaces_even_if_smaller_bytes(self):
        canon = _make_dir(
            self.root,
            "Album",
            {"cover.png": _png(100, 100, pad=500), "01.flac": b"z"},
        )
        src = _make_dir(self.root, "album", {"cover.png": _png(400, 400)})
        run = self._run()
        clean.consolidate_group([canon, src], "t", run)
        run.close()
        self.assertEqual(
            clean._get_image_size((canon / "cover.png").read_bytes()), (400, 400)
        )
        self.assertEqual(run.stats["covers_replaced"], 1)
        self.assertFalse(src.exists())

    def test_equal_res_falls_back_to_larger_bytes(self):
        canon = _make_dir(
            self.root, "Album", {"cover.png": _png(200, 200), "01.flac": b"z"}
        )
        src = _make_dir(self.root, "album", {"cover.png": _png(200, 200, pad=300)})
        run = self._run()
        clean.consolidate_group([canon, src], "t", run)
        run.close()
        self.assertEqual(run.stats["covers_replaced"], 1)
        self.assertEqual(
            len((canon / "cover.png").read_bytes()), len(_png(200, 200, pad=300))
        )


class NormalizeTreeTests(_TreeCase):
    def test_renames_lone_unicode_hyphen_artist(self):
        _make_dir(self.root, "Jay‐Z", {"a.flac": b"1"})
        run = self._run()
        clean.normalize_tree(self.root, run, True, False)
        run.close()
        self.assertTrue((self.root / "Jay-Z").is_dir())
        self.assertFalse((self.root / "Jay‐Z").exists())

    def test_renames_album_within_artist(self):
        album = self.root / "Artist" / "4‐44"
        album.mkdir(parents=True)
        (album / "t.flac").write_bytes(b"1")
        run = self._run()
        clean.normalize_tree(self.root, run, True, False)
        run.close()
        self.assertTrue((self.root / "Artist" / "4-44").is_dir())

    def test_dry_run_counts_but_does_not_rename(self):
        _make_dir(self.root, "Jay‐Z", {"a.flac": b"1"})
        run = self._run(dry_run=True)
        clean.normalize_tree(self.root, run, True, False)
        run.close()
        self.assertTrue((self.root / "Jay‐Z").exists())
        self.assertEqual(run.stats["renamed"], 1)

    def test_ellipsis_folder_left_alone(self):
        # Regression: folding … -> "..." produced an NTFS-illegal trailing-dot
        # name and crashed the run. The glyph is valid, so it must be kept.
        album = self.root / "Artist" / "Rooms…"
        album.mkdir(parents=True)
        (album / "t.flac").write_bytes(b"1")
        run = self._run()
        clean.normalize_tree(self.root, run, True, False)
        run.close()
        self.assertTrue((self.root / "Artist" / "Rooms…").is_dir())
        self.assertEqual(run.stats["renamed"], 0)


from mutagen.flac import FLAC
from mutagen.id3 import ID3, TALB, TIT2, TPE1, TPE2

FIXTURES = Path(__file__).parent / "fixtures" / "library"
MP3_SRC = FIXTURES / "Cursive" / "Domestica" / "01 - The Casualty.mp3"
FLAC_SRC = FIXTURES / "Aphex Twin" / "Selected Ambient Works" / "01 - Xtal.flac"


class TagFoldTests(unittest.TestCase):
    def test_cp1252_mojibake_to_ascii(self):
        # \x93/\x94/\x92 are CP1252 bytes read back as Latin-1 C1 controls.
        self.assertEqual(
            norm.tag_fold("Bonnie \x93Prince\x94 Billy"), 'Bonnie "Prince" Billy'
        )
        self.assertEqual(norm.tag_fold("Tim O\x92Brien"), "Tim O'Brien")

    def test_curly_quotes_to_ascii(self):
        self.assertEqual(norm.tag_fold("Singer’s Grave"), "Singer's Grave")

    def test_whitespace_collapsed(self):
        self.assertEqual(norm.tag_fold("  A   B "), "A B")

    def test_en_em_dash_and_ellipsis_preserved(self):
        # Correct typography (e.g. numeric ranges) must survive the fold.
        self.assertEqual(norm.tag_fold("Works 85–92"), "Works 85–92")
        self.assertEqual(norm.tag_fold("A — B"), "A — B")
        self.assertEqual(norm.tag_fold("Rooms…"), "Rooms…")

    def test_mojibake_dash_repaired_to_real_dash(self):
        # CP1252 0x96/0x97 are broken en/em dashes -> repaired, not hyphenated.
        self.assertEqual(norm.tag_fold("1975\x961985"), "1975–1985")
        self.assertEqual(norm.tag_fold("A\x97B"), "A—B")

    def test_broken_hyphen_folded(self):
        self.assertEqual(norm.tag_fold("Jay‐Z"), "Jay-Z")

    def test_tag_dedupe(self):
        self.assertEqual(
            norm.tag_dedupe("The Documentary 2 / The Documentary 2"),
            "The Documentary 2",
        )
        self.assertEqual(norm.tag_dedupe("Intro / Intro"), "Intro")
        self.assertEqual(
            norm.tag_dedupe("On Me (feat. Kendrick Lamar) / On Me"),
            "On Me (feat. Kendrick Lamar)",
        )
        self.assertEqual(
            norm.tag_dedupe("The Game / The Game feat. DeJ Loaf & Sha Sha"),
            "The Game feat. DeJ Loaf & Sha Sha",
        )
        self.assertEqual(norm.tag_dedupe("A / B"), "A / B")
        self.assertEqual(norm.tag_dedupe("The Game ; The Game"), "The Game")


class CanonTrackArtistTests(unittest.TestCase):
    def test_plain_collapses_to_canonical(self):
        self.assertEqual(
            norm.canon_track_artist("Bonnie Prince Billy", "Bonnie 'Prince' Billy"),
            "Bonnie 'Prince' Billy",
        )

    def test_doubled_junk_collapses(self):
        self.assertEqual(
            norm.canon_track_artist(
                "Bonnie Prince Billy / Bonnie 'Prince' Billy", "Bonnie 'Prince' Billy"
            ),
            "Bonnie 'Prince' Billy",
        )

    def test_feat_preserved_and_folded(self):
        self.assertEqual(
            norm.canon_track_artist(
                "Bonnie \x93Prince\x94 Billy feat. Tim O\x92Brien",
                "Bonnie 'Prince' Billy",
            ),
            "Bonnie 'Prince' Billy feat. Tim O'Brien",
        )

    def test_mid_word_ft_not_a_feat_marker(self):
        # H1: bare \s* before the marker was zero-width, so the "ft" ending
        # "Left"/"Swift"/"Croft" matched and corrupted clean tags.
        self.assertEqual(norm.canon_track_artist("Left Boy", "Left Boy"), "Left Boy")
        self.assertEqual(
            norm.canon_track_artist("Left Lane Cruiser", "Left Lane Cruiser"),
            "Left Lane Cruiser",
        )

    def test_parenthesised_feat_drops_stray_paren(self):
        self.assertEqual(norm.canon_track_artist("A (feat. B)", "A"), "A feat. B")

    def test_idempotent_on_feat_case(self):
        once = norm.canon_track_artist("X (feat. Tim O\x92Brien)", "X")
        self.assertEqual(once, norm.canon_track_artist(once, "X"))


def _mp3(path, artist=None, albumartist=None, title=None, album=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(MP3_SRC, path)
    tags = ID3(path)
    for fid, cls, val in (
        ("TIT2", TIT2, title),
        ("TALB", TALB, album),
        ("TPE1", TPE1, artist),
        ("TPE2", TPE2, albumartist),
    ):
        if val is not None:
            tags.setall(fid, [cls(encoding=3, text=[val])])
    tags.save(path, v2_version=3)


def _flac(path, clear=True, **fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FLAC_SRC, path)
    f = FLAC(path)
    if clear:
        f.delete()
    for key, val in fields.items():
        for existing in [k for k in list(f.keys()) if k.lower() == key.lower()]:
            del f[existing]
        f[key] = [val]
    f.save()


class _RunCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.log = self.root / "cleanup.log"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, dry_run=False, normalize_tags=True, artist_depth=1):
        return clean.Run(
            self.root,
            self.log,
            dry_run=dry_run,
            normalize_tags=normalize_tags,
            artist_depth=artist_depth,
        )


class TagWriterTests(_RunCase):
    """normalize_file_tags() per-format and per-policy."""

    def test_mp3_typographic_fold_keeps_v23(self):
        p = self.root / "t.mp3"
        _mp3(p, artist="A", albumartist="A", title="Cur’ly", album="Da\x92sh")
        run = self._run()
        clean.normalize_file_tags(p, None, run)
        run.close()
        t = ID3(p)
        self.assertEqual(t["TIT2"].text[0], "Cur'ly")
        self.assertEqual(t["TALB"].text[0], "Da'sh")
        self.assertEqual(t.version, (2, 3, 0))  # ID3v2.3 contract preserved

    def test_mp3_noop_not_counted(self):
        p = self.root / "t.mp3"
        _mp3(p, artist="A", albumartist="A", title="Clean", album="Clean")
        run = self._run()
        clean.normalize_file_tags(p, None, run)
        run.close()
        self.assertEqual(run.stats["tags_rewritten"], 0)

    def test_flac_typographic_fold(self):
        p = self.root / "t.flac"
        _flac(
            p,
            title="O’Hare",
            album="Da\x92y",
            artist="Bj\x94rk",
            albumartist="Bj\x94rk",
        )
        run = self._run()
        clean.normalize_file_tags(p, None, run)
        run.close()
        f = FLAC(p)
        self.assertEqual(f["title"][0], "O'Hare")
        self.assertEqual(f["album"][0], "Da'y")  # \x92 is the curly apostrophe
        self.assertEqual(f["artist"][0], 'Bj"rk')  # \x94 is the curly double quote

    def test_flac_uppercase_key_resolved(self):
        p = self.root / "t.flac"
        _flac(p, TITLE="Cur’ly")  # case-variant Vorbis key
        run = self._run()
        clean.normalize_file_tags(p, None, run)
        run.close()
        f = FLAC(p)
        self.assertEqual([k for k in f.keys() if k.lower() == "title"], ["title"])
        self.assertEqual(f["title"][0], "Cur'ly")  # no duplicate key left

    def test_flac_authority_restamp_preserves_feat(self):
        p = self.root / "t.flac"
        _flac(
            p,
            title="So’ng",
            album="Al’bum",
            artist="Bonnie \x93Prince\x94 Billy feat. Tim O\x92Brien",
            albumartist="Bonnie \x93Prince\x94 Billy",
        )
        run = self._run()
        clean.normalize_file_tags(p, "Bonnie 'Prince' Billy", run)
        run.close()
        f = FLAC(p)
        self.assertEqual(f["artist"][0], "Bonnie 'Prince' Billy feat. Tim O'Brien")
        self.assertEqual(f["albumartist"][0], "Bonnie 'Prince' Billy")
        self.assertEqual(f["title"][0], "So'ng")  # title is fold-only, never authority
        self.assertEqual(f["album"][0], "Al'bum")

    def test_absent_field_not_synthesized(self):
        p = self.root / "t.flac"
        _flac(p, title="Cur’ly")  # no album key at all
        run = self._run()
        clean.normalize_file_tags(p, None, run)
        run.close()
        self.assertNotIn("album", [k.lower() for k in FLAC(p).keys()])


class MultiValueTagTests(_RunCase):
    """M18: a multi-valued tag whose first value needs folding must keep every
    value; the authority restamp deliberately collapses to the survivor."""

    def test_fold_preserves_all_artist_values(self):
        p = self.root / "t.flac"
        shutil.copy(FLAC_SRC, p)
        f = FLAC(p)
        f.delete()
        f["artist"] = ["Artist A’s Band", "Artist B"]
        f["title"] = ["Song"]
        f.save()
        run = self._run()
        clean.normalize_file_tags(p, None, run)
        run.close()
        self.assertEqual(FLAC(p)["artist"], ["Artist A's Band", "Artist B"])

    def test_restamp_still_collapses_to_authority(self):
        p = self.root / "t.flac"
        shutil.copy(FLAC_SRC, p)
        f = FLAC(p)
        f.delete()
        f["artist"] = ["Artist A", "Artist B"]
        f.save()
        run = self._run()
        clean.normalize_file_tags(p, "Artist A", run)
        run.close()
        self.assertEqual(FLAC(p)["artist"], ["Artist A"])


class DryRunCreationModelTests(unittest.TestCase):
    """M19: the dry-run must model what apply would have CREATED so far, not
    just what it removed; these fixtures diverged before the fix."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _three_way_merge(self, dry):
        root = self.base / ("dry" if dry else "apply") / "3way"
        canon = _make_dir(root, "Album", {"01.flac": b"x" * 10, "02.flac": b"y"})
        s1 = _make_dir(root, "ALBUM", {"03.flac": b"a" * 5})
        s2 = _make_dir(root, "album", {"03.flac": b"b" * 9})
        run = clean.Run(root, root / "log", dry_run=dry)
        clean.consolidate_group([canon, s1, s2], "t", run)
        run.close()
        return dict(run.stats)

    def test_three_way_merge_same_extra_track_parity(self):
        # Both sources carry 03.flac (different sizes): apply moves the first
        # and keeps the second as a .from-fragment; the preview must agree.
        dry, apply = self._three_way_merge(True), self._three_way_merge(False)
        self.assertEqual(apply["moves"], 1)
        self.assertEqual(apply["collisions_kept"], 1)
        self.assertEqual(dry, apply)

    def _same_render_siblings(self, dry):
        root = self.base / ("dry" if dry else "apply") / "render"
        _make_dir(root, "A‐B", {"a.flac": b"1"})  # U+2010 hyphen
        _make_dir(root, "A‑B", {"b.flac": b"2"})  # U+2011 non-breaking
        run = clean.Run(root, root / "log", dry_run=dry)
        clean.normalize_tree(root, run, True, False)
        run.close()
        return dict(run.stats)

    def test_same_render_siblings_parity(self):
        # Both fold to "A-B"; only one rename can land. (The two names
        # normalize to the same key, so Pass 1 would normally merge them
        # first; this pins the --normalize-names-only shape.)
        dry, apply = self._same_render_siblings(True), self._same_render_siblings(False)
        self.assertEqual(apply["renamed"], 1)
        self.assertEqual(dry, apply)

    def test_find_groups_skips_virtually_removed(self):
        root = self.base / "walkskip"
        a = _make_dir(root, "Album", {"01.flac": b"x"})
        _make_dir(root, "album", {"02.flac": b"y"})
        run = clean.Run(root, root / "log", dry_run=True)
        run._move(a, root / "elsewhere")  # virtually gone
        groups = clean.find_groups(root, run)
        run.close()
        self.assertEqual(groups, [])  # its variant no longer forms a group


class NormalizeTagsMergeTests(_RunCase):
    """Library-wide Pass 4 composed with an artist-folder merge (authority)."""

    def setUp(self):
        super().setUp()
        self.genre = self.root / "Alt Country"
        self.surv = self.genre / "Bonnie 'Prince' Billy"  # wins by file count (2 > 1)
        _mp3(
            self.surv / "I See a Darkness" / "01.mp3",
            artist="Bonnie Prince Billy / Bonnie 'Prince' Billy",
            albumartist="Bonnie 'Prince' Billy",
        )
        _mp3(
            self.surv / "I See a Darkness" / "02.mp3",
            artist="Bonnie 'Prince' Billy",
            albumartist="Bonnie 'Prince' Billy",
        )
        _mp3(
            self.genre / "Bonnie Prince Billy" / "Beware" / "01.mp3",
            artist="Bonnie Prince Billy",
            albumartist="Bonnie Prince Billy",
        )
        _mp3(
            self.genre / "Bonnie “Prince” Billy" / "Purple Bird" / "01.mp3",
            artist="Bonnie \x93Prince\x94 Billy feat. Tim O\x92Brien",
            albumartist="Bonnie \x93Prince\x94 Billy",
        )

    def _run2(self, dry_run=False):
        return self._run(dry_run=dry_run, artist_depth=2)

    def _consolidate(self, run):
        clean.consolidate_group(
            clean.find_groups(self.genre, run)[0], "Alt Country", run
        )

    def test_merge_restamps_all_to_survivor(self):
        run = self._run2()
        self._consolidate(run)
        clean.normalize_tags(run)
        run.close()
        beware = self.surv / "Beware" / "01.mp3"
        purple = self.surv / "Purple Bird" / "01.mp3"
        self.assertEqual(ID3(beware)["TPE1"].text[0], "Bonnie 'Prince' Billy")
        self.assertEqual(ID3(beware)["TPE2"].text[0], "Bonnie 'Prince' Billy")
        self.assertEqual(
            ID3(purple)["TPE1"].text[0], "Bonnie 'Prince' Billy feat. Tim O'Brien"
        )
        self.assertEqual(
            ID3(self.surv / "I See a Darkness" / "01.mp3")["TPE1"].text[0],
            "Bonnie 'Prince' Billy",
        )

    def test_dry_run_reports_but_does_not_write(self):
        run = self._run2(dry_run=True)
        self._consolidate(run)
        clean.normalize_tags(run)
        run.close()
        self.assertEqual(
            ID3(self.genre / "Bonnie Prince Billy" / "Beware" / "01.mp3")["TPE1"].text[
                0
            ],
            "Bonnie Prince Billy",
        )
        self.assertGreater(run.stats["tags_rewritten"], 0)

    def test_clean_file_not_rewritten(self):
        run = self._run2()
        self._consolidate(run)
        clean.normalize_tags(run)
        run.close()
        # 4 tracks scanned; survivor track 02 was already correct.
        self.assertEqual(run.stats["tag_files_scanned"], 4)
        self.assertEqual(run.stats["tags_rewritten"], 3)

    def test_wrong_depth_skips_authority_but_still_folds(self):
        # artist_depth=1: the depth-2 survivor is NOT an artist folder, so no
        # authority is recorded. The no-quotes name is therefore NOT forced to the
        # quoted survivor, but mojibake is still typographically folded everywhere.
        run = self._run(artist_depth=1)
        self._consolidate(run)
        clean.normalize_tags(run)
        run.close()
        self.assertEqual(
            ID3(self.surv / "Beware" / "01.mp3")["TPE1"].text[0], "Bonnie Prince Billy"
        )  # not restamped
        self.assertEqual(
            ID3(self.surv / "Purple Bird" / "01.mp3")["TPE1"].text[0],
            'Bonnie "Prince" Billy feat. Tim O\'Brien',
        )  # plain fold


class LibraryWideTagTests(_RunCase):
    def test_folds_without_any_merge(self):
        # No merge, no authority: title/album/artist still get a typographic fold.
        p = self.root / "Artist" / "Album" / "01.mp3"
        _mp3(
            p,
            artist="Bj\x94rk",
            albumartist="Bj\x94rk",
            title="O’Hare",
            album="Da\x92y",
        )
        run = self._run(artist_depth=2)
        clean.normalize_tags(run)
        run.close()
        t = ID3(p)
        self.assertEqual(t["TIT2"].text[0], "O'Hare")
        self.assertEqual(t["TALB"].text[0], "Da'y")
        self.assertEqual(t["TPE1"].text[0], 'Bj"rk')  # plain fold (no authority)
        self.assertEqual(run.stats["tags_rewritten"], 1)

    def test_unsupported_format_reported_not_touched(self):
        (self.root / "A").mkdir()
        (self.root / "A" / "x.wav").write_bytes(b"RIFFxxxx")
        run = self._run()
        clean.normalize_tags(run)
        run.close()
        self.assertEqual(run.stats["tag_unsupported_skipped"], 1)
        self.assertEqual(run.stats["tag_files_scanned"], 0)


class NameRecursionTests(_RunCase):
    def test_deep_folder_rename_all_levels(self):
        deep = self.root / "Rock" / "Artist‐X" / "Album’s Best"  # U+2010 + curly '
        deep.mkdir(parents=True)
        (deep / "t.mp3").write_bytes(b"x")
        run = self._run(normalize_tags=False)
        clean.normalize_tree(self.root, run, True, False)
        run.close()
        self.assertTrue((self.root / "Rock" / "Artist-X" / "Album's Best").is_dir())

    def test_filename_rename(self):
        d = self.root / "A" / "B"
        d.mkdir(parents=True)
        (d / "01 - Re‐do.flac").write_bytes(b"x")  # U+2010 in stem
        run = self._run(normalize_tags=False)
        clean.normalize_tree(self.root, run, False, True)
        run.close()
        self.assertTrue((d / "01 - Re-do.flac").exists())
        self.assertEqual(run.stats["files_renamed"], 1)

    def test_wma_filename_rename(self):
        # H2: --normalize-filenames must reach .wma tracks too.
        d = self.root / "A"
        d.mkdir()
        (d / "Re‐do.wma").write_bytes(b"x")  # U+2010 in stem
        run = self._run(normalize_tags=False)
        clean.normalize_tree(self.root, run, False, True)
        run.close()
        self.assertTrue((d / "Re-do.wma").exists())
        self.assertEqual(run.stats["files_renamed"], 1)

    def test_filename_collision_retained(self):
        d = self.root / "A"
        d.mkdir()
        (d / "Re‐do.flac").write_bytes(b"x")  # folds onto the existing name
        (d / "Re-do.flac").write_bytes(b"y")
        run = self._run(normalize_tags=False)
        clean.normalize_tree(self.root, run, False, True)
        run.close()
        self.assertTrue((d / "Re‐do.flac").exists())  # not clobbered
        self.assertEqual((d / "Re-do.flac").read_bytes(), b"y")

    def test_folders_flag_leaves_files_alone(self):
        d = self.root / "Artist‐X"
        d.mkdir()
        (d / "01 - Re‐do.flac").write_bytes(b"x")
        run = self._run(normalize_tags=False)
        clean.normalize_tree(self.root, run, True, False)  # folders only
        run.close()
        self.assertTrue((self.root / "Artist-X" / "01 - Re‐do.flac").exists())
        self.assertEqual(run.stats["files_renamed"], 0)

    def test_dry_run_changes_nothing(self):
        d = self.root / "Artist‐X"
        d.mkdir()
        (d / "01 - Re‐do.flac").write_bytes(b"x")
        run = self._run(dry_run=True, normalize_tags=False)
        clean.normalize_tree(self.root, run, True, True)
        run.close()
        self.assertTrue((self.root / "Artist‐X").is_dir())
        self.assertTrue((d / "01 - Re‐do.flac").exists())
        self.assertGreater(run.stats["renamed"] + run.stats["files_renamed"], 0)


class IdempotencyTests(_RunCase):
    def test_full_pipeline_idempotent(self):
        artist = self.root / "Folk" / "Sinéad O’Connor" / "Album"  # curly ' in folder
        _mp3(
            artist / "01 - So’ng.mp3",
            artist="Sinéad O\x92Connor",
            albumartist="Sinéad O\x92Connor",
            title="So’ng",
            album="Al\x92bum",
        )

        run1 = self._run(artist_depth=2)
        clean.normalize_tree(self.root, run1, True, True)
        clean.normalize_tags(run1)
        run1.close()
        self.assertGreater(
            run1.stats["renamed"]
            + run1.stats["files_renamed"]
            + run1.stats["tags_rewritten"],
            0,
        )

        run2 = self._run(artist_depth=2)
        clean.normalize_tree(self.root, run2, True, True)
        clean.normalize_tags(run2)
        run2.close()
        self.assertEqual(run2.stats["renamed"], 0)
        self.assertEqual(run2.stats["files_renamed"], 0)
        self.assertEqual(run2.stats["tags_rewritten"], 0)


class HeadTailEqualTests(unittest.TestCase):
    def _pair(self, a: bytes, b: bytes):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        pa, pb = Path(self.tmp.name, "a"), Path(self.tmp.name, "b")
        pa.write_bytes(a)
        pb.write_bytes(b)
        return pa, pb

    def test_equal_files_match(self):
        pa, pb = self._pair(b"x" * 200_000, b"x" * 200_000)
        self.assertTrue(clean.head_tail_equal(pa, pb))

    def test_difference_in_head_detected(self):
        pa, pb = self._pair(b"a" + b"x" * 199_999, b"b" + b"x" * 199_999)
        self.assertFalse(clean.head_tail_equal(pa, pb))

    def test_difference_in_tail_detected(self):
        pa, pb = self._pair(b"x" * 199_999 + b"a", b"x" * 199_999 + b"b")
        self.assertFalse(clean.head_tail_equal(pa, pb))

    def test_unreadable_is_not_identical(self):
        pa, pb = self._pair(b"x", b"x")
        self.assertFalse(clean.head_tail_equal(pa / "nope", pb))


class LogFormatTests(_TreeCase):
    def test_pass_header_newline_precedes_timestamp(self):
        # C7: run.log("\n--- PASS ---") used to orphan the timestamp prefix
        # onto its own line; the blank line is emitted separately now.
        run = self._run()
        run.log("\n--- PASS test ---")
        run.close()
        lines = self.log.read_text(encoding="utf-8").split("\n")
        self.assertEqual(lines[0], "")
        self.assertRegex(lines[1], r"^\[\d{4}-\d{2}-\d{2}T[\d:]+\] --- PASS test ---$")


class TagTargetDedupeTests(_RunCase):
    def test_survivor_rename_records_one_tag_target(self):
        # C8: a merge whose survivor also gets renamed used to record the
        # artist twice (once from the rename hook, once from the group).
        canon = _make_dir(
            self.root, "Drive‐By Truckers", {"01.mp3": b"a", "02.mp3": b"b"}
        )
        src = _make_dir(self.root, "Drive-By Truckers", {"03.mp3": b"c"})
        run = self._run()
        clean.consolidate_group([canon, src], "test", run)
        run.close()
        self.assertEqual(run.stats["renamed"], 1)  # survivor rename happened
        self.assertEqual(len(run.tag_targets), 1)
        name, folders = run.tag_targets[0]
        self.assertEqual(name, "Drive-By Truckers")
        # The group record carries the sources, so the tag pass still walks
        # every folder involved.
        self.assertIn(src, folders)

    def test_pass3_sweep_rename_still_records(self):
        _make_dir(self.root, "Drive‐By Truckers", {"01.mp3": b"a"})
        run = self._run()
        clean.normalize_tree(self.root, run, True, False)
        run.close()
        self.assertEqual(len(run.tag_targets), 1)
        self.assertEqual(run.tag_targets[0][0], "Drive-By Truckers")


class HeaderlessMp3Tests(_RunCase):
    def test_no_id3_header_is_reported_and_counted(self):
        # C2: an MP3 whose tags live only in APEv2/ID3v1 was silently skipped;
        # the contract is "reported and skipped" (the .wav path is the model).
        p = self.root / "Artist" / "x.mp3"
        p.parent.mkdir()
        p.write_bytes(b"\xff\xfb" + b"\x00" * 200)  # bare MPEG frame, no ID3
        run = self._run()
        clean.normalize_file_tags(p, None, run)
        run.close()
        self.assertEqual(run.stats["tag_no_id3_skipped"], 1)
        self.assertIn("no ID3 header", self.log.read_text(encoding="utf-8"))


class AsfCaseVariantTests(unittest.TestCase):
    def test_wma_case_variant_key_deleted_on_write(self):
        # C3: the ASF branch read keys case-insensitively but wrote canonical
        # case without deleting a case-variant original, leaving two keys.
        from unittest import mock

        class FakeASF(dict):
            saved = False

            def save(self):
                self.saved = True

        fake = FakeASF()
        fake["wm/albumtitle"] = ["Old’s"]
        with mock.patch.object(clean, "ASF", return_value=fake):
            opened = clean._open_for_tags(Path("/x.wma"), ".wma")
        self.assertIsNotNone(opened)
        cur, apply = opened
        self.assertEqual(cur["album"], ["Old’s"])
        apply({"album": ["Old's"]})
        self.assertNotIn("wm/albumtitle", fake)
        self.assertEqual(fake["WM/AlbumTitle"], ["Old's"])
        self.assertTrue(fake.saved)
