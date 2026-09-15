#!/usr/bin/env python3
"""genre_tidy.py — build an artist→genre authority and reconcile a library to it.

A two-phase companion that pairs the lattice package (the read-only scanner) with retag.py
(the per-album genre rewriter):

    genre_tidy.py build <library>   # read-only: scan, write an editable TSV map
    genre_tidy.py apply <library>   # destructive: retag albums that disagree

The map (`<library>/genre_map.tsv` by default) is one editable tab-separated
line per artist, listing every genre that artist is allowed to carry:

    Artist<TAB>Genre<TAB>Second Genre<TAB>...

`build` seeds each line with every genre the artist currently uses (most-common
first), so `apply` is a no-op until you edit. To tidy, REMOVE a stray genre from
a line: `apply` re-scans, and for every album whose genre is no longer on its
artist's line, calls retag.py to overwrite it to the first (canonical) genre.
Reorder the line to change the fix target; leave only the artist (no genres) to
skip that artist entirely. Multi-genre artists also get a `#` comment with the
per-genre counts, so low-count strays worth trimming stand out.

Lives in scripts/ (outside the lattice package) because it mutates tags, which
the package's read-only contract (spec.md §5) forbids. It reads through lattice
and writes through retag.py; the package itself stays read-only.

Usage:
    ./genre_tidy.py build /mnt/SharedData/Music
    ./genre_tidy.py apply /mnt/SharedData/Music --dry-run
    ./genre_tidy.py apply /mnt/SharedData/Music --map ~/genres.tsv --log ~/tidy.log
    ./genre_tidy.py build /library --layout "{genre}/{artist}/{album}"
"""

import argparse
import os
import re
import subprocess
import sys
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from vir_tui import core as ui

__version__ = "1.3.0"

# Folds curly quotes, dash variants, and case so artist/genre strings compare
# the way a human reads them. The canonical table is lattice.norm's
# QUOTE_DASH_FOLD (the package absorbed the cleaner in the 5.0.0 fold); this
# copy predates it and is what build wrote into existing genre maps, so it
# stays rather than re-keying saved maps on a future norm.py change.
_QUOTE_DASH_FOLD = {
    "‘": "'",
    "’": "'",
    "ʼ": "'",
    "“": '"',
    "”": '"',
    "‐": "-",
    "‑": "-",
    "‒": "-",
    "–": "-",
    "—": "-",
    "―": "-",
}

TSV_HEADER = [
    "# lattice-music genre authority map (genre_tidy.py)",
    "# Format:  Artist<TAB>Genre<TAB>Second Genre<TAB>...   (tab-separated)",
    "#   - Every genre on the line is ALLOWED for that artist; albums tagged",
    "#     with any of them are left untouched.",
    "#   - The FIRST genre is the fix target: `apply` retags any album whose",
    "#     genre is NOT on the line to that first genre.",
    "#   - `build` lists every genre an artist currently uses, so `apply`",
    "#     changes nothing until you edit. To tidy, REMOVE a stray genre from a",
    "#     line and its albums collapse to the first genre; reorder to retarget.",
    "#   - Leave only the artist (no genres) to skip that artist entirely.",
    "#   - Compilation album-artists (Various Artists) are flagged EXCLUDED and",
    "#     never enforced: a comp has no single canonical genre.",
    "#   - A line is a comment only when its # is followed by a space, a dash,",
    "#     or nothing — so an artist whose NAME starts with # (e.g. '#1 Dad')",
    "#     is data and survives the round-trip.",
    "",
]

# Comment rule for the map: "#" + space/dash/end-of-line. Generated comments
# always start "# " or "# ---"; a #-leading artist name never matches.
_COMMENT_RE = re.compile(r"^\s*#(?:\s|-|$)")


def norm(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    for k, v in _QUOTE_DASH_FOLD.items():
        s = s.replace(k, v)
    return " ".join(s.split()).lower()


# Compilation album-artists: a "Various Artists" disc collects unrelated tracks
# with no single canonical genre, so there is nothing to enforce. build flags
# these for manual review (a commented line, no data row) and apply never
# retags them. (TagBundle.artist already prefers the album-artist tag, so this
# keys off album-artist, not the per-track performer.) Accepted trade-off: a
# real artist actually named "Various" or "VA" is permanently unenforceable.
EXCLUDED_ARTISTS = {"various artists", "various", "va"}


# ============================ pure map helpers ============================


class MapEntry(NamedTuple):
    display: str
    canonical: str  # first allowed genre, original case; "" means skip artist
    allowed_norm: frozenset[str]


def parse_map(lines) -> dict[str, MapEntry]:
    """Parse TSV lines into {norm_artist: MapEntry}. Skips blank lines and
    comments (per _COMMENT_RE, so '#1 Dad' is a data row, not a comment).
    Columns after the artist are the allowed genres; the first is canonical."""
    entries: dict[str, MapEntry] = {}
    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip() or _COMMENT_RE.match(line):
            continue
        cols = line.split("\t")
        artist = cols[0].strip()
        if not artist:
            continue
        allowed = [c.strip() for c in cols[1:] if c.strip()]
        key = norm(artist)
        if key in entries:
            # Hand-edited rows can collide after normalization ("Jay-Z" vs a
            # curly-dash "Jay‐Z"); silent last-wins hides the lost row.
            print(
                f"warning: map rows {entries[key].display!r} and {artist!r} "
                "normalize to the same artist; the later row wins",
                file=sys.stderr,
            )
        entries[key] = MapEntry(
            display=artist,
            canonical=allowed[0] if allowed else "",
            allowed_norm=frozenset(norm(a) for a in allowed),
        )
    return entries


def is_compliant(album_genre: str | None, allowed_norm: frozenset[str]) -> bool:
    """True when the album's genre is one of the artist's allowed genres. An
    empty/missing genre is never compliant, so `apply` fills it with canonical
    (except albums in formats retag cannot write, which apply skips with an
    UNSUPPORTED FORMAT note instead of retagging into a void)."""
    return norm(album_genre) in allowed_norm


def retag_argv(
    retag_path: Path, album_path: str, canonical: str, *, dry_run: bool
) -> list[str]:
    """Build the retag.py invocation. The canonical is passed verbatim as one
    genre value: a slash canonical like "Emo / Orgcore" is one literal genre
    string in every container, so what apply writes is exactly what the
    compliance check reads back. (Splitting on "/" wrote a multi-value tag
    that never read back equal to the map, retagging the album forever.)"""
    argv = [sys.executable, str(retag_path), album_path, canonical]
    if dry_run:
        argv.append("--dry-run")
    return argv


def reduce_artists(album_dirs) -> dict[str, tuple[str, Counter]]:
    """Collapse scanned album dirs to {norm_artist: (display_name, genre Counter)}.
    Genres are weighted by album count; the display name is the most-common
    spelling seen for that artist."""
    display_counts: dict[str, Counter] = {}
    genre_counts: dict[str, Counter] = {}
    for ad in album_dirs:
        if not ad.artist:
            continue
        key = norm(ad.artist)
        if not key:
            continue
        display_counts.setdefault(key, Counter())[ad.artist] += 1
        gc = genre_counts.setdefault(key, Counter())
        if ad.genre:
            gc[ad.genre] += 1
    return {
        key: (display_counts[key].most_common(1)[0][0], genre_counts[key])
        for key in display_counts
    }


def _tsv_field(s: str) -> str:
    """Tabs/newlines inside a tag value would corrupt the TSV (a genre
    "Rock\\tPop" reads back as two allowed genres, so a fresh map would not be
    a no-op); fold them to a space. norm() collapses whitespace the same way,
    so the sanitized field still matches the raw tag at compliance time."""
    return re.sub(r"[\t\r\n]+", " ", s)


def build_rows(reduced: dict[str, tuple[str, Counter]]) -> list[str]:
    """Format reduced artists into TSV rows, sorted by artist. The line lists
    every genre the artist currently uses (most-frequent first = canonical).
    Multi-genre artists also get a leading # comment with the per-genre counts,
    so low-count strays worth trimming stand out."""
    rows: list[str] = []
    for key in sorted(reduced, key=lambda k: reduced[k][0].lower()):
        display, genres = reduced[key]
        display = _tsv_field(display)
        ordered = [_tsv_field(g) for g, _ in genres.most_common()]
        spread = ", ".join(f"{_tsv_field(g)}×{n}" for g, n in genres.most_common())
        if key in EXCLUDED_ARTISTS:
            rows.append(
                f"# {display}: EXCLUDED (compilation): {spread or 'no genre tags'}. "
                f"Not enforced; investigate manually."
            )
            continue
        if len(ordered) > 1:
            rows.append(f"# {display}: {len(ordered)} genres: {spread}")
        elif not ordered:
            rows.append(f"# {display}: no genre tags found")
        rows.append("\t".join([display, *ordered]))
    return rows


# ============================ lattice (read half) ============================


def _import_lattice():
    try:
        from lattice.modes.library import _scan_album_dirs
        from lattice.utils import _make_pbar, as_roots, count_audio_files
    except ImportError as e:
        print(
            f"error: could not import lattice ({e}).\n"
            "Install it (pip install -e . / pipx install .) or run with "
            "PYTHONPATH=src.",
            file=sys.stderr,
        )
        sys.exit(2)
    return _scan_album_dirs, as_roots, count_audio_files, _make_pbar


def scan_album_dirs(directory: Path, quiet: bool, layout: str = "{artist}/{album}"):
    scan, as_roots, count_audio_files, make_pbar = _import_lattice()
    roots = as_roots(str(directory))
    pbar = make_pbar(count_audio_files(roots), "Scanning", quiet)
    dirs = scan(roots, layout, pbar)
    pbar.close()
    return dirs


# ============================ logging (apply) ============================


class _Log:
    """Append-only timestamped log, file-only (matches cleaner.py's logger)."""

    def __init__(self, path: Path, dry_run: bool):
        self.path = path
        self.dry_run = dry_run
        self.fh = path.open("a", encoding="utf-8")

    def write(self, msg: str = "") -> None:
        if msg:
            ts = datetime.now().isoformat(timespec="seconds")
            prefix = "[DRY] " if self.dry_run else ""
            self.fh.write(f"[{ts}] {prefix}{msg}\n")
        else:
            self.fh.write("\n")
        self.fh.flush()

    def close(self) -> None:
        self.fh.close()


# ============================ subcommands ============================


def cmd_build(args) -> int:
    directory = Path(args.directory).resolve()
    if not directory.is_dir():
        print(ui.error(f"{directory} is not a directory"), file=sys.stderr)
        return 1

    map_path = Path(args.map_path) if args.map_path else directory / "genre_map.tsv"
    reduced = reduce_artists(scan_album_dirs(directory, args.quiet, args.layout))

    if map_path.exists():
        existing = map_path.read_text(encoding="utf-8").splitlines()
        present = set(parse_map(existing).keys())
        # EXCLUDED artists intentionally have no data row, so "present in the
        # map" can never be true for them; without this filter every rebuild
        # re-appended the Various Artists comment forever.
        new = {
            k: v
            for k, v in reduced.items()
            if k not in present and k not in EXCLUDED_ARTISTS
        }
        if not new:
            print(
                ui.info(
                    f"No new artists; {map_path} unchanged ({len(present)} artists)."
                )
            )
            return 0
        added = build_rows(new)
        stamp = datetime.now().date().isoformat()
        map_path.write_text(
            "\n".join(existing + ["", f"# --- added {stamp} ---"] + added) + "\n",
            encoding="utf-8",
        )
        print(ui.info(f"Appended {len(new)} new artist(s) to {map_path}."))
        return 0

    rows = build_rows(reduced)
    map_path.parent.mkdir(parents=True, exist_ok=True)
    map_path.write_text("\n".join(TSV_HEADER + rows) + "\n", encoding="utf-8")
    # Counted from the reduced artists, not from rows starting with "#": that
    # also swept up the EXCLUDED and "no genre tags" comments and reported them
    # as multi-genre artists.
    flagged = sum(
        1
        for key, (_display, genres) in reduced.items()
        if key not in EXCLUDED_ARTISTS and len(genres) > 1
    )
    print(ui.info(f"Wrote {len(reduced)} artists to {map_path}."))
    print(
        ui.info(f"  {flagged} artist(s) carry multiple genres (commented with counts).")
    )
    print(
        ui.info("Trim any stray genres, then: genre_tidy.py apply <library> --dry-run")
    )
    return 0


def cmd_apply(args) -> int:
    directory = Path(args.directory).resolve()
    if not directory.is_dir():
        print(ui.error(f"{directory} is not a directory"), file=sys.stderr)
        return 1

    map_path = Path(args.map_path) if args.map_path else directory / "genre_map.tsv"
    if not map_path.exists():
        print(
            ui.error(f"no map at {map_path}. Run `genre_tidy.py build` first."),
            file=sys.stderr,
        )
        return 1

    retag_path = Path(__file__).resolve().parent / "retag.py"
    if not retag_path.exists():
        print(ui.error(f"retag.py not found at {retag_path}"), file=sys.stderr)
        return 1

    # Sibling module import for the writable-format set: importing (rather
    # than mirroring the extensions here) means the two can't drift.
    import retag as _retag

    writable_exts = set(_retag.AUDIO_EXTENSIONS)

    entries = parse_map(map_path.read_text(encoding="utf-8").splitlines())
    album_dirs = scan_album_dirs(directory, args.quiet, args.layout)
    log_path = Path(args.log_path) if args.log_path else directory / "genre_tidy.log"

    log = _Log(log_path, args.dry_run)
    stats: Counter = Counter()
    try:
        mode = "DRY RUN" if args.dry_run else "APPLY"
        log.write("=" * 70)
        log.write(f"GENRE TIDY [{mode}]: {directory}")
        log.write(f"map: {map_path}  ({len(entries)} artists)")
        log.write("=" * 70)

        for ad in ui.tqdm(
            sorted(album_dirs, key=lambda a: a.path),
            desc=ui.info("Applying genre changes"),
        ):
            rel = os.path.relpath(ad.path, directory)
            if norm(ad.artist) in EXCLUDED_ARTISTS:
                stats["excluded"] += 1
                log.write(f"  SKIP (compilation/various-artists): {rel}  [{ad.artist}]")
                continue
            entry = entries.get(norm(ad.artist))
            if entry is None:
                stats["unmapped"] += 1
                log.write(f"  SKIP (artist not in map): {rel}  [{ad.artist}]")
                continue
            if not entry.canonical:
                stats["skipped_blank"] += 1
                log.write(f"  SKIP (artist genre blanked): {rel}")
                continue
            if is_compliant(ad.genre, entry.allowed_norm):
                stats["ok"] += 1
                continue

            # Formats lattice scans but retag can't write would otherwise be
            # invoked forever as no-op "retags" that never converge.
            try:
                names = os.listdir(ad.path)
            except OSError:
                names = []
            if not any(os.path.splitext(n)[1].lower() in writable_exts for n in names):
                stats["unsupported"] += 1
                exts = sorted(
                    {e for e in (os.path.splitext(n)[1].lower() for n in names) if e}
                )
                log.write(
                    f"  UNSUPPORTED FORMAT (skipped): {rel}  "
                    f"({', '.join(exts) or 'no extensions'})"
                )
                continue

            log.write(f"  RETAG {rel}: {ad.genre or '(none)'!r} -> {entry.canonical}")
            result = subprocess.run(
                retag_argv(retag_path, ad.path, entry.canonical, dry_run=args.dry_run),
                capture_output=True,
                text=True,
            )
            for line in result.stdout.splitlines():
                log.write(f"      {line}")
            if result.returncode != 0:
                # Counted as an error, not a retag: retag exits nonzero when
                # any file failed to write (retag.py v1.1.1).
                stats["errors"] += 1
                for line in result.stderr.splitlines():
                    log.write(f"      ERR {line}")
            else:
                stats["retagged"] += 1

        log.write("--- SUMMARY ---")
        for key, value in stats.items():
            log.write(f"  {key}: {value}")
        log.write(f"GENRE TIDY END [{mode}]")
        log.write("=" * 70)
    finally:
        log.close()

    verb = "would retag" if args.dry_run else "retagged"
    print(
        ui.info(
            f"\n{verb} {stats['retagged']} album(s); {stats['ok']} already compliant."
        )
    )
    if stats["unmapped"]:
        print(ui.info(f"  {stats['unmapped']} album(s) skipped (artist not in map)."))
    if stats["skipped_blank"]:
        print(
            ui.info(
                f"  {stats['skipped_blank']} album(s) skipped (artist genre blanked)."
            )
        )
    if stats["excluded"]:
        print(
            ui.info(
                f"  {stats['excluded']} album(s) skipped (compilation / various-artists)."
            )
        )
    if stats["unsupported"]:
        print(
            ui.info(
                f"  {stats['unsupported']} album(s) skipped (no format retag can write)."
            )
        )
    if stats["errors"]:
        print(ui.info(f"  {stats['errors']} retag error(s) — see log."))
    print(ui.info(f"Log: {log_path}"))
    # Nonzero when any album failed to retag, matching retag.py, rerate.py and
    # replaygain.py; apply used to exit 0 no matter how many writes failed.
    return 1 if stats["errors"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build and enforce an artist→genre authority map for a music library."
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser(
        "build", help="Scan (read-only) and write the artist→genre TSV map"
    )
    b.add_argument("directory", help="Music library root")
    b.add_argument(
        "--map",
        dest="map_path",
        default=None,
        help="Map path (default: <directory>/genre_map.tsv)",
    )
    b.add_argument(
        "--layout",
        default="{artist}/{album}",
        help="Folder layout used to recover artist/genre from paths for "
        "untagged files (default '{artist}/{album}'; for a genre-foldered "
        "library pass '{genre}/{artist}/{album}')",
    )
    b.add_argument("--quiet", action="store_true", help="Minimize progress output")
    b.set_defaults(func=cmd_build)

    a = sub.add_parser(
        "apply", help="Retag albums that disagree with the map (destructive)"
    )
    a.add_argument("directory", help="Music library root")
    a.add_argument(
        "--map",
        dest="map_path",
        default=None,
        help="Map path (default: <directory>/genre_map.tsv)",
    )
    a.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview retag calls; write nothing (log lines prefixed [DRY])",
    )
    a.add_argument(
        "--log",
        dest="log_path",
        default=None,
        help="Log path (default: <directory>/genre_tidy.log)",
    )
    a.add_argument(
        "--layout",
        default="{artist}/{album}",
        help="Folder layout used to recover artist/genre from paths for "
        "untagged files (default '{artist}/{album}'; for a genre-foldered "
        "library pass '{genre}/{artist}/{album}')",
    )
    a.add_argument("--quiet", action="store_true", help="Minimize progress output")
    a.set_defaults(func=cmd_apply)

    args = parser.parse_args()

    is_dry = getattr(args, "dry_run", False)
    ui.print_header(
        "genre_tidy.py - Genre Folder Cleaner" + (" [DRY RUN]" if is_dry else "")
    )

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
