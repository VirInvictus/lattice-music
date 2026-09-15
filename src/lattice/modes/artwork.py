import base64
import os
import struct
import sys
from collections import defaultdict

from lattice.config import (
    ART_FORMAT_PRIORITY,
    DEFAULT_ART_QUALITY_OUTPUT,
    DEFAULT_MISSING_ART_OUTPUT,
)
from lattice.tags import (
    FLAC,
    HAVE_MUTAGEN_BASE,
    HAVE_MUTAGEN_MP3,
    MP4,
    MUTAGEN_MP3,
    MutagenFile,
    Picture,
)
from lattice.utils import (
    _find_cover_file,
    _has_cover_file,
    _make_pbar,
    as_roots,
    is_audio,
    iter_audio_dirs,
    relpath_under,
)

# =====================================
# Mode: Extract cover art
# =====================================


def _extract_art_from_flac(filepath: str) -> bytes | None:
    """Extract embedded art from a FLAC file."""
    try:
        audio = FLAC(filepath)
        pictures = audio.pictures
        if pictures:
            # Prefer front cover (type 3), fall back to first available
            for pic in pictures:
                if pic.type == 3:
                    return pic.data
            return pictures[0].data
    except Exception as e:
        print(f"  [!] Error reading FLAC art from {filepath}: {e}")
    return None


def _extract_art_from_opus(filepath: str) -> bytes | None:
    """Extract embedded art from an Opus file (METADATA_BLOCK_PICTURE)."""
    try:
        audio = MutagenFile(filepath)
        if audio is None or audio.tags is None:
            return None
        b64_data = audio.tags.get("METADATA_BLOCK_PICTURE")
        if not b64_data:
            return None
        for b64_entry in b64_data:
            try:
                data = base64.b64decode(b64_entry)
                picture = Picture(data)
                return picture.data
            except Exception:
                continue
    except Exception as e:
        print(f"  [!] Error reading Opus art from {filepath}: {e}")
    return None


def _extract_art_from_mp3(filepath: str) -> bytes | None:
    """Extract embedded art from an MP3 file (ID3 APIC frame)."""
    if not HAVE_MUTAGEN_MP3:
        return None
    try:
        audio = MUTAGEN_MP3(filepath)
        if audio.tags is None:
            return None
        # Prefer front cover (type 3), fall back to first APIC
        first_apic = None
        for tag in audio.tags.values():
            if getattr(tag, "FrameID", None) == "APIC":
                if first_apic is None:
                    first_apic = tag.data
                if getattr(tag, "type", None) == 3:
                    return tag.data
        return first_apic
    except Exception as e:
        print(f"  [!] Error reading MP3 art from {filepath}: {e}")
    return None


def _extract_art_from_m4a(filepath: str) -> bytes | None:
    """Extract embedded art from an M4A/MP4 file (covr atom)."""
    try:
        audio = MP4(filepath)
        if audio.tags is None:
            return None
        covr = audio.tags.get("covr")
        if covr and len(covr) > 0:
            return bytes(covr[0])
    except Exception as e:
        print(f"  [!] Error reading M4A art from {filepath}: {e}")
    return None


# Map extensions to their extraction functions
_ART_EXTRACTORS = {
    ".flac": _extract_art_from_flac,
    ".opus": _extract_art_from_opus,
    ".ogg": _extract_art_from_opus,  # OGG Vorbis uses same METADATA_BLOCK_PICTURE
    ".m4a": _extract_art_from_m4a,
    ".mp3": _extract_art_from_mp3,
}


def _extract_best_art(directory: str) -> bytes | None:
    """
    Find the best embedded art in a directory by scanning files in format
    priority order: FLAC > Opus/OGG > M4A > MP3.
    Returns the first successful extraction or None.
    """
    try:
        dir_files = os.listdir(directory)
    except OSError:
        return None

    # Group files by extension
    files_by_ext: dict[str, list[str]] = defaultdict(list)
    for f in dir_files:
        ext = os.path.splitext(f)[1].lower()
        if ext in _ART_EXTRACTORS:
            files_by_ext[ext].append(f)

    # Try each format in priority order
    for ext in ART_FORMAT_PRIORITY:
        if ext not in files_by_ext:
            continue
        extractor = _ART_EXTRACTORS.get(ext)
        if not extractor:
            continue
        # Try only the first file of each format (they should all have the same art)
        filepath = os.path.join(directory, files_by_ext[ext][0])
        data = extractor(filepath)
        if data:
            return data

    return None


def _has_embedded_art(directory: str) -> bool:
    """Quick check: does any audio file in this directory have embedded art?"""
    return _extract_best_art(directory) is not None


def run_extract_art(
    root: str | list[str], *, quiet: bool = False, dry_run: bool = False
) -> int:
    """Walk tree, extract cover art to cover.jpg for directories that lack it."""
    if not HAVE_MUTAGEN_BASE:
        print("ERROR: mutagen is required for art extraction.", file=sys.stderr)
        return 2

    roots = as_roots(root)
    extracted = 0
    skipped = 0
    failed = 0

    if not quiet:
        print(f"Scanning for missing cover art under: {', '.join(roots)}")

    for _src_root, dirpath, _dirs, files in iter_audio_dirs(roots):
        # Only process directories that contain audio files
        has_audio = any(is_audio(f) for f in files)
        if not has_audio:
            continue

        # Case-insensitive check for existing cover
        if _has_cover_file(dirpath):
            skipped += 1
            continue

        if not quiet:
            print(f"[+] Processing: {dirpath}")

        image_data = _extract_best_art(dirpath)
        if image_data:
            output_path = os.path.join(dirpath, "cover.jpg")
            if dry_run:
                if not quiet:
                    print(f"  -> [dry-run] Would extract art to {output_path}")
                extracted += 1
            else:
                try:
                    with open(output_path, "wb") as f:
                        f.write(image_data)
                    if not quiet:
                        print(f"  -> Extracted art to {output_path}")
                    extracted += 1
                except OSError as e:
                    print(f"  [!] Write failed: {e}")
                    failed += 1
        else:
            if not quiet:
                print("  [!] No embedded art found in any audio file.")
            failed += 1

    if not quiet:
        print(
            f"\nDone. Extracted: {extracted}  Skipped (art exists): {skipped}  No art found: {failed}"
        )
    return 0


# =====================================
# Mode: Missing art report
# =====================================


def run_missing_art(root: str | list[str], output: str, *, quiet: bool = False) -> int:
    """Report directories that have audio files but no cover art (folder or embedded)."""
    if not HAVE_MUTAGEN_BASE:
        print("ERROR: mutagen is required for art detection.", file=sys.stderr)
        return 2

    roots = as_roots(root)
    missing: list[dict[str, str]] = []

    if not quiet:
        print(f"Scanning for missing art under: {', '.join(roots)}")

    for _src_root, dirpath, _dirs, files in iter_audio_dirs(roots):
        audio_files = [f for f in files if is_audio(f)]
        if not audio_files:
            continue

        has_folder_art = _has_cover_file(dirpath)
        has_embedded = _has_embedded_art(dirpath) if not has_folder_art else True

        if not has_folder_art and not has_embedded:
            missing.append(
                {
                    "directory": dirpath,
                    "audio_count": str(len(audio_files)),
                    "has_folder_art": "no",
                    "has_embedded_art": "no",
                }
            )
        elif not has_folder_art:
            # Has embedded but no folder art — worth noting
            missing.append(
                {
                    "directory": dirpath,
                    "audio_count": str(len(audio_files)),
                    "has_folder_art": "no",
                    "has_embedded_art": "yes",
                }
            )

    out_path = os.path.abspath(output or DEFAULT_MISSING_ART_OUTPUT)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    no_art_at_all = [m for m in missing if m["has_embedded_art"] == "no"]
    embedded_only = [m for m in missing if m["has_embedded_art"] == "yes"]

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("MISSING ART REPORT\n")
        f.write(f"Root: {', '.join(roots)}\n")
        f.write(
            f"No art at all: {len(no_art_at_all)}  Embedded only: {len(embedded_only)}\n"
        )
        f.write("=" * 60 + "\n\n")

        if no_art_at_all:
            f.write("NO ART (no folder image, no embedded art)\n")
            f.write("-" * 40 + "\n")
            for m in no_art_at_all:
                rel = relpath_under(m["directory"], roots)
                f.write(f"  {rel}  ({m['audio_count']} files)\n")
            f.write("\n")

        if embedded_only:
            f.write("EMBEDDED ONLY (no folder image)\n")
            f.write("-" * 40 + "\n")
            for m in embedded_only:
                rel = relpath_under(m["directory"], roots)
                f.write(f"  {rel}  ({m['audio_count']} files)\n")
            f.write("\n")

    if not quiet:
        print(f"\nResults written to: {out_path}")
        print(f"  No art at all: {len(no_art_at_all)}")
        print(f"  Embedded only (no folder art): {len(embedded_only)}")
    return 0


# =====================================
# Mode: Art quality audit
# =====================================


def _get_image_size(data: bytes) -> tuple[int, int] | None:
    """Attempt to parse JPEG or PNG dimensions from binary data without external libraries."""
    size = len(data)
    # PNG
    if size >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
        w, h = struct.unpack(">LL", data[16:24])
        return w, h
    # JPEG
    if size >= 2 and data.startswith(b"\xff\xd8"):
        try:
            i = 2
            while i < size:
                while i < size and data[i] != 0xFF:
                    i += 1
                while i < size and data[i] == 0xFF:
                    i += 1
                if i >= size:
                    break
                marker = data[i]
                i += 1
                if marker == 0x01 or 0xD0 <= marker <= 0xD9:
                    continue
                if i + 2 > size:
                    break
                (length,) = struct.unpack(">H", data[i : i + 2])
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    if i + 7 <= size:
                        h, w = struct.unpack(">HH", data[i + 3 : i + 7])
                        return w, h
                i += length
        except Exception:
            pass
    return None


def run_art_quality_audit(
    root: str | list[str], output: str, min_res: int, *, quiet: bool = False
) -> int:
    """Report extracted/folder covers below a resolution threshold."""
    if not HAVE_MUTAGEN_BASE:
        print("ERROR: mutagen is required for art quality auditing.", file=sys.stderr)
        return 2

    roots = as_roots(root)
    issues: list[dict[str, str]] = []

    if not quiet:
        print(f"Auditing art quality (< {min_res}x{min_res}) under: {', '.join(roots)}")

    # Count directories for progress
    dirs_with_audio = []
    for _src_root, dirpath, _dirs, files in iter_audio_dirs(roots):
        if any(is_audio(f) for f in files):
            dirs_with_audio.append(dirpath)

    pbar = _make_pbar(len(dirs_with_audio), "Auditing art", quiet)

    for dirpath in dirs_with_audio:
        pbar.update(1)

        folder_art_path = _find_cover_file(dirpath)
        art_data = None
        source = ""

        if folder_art_path:
            try:
                # Whole file, not just a header window: a large EXIF/ICC block
                # can push the JPEG SOF marker past any fixed prefix, and a
                # truncated read made such covers silently unparseable.
                with open(folder_art_path, "rb") as f:
                    art_data = f.read()
                source = "folder"
            except Exception:
                pass

        if not art_data:
            art_data = _extract_best_art(dirpath)
            source = "embedded"

        if art_data:
            dims = _get_image_size(art_data)
            if dims:
                w, h = dims
                if w < min_res or h < min_res:
                    issues.append(
                        {
                            "directory": dirpath,
                            "source": source,
                            "resolution": f"{w}x{h}",
                        }
                    )

    pbar.close()

    out_path = os.path.abspath(output or DEFAULT_ART_QUALITY_OUTPUT)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("ART QUALITY AUDIT REPORT\n")
        f.write(f"Root: {', '.join(roots)}\n")
        f.write(f"Floor: < {min_res}x{min_res}\n")
        f.write(f"Scanned: {len(dirs_with_audio)} dirs  Below floor: {len(issues)}\n")
        f.write("=" * 60 + "\n\n")

        for issue in issues:
            rel_dir = relpath_under(issue["directory"], roots)
            f.write(f"  {rel_dir}/\n")
            f.write(
                f"    Source: {issue['source']}  Resolution: {issue['resolution']}\n\n"
            )

    if not quiet:
        print(
            f"\nAudited {len(dirs_with_audio)} directories. Found {len(issues)} below {min_res}x{min_res}."
        )
        print(f"Results written to: {out_path}")

    return 0
