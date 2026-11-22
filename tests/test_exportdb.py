"""Tests for --exportDb: the SQLite library export. The hierarchy contract is
wings-shaped (dominant genre primary, the "/" split of the contributing
directories' dominant genres as album_genres), the rebuild is atomic (temp
file + os.replace, a failed build leaves the previous export intact), and
every stdout form refuses with exit 2 because binary bytes are not a report.
Tags are mocked where the hierarchy under test depends on them, like
test_snapshot mocks the tag layer."""

import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from lattice import config
from lattice.cli import main
from lattice.modes import library as library_mod
from lattice.modes.library import write_export_db
from lattice.modes.playlists import compile_where
from lattice.tags import TagBundle


def _rows(db_path, sql):
    conn = sqlite3.connect(db_path)
    try:
        return list(conn.execute(sql))
    finally:
        conn.close()


class _TreeCase(unittest.TestCase):
    """A two-album temp tree shared by the export test classes."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.td = self._td.name
        self.one = Path(self.td) / "Artist One" / "Album One"
        self.two = Path(self.td) / "Artist Two" / "Album Two"
        self.one.mkdir(parents=True)
        self.two.mkdir(parents=True)
        (self.one / "01 - First.flac").write_bytes(b"aa")
        (self.one / "02 - Second.flac").write_bytes(b"bbb")
        (self.two / "01 - Other.flac").write_bytes(b"cccc")

    def _export(self, out="lib.db", **kw):
        out_path = os.path.join(self.td, out)
        # The art flag has its own test; the junk-byte files otherwise make
        # _has_embedded_art warn on every run.
        with mock.patch.object(library_mod, "_has_embedded_art", return_value=False):
            rc = write_export_db([self.td], out_path, quiet=True, **kw)
        return rc, out_path


class HierarchyTests(_TreeCase):
    """The GENRE (primary) -> ARTIST -> ALBUM -> SONG shape."""

    def test_layout_fallback_round_trip(self):
        rc, db = self._export()
        self.assertEqual(rc, 0)
        self.assertEqual(
            _rows(db, "SELECT name FROM artists ORDER BY name"),
            [("Artist One",), ("Artist Two",)],
        )
        albums = _rows(
            db,
            "SELECT name, track_count, primary_genre_id, has_embedded_art"
            " FROM albums ORDER BY name",
        )
        self.assertEqual(albums, [("Album One", 2, None, 0), ("Album Two", 1, None, 0)])
        self.assertEqual(_rows(db, "SELECT count(*) FROM tracks"), [(3,)])
        meta = dict(_rows(db, "SELECT key, value FROM meta"))
        self.assertEqual(meta["file_count"], "3")
        self.assertEqual(meta["schema_version"], "1")
        self.assertEqual(meta["lattice_version"], config.VERSION)
        self.assertEqual(meta["roots"], self.td)
        self.assertEqual(_rows(db, "SELECT DISTINCT ext FROM tracks"), [("flac",)])

    def test_tags_drive_the_hierarchy(self):
        def fake_tags(filepath):
            if "Album One" in filepath:
                n = 1 if "First" in filepath else 2
                return TagBundle(
                    title=f"Track {n}",
                    artist="Artist One",
                    album="Album One",
                    genre="Metal/Doom",
                    trackno=n,
                    year=1995,
                    rating=4.0,
                    duration_s=100.5,
                    bitrate_kbps=320,
                )
            return TagBundle(
                title="Other",
                artist="Artist Two",
                album="Album Two",
                genre="Jazz",
                trackno=1,
                year=2000,
            )

        with mock.patch.object(library_mod, "get_all_tags", side_effect=fake_tags):
            rc, db = self._export()
        self.assertEqual(rc, 0)
        self.assertEqual(
            _rows(db, "SELECT name FROM genres ORDER BY name"),
            [("Doom",), ("Jazz",), ("Metal",)],
        )
        # Wing membership: the "/" split lands the album in both wings.
        self.assertEqual(
            _rows(
                db,
                "SELECT g.name FROM album_genres ag"
                " JOIN genres g ON g.genre_id = ag.genre_id"
                " JOIN albums a ON a.album_id = ag.album_id"
                " WHERE a.name = 'Album One' ORDER BY g.name",
            ),
            [("Doom",), ("Metal",)],
        )
        primaries = dict(
            _rows(
                db,
                "SELECT a.name, g.name FROM albums a"
                " LEFT JOIN genres g ON g.genre_id = a.primary_genre_id",
            )
        )
        self.assertEqual(primaries, {"Album One": "Metal", "Album Two": "Jazz"})
        track = _rows(
            db,
            "SELECT title, artist, trackno, rating, year, duration_s, bitrate_kbps"
            " FROM tracks WHERE title = 'Track 1'",
        )[0]
        self.assertEqual(track, ("Track 1", "Artist One", 1, 4.0, 1995, 100.5, 320))
        # The denormalized view joins the whole chain in one line.
        self.assertEqual(
            _rows(
                db,
                "SELECT DISTINCT album, album_artist, primary_genre FROM v_track"
                " ORDER BY album",
            ),
            [("Album One", "Artist One", "Metal"), ("Album Two", "Artist Two", "Jazz")],
        )

    def test_same_album_in_two_dirs_merges(self):
        def fake_tags(filepath):
            # Every directory carries the same dominant artist/album, so all
            # three merge into one album row; the Copy directory's dominant
            # genre adds a second wing.
            genre = "Metal/Doom" if "Copy" in filepath else "Metal"
            return TagBundle(artist="Artist One", album="Album One", genre=genre)

        copy = Path(self.td) / "Artist One" / "Album One Copy"
        copy.mkdir()
        (copy / "01 - Extra.flac").write_bytes(b"dd")
        with mock.patch.object(library_mod, "get_all_tags", side_effect=fake_tags):
            rc, db = self._export()
        self.assertEqual(rc, 0)
        self.assertEqual(_rows(db, "SELECT count(*) FROM albums"), [(1,)])
        self.assertIn("; ", _rows(db, "SELECT paths FROM albums")[0][0])
        self.assertEqual(_rows(db, "SELECT count(*) FROM tracks"), [(4,)])
        # Primary: Metal counted once per directory (2), Doom once (1).
        self.assertEqual(
            _rows(
                db,
                "SELECT g.name FROM albums a"
                " JOIN genres g ON g.genre_id = a.primary_genre_id",
            ),
            [("Metal",)],
        )
        self.assertEqual(_rows(db, "SELECT count(*) FROM album_genres"), [(2,)])

    def test_where_scopes_albums(self):
        def fake_tags(filepath):
            return TagBundle(genre="Metal" if "Album One" in filepath else "Jazz")

        with mock.patch.object(library_mod, "get_all_tags", side_effect=fake_tags):
            rc, db = self._export(where=compile_where("genre == 'Jazz'"))
        self.assertEqual(rc, 0)
        self.assertEqual(_rows(db, "SELECT name FROM albums"), [("Album Two",)])


class RebuildTests(_TreeCase):
    """Wholesale rebuild semantics: idempotent, atomic, no litter."""

    def test_rebuild_is_idempotent_and_swaps(self):
        rc1, db = self._export()
        rc2, db2 = self._export()
        self.assertEqual((rc1, rc2), (0, 0))
        self.assertEqual(os.path.abspath(db), os.path.abspath(db2))
        self.assertEqual(_rows(db, "SELECT count(*) FROM tracks"), [(3,)])
        self.assertEqual([f for f in os.listdir(self.td) if ".tmp-" in f], [])

    def test_failed_build_preserves_previous_export(self):
        _rc, db = self._export()
        before = _rows(db, "SELECT count(*) FROM tracks")
        with mock.patch.object(
            library_mod, "_populate_export_db", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                write_export_db([self.td], db, quiet=True)
        self.assertEqual(_rows(db, "SELECT count(*) FROM tracks"), before)
        self.assertEqual([f for f in os.listdir(self.td) if ".tmp-" in f], [])

    def test_rg_values_and_art_flag_land(self):
        # Direct call (not _export): the helper's art patch would shadow this
        # test's True stub.
        db = os.path.join(self.td, "lib.db")
        with (
            mock.patch.object(
                library_mod, "read_replaygain_values", return_value=(7.5, 8.5)
            ),
            mock.patch.object(library_mod, "_has_embedded_art", return_value=True),
        ):
            rc = write_export_db([self.td], db, quiet=True)
        self.assertEqual(rc, 0)
        self.assertEqual(
            _rows(db, "SELECT DISTINCT rg_track_gain, rg_album_gain FROM tracks"),
            [(7.5, 8.5)],
        )
        self.assertEqual(
            _rows(db, "SELECT DISTINCT has_embedded_art FROM albums"), [(1,)]
        )

    def test_stdout_forms_refuse(self):
        for target in (None, "-"):
            err = StringIO()
            with redirect_stderr(err):
                rc = write_export_db([self.td], target, quiet=True)
            self.assertEqual(rc, 2)
            self.assertIn("required", err.getvalue())


class ExportDbCliTests(_TreeCase):
    """The dispatch wiring and the exit-2 refusal, end to end."""

    def test_cli_refuses_every_stdout_form(self):
        for extra in ([], ["--output", "-"]):
            err, out = StringIO(), StringIO()
            with redirect_stderr(err), redirect_stdout(out):
                rc = main(["--exportDb", "--root", self.td, *extra])
            self.assertEqual(rc, 2)
            line = err.getvalue().strip()
            self.assertTrue(line.startswith("error:"))
            self.assertNotIn("\n", line)

    def test_cli_happy_path(self):
        out_path = os.path.join(self.td, "out.db")
        err, out = StringIO(), StringIO()
        with (
            mock.patch.object(library_mod, "_has_embedded_art", return_value=False),
            redirect_stderr(err),
            redirect_stdout(out),
        ):
            rc = main(["--exportDb", "--root", self.td, "--output", out_path])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(out_path))
        self.assertEqual(_rows(out_path, "SELECT count(*) FROM tracks"), [(3,)])
        self.assertEqual([f for f in os.listdir(self.td) if ".tmp-" in f], [])


if __name__ == "__main__":
    unittest.main()
