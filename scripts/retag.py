#!/usr/bin/env python3
"""retag.py — universal genre rewriter for one album directory.

Hard-overwrites the genre tag(s) on every audio file in a directory, hiding the
per-container differences (ID3 null-byte/slash multi-values, Vorbis repeated
GENRE keys, MP4 atoms). Designed to consume `lattice --all-wings --paths`
output one album at a time.

Destructive: it rewrites tags in place. Preview with --dry-run first.

Usage:
    ./retag.py /path/to/album "Genre One" "Genre Two"
    ./retag.py /path/to/album "Alternative Rap" --dry-run
    ./retag.py /path/to/album "Jazz" --log ~/retag.log
"""

import argparse
import os
import sys
from datetime import datetime

import mutagen
from mutagen.apev2 import APENoHeaderError, APEv2
from mutagen.asf import ASF
from mutagen.id3 import ID3, TCON, ID3NoHeaderError, ParseID3v1
from mutagen.mp4 import MP4
from vir_tui import core as ui

__version__ = "1.2.0"

# Only formats whose genre containers are handled below. Raw ADTS .aac is
# intentionally excluded: it has no standard tag container to write a genre to.
AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".mp4", ".wma"}

# Audio formats retag cannot write a genre to; reported per file so a mixed
# album is honest about what was not updated. Non-audio files (covers, logs,
# cue sheets) stay silent.
UNSUPPORTED_AUDIO = {".wav", ".aac", ".alac", ".ape", ".wv", ".aiff"}


def read_genres(filepath: str) -> list[str]:
    """Best-effort current genre(s), for preview and logging. Never raises."""
    try:
        # easy=True has no ASF wrapper, so .wma always read back [] and every
        # dry-run showed the old genre as empty; read WM/Genre directly.
        if filepath.lower().endswith(".wma"):
            vals = ASF(filepath).get("WM/Genre")
            return [str(v) for v in vals] if vals else []
        audio = mutagen.File(filepath, easy=True)
        if audio is not None:
            g = audio.get("genre")
            if g:
                return list(g)
    except Exception:
        pass
    return []


def _id3v1_genre(filepath: str) -> tuple[bool, str | None]:
    """(has_v1_tag, resolved genre name) for the trailing 128-byte ID3v1 block.
    The genre is None when the byte is unset/out of range. Never raises."""
    try:
        with open(filepath, "rb") as f:
            f.seek(-128, os.SEEK_END)
            data = f.read(128)
    except OSError, ValueError:
        return False, None
    if not data.startswith(b"TAG"):
        return False, None
    try:
        frames = ParseID3v1(data) or {}
    except Exception:
        return False, None
    tcon = frames.get("TCON")
    genres = tcon.genres if tcon else []
    genre = genres[0] if genres else None
    return True, genre if genre in TCON.GENRES else None


def is_noop(filepath: str, new_genres: list[str]) -> bool:
    """True when a write would change nothing, so direct invocation is
    idempotent (no gratuitous APEv2 delete + full ID3 re-save on an already
    correct file). For MP3 the hidden genre spots retag exists to clear must
    also be absent: a stray APEv2 tag or bare TXXX:GENRE frame still needs the
    write even when TCON already matches."""
    if read_genres(filepath) != new_genres:
        return False
    if filepath.lower().endswith(".mp3"):
        try:
            APEv2(filepath)
            return False  # APEv2 present; the write would delete it
        except APENoHeaderError:
            pass
        except Exception:
            return False  # unreadable/malformed: let the write path report it
        try:
            tags = ID3(filepath)
        except Exception:
            return False
        if any(k.upper() == "TXXX:GENRE" for k in tags):
            return False
        # The ID3v1 genre byte is another hidden spot the write refreshes: a
        # stale v1 genre survives in v1-reading players even when TCON already
        # matches. A file with no v1 tag at all stays a noop (nothing stale).
        has_v1, v1_genre = _id3v1_genre(filepath)
        if has_v1:
            first = (TCON(encoding=3, text=new_genres).genres or [None])[0]
            expected = first if first in TCON.GENRES else None
            if v1_genre != expected:
                return False
    return True


def apply_genres(filepath: str, new_genres: list[str]) -> bool:
    """Overwrite the genre tag(s) on one file. Returns True on success."""
    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext == ".mp3":
            # A genre can hide in several ID3 spots that override the standard
            # TCON in some players (the deadbeef "won't update" trap): an APEv2
            # tag, the ID3v1 genre byte, and a custom TXXX:GENRE frame. Clear all
            # three, then write one clean TCON. Qualified TXXX frames
            # (AcousticBrainz AB:*, ALBUMGENRE, MusicBrainz, etc.) are left alone.
            try:
                APEv2(filepath).delete()
            except Exception:
                pass

            try:
                tags = ID3(filepath)
            except ID3NoHeaderError:
                tags = ID3()

            tags.delall("TCON")
            for key in [k for k in tags if k.upper() == "TXXX:GENRE"]:
                del tags[key]
            tags.add(TCON(encoding=3, text=new_genres))
            # v2.3 for widespread player compatibility; refresh the ID3v1 genre.
            tags.save(filepath, v2_version=3, v1=2)

        elif ext in (".flac", ".opus", ".ogg"):
            audio = mutagen.File(filepath)
            if audio is None:
                print(
                    ui.error(
                        f"  [!] Failed to tag {os.path.basename(filepath)}: mutagen could not read the file"
                    ),
                    file=sys.stderr,
                )
                return False
            # Vorbis comments natively support repeated keys; mutagen's comment
            # dict is case-insensitive, so one pop clears every case variant.
            audio.pop("genre", None)
            audio["genre"] = new_genres
            audio.save()

        elif ext in (".m4a", ".mp4"):
            audio = MP4(filepath)
            # Clear standard (gnre) and custom (\xa9gen) genre atoms.
            audio.pop("gnre", None)
            audio.pop("\xa9gen", None)
            audio["\xa9gen"] = new_genres
            audio.save()

        elif ext == ".wma":
            audio = ASF(filepath)
            # ASF genre lives in the multi-valued WM/Genre attribute.
            audio["WM/Genre"] = new_genres
            audio.save()

        else:
            return False

        return True
    except Exception as e:
        # stderr, so a caller capturing output (genre_tidy) sees the failure
        # on the error stream instead of buried in the normal log lines.
        print(
            ui.error(f"  [!] Failed to tag {os.path.basename(filepath)}: {e}"),
            file=sys.stderr,
        )
        return False


def strip_junk_frames(filepath: str) -> tuple[bool, str]:
    """One MP3's junk-frame strip: (changed, summary).

    The convert-or-drop companion to `lattice --auditJunkFrames`, which
    classifies the frames (see lattice.modes.audit). Obsolete v2.3-era
    frames go through mutagen's update_to_v24 (TYER/TDAT/TIME fold into
    TDRC, TORY into TDOR, TSIZ dropped, v2.2 three-letter names
    normalized) -- convert where a v2.4 home exists, drop where none
    does -- and empty text frames are deleted outright. Nonstandard
    iTunes-era frames are the audit's report-only class and stay: they
    are functional, so removing them would lose data. Saves as ID3v2.4.
    Read-only files and unreadable tags are reported, never raised.
    """
    try:
        from lattice.modes.audit import audit_id3_junk

        findings = audit_id3_junk(filepath)
        if not findings:
            return False, ""
        tags = ID3(filepath, translate=False)
        if not len(tags):
            return False, ""
        before = sorted(tags.keys())
        tags.update_to_v24()
        for key in list(tags.keys()):
            frames = tags.getall(key)
            values = [
                str(v) for fr in frames for v in (getattr(fr, "text", None) or [])
            ]
            if values and all(not v.strip() or "\x00" in v for v in values):
                del tags[key]
        after = sorted(tags.keys())
        if before == after:
            # The upgrade normalized nothing this file carried (e.g. only
            # nonstandard frames): nothing to write, stay a no-op.
            return False, ""
        dropped = [k for k in before if k not in after]
        tags.save(filepath, v2_version=4)
        parts = [f"{len(dropped)} junk frame(s) removed"]
        if findings.get("nonstandard"):
            parts.append(f"{len(findings['nonstandard'])} nonstandard kept")
        return True, "; ".join(parts)
    except Exception as e:
        print(
            ui.error(f"error: could not strip junk frames from {filepath}: {e}"),
            file=sys.stderr,
        )
        return False, "error"


def run_junk_strip(target_dir: str, *, dry_run: bool, log) -> int:
    """--strip-junk: the junk-frame strip over one album directory,
    MP3-only (the junk classes are ID3-era). Same loop shape as the
    genre verb: sorted, shallow, per-file report, idempotent."""
    updated = failed = unchanged = 0
    for f in sorted(os.listdir(target_dir)):
        ext = os.path.splitext(f)[1].lower()
        if ext != ".mp3":
            continue
        filepath = os.path.join(target_dir, f)
        if dry_run:
            from lattice.modes.audit import audit_id3_junk

            findings = audit_id3_junk(filepath)
            if findings:
                log(f"  would strip {f}: {', '.join(sorted(findings))}")
                updated += 1
            else:
                unchanged += 1
            continue
        try:
            changed, summary = strip_junk_frames(filepath)
        except Exception as e:
            print(
                ui.error(f"error: {filepath}: {e}"),
                file=sys.stderr,
            )
            failed += 1
            continue
        if changed:
            log(f"  stripped {f}: {summary}")
            updated += 1
        else:
            unchanged += 1
    verb = "would strip" if dry_run else "stripped"
    tail = f"  {failed} file(s) failed." if failed else ""
    log(f"  -> {verb} {updated} file(s).  {unchanged} unchanged.{tail}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Overwrite genre tags on every audio file in a directory."
    )
    parser.add_argument("directory", help="Path to the album directory")
    parser.add_argument(
        "genres",
        nargs="*",
        help="One or more genres to apply (omit with --strip-junk)",
    )
    parser.add_argument(
        "--strip-junk",
        action="store_true",
        help="Strip junk ID3 frames (obsolete v2.3-era, empty text) instead "
        "of rewriting genres; takes no genre arguments",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the old -> new genre change per file; write nothing",
    )
    parser.add_argument(
        "--log",
        dest="log_path",
        default=None,
        help="Append a timestamped record of each change to this file",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    args = parser.parse_args()

    ui.print_header(
        "retag.py - "
        + ("Junk-Frame Stripper" if args.strip_junk else "Universal Genre Rewriter")
        + (" [DRY RUN]" if args.dry_run else "")
    )

    target_dir = args.directory
    genres = args.genres

    if args.strip_junk and genres:
        parser.error("--strip-junk takes no genre arguments")
    if not args.strip_junk and not genres:
        parser.error("the genre verb needs one or more genres (or --strip-junk)")

    if not os.path.isdir(target_dir):
        print(ui.error(f"[!] Directory not found: {target_dir}"), file=sys.stderr)
        return 1

    log_fh = None
    if args.log_path:
        try:
            log_fh = open(args.log_path, "a", encoding="utf-8")
        except OSError as e:
            # Matches rerate.py/replaygain.py: an unwritable log path is a
            # reported error, not a traceback.
            print(
                ui.error(f"error: cannot open log file {args.log_path}: {e}"),
                file=sys.stderr,
            )
            return 1

    def log(msg: str) -> None:
        ui.tqdm.write(msg)
        if log_fh is not None:
            prefix = "[DRY] " if args.dry_run else ""
            ts = datetime.now().isoformat(timespec="seconds")
            log_fh.write(f"[{ts}] {prefix}{msg}\n")

    try:
        if args.strip_junk:
            log(f"{'[DRY RUN] ' if args.dry_run else ''}Junk strip: {target_dir}")
            return run_junk_strip(target_dir, dry_run=args.dry_run, log=log)

        log(f"{'[DRY RUN] ' if args.dry_run else ''}Tagging: {target_dir}")
        log(f"Genres:  {genres}")

        updated = 0
        failed = 0
        unchanged = 0
        for f in ui.tqdm(
            sorted(os.listdir(target_dir)), desc=ui.info("Processing files")
        ):
            ext = os.path.splitext(f)[1].lower()
            if ext not in AUDIO_EXTENSIONS:
                if ext in UNSUPPORTED_AUDIO:
                    log(f"  skip (unsupported): {f}")
                continue
            filepath = os.path.join(target_dir, f)
            old = read_genres(filepath)
            if is_noop(filepath, genres):
                unchanged += 1
                log(f"  unchanged {f}: already {genres} (no write)")
            elif args.dry_run:
                log(f"  would retag {f}: {old} -> {genres}")
                updated += 1
            elif apply_genres(filepath, genres):
                log(f"  retagged {f}: {old} -> {genres}")
                updated += 1
            else:
                failed += 1

        verb = "would update" if args.dry_run else "updated"
        if updated == 0 and failed == 0 and unchanged == 0:
            log("  -> No valid audio files found (subdirectories not descended).")
        else:
            tail = f"  {unchanged} unchanged." if unchanged else ""
            tail += f"  {failed} file(s) failed." if failed else ""
            log(f"  -> {verb} {updated} file(s).{tail}")
    finally:
        if log_fh is not None:
            log_fh.close()

    # Nonzero when any file failed to write, so callers (genre_tidy's apply)
    # can count the album as an error instead of a successful retag.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
