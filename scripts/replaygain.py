#!/usr/bin/env python3
"""replaygain.py — scan and write ReplayGain 2.0 tags album-by-album.

Wraps `rsgain easy` (libebur128, ReplayGain 2.0, -18 LUFS reference, which is
the 89 dB reference foobar2000 uses) to do what foobar's "Scan selection as
album" does: compute one album gain + album peak per album folder plus a
per-track gain + peak, and write them into the files. rsgain leaves the audio
stream untouched; only metadata tags change.

Album = one folder, the standard "each album in its own folder" layout the lattice package
assumes. For each album the whole folder is rescanned together so the album gain
is correct; an album that is already fully tagged is rescanned too unless
--skip-tagged is passed, in which case it is skipped as a whole. A partial album
is never half-skipped — skipping the already-tagged tracks would leave rsgain
computing album gain over a subset and corrupt it.

Destructive: it writes tags in place. Preview with --dry-run, which lists every
album and its current coverage without invoking rsgain at all. A real run prints
the worklist and asks for confirmation before writing (skip the prompt with
--yes). After each album is scanned, the tags just written are read back and
logged, so the log doubles as a record of exactly what landed on disk.

Format coverage follows rsgain: MP3 (ID3 TXXX), FLAC/Ogg (Vorbis), Opus (the
R128_*_GAIN convention), M4A, WMA, WAV.

The default target is the 89 dB / -18 LUFS ReplayGain 2.0 reference. Pass
--target-lufs N for a louder or quieter result (e.g. -14 ≈ 93 dB, the
streaming-loudness range): each 1 LUFS is 1 dB, so a higher target attenuates
loud masters less. This switches rsgain to custom mode; keep one target across
the whole library or albums will not be evenly normalized.

Caveat: --skip-tagged checks only that gain tags are present, not what target
they were computed against. An album tagged at the default 89 dB reads as
tagged under --target-lufs -14 (Opus R128 tags from a prior default run
included), so when changing targets, rescan without --skip-tagged.

Usage:
    ./replaygain.py /mnt/SharedData/Music --dry-run
    ./replaygain.py /mnt/SharedData/Music
    ./replaygain.py ~/Music --skip-tagged --threads 4 --yes
    ./replaygain.py ~/Music --target-lufs -14        # louder than 89 dB
"""

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime

__version__ = "1.2.2"

RSGAIN = "rsgain"

# File types rsgain can write tags to, per `rsgain --help` (verified against
# rsgain 3.6). The lattice package's AUDIO_EXTENSIONS is wider (raw ADTS .aac has no tag
# container rsgain handles); easy mode silently skips unsupported files on its
# own, but custom mode takes an explicit file list and rejects the WHOLE list
# ("File list is not valid") over one unsupported file, so it must be filtered.
RSGAIN_SUPPORTED = {
    ".flac",
    ".ogg",
    ".oga",
    ".spx",
    ".opus",
    ".mp2",
    ".mp3",
    ".mp4",
    ".m4a",
    ".wma",
    ".wv",
    ".ape",
    ".wav",
    ".aiff",
    ".aif",
    ".snd",
    ".tak",
}


def split_rsgain_supported(audio_files: list[str]) -> tuple[list[str], list[str]]:
    """(supported, unsupported) partition of an album's audio files by whether
    rsgain can write tags to them."""
    sup = [f for f in audio_files if os.path.splitext(f)[1].lower() in RSGAIN_SUPPORTED]
    unsup = [f for f in audio_files if f not in sup]
    return sup, unsup


def _import_lattice():
    """The lattice package supplies the format-aware ReplayGain reader and the canonical
    audio-extension set. Imported lazily so the script gives a useful hint
    instead of a bare traceback when run outside an install / PYTHONPATH=src."""
    try:
        from lattice.config import AUDIO_EXTENSIONS
        from lattice.tags import read_replaygain
    except ImportError as e:
        print(
            f"error: could not import lattice ({e}).\n"
            "Install it (pip install -e . / pipx install .) or run with "
            "PYTHONPATH=src.",
            file=sys.stderr,
        )
        sys.exit(2)
    return read_replaygain, AUDIO_EXTENSIONS


def find_album_dirs(root: str, audio_exts) -> list[tuple[str, list[str]]]:
    """Every directory holding audio, as (dirpath, sorted audio filenames).
    Hidden directories are pruned; results are sorted for deterministic runs."""
    albums: list[tuple[str, list[str]]] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        audio = sorted(f for f in files if os.path.splitext(f)[1].lower() in audio_exts)
        if audio:
            albums.append((dirpath, audio))
    return albums


def find_nested_parents(albums: list[tuple[str, list[str]]]) -> set[str]:
    """Album dirs with another album dir strictly beneath them (a loose bonus
    track beside CD1/CD2 subfolders). `rsgain easy` scans a directory
    recursively, so scanning such a parent would rescan the nested album(s),
    rewriting their tags and bypassing --skip-tagged; these parents are
    excluded from easy-mode runs and reported instead."""
    dirs = {os.path.normpath(d) for d, _ in albums}
    nested: set[str] = set()
    for d in dirs:
        parent = os.path.dirname(d)
        while parent and parent != os.path.dirname(parent):
            if parent in dirs:
                nested.add(parent)
            parent = os.path.dirname(parent)
    return nested


def coverage_label(n_track: int, n_album: int, n_total: int) -> str:
    """One-word current-coverage label for an album (mirrors the package audit's
    buckets): none / partial / no-album-gain / ok."""
    if n_track == 0:
        return "none"
    if n_track < n_total:
        return "partial"
    if n_album < n_total:
        return "no-album-gain"
    return "ok"


def album_coverage(
    read_rg, dirpath: str, audio_files: list[str]
) -> tuple[int, int, int, str]:
    """Read each file's ReplayGain status and return (n_total, n_track_gain,
    n_album_gain, label). Read-only; shared by the dry-run, the worklist, and
    the --skip-tagged decision so all three agree."""
    n_total = len(audio_files)
    n_track = n_album = 0
    for f in audio_files:
        st = read_rg(os.path.join(dirpath, f))
        if st.has_track_gain:
            n_track += 1
        if st.has_album_gain:
            n_album += 1
    return n_total, n_track, n_album, coverage_label(n_track, n_album, n_total)


def read_gain_strings(path: str) -> tuple[str | None, str | None]:
    """Best-effort (track_gain, album_gain) for the post-write read-back log,
    as strings. The tag walk lives in lattice.tags.read_replaygain_values
    (the value reader --verifyReplayGain uses, key-aware about the standard
    and R128 conventions); this formats its floats for the log, so the two
    cannot drift. Real runs have already imported lattice through
    _import_lattice by the time this is called."""
    from lattice.tags import read_replaygain_values

    def fmt(db):
        return None if db is None else f"{db:.2f} dB"

    track_db, album_db = read_replaygain_values(path)
    return (fmt(track_db), fmt(album_db))


def scan_album(
    dirpath: str, audio_files: list[str], threads: int, target_lufs: float | None
) -> tuple[int, str, str]:
    """Run rsgain over one album folder. Returns (returncode, stdout, stderr).

    Default (target_lufs is None): `rsgain easy`, the recommended per-format
    settings at the ReplayGain 2.0 -18 LUFS (89 dB) reference; `--threads`
    parallelizes its scan.

    With an explicit target_lufs: `rsgain custom` instead, so the target loudness
    can differ from the standard (e.g. -14 for a louder result). It writes
    standard replaygain_* tags (album + track, positive-gain clip protection) for
    every format, Opus included — the R128 convention is fixed at -23 LUFS and
    cannot carry a custom target, so it is deliberately not used here. rsgain's
    custom mode takes an explicit file list and has no scan-thread option (its
    -m means max-peak), so --threads does not apply in this mode.

    Mode asymmetry: easy mode silently skips files rsgain can't tag, but custom
    mode rejects the whole file list over one of them, so the custom branch
    filters to RSGAIN_SUPPORTED (the caller reports what was left out)."""
    if target_lufs is None:
        cmd = [RSGAIN, "easy", "-q"]
        if threads and threads > 1:
            cmd += ["-m", str(threads)]
        cmd.append(dirpath)
    else:
        cmd = [
            RSGAIN,
            "custom",
            "-q",
            "-a",  # album gain + peak
            "-s",
            "i",  # write ReplayGain 2.0 tags (custom mode defaults to no-write)
            "-c",
            "p",  # clip protection for positive gain
            "-l",
            f"{target_lufs:g}",
        ]
        supported, _unsup = split_rsgain_supported(audio_files)
        cmd += [os.path.join(dirpath, f) for f in supported]
    # errors="replace": rsgain's output is decoded with the locale encoding
    # (cp1252 on Windows, possibly non-UTF-8 on Linux); a stray undecodable byte
    # in an error message must not crash the run instead of being logged.
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    return proc.returncode, proc.stdout, proc.stderr


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan and write ReplayGain 2.0 tags album-by-album via rsgain.",
        epilog="Default log: <directory>/replaygain.log",
    )
    parser.add_argument(
        "directory", help="Music library root (or a single album folder)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List albums and current coverage; invoke rsgain on nothing",
    )
    parser.add_argument(
        "--skip-tagged",
        action="store_true",
        help="Skip albums already fully tagged (track + album gain on every file)",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Don't prompt for confirmation before writing (the prompt is also "
        "auto-skipped when stdin is not a TTY, matching apestrip.py)",
    )
    parser.add_argument(
        "--target-lufs",
        dest="target_lufs",
        type=float,
        default=None,
        metavar="N",
        help="Target loudness in LUFS for a louder/quieter result than the 89 dB "
        "(-18 LUFS) standard, e.g. -14 (~93 dB, streaming-loud) or -16 (~91 dB). "
        "Switches rsgain to custom mode; valid range -30 to -5. "
        "Default: the 89 dB standard.",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=1,
        help="Parallel scan threads passed to rsgain (-m); default 1 "
        "(default mode only — ignored with --target-lufs)",
    )
    parser.add_argument(
        "--log",
        dest="log_path",
        default=None,
        help="Override log file path (default: <directory>/replaygain.log)",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    args = parser.parse_args()

    root = args.directory
    if not os.path.isdir(root):
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 1

    if args.target_lufs is not None and not (-30.0 <= args.target_lufs <= -5.0):
        print(
            f"error: --target-lufs must be between -30 and -5 "
            f"(got {args.target_lufs:g})",
            file=sys.stderr,
        )
        return 1

    target_desc = (
        f"{args.target_lufs:g} LUFS"
        if args.target_lufs is not None
        else "-18 LUFS (89 dB standard)"
    )

    if not args.dry_run and shutil.which(RSGAIN) is None:
        print(
            "error: rsgain not found on PATH. Install it and re-run "
            "(Linux: e.g. `dnf install rsgain` or your package manager; "
            "macOS: `brew install rsgain`; "
            "Windows: `winget install rsgain`, scoop, or choco).",
            file=sys.stderr,
        )
        return 2

    read_rg, audio_exts = _import_lattice()

    albums = find_album_dirs(root, audio_exts)
    if not albums:
        print(f"No audio files found under: {root}")
        return 0

    # Read-only coverage pass, shared by every path below.
    worklist = [
        (dirpath, audio, *album_coverage(read_rg, dirpath, audio))
        for dirpath, audio in albums
    ]

    # Easy mode scans directories recursively, so a parent album dir with
    # audio-bearing subdirs would rescan the nested albums (and bypass
    # --skip-tagged for them); custom mode passes explicit direct files and is
    # immune. Computed here so the dry-run and the apply path agree.
    nested = find_nested_parents(albums) if args.target_lufs is None else set()

    # The scan set, filtered identically for dry-run and apply so the preview
    # counts match what apply would actually do.
    def is_nested(dirpath: str) -> bool:
        return os.path.normpath(dirpath) in nested

    to_scan = []
    skipped_tagged = 0
    for entry in worklist:
        dirpath, _audio, _n_total, _nt, _na, label = entry
        if is_nested(dirpath):
            continue
        if args.skip_tagged and label == "ok":
            skipped_tagged += 1
            continue
        to_scan.append(entry)

    log_path = args.log_path or os.path.join(root, "replaygain.log")
    try:
        log_fh = open(log_path, "a", encoding="utf-8")
    except OSError as e:
        print(f"error: cannot open log file {log_path}: {e}", file=sys.stderr)
        return 1

    def log(msg: str = "") -> None:
        if msg:
            ts = datetime.now().isoformat(timespec="seconds")
            prefix = "[DRY] " if args.dry_run else ""
            log_fh.write(f"[{ts}] {prefix}{msg}\n")
        else:
            log_fh.write("\n")
        log_fh.flush()

    def nested_note(dirpath: str) -> str:
        rel = os.path.relpath(dirpath, root)
        return (
            f"  NESTED ALBUM (skipped): {rel} has audio in subfolders; "
            "scan the subfolders or flatten"
        )

    try:
        if args.dry_run:
            log("=" * 70)
            log(f"RG RUN START [DRY RUN]: {root}   target: {target_desc}")
            log("=" * 70)
            for dirpath, _audio, n_total, _nt, _na, label in worklist:
                rel = os.path.relpath(dirpath, root)
                if is_nested(dirpath):
                    log(nested_note(dirpath))
                elif args.skip_tagged and label == "ok":
                    log(f"  would skip {rel}  (fully tagged)")
                else:
                    log(f"  would scan {rel}  ({n_total} tracks, current: {label})")
            log("--- SUMMARY ---")
            log(
                f"  would scan {len(to_scan)} of {len(worklist)} album(s) "
                f"({skipped_tagged} skipped as fully tagged, "
                f"{len(nested)} nested parent(s) skipped)"
            )
            log("RG RUN END [DRY RUN]")
            log("=" * 70)
            print(
                f"Would scan {len(to_scan)} of {len(worklist)} album(s) "
                f"({skipped_tagged} skipped as fully tagged, "
                f"{len(nested)} nested parent(s) skipped). Nothing written."
            )
            print(f"Log: {log_path}")
            return 0

        if not to_scan:
            # A run that writes nothing is still a run; the log records it.
            log("=" * 70)
            log(f"RG RUN START [APPLY]: {root}   target: {target_desc}")
            for dirpath in sorted(nested):
                log(nested_note(dirpath))
            log("--- SUMMARY ---")
            log(
                f"  nothing to scan: {skipped_tagged} album(s) already tagged, "
                f"{len(nested)} nested parent(s) skipped"
            )
            log("RG RUN END [APPLY]")
            log("=" * 70)
            print(
                f"Nothing to scan ({skipped_tagged} album(s) already tagged, "
                f"{len(nested)} nested parent(s) skipped)."
            )
            print(f"Log: {log_path}")
            return 0

        if not args.yes and sys.stdin.isatty():
            print(
                f"About to scan and write ReplayGain for {len(to_scan)} "
                f"album(s) under {root}:"
            )
            for dirpath, _audio, n_total, _nt, _na, label in to_scan[:20]:
                print(
                    f"  {os.path.relpath(dirpath, root)}  ({n_total} tracks, {label})"
                )
            if len(to_scan) > 20:
                print(f"  ... and {len(to_scan) - 20} more")
            if skipped_tagged:
                print(f"({skipped_tagged} already-tagged album(s) skipped.)")
            if nested:
                print(f"({len(nested)} nested parent album(s) skipped; see log.)")
            if not input("Proceed? [y/N] ").strip().lower().startswith("y"):
                log("=" * 70)
                log(f"RG RUN START [APPLY]: {root}   target: {target_desc}")
                log("  aborted by user at confirmation; nothing written")
                log("RG RUN END [APPLY]")
                log("=" * 70)
                print("Aborted.")
                return 0

        scanned = noops = errors = filecount = 0
        log("=" * 70)
        log(f"RG RUN START [APPLY]: {root}   target: {target_desc}")
        log("=" * 70)
        for dirpath in sorted(nested):
            log(nested_note(dirpath))
        for dirpath, audio, n_total, _nt, _na, label in to_scan:
            rel = os.path.relpath(dirpath, root)
            files = audio
            if args.target_lufs is not None:
                files, unsupported = split_rsgain_supported(audio)
                for f in unsupported:
                    log(f"    SKIP (rsgain-unsupported): {rel}/{f}")
                if not files:
                    log(f"  SKIP {rel}: no rsgain-supported files")
                    continue
            rc, out, err = scan_album(dirpath, files, args.threads, args.target_lufs)
            if rc != 0:
                errors += 1
                detail = err.strip() or out.strip() or "no output"
                log(f"  ERROR scanning {rel} (rc={rc}): {detail}")
                continue
            # rc=0 does not mean anything was written: easy mode reports "No
            # files were scanned" (still rc=0) when it finds nothing it can
            # tag. Read back before counting the album as scanned.
            gains = [(f, *read_gain_strings(os.path.join(dirpath, f))) for f in files]
            if "No files were scanned" in out + err or not any(
                tg or ag for _f, tg, ag in gains
            ):
                noops += 1
                log(f"  NO-OP {rel}: rsgain wrote no gains (unsupported formats?)")
                continue
            scanned += 1
            filecount += len(files)
            log(f"  scanned {rel}  ({len(files)} tracks, was {label})")
            for f, tg, ag in gains:
                log(f"    {f}  track={tg}  album={ag}")

        log("--- SUMMARY ---")
        log(
            f"  albums scanned: {scanned}   skipped: {skipped_tagged}   "
            f"nested skipped: {len(nested)}   no-ops: {noops}   errors: {errors}   "
            f"files: {filecount}"
        )
        log("RG RUN END [APPLY]")
        log("=" * 70)

        print(
            f"Scanned {scanned} album(s), {filecount} file(s). "
            f"Skipped {skipped_tagged}, nested skipped {len(nested)}, "
            f"no-ops {noops}, errors {errors}."
        )
        print(f"Log: {log_path}")
        return 1 if errors else 0
    finally:
        log_fh.close()


if __name__ == "__main__":
    sys.exit(main())
