import io
import os
import re
import sys
from collections import defaultdict
from typing import NamedTuple, TextIO

from lattice.tags import TagBundle, get_all_tags, read_replaygain
from lattice.utils import (
    _make_pbar,
    as_roots,
    clean_song_name,
    count_audio_files,
    format_rating,
    is_audio,
    iter_audio_dirs,
    parse_layout,
    relpath_under,
)

Song = tuple[str, str, TagBundle]  # (filename, filepath, tags)
ArtistAlbums = dict[str, dict[str, list[Song]]]


class _AlbumDir(NamedTuple):
    """One audio-containing directory, reduced to its dominant artist/album/genre.

    `artist`, `album`, and `genre` are the most-common tag value across the
    directory's files (`genre` is "" when no file carries one). The same
    aggregation feeds every library/wing mode.
    """

    path: str
    artist: str
    album: str
    genre: str
    songs: list[Song]


def _most_common(counts: dict[str, int], default: str) -> str:
    """Return the most frequent key, breaking ties by first insertion; `default`
    when empty. Matches the directory-dominant selection used across modes."""
    return max(counts, key=lambda k: counts[k]) if counts else default


def _scan_album_dirs(roots, layout: str, pbar) -> list[_AlbumDir]:
    """Walk one or more roots, collapsing each audio directory to an `_AlbumDir`.
    The layout is parsed against whichever root the directory lives under, so
    multi-root scans key artist/album off the correct relative path."""
    # Read serially, on purpose. Routing these through read_tags_concurrent —
    # whether one pool per directory or one for the whole scan — measured
    # ~76% MORE user CPU on a 9.6k-file library (14.1s -> 24.7s): mutagen's
    # parsing holds the GIL, so nothing but the file opens can overlap, and the
    # per-task handoff costs more than it saves. Don't "optimize" this again
    # without measuring CPU time; wall clock on a warm page cache is far too
    # noisy to tell you anything.
    results: list[_AlbumDir] = []
    for root, dirpath, _dirs, files in iter_audio_dirs(roots):
        # Sorted, so a tie in the counts below always breaks the same way
        # (_most_common keeps the first-inserted key) — os.walk hands back
        # readdir order, which made a two-artist directory's dominant name a
        # coin flip between runs.
        audio_in_dir = sorted(f for f in files if is_audio(f))
        if not audio_in_dir:
            continue

        artists_count: dict[str, int] = defaultdict(int)
        albums_count: dict[str, int] = defaultdict(int)
        genres_count: dict[str, int] = defaultdict(int)
        songs: list[Song] = []

        for f in audio_in_dir:
            filepath = os.path.join(dirpath, f)
            parsed = parse_layout(os.path.relpath(filepath, root), layout)
            t = get_all_tags(filepath)
            artist = t.artist or parsed.get("artist", "Unknown Artist")
            album = t.album or parsed.get("album", "Unknown Album")
            genre = t.genre or parsed.get("genre", "")

            artists_count[artist] += 1
            albums_count[album] += 1
            if genre:
                genres_count[genre] += 1
            songs.append((f, filepath, t))
            pbar.update(1)

        results.append(
            _AlbumDir(
                dirpath,
                _most_common(artists_count, "Unknown Artist"),
                _most_common(albums_count, "Unknown Album"),
                _most_common(genres_count, ""),
                songs,
            )
        )

    return results


def _song_display_name(song_filename: str, t: TagBundle, album_artist: str) -> str:
    """Build a track's display label from tags, falling back to a cleaned filename.

    The artist is shown only when it differs from the album artist (so
    compilations stay legible without repeating the headline artist).
    """
    if not (t.title or t.artist):
        return clean_song_name(song_filename)

    guest = t.artist if (t.artist and t.artist != album_artist) else None
    parts: list[str] = []
    if t.trackno:
        parts.append(f"{int(t.trackno):02d}.")
    if guest is not None:
        parts.append(guest)
    if t.title:
        if guest is not None:
            parts.append("—")
        parts.append(t.title)
    return " ".join(parts).strip()


def _write_tree(
    f: TextIO,
    artist_albums: ArtistAlbums,
    *,
    show_genre: bool,
    album_paths: dict[tuple[str, str], str] | None = None,
) -> None:
    """Write an ARTIST → ALBUM → SONG tree. When `album_paths` is given, the
    album line is annotated with its absolute directory path."""
    for artist in sorted(artist_albums):
        f.write(f"ARTIST: {artist}\n")
        albums = sorted(artist_albums[artist])

        for i, album in enumerate(albums):
            songs = sorted(artist_albums[artist][album], key=lambda x: x[0])
            connector = "└──" if i == len(albums) - 1 else "├──"

            genre_str = ""
            if show_genre and songs and songs[0][2].genre:
                genre_str = f" ({songs[0][2].genre})"

            path_str = ""
            if album_paths is not None:
                album_path = album_paths.get((artist, album), "")
                path_str = f" [{album_path}]" if album_path else ""

            f.write(f"  {connector} ALBUM: {album}{genre_str}{path_str}\n")

            for j, (song, _song_path, t) in enumerate(songs):
                display_name = _song_display_name(song, t, artist)
                ext = os.path.splitext(song)[1].lower().strip(".")
                song_connector = "└──" if j == len(songs) - 1 else "├──"
                f.write(
                    f"      {song_connector} SONG: {display_name} ({ext}){format_rating(t.rating)}\n"
                )
            f.write("\n")


# =====================================
# Mode: Library tree
# =====================================


def write_music_library_tree(
    root_dir: str | list[str],
    output_file: str | None,
    *,
    layout: str = "{artist}/{album}",
    quiet: bool = False,
    show_genre: bool = False,
) -> None:
    """Write an ARTIST → ALBUM → SONG tree to `output_file`; None (the TUI's
    "leave blank for screen" answer, like run_stats) renders to stdout."""
    roots = as_roots(root_dir)
    total_files = count_audio_files(roots)
    if not quiet:
        print(f"Found {total_files} audio files to process under: {', '.join(roots)}\n")

    pbar = _make_pbar(total_files, "Scanning library", quiet)
    album_dirs = _scan_album_dirs(roots, layout, pbar)
    pbar.close()

    # Group same-artist albums together for display.
    tree: ArtistAlbums = defaultdict(lambda: defaultdict(list))
    for ad in album_dirs:
        tree[ad.artist][ad.album].extend(ad.songs)

    buf = io.StringIO()
    _write_tree(buf, tree, show_genre=show_genre)

    if not output_file:
        if not quiet:
            print()
            print(buf.getvalue())
        return

    out_path = os.path.abspath(output_file)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(buf.getvalue())
    except KeyboardInterrupt:
        if not quiet:
            print("\nInterrupted by user. Library scan cancelled.")
        return


# =====================================
# Mode: AI-readable library export
# =====================================


def write_ai_library(
    root_dir: str | list[str],
    output_file: str,
    *,
    layout: str = "{artist}/{album}",
    quiet: bool = False,
) -> None:
    """Write a flat, token-efficient library summary for LLM consumption."""
    roots = as_roots(root_dir)
    total = count_audio_files(roots)

    if not quiet:
        print(f"Scanning {total} files under: {', '.join(roots)}")

    pbar = _make_pbar(total, "Building AI library", quiet)
    album_dirs = _scan_album_dirs(roots, layout, pbar)
    pbar.close()

    albums: list[tuple[str, str, str, str, int]] = []
    for ad in album_dirs:
        ratings = [t.rating for _f, _p, t in ad.songs if t.rating is not None]
        rating_str = f"{sum(ratings) / len(ratings):.1f}" if ratings else ""
        albums.append((ad.artist, ad.album, ad.genre, rating_str, len(ad.songs)))

    albums.sort(key=lambda x: (x[0].lower(), x[1].lower()))

    out_path = os.path.abspath(output_file)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("Artist | Album | Genre | Rating | Tracks\n")
        f.write("-" * 50 + "\n")
        f.writelines(
            f"{artist} | {album} | {genre} | {rating} | {tracks}\n"
            for artist, album, genre, rating, tracks in albums
        )

    if not quiet:
        rated = sum(1 for _, _, _, r, _ in albums if r)
        print(f"\nWrote {len(albums)} albums ({rated} rated) to: {out_path}")


# =====================================
# Mode: All wings (genre-based library files)
# =====================================


def _safe_wing_name(genre: str) -> str:
    """Turn a genre label into a filesystem-safe filename stem."""
    return re.sub(r"[^\w\s-]", "_", genre).strip().replace(" ", "_")


def write_all_wings(
    root_dir: str | list[str],
    outdir: str,
    *,
    layout: str = "{artist}/{album}",
    quiet: bool = False,
    show_genre: bool = False,
    show_paths: bool = False,
) -> int:
    """Generate a separate library tree file for each genre."""
    roots = as_roots(root_dir)
    total = count_audio_files(roots)
    if not quiet:
        print(f"Scanning {total} files for genre tags...")

    pbar = _make_pbar(total, "Scanning genres", quiet)
    album_dirs = _scan_album_dirs(roots, layout, pbar)
    pbar.close()

    if not album_dirs:
        print("No albums found under root.", file=sys.stderr)
        return 1

    # Re-bucket by genre -> artist -> album. A "/"-joined genre lands in each.
    final_wings: dict[str, ArtistAlbums] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    album_paths: dict[tuple[str, str], str] = {}

    for ad in album_dirs:
        genre_str = ad.genre or "Uncategorized"
        for genre in (g.strip() for g in genre_str.split("/") if g.strip()):
            final_wings[genre][ad.artist][ad.album].extend(ad.songs)
        # The same (artist, album) can live in two directories (a genuine
        # duplicate); their songs merge above, so the annotation lists every
        # contributing path instead of last-wins misattributing the merge.
        key = (ad.artist, ad.album)
        if key in album_paths and ad.path not in album_paths[key].split("; "):
            album_paths[key] += "; " + ad.path
        else:
            album_paths.setdefault(key, ad.path)

    os.makedirs(outdir, exist_ok=True)

    if not quiet:
        print(f"\nFound {len(final_wings)} genres. Writing wings...\n")

    for genre_name in sorted(final_wings):
        artist_albums = final_wings[genre_name]
        output = os.path.join(outdir, f"{_safe_wing_name(genre_name)}_Library.txt")

        if not quiet:
            album_count = sum(len(albums) for albums in artist_albums.values())
            print(f"→ {genre_name} ({album_count} albums)")

        with open(output, "w", encoding="utf-8") as f:
            _write_tree(
                f,
                artist_albums,
                show_genre=show_genre,
                album_paths=album_paths if show_paths else None,
            )

    if not quiet:
        total_albums = sum(
            sum(len(albums) for albums in artist_albums.values())
            for artist_albums in final_wings.values()
        )
        print(
            f"\n{len(final_wings)} wings ({total_albums} albums) written to: {outdir}"
        )
    return 0


# =====================================
# Mode: AI wings (per-genre flat files)
# =====================================


def write_ai_wings(
    root_dir: str | list[str],
    outdir: str,
    *,
    layout: str = "{artist}/{album}",
    quiet: bool = False,
) -> int:
    """Generate separate, token-efficient AI library files for each genre."""
    roots = as_roots(root_dir)
    total = count_audio_files(roots)
    if not quiet:
        print(f"Scanning {total} files for AI wings...")

    pbar = _make_pbar(total, "Scanning genres", quiet)
    album_dirs = _scan_album_dirs(roots, layout, pbar)
    pbar.close()

    if not album_dirs:
        print("No albums found under root.", file=sys.stderr)
        return 1

    # genre -> list of (artist, album, genre, path)
    wings: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    for ad in album_dirs:
        genre_str = ad.genre or "Uncategorized"
        for genre in (g.strip() for g in genre_str.split("/") if g.strip()):
            wings[genre].append((ad.artist, ad.album, genre, ad.path))

    os.makedirs(outdir, exist_ok=True)

    if not quiet:
        print(f"\nFound {len(wings)} genres. Writing AI wings...\n")

    for genre_name in sorted(wings):
        albums = sorted(wings[genre_name])
        output = os.path.join(outdir, f"{_safe_wing_name(genre_name)}_AI.txt")

        if not quiet:
            print(f"→ {genre_name} ({len(albums)} albums)")

        with open(output, "w", encoding="utf-8") as f:
            f.write("Artist | Album | Genre | Location\n")
            f.write("-" * 60 + "\n")
            for artist, album, genre, path in albums:
                f.write(f"{artist} | {album} | {genre} | {path}\n")

    if not quiet:
        total_albums = sum(len(a) for a in wings.values())
        print(f"\n{len(wings)} AI wings ({total_albums} albums) written to: {outdir}")
    return 0


# =====================================
# Mode: Snapshot / diff
# =====================================

# The evidence half for every writer: a snapshot is one row per audio file
# (path, size, mtime, and the tag fields the companions and write modes
# touch), and --diff replays a snapshot against the current tree. Diffing is
# structural, not byte-level: a tag rewrite shows as RETAGGED, a move as
# MOVED (mv-only writers preserve size and mtime exactly), a re-encode as
# RESIZED. Compound runs compose: snapshot before, snapshot after, one diff
# names everything in between.

_SNAPSHOT_COLUMNS = (
    "path",
    "size",
    "mtime",
    "track",
    "rating",
    "genre",
    "year",
    "rg",
)

# Signature used to pair a removed row with an added path (a move): the
# mv-only writers rename without touching content, so size and mtime survive.
_MOVE_SIGNATURE_COLUMNS = ("size", "mtime")


def _escape_tsv(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n")


def _unescape_tsv(value: str) -> str:
    return value.replace("\\t", "\t").replace("\\n", "\n").replace("\\\\", "\\")


def _snapshot_rows(roots: list[str], pbar) -> dict[str, dict]:
    """Current tree state: path -> {column: value}. One tag read per file,
    serial like the other walkers (the pool is a measured regression)."""
    rows: dict[str, dict] = {}
    for _src_root, dirpath, _subdirs, files in iter_audio_dirs(roots):
        audio = sorted(f for f in files if is_audio(f))
        if not audio:
            continue
        for f in audio:
            filepath = os.path.join(dirpath, f)
            try:
                stat = os.stat(filepath)
            except OSError:
                continue
            t = get_all_tags(filepath)
            rg = read_replaygain(filepath)
            rows[filepath] = {
                "path": filepath,
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
                "track": t.trackno,
                "rating": None if t.rating is None else round(t.rating, 2),
                "genre": t.genre,
                "year": t.year,
                "rg": (1 if rg.has_track_gain else 0, 1 if rg.has_album_gain else 0),
            }
            pbar.update(1)
    return rows


def _format_row(row: dict) -> str:
    rg = f"{row['rg'][0]}{row['rg'][1]}"
    cells = (
        _escape_tsv(str(row["path"])),
        str(row["size"]),
        str(row["mtime"]),
        "" if row["track"] is None else str(row["track"]),
        "" if row["rating"] is None else f"{row['rating']:.2f}",
        "" if row["genre"] is None else _escape_tsv(row["genre"]),
        "" if row["year"] is None else str(row["year"]),
        rg,
    )
    return "\t".join(cells)


def write_snapshot(
    root_dir: str | list[str],
    output_file: str,
    *,
    quiet: bool = False,
) -> int:
    """Write the library snapshot TSV (the --diff input) for one or more
    roots. Overwrites the target: snapshots are points in time, and a diff
    wants a fixed baseline, not an append log."""
    roots = as_roots(root_dir)
    total = count_audio_files(roots)
    if not quiet:
        print(f"Snapshotting {total} audio files under: {', '.join(roots)}")
    pbar = _make_pbar(total, "Snapshotting library", quiet)
    rows = _snapshot_rows(roots, pbar)
    pbar.close()

    out_path = os.path.abspath(output_file)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("# lattice snapshot v1\n")
        f.write("\t".join(_SNAPSHOT_COLUMNS) + "\n")
        for path in sorted(rows):
            f.write(_format_row(rows[path]) + "\n")

    if not quiet:
        print(f"Snapshot ({len(rows)} files) written to: {out_path}")
    return 0


def _load_snapshot(path: str) -> dict[str, dict] | None:
    """Parse a snapshot TSV back into rows, or None when the file is not a
    lattice snapshot (wrong header) so a stray file never diffs as
    everything-removed."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    lines = [ln for ln in lines if ln.strip()]
    if not lines or lines[0] != "# lattice snapshot v1":
        return None
    if len(lines) < 2 or lines[1].split("\t") != list(_SNAPSHOT_COLUMNS):
        return None
    rows: dict[str, dict] = {}
    for line in lines[2:]:
        cells = line.split("\t")
        if len(cells) != len(_SNAPSHOT_COLUMNS):
            continue
        path = _unescape_tsv(cells[0])
        rg = cells[7]
        rows[path] = {
            "path": path,
            "size": int(cells[1]) if cells[1] else 0,
            "mtime": int(cells[2]) if cells[2] else 0,
            "track": int(cells[3]) if cells[3] else None,
            "rating": float(cells[4]) if cells[4] else None,
            "genre": _unescape_tsv(cells[5]) if cells[5] else None,
            "year": int(cells[6]) if cells[6] else None,
            "rg": (
                int(rg[0]) if rg[:1].isdigit() else 0,
                int(rg[1]) if rg[1:2].isdigit() else 0,
            ),
        }
    return rows


_TAG_LABELS = {
    "track": "track",
    "rating": "rating",
    "genre": "genre",
    "year": "year",
    "rg": "replaygain",
}


def _cell(value) -> str:
    if value is None:
        return "(none)"
    if isinstance(value, tuple):
        label = {0: "untagged", 1: "track only", 2: "album only", 3: "track+album"}
        return label.get(value[0] + 2 * value[1], str(value))
    return str(value)


def diff_snapshot(
    root_dir: str | list[str],
    snapshot_file: str,
    output_file: str,
    *,
    quiet: bool = False,
) -> int:
    """Replay a snapshot against the current tree: MOVED (same size+mtime at
    a new path), RETAGGED (same path, changed tag fields, with old -> new per
    field), RESIZED (same path, different bytes), then ADDED and REMOVED
    leftovers. The report is the whole point; nothing is written to the
    library."""
    roots = as_roots(root_dir)
    total = count_audio_files(roots)
    if not quiet:
        print(f"Diffing {total} audio files against {snapshot_file}")
    pbar = _make_pbar(total, "Diffing snapshot", quiet)
    current = _snapshot_rows(roots, pbar)
    pbar.close()

    baseline = _load_snapshot(snapshot_file)
    if baseline is None:
        print(
            f"error: {snapshot_file} is not a lattice snapshot (missing the "
            "# lattice snapshot v1 header); run --snapshot first",
            file=sys.stderr,
        )
        return 2

    moved: list[tuple[str, str]] = []
    retagged: list[tuple[str, list[str]]] = []
    resized: list[tuple[str, str, str]] = []
    added: list[str] = []
    removed: list[str] = []

    old_paths = set(baseline)
    new_paths = set(current)

    # Pair removed -> added on the move signature before classifying the
    # rest, so a rename is not reported as a removal plus an addition.
    removed_candidates = {
        p: baseline[p] for p in old_paths - new_paths if p not in current
    }
    added_candidates = [p for p in new_paths - old_paths]

    for old_path in sorted(removed_candidates):
        old_row = baseline[old_path]
        signature = (old_row["size"], old_row["mtime"])
        match = next(
            (
                p
                for p in added_candidates
                if (current[p]["size"], current[p]["mtime"]) == signature
            ),
            None,
        )
        if match is not None:
            added_candidates.remove(match)
            moved.append((old_path, match))
        else:
            removed.append(old_path)
    added.extend(sorted(added_candidates))

    for path in sorted(old_paths & new_paths):
        old_row, new_row = baseline[path], current[path]
        if old_row["size"] != new_row["size"]:
            resized.append((path, str(old_row["size"]), str(new_row["size"])))
            continue
        changes = []
        for col in ("track", "rating", "genre", "year", "rg"):
            if old_row[col] != new_row[col]:
                changes.append(
                    f"{_TAG_LABELS[col]} {_cell(old_row[col])} -> {_cell(new_row[col])}"
                )
        if changes:
            retagged.append((path, changes))

    out_path = os.path.abspath(output_file)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("SNAPSHOT DIFF REPORT\n")
        f.write(f"Snapshot: {snapshot_file}\n")
        f.write(f"Root: {', '.join(roots)}\n")
        f.write(
            f"Moved: {len(moved)}   Retagged: {len(retagged)}   "
            f"Resized: {len(resized)}   Added: {len(added)}   "
            f"Removed: {len(removed)}\n"
        )
        f.write("=" * 64 + "\n\n")

        if moved:
            f.write(f"MOVED ({len(moved)})\n")
            f.write("-" * 40 + "\n")
            for old_path, new_path in moved:
                f.write(f"  {relpath_under(old_path, roots)}\n")
                f.write(f"    -> {relpath_under(new_path, roots)}\n")
            f.write("\n")
        if retagged:
            f.write(f"RETAGGED ({len(retagged)})\n")
            f.write("-" * 40 + "\n")
            for path, changes in retagged:
                f.write(f"  {relpath_under(path, roots)}\n")
                for change in changes:
                    f.write(f"    {change}\n")
            f.write("\n")
        if resized:
            f.write(f"RESIZED ({len(resized)})\n")
            f.write("-" * 40 + "\n")
            for path, old_size, new_size in resized:
                f.write(
                    f"  {relpath_under(path, roots)} ({old_size} -> {new_size} bytes)\n"
                )
            f.write("\n")
        if added:
            f.write(f"ADDED ({len(added)})\n")
            f.write("-" * 40 + "\n")
            for path in added:
                f.write(f"  {relpath_under(path, roots)}\n")
            f.write("\n")
        if removed:
            f.write(f"REMOVED ({len(removed)})\n")
            f.write("-" * 40 + "\n")
            for path in removed:
                f.write(f"  {relpath_under(path, roots)}\n")
            f.write("\n")
        if not any((moved, retagged, resized, added, removed)):
            f.write("No differences.\n\n")

    if not quiet:
        print(
            f"\nMoved: {len(moved)}  Retagged: {len(retagged)}  Resized: {len(resized)}  Added: {len(added)}  Removed: {len(removed)}"
        )
        print(f"Results written to: {out_path}")

    return 0


# =====================================
# Mode: Snapshot / diff (end)
# =====================================
