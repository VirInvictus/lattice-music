"""Restructure a library into Genre/Artist/Album (the genreMap write mode).

Promoted from scripts/genre_foldermap.py (v1.4.1), which now launches this
module as a compat shim. Reorganizes a flat Artist/Album/Song tree into a
Genre/Artist/Album/Song tree by moving each album folder under a top-level
genre directory. The genre is the album's dominant embedded genre tag, read
through the package's scanner (the same aggregation every library/wing mode
uses), so placement matches what lattice reports. Folder names are preserved
verbatim; nothing is retagged.

This is the package's first file-moving mode, sanctioned by the spec.md §5
amendment of the 6.0.0 program. Safety contract (unchanged from the script):
mv-only on one filesystem (a cross-device move is refused rather than degraded
to copy+delete, so audio bytes are never rewritten), dry-run by default with
--apply to perform, every performed move appended to a manifest TSV that
--revert replays in reverse, destinations never overwritten, and anything
deeper than Genre/Artist/Album flagged TOO DEEP rather than guessed at. The
Runner plans against the same shared dry-run virtual filesystem as the clean
mode (lattice.vfs), so the preview's prunes and stats match the apply run.
"""

import argparse
import re
import sys
from collections import Counter, namedtuple
from datetime import datetime
from pathlib import Path

from lattice.modes.library import _scan_album_dirs
from lattice.utils import _make_pbar, as_roots, count_audio_files
from lattice.vfs import VirtualFS
from vir_tui import core as ui

__version__ = "1.4.1"

# Path-component characters forbidden on Windows/NTFS/exFAT (the library often
# lives on a shared NTFS volume), plus the trailing "." / " " rule. Genre names
# are folded to a safe form so a stray ":" or "/" in a tag can't break the tree.
_ILLEGAL_NAME_CHARS = '<>:"/\\|?*'

SINGLES_DIR = "Singles"

# Default name of the staging inbox at the library root. Artist folders dumped
# here (e.g. by Picard) are filed into the real taxonomy instead of being read
# as a genre folder. Overridable via --staging; pass "" to disable.
STAGING_DIR = "Unfiltered"

# Disc subfolders of a multi-disc album (Album/CD1, Album/Disc 2, ...). The
# scanner emits one record per audio-bearing dir, so these arrive as their own
# records one level deeper than the album; classify collapses them to the
# parent album so the album moves as a unit and the artist name never reads as
# a genre folder.
DISC_DIR_RE = re.compile(r"(?i)^(?:cd|disc|disk|dvd|side|vinyl)[\s._-]*\d+$")

# A single planned filesystem move. `kind` is "dir" (a whole album folder) or
# "file" (one loose track/sidecar destined for a Singles folder); it only
# affects the emitted MV label and which stats counter the move lands in.
Move = namedtuple("Move", "src dst kind")


def sanitize_component(name: str) -> str:
    """Fold a tag value into a filesystem-legal single path component. Forbidden
    characters become spaces, runs of whitespace collapse, and trailing dots or
    spaces (which NTFS rejects) are stripped."""
    cleaned = "".join(" " if c in _ILLEGAL_NAME_CHARS else c for c in name)
    cleaned = " ".join(cleaned.split())
    return cleaned.rstrip(". ") or "Unknown"


# Separators that join several genres into one tag value: ";" from taggers that
# write multi-genre TCON as a single joined string, "/" as the library's own
# multi-genre convention (which the wing modes split the same way).
_MULTI_GENRE_SEP_RE = re.compile(r"[;/]")


def split_multi_genre(genre: str) -> tuple[str, str]:
    """Split a possibly multi-genre tag value into (primary, ignored-rest).
    A single-genre value returns (value, ""); a joined value returns its first
    component and the rest re-joined for reporting. A value that is nothing but
    separators returns ("", "") so callers fall through to their NO GENRE path.
    Without this, a joined value would sanitize into one absurd path component
    (";" is NTFS-legal, "/" would fold to a space)."""
    parts = [p.strip() for p in _MULTI_GENRE_SEP_RE.split(genre) if p.strip()]
    if not parts:
        return "", ""
    return parts[0], "; ".join(parts[1:])


def classify(path: Path, root: Path, staging: str | None = None) -> tuple:
    """Decide how a scanned audio directory maps into the new tree, from its
    depth under root. A `staging` folder name (e.g. "Unfiltered") is stripped
    from the front of the path first, so an album dumped at staging/Artist/Album
    classifies as the flat-stray ("album", Artist, Album) and is filed into the
    real taxonomy, not read as a genre folder. Returns one of:
        ("album", artist, album)             - Artist/Album (depth 2): a stray
                                               flat album to file under a genre.
                                               A disc subfolder (Artist/Album/CD1)
                                               collapses to this too; the parent
                                               album dir is the unit that moves
        ("organized", genre, artist, album)  - Genre/Artist/Album (depth 3), or
                                               a disc subfolder of one (depth 4):
                                               already in the genre tree
        ("loose", artist)                    - Artist/ with loose tracks (depth 1)
        ("toodeep", depth)                   - deeper than Genre/Artist/Album:
                                               not placed (usually the wrong root)
        ("staged-toodeep", depth)            - inside the staging inbox but deeper
                                               than Artist/Album (+discs): nothing
                                               in the inbox is ever "organized",
                                               so it is flagged, left in place
        ("skip", reason)                     - anything we won't place (e.g. root)
    The intended library is Genre/Artist/Album; a flat Artist/Album library is
    the input this tool converts. Anything deeper is flagged rather than guessed
    at, so pointing the tool one level too high can't silently relocate the
    whole tree."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return ("skip", "outside root")
    parts = rel.parts
    staged = False
    if staging and parts and parts[0] == staging:
        parts = parts[1:]
        staged = True
    if len(parts) == 0:
        return ("skip", "loose audio at library root")
    if len(parts) == 1:
        return ("loose", parts[0])
    if len(parts) == 2:
        return ("album", parts[0], parts[1])
    if len(parts) == 3 and DISC_DIR_RE.match(parts[2]):
        # A disc subfolder of a flat album: the parent Artist/Album dir is the
        # unit that moves, discs riding along inside it.
        return ("album", parts[0], parts[1])
    if staged:
        return ("staged-toodeep", len(parts))
    if len(parts) == 3:
        return ("organized", parts[0], parts[1], parts[2])
    if len(parts) == 4 and DISC_DIR_RE.match(parts[3]):
        # A disc subfolder of an organized album IS that album, not TOO DEEP.
        return ("organized", parts[0], parts[1], parts[2])
    return ("toodeep", len(parts))


def build_plan(
    records,
    root: Path,
    only_genres=None,
    allow_new_genre=False,
    staging=None,
    refile_mismatched=False,
):
    """Turn scanner records (each with `.path` and `.genre`) into a list of
    Moves, plus a list of human-readable issues and the set of source artist
    directories that may be left empty. `only_genres` (a set of genre strings)
    restricts the plan to those genres, supporting a staged rollout.

    Placement is gated by the library's existing genre vocabulary: the genres
    of albums this scan found already organized at Genre/Artist/Album depth
    (record-derived, so a genre folder with no readable audio this run drops
    out of the gate). Matching is case-insensitive and a match reuses the
    existing folder's spelling; a stray whose tag genre isn't in the vocabulary
    is flagged, not filed into a new top-level folder, unless `allow_new_genre`
    is set. When no genre folders exist yet (a flat library) the set is empty
    and gating is off.

    `staging` (a folder name like "Unfiltered") is passed through to classify so
    albums dumped in that inbox are filed into the real taxonomy at the root, not
    read as a genre. The inbox's per-artist source dirs are pruned when emptied,
    but the inbox folder itself is never a source dir, so it is left in place.

    Loose-track directories are read from disk here to enumerate the individual
    files to move; album directories move as a single unit. Artist-level
    sidecar files (e.g. an Artist/cover.jpg beside album subfolders) follow the
    artist to its dominant genre so they are not orphaned."""
    moves: list[Move] = []
    issues: list[str] = []
    source_artist_dirs: set[Path] = set()
    seen_dst: dict[Path, Path] = {}
    seen_src: dict[Path, Path] = {}
    # Album-record artist dirs and the genres their albums carry, plus the dirs
    # that are themselves loose records — both feed the artist-level sidecar
    # pass below.
    album_artist_genres: dict[Path, Counter] = {}
    loose_dirs: set[Path] = set()

    def add(src: Path, dst: Path, kind: str) -> bool:
        """Plan one move. Returns True only when a Move was actually appended,
        so callers can gate their bookkeeping (sidecar/prune registration) on
        it instead of acting on a rejected move."""
        if any("\t" in s or "\n" in s for s in (str(src), str(dst))):
            # A tab/newline in a path would corrupt the manifest TSV, making
            # the move unrevertable; refuse it (rename the folder first).
            issues.append(f"UNSAFE NAME (tab/newline in path; skipped): {src}")
            return False
        prior_dst = seen_src.get(src)
        if prior_dst is not None:
            # Disc records of one album resolve to the same source unit: the
            # same (src, dst) pair is one move, a differing dst means the
            # discs' genre tags disagree (first record wins, conflict flagged).
            if prior_dst != dst:
                issues.append(
                    f"DISC GENRE MISMATCH (kept first): {src}\n"
                    f"    {prior_dst}\n    {dst}"
                )
            return False
        prior = seen_dst.get(dst)
        if prior is not None:
            seen_src[src] = dst  # remember the rejection so discs don't re-flag
            issues.append(
                f"COLLISION (two sources -> one dest): {dst}\n    {prior}\n    {src}"
            )
            return False
        if dst.exists():
            seen_src[src] = dst  # remember the rejection so discs don't re-flag
            issues.append(f"DEST EXISTS (skipped): {src} -> {dst}")
            return False
        seen_src[src] = dst
        seen_dst[dst] = src
        moves.append(Move(src, dst, kind))
        return True

    # First pass: classify every record once and learn the library's existing
    # genre vocabulary from the albums already at Genre/Artist/Album depth. That
    # derived set gates placement below; an empty set (a flat library) disables
    # gating so the original flat -> genre conversion still works.
    classified = [
        (
            Path(rec.path),
            (rec.genre or "").strip(),
            classify(Path(rec.path), root, staging),
        )
        for rec in sorted(records, key=lambda r: r.path)
    ]
    allowed_genres = {info[1] for _p, _g, info in classified if info[0] == "organized"}
    gating = bool(allowed_genres) and not allow_new_genre
    # Case-insensitive view of the vocabulary: a stray tagged "hip hop" files
    # into an existing "Hip Hop" folder (reusing its spelling) instead of being
    # flagged UNKNOWN or, worse, minting a case-variant duplicate top level.
    allowed_by_fold = {g.casefold(): g for g in sorted(allowed_genres)}

    def vocab_spelling(safe_genre: str) -> str | None:
        return allowed_by_fold.get(safe_genre.casefold())

    for path, genre, info in classified:
        kind = info[0]
        if kind == "skip":
            issues.append(f"SKIP ({info[1]}): {path}")
            continue
        if kind == "toodeep":
            issues.append(
                f"TOO DEEP (skipped): {path}\n"
                f"    {info[1]} levels under root; expected Genre/Artist/Album. "
                "Wrong root?"
            )
            continue
        if kind == "staged-toodeep":
            issues.append(
                f"STAGED TOO DEEP (left in inbox): {path}\n"
                f"    {info[1]} levels under the inbox; expected Artist/Album "
                "(discs inside the album folder)."
            )
            continue
        genre, ignored = split_multi_genre(genre)
        if ignored:
            issues.append(
                f"MULTI-GENRE TAG (using {genre!r}, ignoring {ignored!r}): {path}"
            )
        if not genre:
            issues.append(f"NO GENRE (skipped): {path}")
            continue
        if only_genres is not None and genre not in only_genres:
            continue
        safe_genre = sanitize_component(genre)

        if kind == "organized":
            current_genre = info[1]
            if safe_genre.casefold() == current_genre.casefold():
                continue  # already filed under its genre (case-insensitively)
            if not refile_mismatched:
                issues.append(
                    f"NOTE: filed under {current_genre!r} but tags say {genre!r} "
                    f"(left in place): {path}"
                )
                continue
            # Re-filing goes through the same vocabulary gate as a stray: the
            # tag's genre must already exist as an organized folder (any
            # casing, reusing its spelling) unless new genres are allowed.
            existing = vocab_spelling(safe_genre)
            if existing is not None:
                safe_genre = existing
            elif gating:
                issues.append(
                    f"UNKNOWN GENRE {genre!r} (left in place): {path}\n"
                    "    not an existing library genre; pass --allow-new-genre "
                    "to create it."
                )
                continue
            artist, album = info[2], info[3]
            # Built from the classified parts, not `path`: a depth-4 disc
            # record must move its parent album as the unit.
            src = root / current_genre / artist / album
            dst = root / safe_genre / artist / album
            if add(src, dst, "dir"):
                source_artist_dirs.add(src.parent)
                album_artist_genres.setdefault(src.parent, Counter())[genre] += 1
            continue

        # A genre the library already uses (any casing) reuses the existing
        # folder's spelling; one it doesn't is refused a new top-level folder
        # unless explicitly allowed.
        existing = vocab_spelling(safe_genre)
        if existing is not None:
            safe_genre = existing
        elif gating:
            issues.append(
                f"UNKNOWN GENRE {genre!r} (skipped): {path}\n"
                "    not an existing library genre; pass --allow-new-genre to create it."
            )
            continue

        if kind == "album":
            artist, album = info[1], info[2]
            rel = path.relative_to(root).parts
            if staging and rel and rel[0] == staging:
                rel = rel[1:]
            # A depth-3 record here is a disc subfolder (classify collapsed it
            # to its parent album); the parent dir is the unit that moves.
            src = path.parent if len(rel) == 3 else path
            dst = root / safe_genre / artist / album
            if dst == src:
                # Only reachable when the genre folder name equals the staging
                # inbox name (dst recomputes to the same path inside the inbox).
                continue
            # Bookkeeping only for a move that was actually planned: a
            # rejected album must not have its artist-level sidecars moved
            # out from under it, or its source dir registered for pruning.
            if add(src, dst, "dir"):
                source_artist_dirs.add(src.parent)
                album_artist_genres.setdefault(src.parent, Counter())[genre] += 1
        else:  # loose
            artist = info[1]
            dst_dir = root / safe_genre / artist / SINGLES_DIR
            loose_files = sorted(p for p in _safe_iterdir(path) if p.is_file())
            planned = sum(1 for f in loose_files if add(f, dst_dir / f.name, "file"))
            if planned:
                source_artist_dirs.add(path)
            # Marked regardless: the sidecar pass must not re-handle a loose
            # dir's direct files even when their moves were all rejected.
            loose_dirs.add(path)

    # Artist-level sidecars: files (cover art, .nfo, ...) sitting in an Artist/
    # folder beside its album subfolders. They belong to no single album, so
    # moving the albums out would orphan them in an otherwise-empty folder.
    # Relocate them to the artist's folder under its dominant genre. Skipped for
    # dirs that are themselves loose records — their direct files already went
    # to Singles above.
    for artist_dir, genres in album_artist_genres.items():
        if artist_dir in loose_dirs:
            continue
        sidecars = sorted(p for p in _safe_iterdir(artist_dir) if p.is_file())
        if not sidecars:
            continue
        dominant = genres.most_common(1)[0][0]
        if only_genres is not None and dominant not in only_genres:
            continue
        if len(genres) > 1:
            issues.append(
                f"NOTE: {len(sidecars)} artist-level file(s) in {artist_dir.name} "
                f"-> dominant genre {dominant!r} (artist spans {len(genres)} genres)"
            )
        safe_dom = sanitize_component(dominant)
        dst_dir = root / (vocab_spelling(safe_dom) or safe_dom) / artist_dir.name
        for f in sidecars:
            add(f, dst_dir / f.name, "file")

    return moves, issues, source_artist_dirs


def _safe_iterdir(p: Path) -> list[Path]:
    try:
        return list(p.iterdir())
    except OSError:
        return []


# ============================ lattice (read half) ============================


def scan_album_dirs(directory: Path, quiet: bool, where=None):
    # Scan against the *current* on-disk layout so the path-fallback fills in
    # any untagged file's artist/album; genre always comes from the tag.
    roots = as_roots(str(directory))
    pbar = _make_pbar(count_audio_files(roots), "Scanning", quiet)
    dirs = _scan_album_dirs(roots, "{artist}/{album}", pbar, where=where)
    pbar.close()
    return dirs


# ============================ execution ============================


class Runner:
    """Performs (or, in dry-run, narrates) the planned moves and appends each
    real move to an append-only manifest used by --revert.

    The dry-run virtual filesystem is the shared lattice.vfs core (the same
    one lattice.modes.clean's Run plans against), so the preview's emptiness
    checks, prune predictions, and stats match the apply run."""

    def __init__(self, manifest_path: Path, dry_run: bool, quiet: bool):
        self.manifest_path = manifest_path
        self.dry_run = dry_run
        self.quiet = quiet
        self.vfs = VirtualFS(dry_run)
        self.mf = None
        self.stats: Counter = Counter()

    # The virtual state lives on the shared core; these attributes keep the
    # historical names the tests (and revert) read.
    @property
    def removed(self) -> set[Path]:
        return self.vfs.removed

    @property
    def created(self) -> dict[Path, Path]:
        return self.vfs.created

    def _effective_children(self, directory: Path) -> list[Path]:
        return self.vfs.effective_children(directory)

    def __enter__(self):
        if not self.dry_run:
            fresh = not self.manifest_path.exists()
            self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            self.mf = self.manifest_path.open("a", encoding="utf-8")
            if fresh:
                self.mf.write("# lattice genreMap manifest — src<TAB>dst<TAB>time\n")
                self.mf.write(
                    "# revert with: lattice --genreMap --revert <this file> --apply\n"
                )
        return self

    def __exit__(self, *_exc):
        if self.mf:
            self.mf.close()

    def _emit(self, msg: str) -> None:
        if not self.quiet:
            prefix = "[DRY] " if self.dry_run else ""
            ui.tqdm.write(f"{prefix}{msg}")

    def _cross_device(self, src: Path, dst: Path) -> bool:
        """Would moving src to dst cross a filesystem boundary? The destination
        may not exist yet, so compare against its nearest existing ancestor."""
        try:
            anchor = dst.parent
            while not anchor.exists():
                anchor = anchor.parent
            return src.stat().st_dev != anchor.stat().st_dev
        except OSError:
            return False

    def do_move(self, src: Path, dst: Path, kind: str) -> None:
        # shutil.move silently degrades to copy+delete across devices, against
        # the mv-only contract (audio bytes are never rewritten); refuse
        # instead of copying — in a dry-run too, so the preview predicts the
        # refusal. mkdir only happens after the check (and only in a real
        # run), so a refused move leaves no stray empty destination tree.
        if self._cross_device(src, dst):
            self._emit(f"CROSS-DEVICE (refused): {src}  ->  {dst}")
            self.stats["cross_device_refused"] += 1
            return
        self._emit(f"MV {kind}: {src}  ->  {dst}")
        self.stats[f"moved_{kind}"] += 1
        if not self.dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
        self.vfs.move(src, dst)
        if not self.dry_run and self.mf:
            ts = datetime.now().isoformat(timespec="seconds")
            self.mf.write(f"{src}\t{dst}\t{ts}\n")
            self.mf.flush()

    def prune_empty(self, directory: Path) -> None:
        """Remove `directory` and any now-empty subdirectories beneath it.
        Used to clear out source artist folders vacated by the moves; a folder
        that still holds files (or album subfolders not yet moved) is kept."""
        if not directory.is_dir() or directory in self.vfs.removed:
            return
        for child in self._effective_children(directory):
            if child.is_dir():
                self.prune_empty(child)
        if not self._effective_children(directory):
            self._emit(f"RMDIR (emptied): {directory}")
            self.stats["pruned"] += 1
            if self.dry_run:
                self.vfs.removed.add(directory)
            else:
                try:
                    directory.rmdir()
                except OSError:
                    pass

    def prune_up(self, directory: Path) -> None:
        """Remove `directory` and walk upward, removing each emptied ancestor
        until one still holds entries (the populated library root stops it).
        Used after a revert to clear the now-empty genre/artist/album dirs."""
        cur = directory
        while cur != cur.parent and cur.is_dir() and not self._effective_children(cur):
            self._emit(f"RMDIR (emptied): {cur}")
            self.stats["pruned"] += 1
            parent = cur.parent
            if self.dry_run:
                self.vfs.removed.add(cur)
            else:
                try:
                    cur.rmdir()
                except OSError:
                    break
            cur = parent


def execute(
    moves,
    source_artist_dirs,
    runner: Runner,
    root: Path | None = None,
    staging: str | None = None,
) -> None:
    for mv in ui.tqdm(moves, desc=ui.info("Moving directories")):
        runner.do_move(mv.src, mv.dst, mv.kind)
    # One prune pass after ALL moves (a source dir is only removable once every
    # album inside it has moved out), deepest-first so a nested source empties
    # before its parent is considered.
    for d in ui.tqdm(
        sorted(source_artist_dirs, key=lambda p: len(p.parts), reverse=True),
        desc=ui.info("Pruning emptied directories"),
    ):
        runner.prune_empty(d)
    # A genre folder emptied by moving its last artist out (--refile-mismatched)
    # is pruned too, like revert's upward prune; never the library root itself,
    # and never the staging inbox (documented to stay in place for next time).
    if root is not None:
        parents = {d.parent for d in source_artist_dirs}
        for p in sorted(parents, key=lambda q: len(q.parts), reverse=True):
            if staging and p == root / staging:
                continue
            if p != root and root in p.parents:
                runner.prune_empty(p)


def parse_manifest(lines) -> list[tuple[str, str]]:
    """Parse manifest lines into (src, dst) pairs, skipping comments/blanks."""
    pairs = []
    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) >= 2:
            pairs.append((cols[0], cols[1]))
    return pairs


def revert(manifest_path: Path, dry_run: bool, quiet: bool) -> int:
    if not manifest_path.is_file():
        print(f"error: no manifest at {manifest_path}", file=sys.stderr)
        return 1
    pairs = parse_manifest(manifest_path.read_text(encoding="utf-8").splitlines())
    # The Runner context gives a revert the same guarantees as an apply: each
    # restore goes through do_move, so it is logged, appended to the
    # .revert.tsv manifest (itself replayable), counted, and — in a dry-run —
    # recorded in `removed` so prune_up predicts the prunes apply performs.
    with Runner(manifest_path.with_suffix(".revert.tsv"), dry_run, quiet) as runner:
        prune_parents: set[Path] = set()
        # Reverse order so nested moves undo cleanly (deepest dst first).
        for src, dst in ui.tqdm(reversed(pairs), desc=ui.info("Reverting moves")):
            srcp, dstp = Path(src), Path(dst)
            if not dstp.exists():
                runner._emit(f"MISSING (already reverted?): {dstp}")
                runner.stats["missing"] += 1
                continue
            if srcp.exists():
                runner._emit(f"SRC EXISTS (skipped): {srcp}")
                runner.stats["src_exists"] += 1
                continue
            runner.do_move(dstp, srcp, "revert")
            prune_parents.add(dstp.parent)
        # Clear out the genre/artist/album dirs vacated by the revert, walking
        # up from each and stopping at the first still-populated ancestor.
        for d in ui.tqdm(
            sorted(prune_parents, key=lambda p: len(p.parts), reverse=True),
            desc=ui.info("Pruning directories"),
        ):
            runner.prune_up(d)
        _print_summary(runner.stats, dry_run, label="revert", quiet=quiet)
    return 0


# ============================ mode entry points ============================


def _print_summary(
    stats: Counter, dry_run: bool, *, label: str, quiet: bool = False
) -> None:
    if quiet:
        return
    verb = "Would" if dry_run else "Done:"
    print()
    print(
        ui.info(
            f"{verb} {label} — "
            + (", ".join(f"{k}={v}" for k, v in stats.items()) or "nothing")
        )
    )


def looks_like_library(directory: Path) -> bool:
    """Cheap guard: the root has at least one subdirectory. Prevents pointing
    the tool at an empty or wrong path by mistake."""
    return any(p.is_dir() for p in _safe_iterdir(directory))


def run_genremap(
    root,
    *,
    apply: bool = False,
    only_genres=None,
    staging: str | None = STAGING_DIR,
    refile_mismatched: bool = False,
    allow_new_genre: bool = False,
    log_path=None,
    quiet: bool = False,
    where=None,
    _title: str = "lattice genreMap - Genre Restructurer",
) -> int:
    """Plan (and, with apply=True, perform) the Genre/Artist/Album
    reorganization of one library root.

    The package write mode behind `lattice --genreMap`: dry-run by default and
    --apply to perform, the same contract the companion script always had (this
    is the one fold that needed no default inversion). Dry-run prints the plan
    only; apply performs the moves and appends each to a manifest TSV (default
    <root>/genre_foldermap.manifest.tsv, --log/log_path to override) that
    `lattice --genreMap --revert <manifest> --apply` replays in reverse.
    Returns a process exit code: 1 when the root is unusable, else 0."""
    directory = Path(root).resolve()
    if not directory.is_dir():
        print(ui.error(f"{directory} is not a directory"))
        return 1
    if not looks_like_library(directory):
        print(ui.error(f"{directory} has no subfolders; refusing to run"))
        return 1

    staging = staging or None
    manifest = (
        Path(log_path) if log_path else directory / "genre_foldermap.manifest.tsv"
    )
    if not quiet:
        ui.print_header(f"{_title}{'' if apply else ' [DRY RUN]'}")
        print(f"Target: {directory}")
        print(f"Manifest: {manifest}\n")

    records = scan_album_dirs(directory, quiet, where=where)
    moves, issues, source_dirs = build_plan(
        records,
        directory,
        set(only_genres) if only_genres else None,
        allow_new_genre,
        staging,
        refile_mismatched,
    )

    if not quiet:
        if issues:
            print(ui.warn(f"--- {len(issues)} issue(s) flagged for review ---"))
            for msg in issues:
                print(ui.warn(f"  {msg}"))
            print()

        if not moves:
            print(
                ui.info(
                    "No moves to make (everything already in place, or filtered out)."
                )
            )
            return 0

    with Runner(manifest, dry_run=not apply, quiet=quiet) as runner:
        execute(moves, source_dirs, runner, root=directory, staging=staging)
        _print_summary(runner.stats, not apply, label="reorganize", quiet=quiet)
        if not quiet:
            if apply:
                print(f"Manifest: {manifest}")
            else:
                print(
                    "\nDry run — nothing moved. Re-run with --apply to perform these moves."
                )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Companion-script CLI, preserving scripts/genre_foldermap.py's historical
    contract: dry-run by default, --apply to perform, --revert to undo. The
    package mode (`lattice --genreMap`) goes through run_genremap instead, with
    the same defaults."""
    parser = argparse.ArgumentParser(
        description="Restructure a music library into Genre/Artist/Album/Song.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "directory",
        nargs="?",
        help="Music library root (e.g. /mnt/SharedData/Music)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the moves. Without this flag the tool only prints the plan.",
    )
    parser.add_argument(
        "--only-genre",
        action="append",
        metavar="GENRE",
        help="Restrict to this genre (repeatable). Useful for a staged rollout. "
        "Note: an artist-level sidecar (e.g. cover.jpg) follows its artist's "
        "dominant genre, which may not be one you selected.",
    )
    parser.add_argument(
        "--staging",
        metavar="DIR",
        default=STAGING_DIR,
        help=f"Name of a top-level staging inbox (default: {STAGING_DIR!r}) whose "
        "Artist/Album contents are filed into the real taxonomy instead of being "
        "read as a genre. The inbox folder itself is left in place. Pass an empty "
        "string to disable.",
    )
    parser.add_argument(
        "--refile-mismatched",
        action="store_true",
        help="Move an already-organized album whose tag genre disagrees with "
        "its genre folder to the tag's folder (still gated by the existing "
        "genre vocabulary). Without it such albums are reported as NOTEs and "
        "left in place. Meant for after a retag pass: correct the tags first, "
        "then let this re-file the folders to match.",
    )
    parser.add_argument(
        "--allow-new-genre",
        action="store_true",
        help="Permit creating a new top-level genre folder when an album's genre "
        "isn't one the library already uses. Without it, such albums are flagged "
        "and skipped; the library's existing genre folders gate placement.",
    )
    parser.add_argument(
        "--log",
        dest="log_path",
        default=None,
        help="Manifest path (default: <directory>/genre_foldermap.manifest.tsv)",
    )
    parser.add_argument(
        "--revert",
        metavar="MANIFEST",
        default=None,
        help="Undo a prior run by replaying its manifest in reverse, then exit. "
        "Dry-run by default, like a forward run: add --apply to execute.",
    )
    parser.add_argument("--quiet", action="store_true", help="Minimize output")
    args = parser.parse_args(argv)

    if args.revert:
        if not args.quiet:
            ui.print_header(
                "genre_foldermap.py - Genre Restructurer"
                + (" [DRY RUN]" if not args.apply else "")
            )
        return revert(
            Path(args.revert).resolve(), dry_run=not args.apply, quiet=args.quiet
        )
    if not args.directory:
        parser.error("directory is required (or use --revert MANIFEST)")
    return run_genremap(
        args.directory,
        apply=args.apply,
        only_genres=args.only_genre,
        staging=args.staging,
        refile_mismatched=args.refile_mismatched,
        allow_new_genre=args.allow_new_genre,
        log_path=args.log_path,
        quiet=args.quiet,
        _title="genre_foldermap.py - Genre Restructurer",
    )


if __name__ == "__main__":
    sys.exit(main())
