import contextlib
import io
import re
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path

# The folder restructurer lives in the package (the 6.0.0 fold of
# scripts/genre_foldermap.py, now a launcher); the gf alias keeps the historic
# name. The lattice scan (scan_album_dirs) is not exercised here — its records
# are faked — mirroring test_genre_tidy.py, which tests decision logic without
# the scan.
from lattice.modes import foldermap as gf

# The scanner yields rows with .path and .genre; only those two fields are read.
FakeAD = namedtuple("FakeAD", "path genre")


class SanitizeTests(unittest.TestCase):
    def test_keeps_ntfs_safe_genres_verbatim(self):
        for g in ("R&B", "Drum & Bass", "G-Funk", "Hip Hop", "Post-Hardcore"):
            self.assertEqual(gf.sanitize_component(g), g)

    def test_folds_forbidden_chars(self):
        self.assertEqual(gf.sanitize_component("AC/DC"), "AC DC")
        self.assertEqual(gf.sanitize_component('a:b*c?"d'), "a b c d")

    def test_strips_trailing_dot_and_space(self):
        self.assertEqual(gf.sanitize_component("Genre. "), "Genre")

    def test_empty_becomes_unknown(self):
        self.assertEqual(gf.sanitize_component("///"), "Unknown")


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("/m")

    def test_depth_two_is_a_stray_album(self):
        self.assertEqual(
            gf.classify(Path("/m/Aesop Rock/Skelethon"), self.root),
            ("album", "Aesop Rock", "Skelethon"),
        )

    def test_depth_three_is_already_organized(self):
        self.assertEqual(
            gf.classify(Path("/m/Abstract Hip Hop/Aesop Rock/Skelethon"), self.root),
            ("organized", "Abstract Hip Hop", "Aesop Rock", "Skelethon"),
        )

    def test_deeper_than_genre_artist_album_is_flagged(self):
        # The wrong-root case: pointing at the parent makes every album a level
        # too deep. It must be flagged, not collapsed to its last two parts.
        self.assertEqual(
            gf.classify(Path("/m/Music/Genre/Artist/Album"), self.root),
            ("toodeep", 4),
        )

    def test_loose_artist_dir(self):
        self.assertEqual(
            gf.classify(Path("/m/J. Cole"), self.root), ("loose", "J. Cole")
        )

    def test_root_itself_is_skipped(self):
        kind, _reason = gf.classify(self.root, self.root)
        self.assertEqual(kind, "skip")

    def test_staging_prefix_is_stripped_to_a_stray_album(self):
        # An album dumped in the staging inbox classifies as a flat stray, so it
        # is filed into the real taxonomy rather than read as a "Unfiltered" genre.
        self.assertEqual(
            gf.classify(
                Path("/m/Unfiltered/Kanye West/BULLY"), self.root, staging="Unfiltered"
            ),
            ("album", "Kanye West", "BULLY"),
        )

    def test_staging_prefix_strips_for_loose_artist(self):
        self.assertEqual(
            gf.classify(Path("/m/Unfiltered/J. Cole"), self.root, staging="Unfiltered"),
            ("loose", "J. Cole"),
        )

    def test_without_staging_the_inbox_looks_organized(self):
        # The bug being fixed: with no staging set, Unfiltered/Artist/Album reads
        # as an already-organized album under a genre named "Unfiltered".
        self.assertEqual(
            gf.classify(Path("/m/Unfiltered/Kanye West/BULLY"), self.root),
            ("organized", "Unfiltered", "Kanye West", "BULLY"),
        )

    def test_flat_disc_subfolder_is_the_parent_album(self):
        # H4: Artist/Album/CD1 in a flat library is NOT an organized album under
        # a genre named "Artist"; it collapses to its parent album unit.
        for disc in ("CD1", "CD 2", "Disc 1", "disk_2", "DVD 1", "Side 1", "Vinyl 2"):
            self.assertEqual(
                gf.classify(Path(f"/m/Artist/Album/{disc}"), self.root),
                ("album", "Artist", "Album"),
            )

    def test_organized_disc_subfolder_is_its_album_not_toodeep(self):
        self.assertEqual(
            gf.classify(Path("/m/Rock/Artist/Album/CD2"), self.root),
            ("organized", "Rock", "Artist", "Album"),
        )

    def test_staged_disc_subfolder_collapses_to_the_album(self):
        self.assertEqual(
            gf.classify(
                Path("/m/Unfiltered/Artist/Album/CD1"), self.root, staging="Unfiltered"
            ),
            ("album", "Artist", "Album"),
        )

    def test_staged_non_disc_depth_is_never_organized(self):
        # Nothing inside the inbox can be "organized"; a weird deep dump is
        # flagged for manual review instead.
        self.assertEqual(
            gf.classify(Path("/m/Unfiltered/A/B/C"), self.root, staging="Unfiltered"),
            ("staged-toodeep", 3),
        )
        self.assertEqual(
            gf.classify(
                Path("/m/Unfiltered/A/B/C/weird"), self.root, staging="Unfiltered"
            ),
            ("staged-toodeep", 4),
        )

    def test_album_actually_named_like_a_disc_is_not_collapsed(self):
        # A real album folder that merely resembles a disc name must not drag
        # its artist dir along as the move unit.
        self.assertEqual(
            gf.classify(Path("/m/Artist/CD1"), self.root), ("album", "Artist", "CD1")
        )


def _make_tree(root: Path, layout: dict) -> None:
    """layout maps a relative path -> file contents (str). Parent dirs are made."""
    for rel, content in layout.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


class BuildPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_album_maps_to_genre_artist_album(self):
        _make_tree(self.root, {"Aesop Rock/Skelethon/01.opus": "x"})
        rec = FakeAD(str(self.root / "Aesop Rock/Skelethon"), "Abstract Hip Hop")
        moves, issues, sources = gf.build_plan([rec], self.root)
        self.assertEqual(issues, [])
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0].kind, "dir")
        self.assertEqual(
            moves[0].dst, self.root / "Abstract Hip Hop/Aesop Rock/Skelethon"
        )
        self.assertIn(self.root / "Aesop Rock", sources)

    def test_loose_tracks_go_to_singles_as_file_moves(self):
        _make_tree(
            self.root,
            {
                "J. Cole/track1.mp3": "a",
                "J. Cole/track2.mp3": "b",
                "J. Cole/cover.jpg": "c",
            },
        )
        rec = FakeAD(str(self.root / "J. Cole"), "Conscious Hip Hop")
        moves, issues, _sources = gf.build_plan([rec], self.root)
        self.assertEqual(issues, [])
        self.assertTrue(all(m.kind == "file" for m in moves))
        dsts = {m.dst for m in moves}
        self.assertEqual(
            dsts,
            {
                self.root / "Conscious Hip Hop/J. Cole/Singles/track1.mp3",
                self.root / "Conscious Hip Hop/J. Cole/Singles/track2.mp3",
                self.root / "Conscious Hip Hop/J. Cole/Singles/cover.jpg",
            },
        )

    def test_loose_dir_with_subfolder_album_only_moves_loose_files(self):
        # An artist with both a loose single AND an album subfolder: the loose
        # file is its own record; the subfolder is a separate record/genre.
        _make_tree(
            self.root,
            {
                "Denzel Curry/single.mp3": "a",
                "Denzel Curry/TA13OO/01.mp3": "b",
            },
        )
        loose = FakeAD(str(self.root / "Denzel Curry"), "Trap Metal")
        album = FakeAD(str(self.root / "Denzel Curry/TA13OO"), "Experimental Hip Hop")
        moves, issues, _ = gf.build_plan([loose, album], self.root)
        self.assertEqual(issues, [])
        by_dst = {m.dst: m for m in moves}
        # Loose single -> its genre's Singles; subfolder album never dragged along.
        self.assertIn(self.root / "Trap Metal/Denzel Curry/Singles/single.mp3", by_dst)
        self.assertIn(self.root / "Experimental Hip Hop/Denzel Curry/TA13OO", by_dst)
        self.assertNotIn(self.root / "Trap Metal/Denzel Curry/Singles/TA13OO", by_dst)

    def test_artist_level_cover_follows_artist_to_genre(self):
        # The orphan bug: an Artist/cover.jpg beside album subfolders must not be
        # left behind. It follows the artist to its (single) genre, sibling to
        # the album folders — not into a Singles folder.
        _make_tree(
            self.root,
            {
                "AFI/Sing the Sorrow/01.mp3": "a",
                "AFI/Decemberunderground/01.mp3": "b",
                "AFI/cover.jpg": "art",
            },
        )
        recs = [
            FakeAD(str(self.root / "AFI/Sing the Sorrow"), "Post-Hardcore"),
            FakeAD(str(self.root / "AFI/Decemberunderground"), "Post-Hardcore"),
        ]
        moves, issues, _ = gf.build_plan(recs, self.root)
        self.assertEqual(issues, [])  # single genre -> no ambiguity note
        cover = next(m for m in moves if m.src.name == "cover.jpg")
        self.assertEqual(cover.kind, "file")
        self.assertEqual(cover.dst, self.root / "Post-Hardcore/AFI/cover.jpg")

    def test_artist_level_sidecar_uses_dominant_genre_when_split(self):
        # An artist split across genres: the artist-level file goes to the
        # dominant (most-album) genre, and the split is flagged as a NOTE.
        _make_tree(
            self.root,
            {
                "Deftones/White Pony/01.mp3": "a",
                "Deftones/Around the Fur/01.mp3": "b",
                "Deftones/Saturday Night Wrist/01.mp3": "c",
                "Deftones/band.jpg": "art",
            },
        )
        recs = [
            FakeAD(str(self.root / "Deftones/White Pony"), "Alternative Metal"),
            FakeAD(str(self.root / "Deftones/Around the Fur"), "Alternative Metal"),
            FakeAD(str(self.root / "Deftones/Saturday Night Wrist"), "Nu Metal"),
        ]
        moves, issues, _ = gf.build_plan(recs, self.root)
        sidecar = next(m for m in moves if m.src.name == "band.jpg")
        self.assertEqual(sidecar.dst, self.root / "Alternative Metal/Deftones/band.jpg")
        self.assertTrue(any("artist spans 2 genres" in m for m in issues))

    def test_loose_artist_sidecars_stay_in_singles(self):
        # A loose-track artist's direct files (incl. cover) are swept to Singles
        # by the loose pass; the sidecar pass must not also touch them.
        _make_tree(
            self.root, {"Desiigner/01 - Panda.mp3": "a", "Desiigner/cover.jpg": "art"}
        )
        rec = FakeAD(str(self.root / "Desiigner"), "Trap")
        moves, issues, _ = gf.build_plan([rec], self.root)
        self.assertEqual(issues, [])
        dsts = {m.dst for m in moves}
        self.assertIn(self.root / "Trap/Desiigner/Singles/cover.jpg", dsts)
        self.assertNotIn(self.root / "Trap/Desiigner/cover.jpg", dsts)

    def test_artist_with_cover_is_fully_pruned_after_apply(self):
        # End-to-end: the artist folder must be empty (and removed) after the
        # albums and the artist-level cover have all moved out.
        _make_tree(
            self.root,
            {"AFI/Sing the Sorrow/01.mp3": "a", "AFI/cover.jpg": "art"},
        )
        rec = FakeAD(str(self.root / "AFI/Sing the Sorrow"), "Post-Hardcore")
        moves, issues, sources = gf.build_plan([rec], self.root)
        self.assertEqual(issues, [])
        with gf.Runner(self.root / "m.tsv", dry_run=False, quiet=True) as runner:
            gf.execute(moves, sources, runner)
        self.assertFalse((self.root / "AFI").exists())
        self.assertEqual((self.root / "Post-Hardcore/AFI/cover.jpg").read_text(), "art")
        self.assertEqual(
            (self.root / "Post-Hardcore/AFI/Sing the Sorrow/01.mp3").read_text(), "a"
        )

    def test_staging_album_files_into_real_taxonomy(self):
        # An organized album seeds the genre vocabulary ({"Hip Hop"}); an album
        # in the Unfiltered inbox tagged with that genre is filed into the real
        # tree at the root, not left under "Unfiltered".
        _make_tree(
            self.root,
            {
                "Hip Hop/Nas/Illmatic/01.mp3": "a",
                "Unfiltered/Kanye West/BULLY/01.mp3": "b",
            },
        )
        recs = [
            FakeAD(str(self.root / "Hip Hop/Nas/Illmatic"), "Hip Hop"),
            FakeAD(str(self.root / "Unfiltered/Kanye West/BULLY"), "Hip Hop"),
        ]
        moves, issues, sources = gf.build_plan(recs, self.root, staging="Unfiltered")
        self.assertEqual(issues, [])
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0].dst, self.root / "Hip Hop/Kanye West/BULLY")
        self.assertIn(self.root / "Unfiltered/Kanye West", sources)

    def test_staging_apply_moves_cover_and_keeps_inbox(self):
        # End-to-end: the album (incl. its cover) moves out of the inbox, the
        # per-artist source dir is pruned, but the Unfiltered inbox is left intact.
        _make_tree(
            self.root,
            {
                "Hip Hop/Nas/Illmatic/01.mp3": "a",
                "Unfiltered/Kanye West/BULLY/01.mp3": "b",
                "Unfiltered/Kanye West/BULLY/cover.jpg": "art",
            },
        )
        recs = [
            FakeAD(str(self.root / "Hip Hop/Nas/Illmatic"), "Hip Hop"),
            FakeAD(str(self.root / "Unfiltered/Kanye West/BULLY"), "Hip Hop"),
        ]
        moves, issues, sources = gf.build_plan(recs, self.root, staging="Unfiltered")
        self.assertEqual(issues, [])
        with gf.Runner(self.root / "m.tsv", dry_run=False, quiet=True) as runner:
            gf.execute(moves, sources, runner, root=self.root, staging="Unfiltered")
        self.assertEqual(
            (self.root / "Hip Hop/Kanye West/BULLY/01.mp3").read_text(), "b"
        )
        self.assertEqual(
            (self.root / "Hip Hop/Kanye West/BULLY/cover.jpg").read_text(), "art"
        )
        self.assertFalse((self.root / "Unfiltered/Kanye West").exists())
        self.assertTrue((self.root / "Unfiltered").is_dir())  # inbox kept

    def test_staging_unknown_genre_is_gated(self):
        # The vocabulary gate still applies to inbox albums: a genre the library
        # doesn't use is flagged, not filed, even from the inbox.
        _make_tree(
            self.root,
            {
                "Hip Hop/Nas/Illmatic/01.mp3": "a",
                "Unfiltered/Weird Al/Polka Party/01.mp3": "b",
            },
        )
        recs = [
            FakeAD(str(self.root / "Hip Hop/Nas/Illmatic"), "Hip Hop"),
            FakeAD(str(self.root / "Unfiltered/Weird Al/Polka Party"), "Polka"),
        ]
        moves, issues, _ = gf.build_plan(recs, self.root, staging="Unfiltered")
        self.assertEqual(moves, [])
        self.assertTrue(any("UNKNOWN GENRE" in m and "Polka" in m for m in issues))

    def test_flat_disc_album_moves_as_one_unit_and_gate_stays_off(self):
        # H4: the scanner emits one record per audio-bearing dir, so a disc
        # album yields CD1+CD2 records. They must dedupe to ONE album move, the
        # artist name must not enter the genre vocabulary, and an ordinary stray
        # in the same library must still convert (gate off).
        _make_tree(
            self.root,
            {
                "Sigur Ros/Agaetis/CD1/01.flac": "a",
                "Sigur Ros/Agaetis/CD2/01.flac": "b",
                "Outkast/Stankonia/01.mp3": "c",
            },
        )
        recs = [
            FakeAD(str(self.root / "Sigur Ros/Agaetis/CD1"), "Post-Rock"),
            FakeAD(str(self.root / "Sigur Ros/Agaetis/CD2"), "Post-Rock"),
            FakeAD(str(self.root / "Outkast/Stankonia"), "Hip Hop"),
        ]
        moves, issues, sources = gf.build_plan(recs, self.root)
        self.assertEqual(issues, [])
        self.assertEqual(
            {m.dst for m in moves},
            {
                self.root / "Post-Rock/Sigur Ros/Agaetis",
                self.root / "Hip Hop/Outkast/Stankonia",
            },
        )
        album_moves = [m for m in moves if m.src == self.root / "Sigur Ros/Agaetis"]
        self.assertEqual(len(album_moves), 1)  # CD1+CD2 deduped to one unit
        self.assertIn(self.root / "Sigur Ros", sources)
        # End-to-end: the discs ride along inside the moved album.
        with gf.Runner(self.root / "m.tsv", dry_run=False, quiet=True) as runner:
            gf.execute(moves, sources, runner)
        self.assertEqual(
            (self.root / "Post-Rock/Sigur Ros/Agaetis/CD1/01.flac").read_text(), "a"
        )
        self.assertEqual(
            (self.root / "Post-Rock/Sigur Ros/Agaetis/CD2/01.flac").read_text(), "b"
        )
        self.assertFalse((self.root / "Sigur Ros").exists())

    def test_organized_disc_album_produces_no_toodeep_noise(self):
        _make_tree(self.root, {"Rock/Artist/Album/CD1/01.mp3": "a"})
        rec = FakeAD(str(self.root / "Rock/Artist/Album/CD1"), "Rock")
        moves, issues, _ = gf.build_plan([rec], self.root)
        self.assertEqual(moves, [])
        self.assertEqual(issues, [])

    def test_staged_disc_album_files_into_taxonomy(self):
        _make_tree(
            self.root,
            {
                "Hip Hop/Nas/Illmatic/01.mp3": "a",
                "Unfiltered/Artist/Album/CD1/01.mp3": "b",
            },
        )
        recs = [
            FakeAD(str(self.root / "Hip Hop/Nas/Illmatic"), "Hip Hop"),
            FakeAD(str(self.root / "Unfiltered/Artist/Album/CD1"), "Hip Hop"),
        ]
        moves, issues, _ = gf.build_plan(recs, self.root, staging="Unfiltered")
        self.assertEqual(issues, [])
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0].src, self.root / "Unfiltered/Artist/Album")
        self.assertEqual(moves[0].dst, self.root / "Hip Hop/Artist/Album")

    def test_staged_deep_non_disc_is_flagged_not_organized(self):
        _make_tree(
            self.root,
            {
                "Hip Hop/Nas/Illmatic/01.mp3": "a",
                "Unfiltered/A/B/C/weird/01.mp3": "b",
            },
        )
        recs = [
            FakeAD(str(self.root / "Hip Hop/Nas/Illmatic"), "Hip Hop"),
            FakeAD(str(self.root / "Unfiltered/A/B/C/weird"), "Polka"),
        ]
        moves, issues, _ = gf.build_plan(recs, self.root, staging="Unfiltered")
        self.assertEqual(moves, [])
        self.assertTrue(any("STAGED TOO DEEP" in m for m in issues))
        # "A" (the inbox artist) must not have entered the vocabulary as a genre.
        self.assertFalse(any("UNKNOWN GENRE" in m for m in issues))

    def test_disc_records_with_disagreeing_genres_keep_first_and_flag(self):
        _make_tree(
            self.root,
            {
                "Artist/Album/CD1/01.mp3": "a",
                "Artist/Album/CD2/01.mp3": "b",
            },
        )
        recs = [
            FakeAD(str(self.root / "Artist/Album/CD1"), "Rock"),
            FakeAD(str(self.root / "Artist/Album/CD2"), "Pop"),
        ]
        moves, issues, _ = gf.build_plan(recs, self.root)
        self.assertEqual(len(moves), 1)  # one unit, first record's genre wins
        self.assertEqual(moves[0].dst, self.root / "Rock/Artist/Album")
        self.assertTrue(any("DISC GENRE MISMATCH" in m for m in issues))

    def test_missing_genre_is_flagged_not_moved(self):
        _make_tree(self.root, {"Mystery/Album/01.opus": "x"})
        rec = FakeAD(str(self.root / "Mystery/Album"), "")
        moves, issues, _ = gf.build_plan([rec], self.root)
        self.assertEqual(moves, [])
        self.assertTrue(any("NO GENRE" in m for m in issues))

    def test_only_genre_filter(self):
        _make_tree(
            self.root,
            {"A/Alb1/01.opus": "x", "B/Alb2/01.opus": "y"},
        )
        recs = [
            FakeAD(str(self.root / "A/Alb1"), "Trap"),
            FakeAD(str(self.root / "B/Alb2"), "Drill"),
        ]
        moves, _, _ = gf.build_plan(recs, self.root, only_genres={"Trap"})
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0].dst, self.root / "Trap/A/Alb1")

    def test_rejected_album_keeps_its_sidecar_and_source_dir(self):
        # M12: an album skipped with DEST EXISTS must not have its artist-level
        # sidecar moved out from under it, nor its artist dir queued for prune.
        _make_tree(
            self.root,
            {"AFI/Sing the Sorrow/01.mp3": "a", "AFI/cover.jpg": "art"},
        )
        (self.root / "Post-Hardcore/AFI/Sing the Sorrow").mkdir(parents=True)
        rec = FakeAD(str(self.root / "AFI/Sing the Sorrow"), "Post-Hardcore")
        moves, issues, sources = gf.build_plan([rec], self.root)
        self.assertTrue(any("DEST EXISTS" in m for m in issues))
        self.assertEqual(moves, [])  # no album move AND no sidecar move
        self.assertNotIn(self.root / "AFI", sources)

    def test_dest_collision_is_flagged(self):
        # Two albums that would land on the same destination path.
        _make_tree(
            self.root,
            {"A/Live/01.opus": "x", "A/Live2/01.opus": "y"},
        )
        # Force a collision by faking identical album folder names via two recs
        # pointing at the same dest is unnatural; instead simulate a pre-existing
        # destination on disk.
        (self.root / "Trap/A/Live").mkdir(parents=True)
        rec = FakeAD(str(self.root / "A/Live"), "Trap")
        moves, issues, _ = gf.build_plan([rec], self.root)
        self.assertEqual(moves, [])
        self.assertTrue(any("DEST EXISTS" in m for m in issues))

    def test_stray_with_known_genre_moves_unknown_is_gated(self):
        # An organized album seeds the genre vocabulary ({"Hip Hop"}). A stray
        # tagged with that genre is filed; a stray tagged with a genre the
        # library doesn't use is flagged, never given a new top-level folder.
        _make_tree(
            self.root,
            {
                "Hip Hop/Nas/Illmatic/01.mp3": "a",
                "Outkast/Stankonia/01.mp3": "b",
                "Weird Al/Polka Party/01.mp3": "c",
            },
        )
        recs = [
            FakeAD(str(self.root / "Hip Hop/Nas/Illmatic"), "Hip Hop"),
            FakeAD(str(self.root / "Outkast/Stankonia"), "Hip Hop"),
            FakeAD(str(self.root / "Weird Al/Polka Party"), "Polka"),
        ]
        moves, issues, _ = gf.build_plan(recs, self.root)
        dsts = {m.dst for m in moves}
        self.assertEqual(dsts, {self.root / "Hip Hop/Outkast/Stankonia"})
        self.assertTrue(any("UNKNOWN GENRE" in m and "Polka" in m for m in issues))

    def test_allow_new_genre_bypasses_gating(self):
        _make_tree(
            self.root,
            {
                "Hip Hop/Nas/Illmatic/01.mp3": "a",
                "Weird Al/Polka Party/01.mp3": "c",
            },
        )
        recs = [
            FakeAD(str(self.root / "Hip Hop/Nas/Illmatic"), "Hip Hop"),
            FakeAD(str(self.root / "Weird Al/Polka Party"), "Polka"),
        ]
        moves, issues, _ = gf.build_plan(recs, self.root, allow_new_genre=True)
        self.assertIn(self.root / "Polka/Weird Al/Polka Party", {m.dst for m in moves})
        self.assertFalse(any("UNKNOWN GENRE" in m for m in issues))

    def test_greenfield_library_trusts_tags(self):
        # No genre folders yet: gating is off, every stray is filed by its tag.
        _make_tree(
            self.root,
            {"A/Alb1/01.opus": "x", "B/Alb2/01.opus": "y"},
        )
        recs = [
            FakeAD(str(self.root / "A/Alb1"), "Trap"),
            FakeAD(str(self.root / "B/Alb2"), "Polka"),
        ]
        moves, issues, _ = gf.build_plan(recs, self.root)
        self.assertEqual(len(moves), 2)
        self.assertEqual(issues, [])

    def test_organized_album_in_place_is_skipped(self):
        _make_tree(self.root, {"Hip Hop/Nas/Illmatic/01.mp3": "a"})
        rec = FakeAD(str(self.root / "Hip Hop/Nas/Illmatic"), "Hip Hop")
        moves, issues, _ = gf.build_plan([rec], self.root)
        self.assertEqual(moves, [])
        self.assertEqual(issues, [])

    def test_organized_album_mistagged_is_noted_not_moved(self):
        # Filed under "Blues" but tags say "Jazz" (itself an existing folder):
        # reported as a NOTE and left in place, never silently re-filed.
        _make_tree(
            self.root,
            {
                "Blues/B.B. King/Live in Cook County Jail/01.mp3": "a",
                "Jazz/Miles Davis/Kind of Blue/01.mp3": "b",
            },
        )
        recs = [
            FakeAD(str(self.root / "Blues/B.B. King/Live in Cook County Jail"), "Jazz"),
            FakeAD(str(self.root / "Jazz/Miles Davis/Kind of Blue"), "Jazz"),
        ]
        moves, issues, _ = gf.build_plan(recs, self.root)
        self.assertEqual(moves, [])
        self.assertTrue(
            any("NOTE" in m and "Blues" in m and "Jazz" in m for m in issues)
        )

    def test_too_deep_is_flagged_in_plan(self):
        _make_tree(self.root, {"Music/Genre/Artist/Album/01.mp3": "a"})
        rec = FakeAD(str(self.root / "Music/Genre/Artist/Album"), "Genre")
        moves, issues, _ = gf.build_plan([rec], self.root)
        self.assertEqual(moves, [])
        self.assertTrue(any("TOO DEEP" in m for m in issues))


class ApplyRevertRoundTripTests(unittest.TestCase):
    """End-to-end on a real temp tree: apply the plan, assert the new layout,
    then revert and assert the original tree is restored byte-for-byte."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.original = {
            "Aesop Rock/Skelethon/01 - ZZZ Top.opus": "song-a",
            "Aesop Rock/Skelethon/cover.jpg": "art",
            "J. Cole/loose single.mp3": "song-b",
            # "The Diplomats" is both a genre and an artist (the real name clash).
            "The Diplomats/Diplomatic Immunity/01.mp3": "song-c",
        }
        _make_tree(self.root, self.original)
        self.records = [
            FakeAD(str(self.root / "Aesop Rock/Skelethon"), "Abstract Hip Hop"),
            FakeAD(str(self.root / "J. Cole"), "Conscious Hip Hop"),
            FakeAD(
                str(self.root / "The Diplomats/Diplomatic Immunity"), "The Diplomats"
            ),
        ]
        self.manifest = self.root / "manifest.tsv"

    def tearDown(self):
        self.tmp.cleanup()

    def _snapshot(self) -> dict:
        # .tsv excluded: the manifest and (post-M13) the revert's own
        # .revert.tsv are bookkeeping, not library content.
        return {
            str(p.relative_to(self.root)): p.read_text(encoding="utf-8")
            for p in self.root.rglob("*")
            if p.is_file() and p.suffix != ".tsv"
        }

    def _apply(self):
        moves, issues, sources = gf.build_plan(self.records, self.root)
        self.assertEqual(issues, [])
        with gf.Runner(self.manifest, dry_run=False, quiet=True) as runner:
            gf.execute(moves, sources, runner)

    def test_applied_layout(self):
        self._apply()
        snap = self._snapshot()
        self.assertEqual(
            snap["Abstract Hip Hop/Aesop Rock/Skelethon/01 - ZZZ Top.opus"], "song-a"
        )
        self.assertEqual(snap["Abstract Hip Hop/Aesop Rock/Skelethon/cover.jpg"], "art")
        # Loose track wrapped in Singles/.
        self.assertEqual(
            snap["Conscious Hip Hop/J. Cole/Singles/loose single.mp3"], "song-b"
        )
        # The name-clash album placed under the genre that shares the artist name.
        self.assertEqual(
            snap["The Diplomats/The Diplomats/Diplomatic Immunity/01.mp3"], "song-c"
        )
        # Original artist folders pruned away.
        self.assertFalse((self.root / "Aesop Rock").exists())
        self.assertFalse((self.root / "J. Cole").exists())

    def test_dry_run_touches_nothing(self):
        before = self._snapshot()
        moves, _, sources = gf.build_plan(self.records, self.root)
        with gf.Runner(self.manifest, dry_run=True, quiet=True) as runner:
            gf.execute(moves, sources, runner)
        self.assertEqual(self._snapshot(), before)
        self.assertFalse(self.manifest.exists())

    def test_dry_run_predicts_pruning_without_removing(self):
        # The prune step must *predict* emptied source dirs in a dry-run (via the
        # virtual-removed set), not read the unchanged disk and report nothing.
        moves, _, sources = gf.build_plan(self.records, self.root)
        with gf.Runner(self.manifest, dry_run=True, quiet=True) as runner:
            gf.execute(moves, sources, runner)
        self.assertGreater(runner.stats["pruned"], 0)
        self.assertTrue((self.root / "Aesop Rock").exists())  # still there

    def _revert_summary(self, dry: bool) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = gf.revert(self.manifest, dry_run=dry, quiet=False)
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_revert_writes_replayable_manifest_and_log_lines(self):
        # M13: a revert moves as much as an apply; it must leave the same
        # audit trail (a .revert.tsv manifest with one row per restore).
        self._apply()
        out = self._revert_summary(dry=False)
        self.assertIn("MV revert:", out)
        revert_manifest = self.manifest.with_suffix(".revert.tsv")
        self.assertTrue(revert_manifest.exists())
        pairs = gf.parse_manifest(
            revert_manifest.read_text(encoding="utf-8").splitlines()
        )
        self.assertGreater(len(pairs), 0)

    def test_dry_run_revert_predicts_prunes(self):
        # M13: dry-run revert used to read unchanged disk and predict pruned=0.
        self._apply()
        dry = re.search(r"pruned=(\d+)", self._revert_summary(dry=True))
        self.assertIsNotNone(dry)
        real = re.search(r"pruned=(\d+)", self._revert_summary(dry=False))
        self.assertIsNotNone(real)
        self.assertEqual(dry.group(1), real.group(1))
        self.assertGreater(int(real.group(1)), 0)

    def test_dry_run_revert_touches_nothing(self):
        self._apply()
        before = self._snapshot()
        self._revert_summary(dry=True)
        self.assertEqual(self._snapshot(), before)

    def test_round_trip_restores_original(self):
        before = self._snapshot()
        self._apply()
        self.assertNotEqual(self._snapshot(), before)
        rc = gf.revert(self.manifest, dry_run=False, quiet=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self._snapshot(), before)
        # Genre trees cleared out by the revert prune.
        self.assertFalse((self.root / "Abstract Hip Hop").exists())
        self.assertFalse((self.root / "Conscious Hip Hop").exists())


class ManifestTests(unittest.TestCase):
    def test_parse_skips_comments_and_blanks(self):
        lines = [
            "# header",
            "",
            "/m/A/Alb\t/m/Genre/A/Alb\t2026-05-30T12:00:00",
            "  ",
        ]
        self.assertEqual(gf.parse_manifest(lines), [("/m/A/Alb", "/m/Genre/A/Alb")])


class GenreGateCaseTests(unittest.TestCase):
    """GF4: the vocabulary gate matches case-insensitively and reuses the
    existing folder's spelling, so a lowercase tag is neither flagged UNKNOWN
    nor (with --allow-new-genre) minted as a case-variant duplicate folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _make_tree(
            self.root,
            {
                "Hip Hop/Nas/Illmatic/01.mp3": "x",  # vocabulary: {"Hip Hop"}
                "Aesop Rock/Skelethon/01.mp3": "x",  # flat stray
            },
        )
        self.organized = FakeAD(str(self.root / "Hip Hop/Nas/Illmatic"), "Hip Hop")
        self.stray = FakeAD(str(self.root / "Aesop Rock/Skelethon"), "hip hop")

    def tearDown(self):
        self.tmp.cleanup()

    def test_case_variant_tag_files_into_existing_folder(self):
        moves, issues, _ = gf.build_plan([self.organized, self.stray], self.root)
        self.assertEqual(issues, [])
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0].dst, self.root / "Hip Hop/Aesop Rock/Skelethon")

    def test_allow_new_genre_still_reuses_existing_spelling(self):
        moves, issues, _ = gf.build_plan(
            [self.organized, self.stray], self.root, allow_new_genre=True
        )
        self.assertEqual(issues, [])
        self.assertEqual(moves[0].dst, self.root / "Hip Hop/Aesop Rock/Skelethon")

    def test_organized_case_variant_genre_is_not_a_note(self):
        organized = FakeAD(str(self.root / "Hip Hop/Nas/Illmatic"), "hip hop")
        moves, issues, _ = gf.build_plan([organized], self.root)
        self.assertEqual(issues, [])
        self.assertEqual(moves, [])


class UnsafeNameTests(unittest.TestCase):
    """GF3: a tab/newline in a folder name would corrupt the manifest TSV and
    make the move unrevertable; such moves are refused with an issue."""

    def test_tab_in_album_name_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_tree(root, {"Artist/Al\tbum/01.mp3": "x"})
            rec = FakeAD(str(root / "Artist/Al\tbum"), "Rock")
            moves, issues, sources = gf.build_plan([rec], root)
            self.assertEqual(moves, [])
            self.assertTrue(any("UNSAFE NAME" in i for i in issues))
            self.assertEqual(sources, set())  # rejected move: no prune target


class CrossDeviceTests(unittest.TestCase):
    """GF2: shutil.move silently degrades to copy+delete across devices; the
    Runner refuses instead (mv-only contract, audio bytes never rewritten)."""

    def test_cross_device_move_is_refused(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_tree(root, {"Artist/Album/01.mp3": "x"})
            src = root / "Artist" / "Album"
            dst = root / "Rock" / "Artist" / "Album"
            manifest = root / "m.tsv"

            def fake_stat(p, **kw):
                return mock.Mock(st_dev=1 if p == src else 2)

            with gf.Runner(manifest, dry_run=False, quiet=True) as runner:
                with mock.patch("pathlib.Path.stat", new=fake_stat):
                    runner.do_move(src, dst, "dir")
            self.assertTrue(src.exists())
            self.assertFalse(dst.exists())
            # The refusal check runs before any mkdir, so no stray empty
            # destination tree is left behind.
            self.assertFalse((root / "Rock").exists())
            self.assertEqual(runner.stats["cross_device_refused"], 1)
            self.assertEqual(runner.stats["moved_dir"], 0)
            # Nothing recorded in the manifest for a refused move.
            self.assertEqual(gf.parse_manifest(manifest.read_text().splitlines()), [])

    def test_dry_run_predicts_the_refusal(self):
        # The check must run in dry-run too: a preview that shows a clean
        # "would move" for a move --apply will refuse is a broken preview.
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_tree(root, {"Artist/Album/01.mp3": "x"})
            src = root / "Artist" / "Album"
            dst = root / "Rock" / "Artist" / "Album"

            def fake_stat(p, **kw):
                return mock.Mock(st_dev=1 if p == src else 2)

            with gf.Runner(root / "m.tsv", dry_run=True, quiet=True) as runner:
                with mock.patch("pathlib.Path.stat", new=fake_stat):
                    runner.do_move(src, dst, "dir")
            self.assertEqual(runner.stats["cross_device_refused"], 1)
            self.assertEqual(runner.stats["moved_dir"], 0)
            self.assertEqual(runner.removed, set())
            self.assertEqual(runner.created, {})


class MultiGenreTagTests(unittest.TestCase):
    """A single tag value carrying several genres joined by ';' or '/' must
    never become one path component (the 'Drill;East Coast Hip Hop;...' folder
    bug): the first component places the album, the rest are flagged."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_semicolon_joined_tag_files_under_first_component(self):
        _make_tree(
            self.root,
            {
                "6ix9ine/BLACKBALLED/01.mp3": "x",
                "Drill/Pop Smoke/Meet the Woo/01.mp3": "y",
            },
        )
        recs = [
            FakeAD(
                str(self.root / "6ix9ine/BLACKBALLED"),
                "Drill;East Coast Hip Hop;Hip Hop;Trap;Hardcore Hip Hop",
            ),
            FakeAD(str(self.root / "Drill/Pop Smoke/Meet the Woo"), "Drill"),
        ]
        moves, issues, _ = gf.build_plan(recs, self.root)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0].dst, self.root / "Drill/6ix9ine/BLACKBALLED")
        self.assertTrue(any("MULTI-GENRE TAG" in m for m in issues))
        self.assertFalse(any(";" in str(m.dst) for m in moves))

    def test_slash_joined_tag_splits_instead_of_sanitizing(self):
        # Before the guard, "Hip-Hop/Rap" sanitized to a single "Hip-Hop Rap"
        # folder; the slash is the documented multi-genre separator (§3), so it
        # must split the same way the wing modes split it.
        _make_tree(self.root, {"AZ/Doe or Die/01.mp3": "x"})
        rec = FakeAD(str(self.root / "AZ/Doe or Die"), "Hip-Hop/Rap")
        moves, issues, _ = gf.build_plan([rec], self.root)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0].dst, self.root / "Hip-Hop/AZ/Doe or Die")
        self.assertTrue(any("MULTI-GENRE TAG" in m for m in issues))

    def test_separator_only_tag_is_no_genre(self):
        _make_tree(self.root, {"A/B/01.mp3": "x"})
        rec = FakeAD(str(self.root / "A/B"), " ; / ;")
        moves, issues, _ = gf.build_plan([rec], self.root)
        self.assertEqual(moves, [])
        self.assertTrue(any("NO GENRE" in m for m in issues))
        self.assertFalse(any("MULTI-GENRE TAG" in m for m in issues))

    def test_organized_album_with_multi_tag_matching_folder_stays_put(self):
        # Primary component matches the folder: no move, no NOTE, but the bad
        # tag is still surfaced so it can be retagged at the source.
        _make_tree(self.root, {"Drill/6ix9ine/BLACKBALLED/01.mp3": "x"})
        rec = FakeAD(str(self.root / "Drill/6ix9ine/BLACKBALLED"), "Drill;Trap")
        moves, issues, _ = gf.build_plan([rec], self.root)
        self.assertEqual(moves, [])
        self.assertTrue(any("MULTI-GENRE TAG" in m for m in issues))
        self.assertFalse(any("NOTE" in m for m in issues))


class RefileMismatchedTests(unittest.TestCase):
    """--refile-mismatched: an organized album whose tag genre disagrees with
    its genre folder moves to the tag's folder (still vocabulary-gated);
    without the flag the old NOTE-and-leave behavior is unchanged."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _wlr(self):
        _make_tree(
            self.root,
            {
                "Trap/Playboi Carti/Whole Lotta Red/01.mp3": "x",
                "Rage/YNG Martyr/BURR/01.mp3": "y",
            },
        )
        return [
            FakeAD(str(self.root / "Trap/Playboi Carti/Whole Lotta Red"), "Rage"),
            FakeAD(str(self.root / "Rage/YNG Martyr/BURR"), "Rage"),
        ]

    def test_default_still_notes_and_leaves_in_place(self):
        recs = self._wlr()
        moves, issues, _ = gf.build_plan(recs, self.root)
        self.assertEqual(moves, [])
        self.assertTrue(any("NOTE" in m for m in issues))

    def test_refile_moves_album_to_tag_genre(self):
        recs = self._wlr()
        moves, issues, sources = gf.build_plan(recs, self.root, refile_mismatched=True)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0].kind, "dir")
        self.assertEqual(moves[0].src, self.root / "Trap/Playboi Carti/Whole Lotta Red")
        self.assertEqual(moves[0].dst, self.root / "Rage/Playboi Carti/Whole Lotta Red")
        self.assertIn(self.root / "Trap/Playboi Carti", sources)
        self.assertFalse(any("NOTE" in m for m in issues))

    def test_refile_respects_vocabulary_gate(self):
        # Tag genre "Rage" has no organized album seeding the vocabulary, so
        # the refile is refused like any stray with an unknown genre.
        _make_tree(self.root, {"Trap/Playboi Carti/Whole Lotta Red/01.mp3": "x"})
        rec = FakeAD(str(self.root / "Trap/Playboi Carti/Whole Lotta Red"), "Rage")
        moves, issues, _ = gf.build_plan([rec], self.root, refile_mismatched=True)
        self.assertEqual(moves, [])
        self.assertTrue(any("UNKNOWN GENRE" in m for m in issues))

    def test_refile_allow_new_genre_overrides_gate(self):
        _make_tree(self.root, {"Trap/Playboi Carti/Whole Lotta Red/01.mp3": "x"})
        rec = FakeAD(str(self.root / "Trap/Playboi Carti/Whole Lotta Red"), "Rage")
        moves, _, _ = gf.build_plan(
            [rec], self.root, allow_new_genre=True, refile_mismatched=True
        )
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0].dst, self.root / "Rage/Playboi Carti/Whole Lotta Red")

    def test_refile_reuses_existing_folder_spelling(self):
        # Tag says "rage" but the library's folder is "Rage": the move must
        # reuse the on-disk spelling, not mint a case-variant sibling.
        recs = self._wlr()
        recs[0] = FakeAD(recs[0].path, "rage")
        moves, _, _ = gf.build_plan(recs, self.root, refile_mismatched=True)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0].dst, self.root / "Rage/Playboi Carti/Whole Lotta Red")

    def test_refile_dest_exists_is_skipped(self):
        _make_tree(
            self.root,
            {
                "Trap/Playboi Carti/Whole Lotta Red/01.mp3": "x",
                "Rage/Playboi Carti/Whole Lotta Red/01.mp3": "dupe",
                "Rage/YNG Martyr/BURR/01.mp3": "y",
            },
        )
        recs = [
            FakeAD(str(self.root / "Trap/Playboi Carti/Whole Lotta Red"), "Rage"),
            FakeAD(str(self.root / "Rage/Playboi Carti/Whole Lotta Red"), "Rage"),
            FakeAD(str(self.root / "Rage/YNG Martyr/BURR"), "Rage"),
        ]
        moves, issues, _ = gf.build_plan(recs, self.root, refile_mismatched=True)
        self.assertEqual(moves, [])
        self.assertTrue(any("DEST EXISTS" in m for m in issues))

    def test_refile_disc_album_moves_as_one_unit(self):
        # Two disc records of one mismatched organized album resolve to a
        # single move of the album folder, discs riding inside.
        _make_tree(
            self.root,
            {
                "Trap/Artist/Album/Disc 1/01.mp3": "a",
                "Trap/Artist/Album/Disc 2/01.mp3": "b",
                "Rage/YNG Martyr/BURR/01.mp3": "y",
            },
        )
        recs = [
            FakeAD(str(self.root / "Trap/Artist/Album/Disc 1"), "Rage"),
            FakeAD(str(self.root / "Trap/Artist/Album/Disc 2"), "Rage"),
            FakeAD(str(self.root / "Rage/YNG Martyr/BURR"), "Rage"),
        ]
        moves, _, _ = gf.build_plan(recs, self.root, refile_mismatched=True)
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0].src, self.root / "Trap/Artist/Album")
        self.assertEqual(moves[0].dst, self.root / "Rage/Artist/Album")

    def test_refile_artist_sidecar_follows_when_all_albums_leave(self):
        _make_tree(
            self.root,
            {
                "Trap/Playboi Carti/Whole Lotta Red/01.mp3": "x",
                "Trap/Playboi Carti/cover.jpg": "art",
                "Rage/YNG Martyr/BURR/01.mp3": "y",
            },
        )
        recs = [
            FakeAD(str(self.root / "Trap/Playboi Carti/Whole Lotta Red"), "Rage"),
            FakeAD(str(self.root / "Rage/YNG Martyr/BURR"), "Rage"),
        ]
        moves, _, _ = gf.build_plan(recs, self.root, refile_mismatched=True)
        by_dst = {m.dst for m in moves}
        self.assertIn(self.root / "Rage/Playboi Carti/Whole Lotta Red", by_dst)
        self.assertIn(self.root / "Rage/Playboi Carti/cover.jpg", by_dst)

    def test_refile_matching_album_is_untouched(self):
        recs = self._wlr()
        moves, _, _ = gf.build_plan(recs, self.root, refile_mismatched=True)
        self.assertFalse(
            any(m.src == self.root / "Rage/YNG Martyr/BURR" for m in moves)
        )

    def test_emptied_genre_folder_is_pruned(self):
        # Moving the last artist out of Trap must prune the emptied Trap
        # folder itself (not just Trap/Playboi Carti), and the dry-run must
        # predict the same prune count.
        def scenario(dry):
            with tempfile.TemporaryDirectory() as td:
                base = Path(td)
                _make_tree(
                    base,
                    {
                        "Trap/Playboi Carti/Whole Lotta Red/01.mp3": "x",
                        "Rage/YNG Martyr/BURR/01.mp3": "y",
                    },
                )
                recs = [
                    FakeAD(str(base / "Trap/Playboi Carti/Whole Lotta Red"), "Rage"),
                    FakeAD(str(base / "Rage/YNG Martyr/BURR"), "Rage"),
                ]
                moves, _, sources = gf.build_plan(recs, base, refile_mismatched=True)
                with gf.Runner(base / "m.tsv", dry_run=dry, quiet=True) as runner:
                    gf.execute(moves, sources, runner, root=base)
                return dict(runner.stats), (base / "Trap").exists()

        apply_stats, trap_exists = scenario(dry=False)
        dry_stats, _ = scenario(dry=True)
        self.assertFalse(trap_exists)
        self.assertEqual(apply_stats["pruned"], 2)  # Trap/Playboi Carti + Trap
        self.assertEqual(dry_stats, apply_stats)


class CollisionReportingTests(unittest.TestCase):
    def test_multi_disc_collision_is_flagged_once(self):
        # The flat stray plans Rock/Nas/Illmatic first (records sort by path);
        # the inbox copy of the same album is multi-disc, so its disc records
        # collapse to one source unit — the destination collision must produce
        # one COLLISION issue, not one per disc.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_tree(
                root,
                {
                    "Rock/Other/AlbumX/01.mp3": "a",
                    "Nas/Illmatic/01.mp3": "b",
                    "Unfiltered/Nas/Illmatic/CD1/01.mp3": "c",
                    "Unfiltered/Nas/Illmatic/CD2/01.mp3": "d",
                },
            )
            recs = [
                FakeAD(str(root / "Rock/Other/AlbumX"), "Rock"),
                FakeAD(str(root / "Nas/Illmatic"), "Rock"),
                FakeAD(str(root / "Unfiltered/Nas/Illmatic/CD1"), "Rock"),
                FakeAD(str(root / "Unfiltered/Nas/Illmatic/CD2"), "Rock"),
            ]
            _moves, issues, _sources = gf.build_plan(recs, root, staging="Unfiltered")
            collisions = [m for m in issues if "COLLISION" in m]
            self.assertEqual(len(collisions), 1)


if __name__ == "__main__":
    unittest.main()
