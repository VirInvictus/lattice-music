"""Delete rating tags from audio files, nonzero values only (the stripRatings
write mode).

Rips and shared downloads carry the *sharer's* player ratings: Windows Media
Player popularimeter frames, MusicBee/fre:ac POPM, FMPS floats, Vorbis RATING
stamps, MP4 rate atoms. Anything that seeds a star rating from file tags
(Conservatory's import, players that read POPM/RATING) imports pre-rated, and
the values are never the listener's own — a real library has 2- and 3-star
tracks, sharer stamps are uniformly 4/5. MusicBrainz and Picard never supply
ratings, so there is nothing on the tagging side to disable; the tags have to
go. rerate.py reconciles *scale* conflicts on kept ratings; this mode removes
ratings outright.

What is deleted (only where it carries a rating the reader would decode):

    MP3   POPM frames with a nonzero rating byte (any email)
          TXXX frames whose desc is a rating desc (shared predicate)
          APEv2 `Rating` key (players merge APEv2 over ID3; apestrip owns
          the whole-tag removal, this only takes the rating field)
    FLAC/OGG/Opus   every Vorbis key matching the rating predicate
    MP4/M4A         every atom matching the rating predicate (the `rtng`
                    Explicit/Clean advisory is not a rating and survives)
    WMA/ASF         every attribute matching the rating predicate

"Nonzero only" keeps the zero-value shells: a rating-0 POPM frame displays as
no stars anywhere and still carries the ripper's play count, and a RATING=0
key shows nothing. A file's title/artist/genre/covers and every non-rating
tag are never touched. The strip verdict is the reader's own decode
(_popm_stars / _tag_rating over the shared source predicates), so after a run
get_all_tags decodes no rating from any stripped file — reader and stripper
cannot drift.

Destructive: it rewrites tags in place. Dry-run is the default, both here and
in the `lattice --stripRatings` mode; a real run prints the worklist and asks
for confirmation. Unsupported containers (.wav, .aac) are out of scope, not
findings. Recursive over the given directory, so it handles one album or a
whole library. Idempotent: a file with no rating tags is left byte-identical.
Every removal is recorded in an append-only timestamped log (default
<root>/stripratings.log). MP3 saves use the retag convention (ID3v2.3 plus a
refreshed ID3v1). One stage of the `--ingest` ritual (run first, before
anything reorganizes the tree).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime

from mutagen import File as MutagenFile
from mutagen.apev2 import APEv2
from mutagen.asf import ASF
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from vir_tui import core as ui

from lattice.tags import (
    _popm_stars,
    _tag_rating,
    _unwrap,
    is_asf_rating_key,
    is_id3_rating_desc,
    is_mp4_rating_key,
    is_vorbis_rating_key,
)
from lattice.utils import _make_pbar, iter_audio_dirs, json_summary

# Containers whose ratings this mode strips. .wav/.aac carry no rating the
# reader decodes, so they are not walked (the apestrip convention: unsupported
# formats are out of scope, not findings).
STRIP_EXTENSIONS = (".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wma")

APE_RATING_KEY = "rating"


@dataclass
class FilePlan:
    """What one file would lose, discovered without writing."""

    path: str
    removals: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def has_changes(self) -> bool:
        return not self.error and bool(self.removals)


def _decoded(value) -> bool:
    """True when the reader decodes this value as a nonzero rating."""
    stars = _tag_rating("", value)
    return stars is not None and stars > 0


def _iter_files(root: str) -> list[str]:
    files = [
        os.path.join(dirpath, fn)
        for _r, dirpath, _dirs, filenames in iter_audio_dirs(root)
        for fn in filenames
        if os.path.splitext(fn)[1].lower() in STRIP_EXTENSIONS
    ]
    files.sort()
    return files


def _plan_mp3(path: str, fp: FilePlan, apply: bool) -> None:
    id3 = None
    try:
        id3 = ID3(path)
    except Exception:
        id3 = None  # no ID3v2: the APE pass below still runs

    saved_id3 = False
    if id3 is not None:
        for key in list(id3.keys()):
            frame = id3[key]
            if key.startswith("POPM"):
                byte = getattr(frame, "rating", 0) or 0
                stars = _popm_stars(byte) if byte else 0.0
                if stars is not None and stars > 0:
                    fp.removals.append(
                        f"POPM email={getattr(frame, 'email', '')!r} "
                        f"rating={byte} (={stars:g} stars)"
                    )
                    if apply:
                        del id3[key]
                        saved_id3 = True
            elif key.startswith("TXXX:") and is_id3_rating_desc(frame.desc):
                text = frame.text[0] if frame.text else None
                if _decoded(text):
                    fp.removals.append(f"TXXX:{frame.desc}={text!r}")
                    if apply:
                        del id3[key]
                        saved_id3 = True
        if apply and saved_id3:
            id3.save(path, v2_version=3, v1=2)

    # APEv2 rides outside ID3; its Rating field merges over ID3 in the
    # players that read both, so it goes too. The rest of the APE tag is
    # apestrip's territory.
    try:
        ape = APEv2(path)
    except Exception:
        return  # no APEv2 tag
    try:
        ape_keys = [k for k in ape if k.lower() == APE_RATING_KEY]
        for key in ape_keys:
            raw = ape[key]
            text = getattr(raw, "text", bytes(raw))
            if _decoded(text):
                fp.removals.append(f"APEv2:{key}={text!r}")
                if apply:
                    del ape[key]
        if apply and ape_keys:
            if list(ape.keys()):
                ape.save()
            else:
                ape.delete()
    except Exception as e:
        fp.error = f"APEv2: {e}"


def _plan_vorbis(path: str, audio, fp: FilePlan, apply: bool) -> None:
    for key in list(audio.tags.keys()):
        kl = key.lower()
        if not is_vorbis_rating_key(kl):
            continue
        values = audio.tags[key]
        if any(_decoded(v) for v in values):
            fp.removals.append(f"{key}={list(values)!r}")
            if apply:
                del audio.tags[key]
    if apply and fp.removals:
        audio.save()


def _plan_mp4(path: str, audio, fp: FilePlan, apply: bool) -> None:
    for key in list(audio.tags.keys()):
        kl = key.lower() if isinstance(key, str) else str(key).lower()
        if not is_mp4_rating_key(kl):
            continue
        raw = _unwrap(audio.tags[key])
        if _decoded(raw):
            fp.removals.append(f"{key}={raw!r}")
            if apply:
                del audio.tags[key]
    if apply and fp.removals:
        audio.save()


def _plan_asf(path: str, audio, fp: FilePlan, apply: bool) -> None:
    for key in list(audio.tags.keys()):
        kl = key.lower()
        if not is_asf_rating_key(kl):
            continue
        raw = _unwrap(audio.tags[key])
        if _decoded(raw):
            fp.removals.append(f"{key}={raw!r}")
            if apply:
                del audio.tags[key]
    if apply and fp.removals:
        audio.save()


def strip_ratings_from_file(path: str, apply: bool = False) -> FilePlan:
    """Plan (apply=False) or perform (apply=True) the rating strip on one
    file. Unreadable files come back as an error plan; anything without a
    supported container is skipped silently by the caller's extension filter.
    """
    fp = FilePlan(path=path)
    try:
        audio = MutagenFile(path)
        if audio is None:
            return fp
        ext = os.path.splitext(path)[1].lower()
        if ext == ".mp3":
            _plan_mp3(path, fp, apply)
        elif isinstance(audio, MP4):
            _plan_mp4(path, audio, fp, apply)
        elif isinstance(audio, (FLAC, OggVorbis, OggOpus)):
            _plan_vorbis(path, audio, fp, apply)
        elif isinstance(audio, ASF):
            _plan_asf(path, audio, fp, apply)
    except Exception as e:
        fp.error = str(e)
    return fp


def run_stripratings(
    root,
    *,
    dry_run: bool = True,
    log_path=None,
    assume_yes: bool = False,
    quiet: bool = False,
    json_mode: bool = False,
    protected_roots: list[str] | None = None,
    _title: str = "lattice stripRatings - Rating Tag Stripper",
) -> int:
    """Run the rating strip over one directory tree.

    The package write mode behind `lattice --stripRatings`: dry-run by
    default, apply only on an explicit opt-in (dry_run=False, the CLI's
    --apply). A real run keeps the house two-phase shape: a dry planning pass
    builds the worklist, the worklist is printed, and (unless assume_yes or
    stdin is not a TTY) a confirmation prompt guards the write pass — a
    non-interactive run proceeds, so cron and pipelines keep working. Every
    removal is recorded to an append-only timestamped log (default
    <root>/stripratings.log). Returns a process exit code: 2 when the root is
    rating-protected, 1 when the root is unusable, the log is unopenable, or
    any file errored (read or write), else 0.

    Rating protection (the v6.4.1 guard, Brandon's standing rule after the
    2026-10-10 hand-ratings incident): a real run refuses any root at or
    under a config `rating_protected_roots` entry, except through a staging
    inbox (a path component naming the staging dir, default Unfiltered) —
    that is where the ritual strips by design. Dry-run previews are never
    blocked: seeing what a strip WOULD do is always safe.
    """
    root = str(root)
    if not dry_run:
        from lattice.config import get_rating_protected_roots
        from lattice.modes.foldermap import STAGING_DIR

        if protected_roots is None:
            protected_roots = get_rating_protected_roots()
        root_abs = os.path.abspath(root)
        for protected in protected_roots:
            if root_abs == protected or root_abs.startswith(protected + os.sep):
                rel_parts = os.path.relpath(root_abs, protected).split(os.sep)
                if STAGING_DIR not in rel_parts:
                    print(
                        f"error: refusing --apply: {root_abs} is under the "
                        f"rating-protected root {protected} "
                        "(config rating_protected_roots); a library this "
                        "guarded may carry its owner's own hand ratings in "
                        "its tags. Dry-run previews still work; the staging "
                        f"inbox ({STAGING_DIR}/) inside a protected tree "
                        "stays strippable; edit rating_protected_roots in "
                        "~/.config/lattice/config.json to change it.",
                        file=sys.stderr,
                    )
                    return 2
                break

    if not os.path.isdir(root):
        print(f"[!] Directory not found: {root}", file=sys.stderr)
        return 1

    if log_path is None:
        log_path = os.path.join(root, "stripratings.log")

    if not quiet and not json_mode:
        ui.print_header(f"{_title}{' [DRY RUN]' if dry_run else ''}")
        print(f"Target: {root}\n")

    log_fh = None
    if not dry_run:
        try:
            log_fh = open(log_path, "a", encoding="utf-8")
        except OSError as e:
            print(ui.error(f"cannot open log file {log_path}: {e}"), file=sys.stderr)
            return 1

    def log(msg: str) -> None:
        if not json_mode:
            ui.tqdm.write(msg)
        if log_fh is not None:
            ts = datetime.now().isoformat(timespec="seconds")
            log_fh.write(f"[{ts}] {ui.color(msg, '')}\n")

    try:
        files = _iter_files(root)
        worklist: list[FilePlan] = []
        pbar = _make_pbar(len(files), ui.info("Scanning tags"), quiet)
        for path in files:
            plan = strip_ratings_from_file(path, apply=False)
            if plan.error or plan.has_changes:
                worklist.append(plan)
            pbar.update(1)
        pbar.close()

        errors = sum(1 for r in worklist if r.error)
        rated = [r for r in worklist if r.has_changes]
        total_removals = sum(len(r.removals) for r in worklist)

        if not worklist:
            if json_mode:
                print(
                    json_summary(
                        "stripratings",
                        root,
                        dry_run,
                        {"files_with_ratings": 0, "removed": 0, "errors": 0},
                    ),
                    end="",
                )
            else:
                print(ui.success("No rating tags found. Nothing to do."))
            return 0

        head = "[DRY RUN] " if dry_run else ""
        if not json_mode:
            print(
                f"{head}Rating tags found in {len(rated)} file(s) under {root}"
                f" ({total_removals} tag(s), {errors} unreadable)\n"
            )
            for r in worklist:
                rel = os.path.relpath(r.path, root)
                if r.error:
                    print(f"  [!] {rel}: {r.error}")
                    continue
                print(f"  {rel}")
                for removal in r.removals:
                    print(f"      remove {removal}")

        if json_mode:
            print(
                json_summary(
                    "stripratings",
                    root,
                    True,
                    {
                        "files_with_ratings": len(rated),
                        "removed": total_removals,
                        "errors": errors,
                    },
                ),
                end="",
            )
            return 0

        if dry_run:
            print("\nDry run: no files modified.")
            return 0

        # Confirmation, apestrip style. assume_yes stands in for the TUI's
        # ask_yn confirm; a non-TTY run proceeds so pipelines keep working.
        if not assume_yes and sys.stdin.isatty():
            try:
                ans = input("\nStrip these rating tags? [y/N] ").strip().lower()
            except EOFError:
                ans = ""
            if ans not in ("y", "yes"):
                log("Aborted by user; no files modified.")
                return 0

        log(f"Stripping rating tags from {len(rated)} file(s) under {root}")
        stripped = 0
        write_errors = errors
        for plan in rated:
            rel = os.path.relpath(plan.path, root)
            r = strip_ratings_from_file(plan.path, apply=True)
            if r.error:
                write_errors += 1
                log(f"  [!] {rel}: {r.error}")
                continue
            if not r.removals:
                # The tags vanished between the planning and write passes;
                # the log must not claim a strip that never happened.
                log(f"  {rel}: no rating tags at write time (skipped)")
                continue
            stripped += 1
            for removal in r.removals:
                log(f"  {rel}: removed {removal}")
        log(f"-> stripped {stripped} file(s); {write_errors} error(s).")
        if json_mode:
            print(
                json_summary(
                    "stripratings",
                    root,
                    False,
                    {
                        "files_with_ratings": len(rated),
                        "removed": total_removals,
                        "errors": write_errors,
                    },
                ),
                end="",
            )
        if write_errors:
            return 1
    finally:
        if log_fh is not None:
            log_fh.close()

    return 0
