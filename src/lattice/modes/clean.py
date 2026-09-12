"""Fragmented-album consolidation and name/tag normalization (the clean passes).

Promoted from scripts/cleaner.py (v1.5.0), which now launches this module as a
compat shim. Four passes over a library root:

  1. Artist-folder consolidation (e.g. 'Jay-Z & Kanye West' vs 'JAY‐Z & Kanye West')
  2. Album-folder consolidation within each artist directory
  3. (--normalize-names / --normalize-filenames) rename every remaining folder
     (any depth) and/or audio file whose name uses non-standard characters to
     its normalized form
  4. (--normalize-tags) library-wide tag normalization, all formats
     (MP3/FLAC/Ogg/Opus/m4a/WMA; other audio is reported and skipped).
     Title/album get a pure typographic fold everywhere (the words never
     change, so the folder is never an authority for them). Artist/albumartist
     fold the same way, except under a merged or renamed artist folder, where
     they are restamped to the surviving folder name: the folder depth of the
     artist component is read from the layout (default {artist}/{album}), and
     the survivor is the naming authority, so a merged-in variant like
     'Bonnie Prince Billy' (no quotes) and a CP1252-mojibake
     'Bonnie \x93Prince\x94 Billy' both become the survivor's
     "Bonnie 'Prince' Billy" in the tags. Guest credits are kept
     ("... feat. X"); the punctuation is folded to straight ASCII. Two players
     read APEv2 over ID3, so run the apestrip mode first if a stray APE tag is
     in play.

Conservative by design — folders whose normalized names don't match are
never touched, even if they're "obviously" the same album. Cases like
'Domestica' vs 'Cursive's Domestica (Deluxe Edition)' require manual
intervention.

Dry-run fidelity: existence/size checks go through virtual-aware views of the
filesystem (removals AND creations this run would have made), so the preview's
decisions and stats match the apply run. Residual limitation: the *contents*
of a folder that only virtually moved are not modeled recursively, so a
pathological chain of merges-into-merged-folders may still preview
imperfectly; every normal fragment/rename shape is exact.

This is one of the package's two write modes (the other is
lattice.modes.apestrip). The entry points differ deliberately: `run_clean`
(the `lattice --clean` mode) dry-runs by default and applies only on an
explicit opt-in, while `main` keeps the companion script's historical
apply-by-default contract for `scripts/cleaner.py`. Every write is recorded
in an append-only timestamped log (default <root>/cleanup.log).
"""

import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import mutagen
from mutagen.asf import ASF
from mutagen.id3 import ID3, TALB, TIT2, TPE1, TPE2, ID3NoHeaderError
from mutagen.mp4 import MP4
from vir_tui import core as ui

from lattice.modes.artwork import _get_image_size
from lattice.norm import (
    _base_artist,
    canonical_render,
    canon_track_artist,
    is_legal_name,
    normalize_name,
    tag_dedupe,
    tag_fold,
)

# Containers whose title/album/artist/albumartist the tag pass can rewrite. Other
# AUDIO_EXT members (.wav/.aac/.alac/.ape/.wv/.aiff) carry no handled tag layout
# and are reported + skipped rather than silently no-oped.
TAGGABLE_EXT = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".mp4", ".wma"}

# Superset of config.AUDIO_EXTENSIONS (the read-only scan set) plus rarer
# lossless containers (.alac/.ape/.wv/.aiff) — a collision on any of these must
# keep both copies. ".mp4" is deliberately absent from this set (ambiguous with
# video, so a colliding .mp4 is treated as non-audio) while staying taggable
# above.
AUDIO_EXT = {
    ".mp3",
    ".opus",
    ".flac",
    ".wav",
    ".m4a",
    ".ogg",
    ".aac",
    ".alac",
    ".ape",
    ".wv",
    ".aiff",
    ".wma",
}

IMAGE_EXT = {".jpg", ".jpeg", ".png"}


class Run:
    def __init__(
        self,
        root: Path,
        log_path: Path,
        dry_run: bool,
        normalize_tags: bool = False,
        artist_depth: int = 1,
    ):
        self.root = root
        self.dry_run = dry_run
        self.normalize_tags = normalize_tags
        # Path depth (components below root) at which a folder names an artist,
        # derived from the layout. Only folders at this depth seed the tag pass.
        self.artist_depth = artist_depth
        self.log_file = log_path.open("a", encoding="utf-8")
        # Paths (virtually) removed/created this run; lets dry-run existence
        # and emptiness checks predict the real outcome instead of seeing the
        # unchanged filesystem. `created` maps each virtual destination to the
        # real on-disk path currently holding its bytes, so size/kind checks
        # against a not-yet-moved file still read real data.
        self.removed: set[Path] = set()
        self.created: dict[Path, Path] = {}
        # (canonical_artist_name, [folders to walk]) seeded by merges/renames;
        # consumed by the Pass-4 tag pass. Folders are filtered for existence at
        # walk time, so dry-run (sources still present) and apply (sources gone,
        # everything under the survivor) both resolve to the same file set.
        self.tag_targets: list[tuple[str, list[Path]]] = []
        self.stats = {
            "groups": 0,
            "moves": 0,
            "collisions_kept": 0,
            "covers_replaced": 0,
            "non_audio_dropped": 0,
            "exact_dupes_dropped": 0,
            "renamed": 0,
            "rmdirs": 0,
            "files_renamed": 0,
            "tags_rewritten": 0,
            "tag_files_scanned": 0,
            "tag_unsupported_skipped": 0,
            "tag_no_id3_skipped": 0,
        }

    def _is_artist_level(self, p: Path) -> bool:
        try:
            return len(p.relative_to(self.root).parts) == self.artist_depth
        except ValueError:
            return False

    def _record_tag_target(self, name: str, folders: list[Path]) -> None:
        if self.normalize_tags:
            self.tag_targets.append((tag_fold(name), folders))

    def log(self, msg: str = "") -> None:
        ts = datetime.now().isoformat(timespec="seconds")
        prefix = "[DRY] " if self.dry_run else ""
        if msg.startswith("\n"):
            self.log_file.write("\n")
            msg = msg.lstrip("\n")

        if msg:
            disp_msg = msg
            if msg.startswith("--- PASS"):
                disp_msg = ui.color(msg, ui.BOLD + ui.CYAN)
            elif " SKIP" in msg:
                disp_msg = ui.warn(msg.strip())
            elif " TAG:" in msg or " TAG ERROR" in msg:
                disp_msg = ui.info(msg.strip())
            ui.tqdm.write(disp_msg)

        line = f"[{ts}] {prefix}{msg}" if msg else ""
        self.log_file.write(line + "\n")
        self.log_file.flush()

    def close(self) -> None:
        self.log_file.close()

    # ------- filesystem ops with dry-run guards -------

    def _effective_children(self, p: Path) -> list[Path]:
        """Children of p adjusted for this run's virtual removals/creations,
        so a dry-run predicts whether p would really be empty."""
        try:
            kids = [c for c in p.iterdir() if c not in self.removed]
        except OSError:
            return []
        kids += [c for c in self.created if c.parent == p and c not in kids]
        return kids

    # Virtual-aware filesystem views: identical to the plain calls during an
    # apply run (removed/created stay empty), but a dry-run sees the state the
    # apply run would have produced so far, which keeps collision and rename
    # decisions (and therefore the stats) identical between the two.

    def _real(self, p: Path) -> Path:
        """The on-disk path currently holding p's bytes (p itself unless p is
        a virtual destination of this dry-run)."""
        return self.created.get(p, p)

    def _exists(self, p: Path) -> bool:
        return p in self.created or (p.exists() and p not in self.removed)

    def _is_file(self, p: Path) -> bool:
        real = self.created.get(p)
        if real is not None:
            return real.is_file()
        return p.is_file() and p not in self.removed

    def _is_dir(self, p: Path) -> bool:
        real = self.created.get(p)
        if real is not None:
            return real.is_dir()
        return p.is_dir() and p not in self.removed

    def _size(self, p: Path) -> int:
        return self._real(p).stat().st_size

    def _move(self, src: Path, dst: Path) -> None:
        if self.dry_run:
            origin = self.created.pop(src, src)
            self.removed.add(src)
            self.created[dst] = origin
            return
        shutil.move(str(src), str(dst))

    def _unlink(self, p: Path) -> None:
        if self.dry_run:
            self.removed.add(p)
            self.created.pop(p, None)
            return
        p.unlink()

    def _rmdir(self, p: Path) -> bool:
        if self.dry_run:
            if self._effective_children(p):
                return False
            self.removed.add(p)
            return True
        try:
            p.rmdir()
            return True
        except OSError:
            return False

    def _rename(self, src: Path, dst: Path) -> None:
        if self.dry_run:
            origin = self.created.pop(src, src)
            self.removed.add(src)
            self.created[dst] = origin
            return
        src.rename(dst)

    def _survives(self, p: Path) -> bool:
        """Dry-run: does p's content still exist somewhere after the virtual
        ops so far? True for untouched paths and for rename/move origins (the
        bytes live on under a new name, so later passes must still preview
        them, at their current on-disk path); False for merged-away or
        unlinked paths. Always True during an apply run (both sets empty)."""
        lineage = (p, *p.parents)
        if not any(q in self.removed for q in lineage):
            return True
        alive = set(self.created.values())
        return any(q in alive for q in lineage)


def find_groups(directory: Path, run: Run) -> list[list[Path]]:
    """Find groups of subdirs whose names normalize to the same key."""
    if not directory.is_dir():
        return []
    groups: dict[str, list[Path]] = {}
    try:
        for child in directory.iterdir():
            if child in run.removed:
                continue  # dry-run: merged away by an earlier pass
            if child.is_dir() and not child.name.startswith("."):
                key = normalize_name(child.name)
                groups.setdefault(key, []).append(child)
    except (PermissionError, OSError) as e:
        run.log(f"  WARN scan {directory}: {e}")
        return []
    return [g for g in groups.values() if len(g) > 1]


def file_count(p: Path) -> int:
    try:
        return sum(1 for f in p.rglob("*") if f.is_file())
    except OSError:  # PermissionError is an OSError subclass
        return 0


def head_tail_equal(a: Path, b: Path, chunk: int = 65536) -> bool:
    """Cheap content check for same-size files: equal first and last `chunk`
    bytes. Not a full compare — a difference confined to the middle of two
    same-size files slips through — but it catches re-encodes, retags, and
    truncation-with-padding that a size-only check calls "identical"."""
    try:
        size = a.stat().st_size
        with a.open("rb") as fa, b.open("rb") as fb:
            if fa.read(chunk) != fb.read(chunk):
                return False
            if size > chunk:
                fa.seek(max(0, size - chunk))
                fb.seek(max(0, size - chunk))
                return fa.read(chunk) == fb.read(chunk)
        return True
    except OSError:
        return False


def image_pixels(path: Path) -> int | None:
    """Pixel count (w*h) of an image file, or None if it is not a parseable
    JPEG/PNG. Used to keep the higher-resolution cover on a collision."""
    try:
        with path.open("rb") as f:
            dims = _get_image_size(f.read())
    except OSError:
        return None
    return dims[0] * dims[1] if dims else None


def merge_dir(source: Path, target: Path, run: Run) -> None:
    """Merge source contents into target, recursing into subdirs. Existence
    and size checks go through the Run's virtual-aware views so a dry-run
    makes the same decisions the apply run will."""
    for item in list(source.iterdir()):
        target_item = target / item.name
        if run._exists(target_item):
            if item.is_dir() and run._is_dir(target_item):
                merge_dir(item, target_item, run)
                if run._rmdir(item):
                    run.stats["rmdirs"] += 1
                    run.log(f"    RMDIR (after recursive merge): {item}")
                else:
                    run.log(f"    RETAIN (subdir not empty): {item}")
            elif item.is_file() and run._is_file(target_item):
                src_size = item.stat().st_size
                tgt_size = run._size(target_item)
                identical = src_size == tgt_size and head_tail_equal(
                    item, run._real(target_item)
                )
                if identical:
                    run.log(
                        f"    DROP DUPE (identical size + sampled bytes, "
                        f"{src_size}B): {item}"
                    )
                    run._unlink(item)
                    run.stats["exact_dupes_dropped"] += 1
                else:
                    if item.suffix.lower() in AUDIO_EXT:
                        stem = item.stem
                        suffix = item.suffix
                        new_target = target / f"{stem}.from-fragment{suffix}"
                        counter = 1
                        while run._exists(new_target):
                            counter += 1
                            new_target = (
                                target / f"{stem}.from-fragment-{counter}{suffix}"
                            )
                        run._move(item, new_target)
                        run.stats["collisions_kept"] += 1
                        run.log(
                            f"    AUDIO COLLISION (kept both): {item.name} "
                            f"({src_size}B) -> {new_target.name} "
                            f"vs existing ({tgt_size}B)"
                        )
                    elif item.suffix.lower() in IMAGE_EXT:
                        # Keep the better cover instead of blindly keeping
                        # canonical's: more pixels wins, ties (or unparseable)
                        # fall back to larger bytes.
                        src_px = image_pixels(item)
                        tgt_px = image_pixels(run._real(target_item))
                        if src_px is not None and tgt_px is not None:
                            source_wins = (src_px, src_size) > (tgt_px, tgt_size)
                        elif (src_px is None) != (tgt_px is None):
                            # Exactly one side parses as an image: it wins. A
                            # larger-but-unparseable blob must not replace a
                            # legitimate cover on byte count alone.
                            source_wins = src_px is not None
                        else:
                            source_wins = src_size > tgt_size
                        if source_wins:
                            run._unlink(target_item)
                            run._move(item, target_item)
                            run.stats["covers_replaced"] += 1
                            run.log(
                                f"    REPLACE IMAGE (higher-res source kept): "
                                f"{item.name}  src={src_px}px/{src_size}B "
                                f"tgt={tgt_px}px/{tgt_size}B"
                            )
                        else:
                            run._unlink(item)
                            run.stats["non_audio_dropped"] += 1
                            run.log(
                                f"    DROP IMAGE (canonical higher-res): "
                                f"{item.name}  src={src_px}px/{src_size}B "
                                f"tgt={tgt_px}px/{tgt_size}B"
                            )
                    else:
                        run.log(
                            f"    DROP NON-AUDIO ({item.suffix}, "
                            f"src={src_size}B tgt={tgt_size}B): {item}"
                        )
                        run._unlink(item)
                        run.stats["non_audio_dropped"] += 1
            else:
                run.log(f"    SKIP (type mismatch dir-vs-file): {item.name}")
        else:
            run._move(item, target_item)
            run.stats["moves"] += 1
            run.log(f"    MV: {item.name}")


def _rename_to(path: Path, target_name: str, run: Run, *, kind: str) -> Path:
    """Shared rename-with-guards for folder and file normalization: legality
    check, virtual-aware collision guard (in a dry-run a merged-away source
    still exists on disk, and an earlier virtual rename may already occupy the
    target), error containment (a filesystem-rejected name logs and is skipped,
    never aborting the run), stats and logging. Returns the (possibly
    unchanged) path."""
    folder_kind = kind == "folder"
    if target_name == path.name:
        return path
    if not is_legal_name(target_name):
        label = "RENAME" if folder_kind else "RENAME FILE"
        run.log(f"    SKIP {label} (illegal target name): {path.name}")
        return path
    dst = path.parent / target_name
    if run._exists(dst) and dst != path:
        retain = "RETAIN NAME" if folder_kind else "RETAIN FILE NAME"
        run.log(f"    {retain} (normalized target exists): {path.name}")
        return path
    try:
        run._rename(path, dst)
    except OSError as e:
        noun = "" if folder_kind else "file "
        run.log(f"    ERROR rename {noun}{path.name} -> {target_name}: {e}")
        return path
    if folder_kind:
        run.stats["renamed"] += 1
        run.log(f"    RENAME: {path.name}  ->  {target_name}")
    else:
        run.stats["files_renamed"] += 1
        run.log(f"    RENAME FILE: {path.name}  ->  {target_name}")
    return dst


def _normalize_folder_name(folder: Path, run: Run, record_tags: bool = True) -> Path:
    """Rename `folder` to its canonical_render when they differ. Used for both
    merge survivors and the --normalize-names sweep. A renamed artist folder is
    also a tag target for the Pass-4 sweeps; Pass 1/2 survivor renames pass
    record_tags=False because consolidate_group records its own entry (which
    also includes the merged sources), so the survivor isn't recorded twice."""
    dst = _rename_to(folder, canonical_render(folder.name), run, kind="folder")
    if (
        record_tags
        and dst != folder
        and run.normalize_tags
        and run._is_artist_level(dst)
    ):
        # dst exists after an apply rename, the original after a dry-run one;
        # both are offered to walk.
        run._record_tag_target(dst.name, [dst, folder])
    return dst


def consolidate_group(folders: list[Path], context: str, run: Run) -> None:
    counts = {p: file_count(p) for p in folders}  # one rglob walk per folder
    folders_sorted = sorted(folders, key=lambda p: (-counts[p], p.name))
    canonical = folders_sorted[0]
    sources = folders_sorted[1:]
    run.log(f"  GROUP @ {context}")
    run.log(f"    canonical: {canonical.name}  ({counts[canonical]} files)")
    for s in sources:
        run.log(f"    source:    {s.name}  ({counts[s]} files)")
    run.stats["groups"] += 1

    for source in sources:
        run.log(f"  MERGING: {source.name}  ->  {canonical.name}")
        merge_dir(source, canonical, run)
        remaining = run._effective_children(source)
        if not remaining:
            if run._rmdir(source):
                run.stats["rmdirs"] += 1
                run.log(f"    RMDIR: {source}")
        else:
            run.log(
                f"    RETAIN (not empty after merge, {len(remaining)} items): {source}"
            )

    # The folder with the most files won as canonical, but its name may be the
    # less-standard variant (unicode hyphen, curly quote); normalize the
    # survivor. record_tags=False: the recording below covers the rename case
    # too and also carries the sources.
    survivor = _normalize_folder_name(canonical, run, record_tags=False)

    # Seed the tag pass when the survivor is an artist folder: its name is the
    # naming authority for every track merged under it. Include the survivor's
    # pre-rename path and the sources so a dry-run (nothing has moved yet) still
    # walks the same files an apply run would find consolidated under survivor.
    if run.normalize_tags and run._is_artist_level(survivor):
        run._record_tag_target(survivor.name, [survivor, canonical, *sources])


def _normalize_file_name(path: Path, run: Run) -> None:
    """Rename a track file to canonical_render(stem) + suffix when they differ,
    with the same legality + collision guards as the folder rename. The extension
    is preserved verbatim."""
    _rename_to(path, canonical_render(path.stem) + path.suffix, run, kind="file")


def normalize_tree(root: Path, run: Run, do_folders: bool, do_files: bool) -> None:
    """Bottom-up rename of folder names (every depth) and/or audio file names to
    their canonical_render form. Files in a directory are renamed before the
    directory itself, and directories before their parents (os.walk topdown=False),
    so captured paths stay valid through an apply run. Rename-only: merging is
    Passes 1-2' job. `--normalize-names` drives folders, `--normalize-filenames`
    drives files; either may run alone."""
    alive_origins = set(run.created.values())
    for dirpath, _dirnames, filenames in list(os.walk(root, topdown=False)):
        d = Path(dirpath)
        # Dry-run: skip folders (and their contents) an earlier pass merged
        # away; the apply run would not find them here. A folder Pass 1
        # *renamed* survives (its bytes live on under the new name), so its
        # subtree is still previewed, at its current on-disk path.
        if not run._survives(d):
            continue
        if do_files:
            for fn in sorted(filenames):
                if fn.startswith("."):
                    continue
                fp = d / fn
                if not run._survives(fp):
                    continue  # dry-run: dropped as an exact duplicate
                if fp.suffix.lower() in AUDIO_EXT:
                    _normalize_file_name(fp, run)
        if (
            do_folders
            and d != root
            and not d.name.startswith(".")
            and d not in alive_origins  # already renamed by an earlier pass
        ):
            _normalize_folder_name(d, run)


def _planned_tag_values(
    cur: dict[str, list[str] | None],
    authority: str | None,
    global_artist_authority: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """Given the current title/album/artist/albumartist value lists (None =
    field absent), return only the fields whose values should change. Title and
    album get a pure typographic fold of every value (multi-valued tags keep
    all their values) and are never synthesized when absent. Artist/albumartist
    follow the surviving folder name when `authority` is set (and are created
    if absent, as the artist restamp did before), else a typographic fold."""
    out: dict[str, list[str]] = {}
    for field in ("title", "album", "artist", "albumartist"):
        vals = cur.get(field)
        if vals is None:
            continue
        folded = [tag_dedupe(tag_fold(v)) for v in vals]
        if folded != vals:
            out[field] = folded

    artist = out.get("artist", cur.get("artist"))
    albumartist = out.get("albumartist", cur.get("albumartist"))

    # If the file isn't in a renamed/merged artist folder, use the global authority if available.
    if not authority and global_artist_authority and artist and artist[0]:
        base = _base_artist(artist[0])
        norm_base = normalize_name(base)
        if norm_base in global_artist_authority:
            authority = global_artist_authority[norm_base]

    if authority:
        # Deliberate collapse: under a merged/renamed artist folder the
        # surviving folder name IS the artist, so a multi-valued artist tag
        # becomes the single canonical value (guest credit preserved).
        new_artist = [canon_track_artist(artist[0] if artist else "", authority)]
        if artist is None or new_artist != artist:
            out["artist"] = new_artist
        if albumartist is None or [authority] != albumartist:
            out["albumartist"] = [authority]

    return out


def _values(values) -> list[str] | None:
    """Non-empty values of a mutagen list as plain strings, unwrapping ASF
    attributes; None when the field is absent or entirely empty."""
    if not values:
        return None
    out = [str(getattr(v, "value", v)) for v in values]  # ASFUnicodeAttribute
    out = [v for v in out if v]
    return out or None


def _open_for_tags(path: Path, ext: str):
    """Return (current_fields, apply_fn) for a taggable file, or None if it has no
    readable tag block (e.g. an MP3 with no ID3 header). apply_fn(out) writes only
    the fields in `out`. Raw per-container mutagen, mirroring retag.py."""
    if ext == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            return None
        id3map = {
            "title": ("TIT2", TIT2),
            "album": ("TALB", TALB),
            "artist": ("TPE1", TPE1),
            "albumartist": ("TPE2", TPE2),
        }
        cur = {}
        for field, (fid, _cls) in id3map.items():
            frame = tags.get(fid)
            cur[field] = (
                _values([str(t) for t in frame.text]) if frame is not None else None
            )

        def apply(out):
            for field, vals in out.items():
                fid, cls = id3map[field]
                tags.setall(fid, [cls(encoding=3, text=list(vals))])
            tags.save(path, v2_version=3, v1=2)

        return cur, apply

    if ext in (".flac", ".ogg", ".opus"):
        tags = mutagen.File(path)
        if tags is None:
            raise ValueError(f"mutagen could not recognize file: {path}")
        keymap = {k.lower(): k for k in tags.keys()}

        def get(name):
            key = keymap.get(name)
            return _values(tags[key]) if key is not None else None

        cur = {f: get(f) for f in ("title", "album", "artist", "albumartist")}

        def apply(out):
            for field, vals in out.items():
                for existing in [k for k in list(tags.keys()) if k.lower() == field]:
                    del tags[existing]
                tags[field] = list(vals)
            tags.save()

        return cur, apply

    if ext in (".m4a", ".mp4"):
        tags = MP4(path)
        atom = {
            "title": "\xa9nam",
            "album": "\xa9alb",
            "artist": "\xa9ART",
            "albumartist": "aART",
        }
        cur = {f: _values(tags.get(a)) for f, a in atom.items()}

        def apply(out):
            for field, vals in out.items():
                tags[atom[field]] = list(vals)
            tags.save()

        return cur, apply

    if ext == ".wma":
        tags = ASF(path)
        keymap = {k.lower(): k for k in tags.keys()}
        asf = {
            "title": "Title",
            "album": "WM/AlbumTitle",
            "artist": "Author",
            "albumartist": "WM/AlbumArtist",
        }
        cur = {f: _values(tags.get(keymap.get(k.lower(), k))) for f, k in asf.items()}

        def apply(out):
            for field, vals in out.items():
                key = asf[field]
                # Delete case-variant originals first (like the Vorbis branch):
                # writing canonical case beside a variant would leave two keys.
                for existing in [
                    k
                    for k in list(tags.keys())
                    if k.lower() == key.lower() and k != key
                ]:
                    del tags[existing]
                tags[key] = list(vals)
            tags.save()

        return cur, apply

    return None


def normalize_file_tags(
    path: Path,
    authority: str | None,
    run: Run,
    global_artist_authority: dict[str, str] | None = None,
) -> None:
    """Typographically normalize title/album (and artist/albumartist, restamped to
    `authority` when set) on one file. No-op when nothing changes. Multi-format."""
    ext = path.suffix.lower()
    rel = path.relative_to(run.root)
    try:
        opened = _open_for_tags(path, ext)
    except Exception as e:  # a corrupt tag block must not abort the whole run
        run.log(f"    TAG ERROR (read {rel}): {e}")
        return
    if opened is None:
        # An MP3 whose tags live only in APEv2/ID3v1: reported and skipped,
        # like the unsupported-format path (the contract is never a silent skip).
        run.stats["tag_no_id3_skipped"] += 1
        run.log(f"    SKIP (no ID3 header; tags in APEv2/ID3v1 only?): {rel}")
        return
    cur, apply = opened
    out = _planned_tag_values(cur, authority, global_artist_authority)
    if not out:
        return

    def disp(v):
        # Single-valued fields (the norm) log as the bare string.
        return v[0] if isinstance(v, list) and len(v) == 1 else v

    run.stats["tags_rewritten"] += 1
    run.log(f"    TAG: {rel}")
    for field in ("title", "album", "artist", "albumartist"):
        if field in out:
            run.log(f"      {field}: {disp(cur.get(field))!r} -> {disp(out[field])!r}")
    if run.dry_run:
        return
    try:
        apply(out)
    except Exception as e:
        run.stats["tags_rewritten"] -= 1
        run.log(f"    TAG ERROR (save {rel}): {e}")


def normalize_tags(run: Run) -> None:
    """Pass 4: library-wide tag normalization. Title and album get a typographic
    fold everywhere; artist/albumartist are restamped to the surviving folder name
    under merged/renamed artist folders (the authority map) and folded elsewhere.
    Every taggable file is visited once; unsupported audio is reported and skipped."""
    run.log("\n--- PASS 4: normalize tags (library-wide) ---")

    # Artist-depth folders recorded by merges/renames map to their canonical name.
    # Sources are recorded alongside survivors, so a dry-run file under a not-yet-
    # moved source resolves to the same authority an apply run would give it.
    authority_map = {
        os.path.realpath(folder): canonical
        for canonical, folders in run.tag_targets
        for folder in folders
    }

    # Build a library-wide global artist authority from all artist-level folders.
    # If multiple unmerged folders normalize to the same name (e.g. they are in
    # different parent directories), pick the one with the most files.
    global_artist_authority: dict[str, str] = {}
    artist_folders: dict[str, list[Path]] = {}
    for p in run.root.rglob("*"):
        if not p.is_dir() or not run._survives(p):
            continue
        if run._is_artist_level(p):
            key = normalize_name(p.name)
            artist_folders.setdefault(key, []).append(p)

    for key, paths in artist_folders.items():
        if len(paths) == 1:
            global_artist_authority[key] = paths[0].name
        else:
            paths_sorted = sorted(paths, key=lambda p: (-file_count(p), p.name))
            global_artist_authority[key] = paths_sorted[0].name

    def resolve_authority(p: Path) -> str | None:
        for parent in p.parents:
            name = authority_map.get(os.path.realpath(parent))
            if name is not None:
                return name
        return None

    show_progress = sys.stderr.isatty()
    seen: set[str] = set()
    scanned = 0
    for f in sorted(run.root.rglob("*")):
        if not f.is_file():
            continue
        if not run._survives(f):
            continue  # dry-run: dropped or merged away by an earlier pass
        ext = f.suffix.lower()
        if ext not in AUDIO_EXT and ext not in TAGGABLE_EXT:
            continue
        real = os.path.realpath(f)
        if real in seen:
            continue
        seen.add(real)
        if ext not in TAGGABLE_EXT:
            run.stats["tag_unsupported_skipped"] += 1
            run.log(f"    SKIP (no tag layout, {ext}): {f.relative_to(run.root)}")
            continue
        run.stats["tag_files_scanned"] += 1
        scanned += 1
        normalize_file_tags(f, resolve_authority(f), run, global_artist_authority)
        if show_progress and scanned % 250 == 0:
            print(
                f"\r  tags: {scanned} scanned, {run.stats['tags_rewritten']} changed",
                end="",
                file=sys.stderr,
            )
    if show_progress and scanned:
        print(
            f"\r  tags: {scanned} scanned, {run.stats['tags_rewritten']} changed",
            file=sys.stderr,
        )


# run_clean exposes a `normalize_tags` flag, which shadows the pass function of
# the same name inside its body; the alias lets it invoke the pass anyway.
_normalize_tags_pass = normalize_tags


def _artist_depth_from_layout(layout: str) -> int:
    """Depth (components below root) of the {artist} component in a layout
    pattern, e.g. '{artist}/{album}' -> 1, '{genre}/{artist}/{album}' -> 2.
    Raises ValueError when the pattern names no artist level."""
    layout_parts = [p for p in layout.replace("\\", "/").split("/") if p]
    return (
        next(i for i, p in enumerate(layout_parts) if p.strip("{}").lower() == "artist")
        + 1
    )


def run_clean(
    root,
    *,
    dry_run: bool = True,
    normalize_names: bool = False,
    normalize_filenames: bool = False,
    normalize_tags: bool = False,
    layout: str = "{artist}/{album}",
    log_path=None,
    quiet: bool = False,
    _title: str = "lattice clean - Fragmented Album Consolidator",
) -> int:
    """Run the consolidation/normalization passes over one library root.

    The package write mode behind `lattice --clean`: dry-run by default, apply
    only on an explicit opt-in (dry_run=False, the CLI's --apply). Every move,
    rename, drop, and tag rewrite is recorded to an append-only timestamped log
    (default <root>/cleanup.log). Returns a process exit code: 1 when the root
    or layout is unusable or the log is unopenable, else 0.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 1

    artist_depth = 1
    if normalize_tags:
        try:
            artist_depth = _artist_depth_from_layout(layout)
        except StopIteration:
            print(
                f"error: --layout {layout!r} has no {{artist}} component",
                file=sys.stderr,
            )
            return 1

    if log_path is None:
        log_path = root / "cleanup.log"
    log_path = Path(log_path)

    if not quiet:
        ui.print_header(f"{_title}{' [DRY RUN]' if dry_run else ''}")
        print(f"Target: {root}")
        print(f"Log path: {log_path}\n")

    try:
        run = Run(
            root,
            log_path,
            dry_run=dry_run,
            normalize_tags=normalize_tags,
            artist_depth=artist_depth,
        )
    except OSError as e:
        # Run opens the log in its constructor; an unwritable path used to be a
        # bare traceback. rerate.py/replaygain.py report and exit 1.
        print(f"error: cannot open log file {log_path}: {e}", file=sys.stderr)
        return 1

    try:
        run.log("=" * 70)
        mode = "DRY RUN" if dry_run else "APPLY"
        run.log(f"CLEANUP RUN START [{mode}]: {root}")
        run.log("=" * 70)

        run.log("\n--- PASS 1: artist-level consolidation ---")
        artist_groups = find_groups(root, run)
        run.log(f"detected {len(artist_groups)} artist group(s)")
        for group in artist_groups:
            consolidate_group(group, context="artists", run=run)

        run.log("\n--- PASS 2: album-level consolidation per artist ---")
        # run._survives, not `p not in run.removed`: a survivor Pass 1 renamed
        # is in `removed` under its old name but its albums still need
        # consolidation; the apply run finds it via a plain iterdir. The
        # dry-run previews it at its current on-disk (pre-rename) path.
        artists = sorted(
            (
                p
                for p in root.iterdir()
                if p.is_dir() and not p.name.startswith(".") and run._survives(p)
            ),
            key=lambda p: p.name.lower(),
        )
        scanned = 0
        for artist_dir in artists:
            album_groups = find_groups(artist_dir, run)
            if not album_groups:
                continue
            scanned += 1
            for group in album_groups:
                consolidate_group(group, context=artist_dir.name, run=run)
        run.log(f"album-level consolidation touched {scanned} artist(s)")

        if normalize_names or normalize_filenames:
            what = " + ".join(
                w
                for w, on in (
                    ("folders", normalize_names),
                    ("filenames", normalize_filenames),
                )
                if on
            )
            run.log(f"\n--- PASS 3: normalize names ({what}) ---")
            normalize_tree(root, run, normalize_names, normalize_filenames)

        if normalize_tags:
            _normalize_tags_pass(run)

        run.log("\n--- SUMMARY ---")
        for k, v in run.stats.items():
            run.log(f"  {k}: {v}")
        run.log(f"CLEANUP RUN END [{mode}]")
        run.log("=" * 70 + "\n")

        print(ui.success(f"Cleanup run complete ({mode}). See {log_path} for details."))
        ui.print_summary(run.stats)
    finally:
        run.close()

    return 0


def main(argv: list[str] | None = None) -> int:
    """Companion-script CLI for the clean passes, preserving scripts/cleaner.py's
    historical contract: apply by default, `--dry-run` to opt out. The package
    mode (`lattice --clean`) goes through run_clean instead, which dry-runs by
    default."""
    parser = argparse.ArgumentParser(
        description="Consolidate fragmented album folders within a music library.",
        epilog="Default log: <directory>/cleanup.log",
    )
    parser.add_argument(
        "directory", help="Music library root (e.g. /mnt/SharedData/Music)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only — no filesystem changes; log lines prefixed [DRY]",
    )
    parser.add_argument(
        "--log",
        dest="log_path",
        default=None,
        help="Override log file path (default: <directory>/cleanup.log)",
    )
    parser.add_argument(
        "--normalize-names",
        action="store_true",
        help="Also rename non-duplicate folders at every depth with non-standard "
        "characters (unicode dashes, curly quotes) to their normalized form",
    )
    parser.add_argument(
        "--normalize-filenames",
        action="store_true",
        help="Also rename audio track files the same way (separate from "
        "--normalize-names since renaming files is a distinct change)",
    )
    parser.add_argument(
        "--normalize-tags",
        action="store_true",
        help="Library-wide: typographically normalize title/album tags on every "
        "file and restamp artist/albumartist to the surviving folder name under "
        "merged/renamed artist folders (all formats; Pass 4)",
    )
    parser.add_argument(
        "--layout",
        default="{artist}/{album}",
        help="Folder layout, used by --normalize-tags to locate the artist level "
        "(default '{artist}/{album}'; for a genre-foldered library pass "
        "'{genre}/{artist}/{album}')",
    )
    parser.add_argument(
        "--all",
        dest="all_passes",
        action="store_true",
        help="Run all normalization passes (--normalize-names, --normalize-filenames, --normalize-tags)",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 1.6.0")
    args = parser.parse_args(argv)

    if args.all_passes:
        args.normalize_names = True
        args.normalize_filenames = True
        args.normalize_tags = True

    return run_clean(
        args.directory,
        dry_run=args.dry_run,
        normalize_names=args.normalize_names,
        normalize_filenames=args.normalize_filenames,
        normalize_tags=args.normalize_tags,
        layout=args.layout,
        log_path=Path(args.log_path) if args.log_path else None,
        _title="cleaner.py - Fragmented Album Consolidator",
    )
