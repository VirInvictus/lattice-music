import json
import os
import re

VERSION = "5.6.0"

DEFAULT_LIBRARY_OUTPUT = "music_library.txt"
DEFAULT_FLAC_OUTPUT = "flac_errors.txt"
DEFAULT_MP3_OUTPUT = "mp3_scan_results.txt"
DEFAULT_OPUS_OUTPUT = "opus_scan_results.txt"
DEFAULT_WAV_OUTPUT = "wav_scan_results.txt"
DEFAULT_WMA_OUTPUT = "wma_scan_results.txt"
DEFAULT_MISSING_ART_OUTPUT = "missing_art.txt"
DEFAULT_ART_QUALITY_OUTPUT = "art_quality_audit.txt"
DEFAULT_ART_MISMATCH_OUTPUT = "art_mismatch.txt"
DEFAULT_DUPLICATES_OUTPUT = "duplicates.txt"
DEFAULT_AUDIO_DUPES_OUTPUT = "audio_dupes.txt"
DEFAULT_HEALTH_SCORE_OUTPUT = "health_score.txt"
DEFAULT_TAG_AUDIT_OUTPUT = "tag_audit.txt"
DEFAULT_ALBUM_CONSISTENCY_OUTPUT = "album_consistency.txt"
DEFAULT_JUNK_FRAME_OUTPUT = "junk_frames.txt"
DEFAULT_BITRATE_AUDIT_OUTPUT = "bitrate_audit.txt"
DEFAULT_REPLAYGAIN_AUDIT_OUTPUT = "replaygain_audit.txt"
DEFAULT_REPLAYGAIN_VERIFY_OUTPUT = "replaygain_verify.txt"
DEFAULT_STRAY_AUDIT_OUTPUT = "stray_audit.txt"
DEFAULT_AI_LIBRARY_OUTPUT = "library_ai.txt"
DEFAULT_PLAYLIST_OUTPUT = "smart_playlist.m3u"
DEFAULT_PLAYLIST_CHECK_OUTPUT = "playlist_check.txt"
DEFAULT_SNAPSHOT_OUTPUT = "library_snapshot.tsv"
DEFAULT_SNAPSHOT_DIFF_OUTPUT = "snapshot_diff.txt"

# Path-extraction layout used to recover artist/album/genre from a file's path
# when its tags are missing. The default suits an Artist/Album library; a
# genre-first library can pin "{genre}/{artist}/{album}" via the `layout`
# config key (see get_layout).
DEFAULT_LAYOUT = "{artist}/{album}"

AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav", ".wma", ".aac"}

COVER_NAMES = {
    "cover.jpg",
    "cover.jpeg",
    "cover.png",
    "folder.jpg",
    "folder.jpeg",
    "folder.png",
    "front.jpg",
    "front.jpeg",
    "front.png",
    "album.jpg",
    "album.jpeg",
    "album.png",
}

ART_FORMAT_PRIORITY = [".flac", ".opus", ".ogg", ".m4a", ".mp3"]

RE_CLEAN_PREFIX = re.compile(r"^[^\-\d]*-\s*")
RE_CLEAN_PATTERNS = [
    re.compile(r"^(?:\d+\s*[-–—]\s*)?(\d+)\.?\s*[-–—]?\s*(.+)$"),
    re.compile(r"^[Tt]rack\s*(\d+)\.?\s*[-–—]?\s*(.+)$"),
    re.compile(r"^(\d+)\s+(.+)$"),
]

CONFIG_FILE = os.path.expanduser("~/.config/lattice/config.json")


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(config: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)


def get_library_root() -> str | None:
    # set_library_root always stores an absolute path, but the config invites
    # hand-editing, so expand a hand-written "~/Music" here too.
    root = load_config().get("library_root")
    return os.path.abspath(os.path.expanduser(root)) if root else None


def get_layout() -> str:
    """Path-extraction layout for tag fallback: the `layout` config key when
    set (e.g. "{genre}/{artist}/{album}" for a genre-first library), else
    DEFAULT_LAYOUT. Lets a user pin a non-default tree shape once by hand."""
    return load_config().get("layout") or DEFAULT_LAYOUT


# Thread count for tag reads (stats; the tag/bitrate/ReplayGain/health-score
# audits; duplicates; and --auditAudioDupes, whose pool fingerprints files
# instead of reading tags).
# Serial by default: mutagen parses tags in Python, so the GIL is held for all
# but the file open, and on local storage a pool costs more than it saves —
# measured 1.4-1.5x SLOWER than serial across a 9.6k-file NVMe/ntfs-3g library.
# That measurement covers the tag readers only: hashing in the audio-dupes
# pool releases the GIL, so the rationale does not transfer to it. The pool
# is kept rather than deleted because the picture inverts where an
# open really blocks (SMB/NFS shares, spinning disks, a sleeping external
# drive): there the overlap is free latency-hiding. Gated on the storage, not
# on a file count — concurrency loses at every library size on fast local
# disks, so a "big library => go parallel" rule would pick the slow path
# exactly where it hurts most.
DEFAULT_TAG_WORKERS = 1


def get_tag_workers() -> int:
    """Tag-read thread count: LATTICE_TAG_WORKERS wins over the `tag_workers`
    config key, else DEFAULT_TAG_WORKERS. "auto" means two per CPU (capped at
    16); 1 or less means serial. An unparseable value falls back to the default
    rather than failing a scan."""
    raw = os.environ.get("LATTICE_TAG_WORKERS")
    if raw is None:
        raw = load_config().get("tag_workers")
    if raw is None:
        return DEFAULT_TAG_WORKERS
    if str(raw).strip().lower() == "auto":
        return max(1, min(16, (os.cpu_count() or 4) * 2))
    try:
        return max(1, int(raw))
    except ValueError, TypeError:
        return DEFAULT_TAG_WORKERS


def get_library_roots() -> list[str]:
    """Configured root(s) used as the default when no --root is passed: the
    optional `library_roots` array if present, else the single `library_root`,
    else empty. Lets a user pin several permanent libraries by hand-editing the
    config; the first-run prompt still saves only the one `library_root`."""
    config = load_config()
    roots = config.get("library_roots")
    if isinstance(roots, list) and roots:
        return [os.path.abspath(os.path.expanduser(r)) for r in roots if r]
    single = config.get("library_root")
    return [os.path.abspath(os.path.expanduser(single))] if single else []


def set_library_root(root: str) -> None:
    config = load_config()
    config["library_root"] = os.path.abspath(os.path.expanduser(root))
    save_config(config)
