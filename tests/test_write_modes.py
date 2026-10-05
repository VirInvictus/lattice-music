"""Tests for the write-mode wiring: the `lattice --clean` / `--apestrip` /
`--lyrics` / `--replayGain` CLI dispatch and the TUI MAINTENANCE menu entries
the folds added (v5.0.0 through the 6.0.0 program). The modes' brains are
covered by test_cleaner.py, test_apestrip.py, test_lyrics.py, and
test_replaygain.py; this file pins the contract that matters at the package
surface: dry-run is the default, --apply opts in, the root-list guard fires,
and the TUI entries exist with the confirms in front of any apply."""

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mutagen.apev2 import TEXT, APENoHeaderError, APEv2, APEValue

from lattice import cli, tui
from lattice.modes import replaygain as rg_mode

FIXTURES = Path(__file__).parent / "fixtures" / "library"
MP3_SRC = FIXTURES / "Cursive" / "Domestica" / "01 - The Casualty.mp3"
FLAC_SRC = FIXTURES / "Aphex Twin" / "Selected Ambient Works" / "01 - Xtal.flac"


def _fragmented_tree(root: Path) -> None:
    """A canonical album folder plus a case-variant sibling holding one track."""
    canon = root / "Album"
    variant = root / "album"
    canon.mkdir(parents=True)
    variant.mkdir()
    (canon / "01.flac").write_bytes(b"x")
    (variant / "02.flac").write_bytes(b"y")


def _mp3_with_ape(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(MP3_SRC, path)
    ape = APEv2()
    ape["Genre"] = APEValue("Trash Metal", TEXT)
    ape.save(str(path))


class CleanDispatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "Music"
        _fragmented_tree(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _main(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return cli.main(argv)

    def test_dry_run_is_the_default(self):
        rc = self._main(["--clean", str(self.root)])
        self.assertEqual(rc, 0)
        # The case-variant folder survived untouched (log lines say [DRY]).
        self.assertTrue((self.root / "album" / "02.flac").exists())
        self.assertFalse((self.root / "Album" / "02.flac").exists())

    def test_apply_merges_and_logs(self):
        rc = self._main(["--clean", str(self.root), "--apply"])
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / "Album" / "02.flac").exists())
        self.assertFalse((self.root / "album").exists())
        log = (self.root / "cleanup.log").read_text(encoding="utf-8")
        self.assertIn("CLEANUP RUN START [APPLY]", log)

    def test_dry_run_flag_beats_apply(self):
        rc = self._main(["--clean", str(self.root), "--apply", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / "album" / "02.flac").exists())
        log = (self.root / "cleanup.log").read_text(encoding="utf-8")
        self.assertIn("CLEANUP RUN START [DRY RUN]", log)

    def test_all_flag_enables_every_pass(self):
        # --all turns on the three opt-in passes; dry-run default keeps it
        # safe while proving the flags reach the mode (Pass 4 header logged).
        rc = self._main(["--clean", str(self.root), "--all"])
        self.assertEqual(rc, 0)
        log = (self.root / "cleanup.log").read_text(encoding="utf-8")
        self.assertIn("--- PASS 3: normalize names", log)
        self.assertIn("--- PASS 4: normalize tags (library-wide) ---", log)

    def test_multiple_roots_are_rejected(self):
        other = Path(self._tmp.name) / "Other"
        _fragmented_tree(other)
        rc = self._main(
            ["--clean", "--root", str(self.root), "--root", str(other), "--apply"]
        )
        self.assertEqual(rc, 2)
        # Nothing was written anywhere.
        self.assertTrue((self.root / "album" / "02.flac").exists())

    def test_mode_flags_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self._main(["--clean", "--apestrip", str(self.root)])


class ApestripDispatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.track = self.root / "t.mp3"
        _mp3_with_ape(self.track)

    def tearDown(self):
        self._tmp.cleanup()

    def _main(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return cli.main(argv)

    def _ape_present(self) -> bool:
        try:
            APEv2(str(self.track))
            return True
        except APENoHeaderError:
            return False

    def test_dry_run_is_the_default(self):
        rc = self._main(["--apestrip", str(self.root)])
        self.assertEqual(rc, 0)
        self.assertTrue(self._ape_present())
        self.assertFalse((self.root / "apestrip.log").exists())

    def test_apply_strips_and_logs(self):
        # stdin is not a TTY under the test runner, so the confirmation is
        # auto-skipped: the script's non-interactive convention, preserved.
        rc = self._main(["--apestrip", str(self.root), "--apply"])
        self.assertEqual(rc, 0)
        self.assertFalse(self._ape_present())
        log = (self.root / "apestrip.log").read_text(encoding="utf-8")
        self.assertIn("stripped APEv2 tag", log)

    def test_keep_metadata_flag_reaches_the_mode(self):
        rc = self._main(["--apestrip", str(self.root), "--apply", "--keep-metadata"])
        self.assertEqual(rc, 0)
        self.assertFalse(self._ape_present())
        log = (self.root / "apestrip.log").read_text(encoding="utf-8")
        # The fixture's ID3 already has the APE's fields, so the strip is
        # logged plainly; the flag's acceptance is the dry-run worklist's job
        # (covered in test_apestrip.py). Here we pin the exit path only.
        self.assertIn("-> stripped 1 file(s)", log)


class ReplaygainDispatchTests(unittest.TestCase):
    """The `lattice --replayGain` write-mode wiring (the 6.0.0 fold of
    scripts/replaygain.py): dry-run is the default, --apply opts in, rsgain is
    a required external binary with the exit-2 refusal, and the root-list guard
    fires. The mode's brain is covered by test_replaygain.py."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "Album"
        self.root.mkdir(parents=True)
        shutil.copy(MP3_SRC, self.root / "01.mp3")

    def tearDown(self):
        self._tmp.cleanup()

    def _main(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return cli.main(argv)

    def _has_track_gain(self) -> bool:
        from mutagen.id3 import ID3

        try:
            tags = ID3(str(self.root / "01.mp3"))
        except Exception:
            return False
        return any(k.upper() == "TXXX:REPLAYGAIN_TRACK_GAIN" for k in tags)

    def test_dry_run_is_the_default(self):
        rc = self._main(["--replayGain", str(self.root)])
        self.assertEqual(rc, 0)
        self.assertFalse(self._has_track_gain())
        log = (self.root / "replaygain.log").read_text(encoding="utf-8")
        self.assertIn("RG RUN START [DRY RUN]", log)

    def test_apply_invokes_rsgain(self):
        # stdin is not a TTY under the test runner, so the confirmation is
        # auto-skipped (the script's non-interactive convention, preserved).
        with mock.patch.object(rg_mode.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            rc = self._main(["--replayGain", str(self.root), "--apply"])
        self.assertEqual(rc, 0)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["rsgain", "easy"])
        log = (self.root / "replaygain.log").read_text(encoding="utf-8")
        self.assertIn("RG RUN START [APPLY]", log)

    def test_missing_rsgain_is_exit_2(self):
        with mock.patch.object(rg_mode.shutil, "which", return_value=None):
            rc = self._main(["--replayGain", str(self.root), "--apply"])
        self.assertEqual(rc, 2)

    def test_missing_rsgain_is_fine_for_a_dry_run(self):
        with mock.patch.object(rg_mode.shutil, "which", return_value=None):
            rc = self._main(["--replayGain", str(self.root)])
        self.assertEqual(rc, 0)
        self.assertFalse(self._has_track_gain())

    def test_multiple_roots_are_rejected(self):
        other = Path(self._tmp.name) / "Other"
        other.mkdir()
        shutil.copy(MP3_SRC, other / "01.mp3")
        rc = self._main(
            ["--replayGain", "--root", str(self.root), "--root", str(other)]
        )
        self.assertEqual(rc, 2)

    def test_mode_flags_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self._main(["--replayGain", "--clean", str(self.root)])


class RetagDispatchTests(unittest.TestCase):
    """The `lattice --retag` write-mode wiring (the 6.0.0 fold of
    scripts/retag.py): the package's first dir-scoped mode (DIR GENRE...
    positionals, not a root walk), dry-run default, --apply opts in, and the
    genre/--strip-junk argument discipline holds. The writer's brain is
    covered by test_retag.py; the genre_tidy call path by test_genre_tidy.py."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.album = Path(self._tmp.name) / "Album"
        self.album.mkdir()
        shutil.copy(FLAC_SRC, self.album / "01.flac")

    def tearDown(self):
        self._tmp.cleanup()

    def _main(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return cli.main(argv)

    def _genre(self) -> str | None:
        from mutagen.flac import FLAC

        return FLAC(str(self.album / "01.flac")).get("genre")

    def test_dry_run_is_the_default(self):
        rc = self._main(["--retag", str(self.album), "Ambient"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._genre(), ["Electronic"])
        log = (self.album / "retag.log").read_text(encoding="utf-8")
        self.assertIn("[DRY]", log)
        self.assertIn("would retag", log)

    def test_apply_writes_genre_and_logs(self):
        rc = self._main(["--retag", str(self.album), "Ambient", "--apply"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._genre(), ["Ambient"])
        log = (self.album / "retag.log").read_text(encoding="utf-8")
        self.assertIn("retagged", log)
        self.assertNotIn("[DRY]", log)

    def test_apply_is_idempotent(self):
        self._main(["--retag", str(self.album), "Ambient", "--apply"])
        rc = self._main(["--retag", str(self.album), "Ambient", "--apply"])
        self.assertEqual(rc, 0)
        log = (self.album / "retag.log").read_text(encoding="utf-8")
        self.assertIn("unchanged", log)

    def test_multiple_genres_are_one_write(self):
        rc = self._main(["--retag", str(self.album), "Ambient", "IDM", "--apply"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._genre(), ["Ambient", "IDM"])

    def test_genres_are_required(self):
        rc = self._main(["--retag", str(self.album)])
        self.assertEqual(rc, 2)

    def test_strip_junk_takes_no_genres(self):
        rc = self._main(["--retag", str(self.album), "--strip-junk", "Ambient"])
        self.assertEqual(rc, 2)

    def test_root_flag_is_refused(self):
        rc = self._main(["--retag", "--root", str(self.album), "Ambient"])
        self.assertEqual(rc, 2)

    def test_extra_positionals_without_the_mode_are_refused(self):
        rc = self._main(["--stats", str(self.album), "Ambient"])
        self.assertEqual(rc, 2)

    def test_mode_flags_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self._main(["--retag", "--clean", str(self.album), "Ambient"])


class GenreTidyDispatchTests(unittest.TestCase):
    """The `lattice --genreTidy-build` / `--genreTidy-apply` wiring (the 6.0.0
    fold of scripts/genre_tidy.py): two flat flags for the two verbs, the map
    defaults beside the root (--map overrides), apply dry-runs by default and
    joins the single-root refusal. The tool's brain is covered by
    test_genre_tidy.py."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "Music"
        album = self.root / "Aphex Twin" / "Selected Ambient Works"
        album.mkdir(parents=True)
        shutil.copy(FLAC_SRC, album / "01 - Xtal.flac")

    def tearDown(self):
        self._tmp.cleanup()

    def _main(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(argv)
        return rc, buf.getvalue()

    def test_build_writes_the_map(self):
        rc, _out = self._main(["--genreTidy-build", str(self.root)])
        self.assertEqual(rc, 0)
        map_text = (self.root / "genre_map.tsv").read_text(encoding="utf-8")
        self.assertIn("Aphex Twin\tElectronic", map_text)

    def test_apply_dry_run_is_the_default(self):
        self._main(["--genreTidy-build", str(self.root)])
        (self.root / "genre_map.tsv").write_text("Aphex Twin\tIDM\n", encoding="utf-8")
        rc, out = self._main(["--genreTidy-apply", str(self.root)])
        self.assertEqual(rc, 0)
        from mutagen.flac import FLAC

        self.assertEqual(
            FLAC(
                str(
                    self.root
                    / "Aphex Twin"
                    / "Selected Ambient Works"
                    / "01 - Xtal.flac"
                )
            ).get("genre"),
            ["Electronic"],
        )
        log = (self.root / "genre_tidy.log").read_text(encoding="utf-8")
        self.assertIn("GENRE TIDY [DRY RUN]", log)
        self.assertIn("would retag 1 album(s)", out)

    def test_apply_writes_and_is_idempotent(self):
        self._main(["--genreTidy-build", str(self.root)])
        (self.root / "genre_map.tsv").write_text("Aphex Twin\tIDM\n", encoding="utf-8")
        rc, _out = self._main(["--genreTidy-apply", str(self.root), "--apply"])
        self.assertEqual(rc, 0)
        from mutagen.flac import FLAC

        path = self.root / "Aphex Twin" / "Selected Ambient Works" / "01 - Xtal.flac"
        self.assertEqual(FLAC(str(path)).get("genre"), ["IDM"])
        rc, out = self._main(["--genreTidy-apply", str(self.root), "--apply"])
        self.assertEqual(rc, 0)
        self.assertIn("retagged 0 album(s); 1 already compliant", out)
        log = (self.root / "genre_tidy.log").read_text(encoding="utf-8")
        self.assertIn("GENRE TIDY [APPLY]", log)

    def test_apply_without_a_map_is_exit_1(self):
        rc, _out = self._main(["--genreTidy-apply", str(self.root)])
        self.assertEqual(rc, 1)

    def test_map_flag_reaches_the_mode(self):
        map_path = Path(self._tmp.name) / "custom.tsv"
        rc, _out = self._main(
            ["--genreTidy-build", str(self.root), "--map", str(map_path)]
        )
        self.assertEqual(rc, 0)
        self.assertIn("Aphex Twin", map_path.read_text(encoding="utf-8"))

    def test_multiple_roots_are_rejected(self):
        other = Path(self._tmp.name) / "Other"
        other.mkdir()
        rc, _out = self._main(
            ["--genreTidy-build", "--root", str(self.root), "--root", str(other)]
        )
        self.assertEqual(rc, 2)

    def test_mode_flags_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self._main(["--genreTidy-apply", "--clean", str(self.root)])


class GenreMapDispatchTests(unittest.TestCase):
    """The `lattice --genreMap` write-mode wiring (the 6.0.0 fold of
    scripts/genre_foldermap.py, the package's first file-moving mode): dry-run
    is the default (no inversion; the script always was), --apply moves and
    writes the manifest, --revert replays it, and the dry-run-vs-apply parity
    harness covers the folder-moving mode. The brain is covered by
    test_genre_foldermap.py."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "Music"
        album = self.root / "Aphex Twin" / "Selected Ambient Works"
        album.mkdir(parents=True)
        shutil.copy(FLAC_SRC, album / "01 - Xtal.flac")

    def tearDown(self):
        self._tmp.cleanup()

    def _main(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(argv)
        return rc, buf.getvalue()

    def _tree_hash(self):
        # The revert manifest (genre_foldermap.revert.tsv) is written beside
        # the forward manifest inside the root; both are records, not tree.
        import hashlib

        h = hashlib.sha256()
        for p in sorted(self.root.rglob("*")):
            if p.suffix == ".tsv":
                continue
            h.update(str(p.relative_to(self.root)).encode())
            if p.is_file():
                h.update(p.read_bytes())
        return h.hexdigest()

    def test_dry_run_is_the_default(self):
        before = self._tree_hash()
        rc, out = self._main(["--genreMap", str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn("moved_dir=1", out)  # a flat stray plans one album move
        self.assertIn("Dry run", out)
        self.assertEqual(self._tree_hash(), before)

    def test_dry_run_and_apply_parity(self):
        # The parity harness over the folder-moving mode: two identical trees,
        # one planned dry, one applied; the stats must agree (same move set,
        # same prunes), and the dry tree must be untouched while the applied
        # tree lands in Genre/Artist/Album.
        from mutagen.flac import FLAC

        def staged_tree(base: Path) -> Path:
            base.mkdir(parents=True)
            album = base / "Aphex Twin" / "Selected Ambient Works"
            album.mkdir(parents=True)
            shutil.copy(FLAC_SRC, album / "01 - Xtal.flac")
            return base

        genre = FLAC(str(FLAC_SRC)).get("genre")[0]
        self.assertTrue(genre)

        dry_root = staged_tree(Path(self._tmp.name) / "dry")
        apply_root = staged_tree(Path(self._tmp.name) / "apply")

        rc1, out1 = self._main(["--genreMap", str(dry_root)])
        self.assertEqual(rc1, 0)
        rc2, out2 = self._main(["--genreMap", str(apply_root), "--apply"])
        self.assertEqual(rc2, 0)

        dry_stats = out1.split("Would reorganize")[1].split("\n")[0]
        apply_stats = out2.split("Done: reorganize")[1].split("\n")[0]
        self.assertEqual(dry_stats, apply_stats)

        # The dry run left the tree untouched; the apply run reorganized.
        self.assertTrue(
            (
                dry_root / "Aphex Twin" / "Selected Ambient Works" / "01 - Xtal.flac"
            ).exists()
        )
        self.assertTrue(
            (
                apply_root
                / genre
                / "Aphex Twin"
                / "Selected Ambient Works"
                / "01 - Xtal.flac"
            ).exists()
        )
        self.assertFalse((dry_root / genre).exists())
        manifest = apply_root / "genre_foldermap.manifest.tsv"
        self.assertTrue(manifest.exists())
        manifest_text = manifest.read_text(encoding="utf-8")
        self.assertIn("src<TAB>dst<TAB>time", manifest_text)
        # One real move row: src<TAB>dst<TAB>timestamp.
        rows = [
            ln
            for ln in manifest_text.splitlines()
            if "\t" in ln and not ln.startswith("#")
        ]
        self.assertEqual(len(rows), 1)
        self.assertIn("Selected Ambient Works", rows[0])

    def test_apply_moves_and_revert_restores(self):
        before = self._tree_hash()
        rc, _out = self._main(["--genreMap", str(self.root), "--apply"])
        self.assertEqual(rc, 0)
        moved = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        self.assertTrue(any(p.startswith("Electronic/") for p in moved), moved)
        manifest = self.root / "genre_foldermap.manifest.tsv"
        rc, _out = self._main(["--genreMap", "--revert", str(manifest), "--apply"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._tree_hash(), before)

    def test_allow_new_genre_default_stays_off(self):
        # The vocabulary gate: with one organized album, a second stray whose
        # genre is NOT already in use is flagged, not given a new top-level
        # folder, unless --allow-new-genre is passed (the launcher's
        # most-typed flag stays opt-in in the package too).
        from mutagen.flac import FLAC

        stray = self.root / "Other Artist" / "Other Album"
        stray.mkdir(parents=True)
        shutil.copy(FLAC_SRC, stray / "01.flac")
        f = FLAC(str(stray / "01.flac"))
        f["genre"] = ["Polka"]
        f.save()
        # Organize only the Electronic album so the vocabulary exists while
        # the Polka stray stays flat (--only-genre is the staged rollout).
        rc, _out = self._main(
            ["--genreMap", str(self.root), "--only-genre", "Electronic", "--apply"]
        )
        self.assertEqual(rc, 0)
        self.assertTrue(stray.exists())
        self.assertTrue(
            (
                self.root / "Electronic" / "Aphex Twin" / "Selected Ambient Works"
            ).exists()
        )
        # The vocabulary gate: Polka is not an existing library genre, so the
        # dry-run flags it instead of planning a new top-level folder.
        rc, out = self._main(["--genreMap", str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn("UNKNOWN GENRE", out)
        self.assertFalse((self.root / "Polka").exists())
        # --allow-new-genre lifts the gate (the launcher's most-typed flag
        # stays opt-in in the package).
        rc, _out = self._main(
            ["--genreMap", str(self.root), "--allow-new-genre", "--apply"]
        )
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / "Polka" / "Other Artist" / "Other Album").exists())

    def test_mode_flags_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self._main(["--genreMap", "--clean", str(self.root)])


class TuiWriteModeTests(unittest.TestCase):
    def test_maintenance_section_holds_the_write_modes(self):
        sections = {name: items for name, items in tui._MAIN_SECTIONS if name}
        self.assertEqual(
            sections["MAINTENANCE"],
            [
                "Consolidate fragmented albums (clean)",
                "Strip APEv2 tags (apestrip)",
                "Fetch synced lyrics (lyrics)",
                "Write ReplayGain tags (replaygain)",
                "Rewrite genre tags on one album (retag)",
                "Build genre authority map (genreTidy build)",
                "Apply genre authority map (genreTidy apply)",
                "Reorganize into Genre/Artist/Album (genreMap)",
            ],
        )

    def test_aliases_and_selection_constants_track_the_new_section(self):
        self.assertEqual(tui._MAIN_ALIASES["clean"], (4, 0))
        self.assertEqual(tui._MAIN_ALIASES["apestrip"], (4, 1))
        self.assertEqual(tui._MAIN_ALIASES["lyrics"], (4, 2))
        self.assertEqual(tui._MAIN_ALIASES["lrc"], (4, 2))
        self.assertEqual(tui._MAIN_ALIASES["rgwrite"], (4, 3))
        self.assertEqual(tui._MAIN_ALIASES["retag"], (4, 4))
        self.assertEqual(tui._MAIN_ALIASES["tidybuild"], (4, 5))
        self.assertEqual(tui._MAIN_ALIASES["tidy"], (4, 6))
        self.assertEqual(tui._MAIN_ALIASES["tidyapply"], (4, 6))
        self.assertEqual(tui._MAIN_ALIASES["foldermap"], (4, 7))
        self.assertEqual(tui._MAIN_ALIASES["genremap"], (4, 7))
        self.assertEqual(tui._SEL_CHANGE_ROOT, (5, 0))
        self.assertEqual(tui._SEL_QUIT, (6, 0))

    def test_every_alias_points_at_a_real_menu_item(self):
        for key, sel in tui._MAIN_ALIASES.items():
            if sel is None:
                continue
            si, ii = sel
            self.assertLess(si, len(tui._MAIN_SECTIONS), f"{key}: bad section")
            self.assertLess(ii, len(tui._MAIN_SECTIONS[si][1]), f"{key}: bad item")


if __name__ == "__main__":
    unittest.main()
