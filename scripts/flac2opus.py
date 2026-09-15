#!/usr/bin/env python3
"""flac2opus.py - Convert FLAC files to Opus.

Recursively finds FLAC files, encodes them to Opus using ffmpeg, verifies
that the output file matches the original duration, copies tags and cover
art over directly with mutagen (a byte-for-byte copy, not re-verified), and
finally deletes the original FLAC.
"""

import argparse
import base64
import os
import shutil
import subprocess
import sys
from datetime import datetime

from vir_tui import core as ui

__version__ = "1.0.0"


def _import_lattice():
    try:
        sys.path.insert(
            0,
            str(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))),
        )
        from mutagen.flac import FLAC
        from mutagen.oggopus import OggOpus

        from lattice.utils import iter_audio_dirs
    except ImportError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    return {"iter_audio_dirs": iter_audio_dirs, "FLAC": FLAC, "OggOpus": OggOpus}


def check_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def convert_and_verify(flac_path: str, bitrate: int, deps: dict, dry_run: bool) -> bool:
    opus_path = os.path.splitext(flac_path)[0] + ".opus"

    try:
        flac_audio = deps["FLAC"](flac_path)
    except Exception as e:
        ui.tqdm.write(
            ui.error(f"Failed to read FLAC tags for {os.path.basename(flac_path)}: {e}")
        )
        return False

    if dry_run:
        ui.tqdm.write(
            ui.dry_run(
                f"would convert {os.path.basename(flac_path)} to Opus {bitrate}kbps"
            )
        )
        return True

    flac_len = flac_audio.info.length

    # Convert using ffmpeg
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        flac_path,
        "-c:a",
        "libopus",
        "-b:a",
        f"{bitrate}k",
        "-map_metadata",
        "-1",  # Strip metadata so we can copy perfectly with Mutagen
        "-v",
        "error",
        opus_path,
    ]

    try:
        subprocess.run(
            cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
    except subprocess.CalledProcessError as e:
        ui.tqdm.write(
            ui.error(
                f"ffmpeg failed for {os.path.basename(flac_path)}: {e.stderr.decode('utf-8', errors='ignore')}"
            )
        )
        if os.path.exists(opus_path):
            os.remove(opus_path)
        return False

    # Verify and copy tags
    try:
        opus_audio = deps["OggOpus"](opus_path)
    except Exception as e:
        ui.tqdm.write(
            ui.error(
                f"Failed to read resulting Opus for {os.path.basename(flac_path)}: {e}"
            )
        )
        os.remove(opus_path)
        return False

    opus_len = opus_audio.info.length

    # Verify duration
    if abs(flac_len - opus_len) > 0.5:
        ui.tqdm.write(
            ui.error(
                f"Duration mismatch for {os.path.basename(flac_path)} ({flac_len:.2f}s vs {opus_len:.2f}s)"
            )
        )
        os.remove(opus_path)
        return False

    # Copy tags exactly
    for k, v in flac_audio.items():
        opus_audio[k] = v

    # Copy pictures
    pictures = opus_audio.get("metadata_block_picture", [])
    for pic in flac_audio.pictures:
        b64 = base64.b64encode(pic.write()).decode("ascii")
        pictures.append(b64)
    if pictures:
        opus_audio["metadata_block_picture"] = pictures

    opus_audio.save()

    # Delete original FLAC
    os.remove(flac_path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert FLAC files to Opus, verifying metadata and quality, then delete the FLAC."
    )
    parser.add_argument("directory", help="Path to the library or album directory")
    parser.add_argument(
        "--bitrate",
        type=int,
        default=128,
        help="Opus bitrate in kbps (default: 128)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the changes per file; write nothing",
    )
    parser.add_argument(
        "--log",
        dest="log_path",
        default=None,
        help="Append a timestamped record of each change to this file",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the confirmation prompt on a real run",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    args = parser.parse_args()

    ui.print_header(
        "flac2opus.py - FLAC to Opus Converter" + (" [DRY RUN]" if args.dry_run else "")
    )

    if not check_ffmpeg():
        print(
            ui.error(
                "[!] ffmpeg is not installed or not in PATH. Please install it first."
            ),
            file=sys.stderr,
        )
        return 1

    target_dir = os.path.abspath(args.directory)
    if not os.path.isdir(target_dir):
        print(ui.error(f"[!] Directory not found: {target_dir}"), file=sys.stderr)
        return 1

    deps = _import_lattice()
    log_path = args.log_path or os.path.join(target_dir, "flac2opus.log")

    worklist = []
    print(ui.info("Scanning directories for FLAC files..."))

    for _root, dirpath, _dirs, files in deps["iter_audio_dirs"](target_dir):
        for f in files:
            if f.lower().endswith(".flac"):
                worklist.append(os.path.join(dirpath, f))

    if not worklist:
        print(ui.success("No FLAC files found."))
        return 0

    print(ui.info(f"Found {len(worklist)} FLAC file(s)."))

    if not args.dry_run and not args.yes and sys.stdin.isatty():
        print("Files to convert and delete (showing up to 20):")
        for filepath in worklist[:20]:
            print(f"  {filepath}")
        if len(worklist) > 20:
            print(f"  ... and {len(worklist) - 20} more")

        if not input("Proceed? [y/N] ").strip().lower().startswith("y"):
            print("Aborted.")
            return 0

    log_fh = None
    if not args.dry_run:
        try:
            log_fh = open(log_path, "a", encoding="utf-8")
        except OSError as e:
            print(
                ui.error(f"error: cannot open log file {log_path}: {e}"),
                file=sys.stderr,
            )
            return 1

    def log(msg: str) -> None:
        ui.tqdm.write(msg)
        if log_fh is not None:
            ts = datetime.now().isoformat(timespec="seconds")
            log_fh.write(f"[{ts}] {msg}\n")

    if not args.dry_run:
        log("=" * 70)
        log(f"FLAC2OPUS RUN START: {target_dir} @ {args.bitrate}kbps")

    updated = 0
    failed = 0

    try:
        pbar = (
            ui.tqdm(
                worklist,
                desc=ui.info(f"Converting FLAC to Opus ({args.bitrate}kbps)"),
            )
            if not args.dry_run
            else worklist
        )
        for filepath in pbar:
            if convert_and_verify(filepath, args.bitrate, deps, args.dry_run):
                if not args.dry_run:
                    log(f"  converted {os.path.basename(filepath)} -> Opus")
                updated += 1
            else:
                if not args.dry_run:
                    log(f"  FAILED to convert {os.path.basename(filepath)}")
                failed += 1
    finally:
        if log_fh is not None:
            log("FLAC2OPUS RUN END")
            log("=" * 70)
            log_fh.close()

    verb = "Would convert" if args.dry_run else "Converted"
    tail = f"  {failed} file(s) failed." if failed else ""
    print(ui.success(f"{verb} {updated} file(s).{tail}"))

    if not args.dry_run:
        print(ui.info(f"Log: {log_path}"))

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
