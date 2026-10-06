"""The two-level help surface.

`lattice --help` renders a categorized mode index and `lattice help MODE`
(or `--MODE --help`) renders a full page for one mode: its own options, the
shared options that apply to it, and a worked example. This registry is the
single source of truth for help text; cli.py renders nothing itself and
intercepts the help paths before argparse ever sees them.

The applicability data is pinned against cli.py's enforcement sets
(_WHERE_MODES / _JSON_MODES / _FAIL_MODES) in tests/test_help.py, so a page
cannot drift from what the CLI actually refuses.
"""

from __future__ import annotations

import os
import re
import sys
import textwrap
from dataclasses import dataclass

from lattice.config import (
    DEFAULT_AI_LIBRARY_OUTPUT,
    DEFAULT_ALBUM_CONSISTENCY_OUTPUT,
    DEFAULT_ART_MISMATCH_OUTPUT,
    DEFAULT_ART_QUALITY_OUTPUT,
    DEFAULT_AUDIO_DUPES_OUTPUT,
    DEFAULT_BITRATE_AUDIT_OUTPUT,
    DEFAULT_DUPLICATES_OUTPUT,
    DEFAULT_FLAC_OUTPUT,
    DEFAULT_HEALTH_OUTPUT,
    DEFAULT_HEALTH_SCORE_OUTPUT,
    DEFAULT_JUNK_FRAME_OUTPUT,
    DEFAULT_LIBRARY_OUTPUT,
    DEFAULT_MISSING_ART_OUTPUT,
    DEFAULT_MP3_OUTPUT,
    DEFAULT_OPUS_OUTPUT,
    DEFAULT_PLAYLIST_CHECK_OUTPUT,
    DEFAULT_PLAYLIST_OUTPUT,
    DEFAULT_REPLAYGAIN_AUDIT_OUTPUT,
    DEFAULT_REPLAYGAIN_VERIFY_OUTPUT,
    DEFAULT_SNAPSHOT_DIFF_OUTPUT,
    DEFAULT_SNAPSHOT_OUTPUT,
    DEFAULT_STRAY_AUDIT_OUTPUT,
    DEFAULT_TAG_AUDIT_OUTPUT,
    DEFAULT_WAV_OUTPUT,
    DEFAULT_WMA_OUTPUT,
    VERSION,
)

_WIDTH = 78

# The color theme mirrors the CPython 3.14 argparse default (_colorize's
# ColorfulTheme: bold blue headings and usage, bold magenta prog, bold cyan
# long options, bold green short options, bold yellow metavars), so lattice's
# help reads like `python --help` in the same terminal.
_THEME = {
    "heading": "\x1b[1;34m",
    "prog": "\x1b[1;35m",
    "long_option": "\x1b[1;36m",
    "short_option": "\x1b[1;32m",
    "label": "\x1b[1;33m",
    "reset": "\x1b[0m",
}

_CSI = re.compile(r"\x1b\[[0-9;]*m")
_LONG_OPT = re.compile(r"(?<![\w/-])--[\w][\w-]*")
_SHORT_OPT = re.compile(r"(?<![\w-])-([A-Za-z])\b")
_LABEL = re.compile(r"\b[A-Z][A-Z0-9]*\b")
_PROG = re.compile(r"(?<![\w~./])lattice\b")


def _can_color(stream=None) -> bool:
    """FORCE_COLOR wins, then NO_COLOR and PYTHON_COLORS=0 suppress, then a
    TTY check: piped output (tests, README embedding) stays plain."""
    if stream is None:
        stream = sys.stdout
    force = os.environ.get("FORCE_COLOR")
    if force and force != "0":
        return True
    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("PYTHON_COLORS") == "0":
        return False
    try:
        return stream.isatty()
    except ValueError:
        return False


class _Style:
    """ANSI painter for one render pass. The disabled variant emits empty
    strings so a single layout path serves both faces."""

    def __init__(self, enabled: bool):
        if enabled:
            self.heading = _THEME["heading"]
            self.prog = _THEME["prog"]
            self.long_option = _THEME["long_option"]
            self.short_option = _THEME["short_option"]
            self.label = _THEME["label"]
            self.reset = _THEME["reset"]
        else:
            self.heading = self.prog = self.long_option = ""
            self.short_option = self.label = self.reset = ""

    def tokens(self, s: str) -> str:
        """Paint the command-shaped tokens of a usage/flag/example string:
        long options, short options, ALL-CAPS metavars, and a leading
        `lattice`. Prose stays plain."""
        s = _LONG_OPT.sub(self.long_option + r"\g<0>" + self.reset, s)
        s = _SHORT_OPT.sub(self.short_option + r"\g<0>" + self.reset, s)
        s = _LABEL.sub(self.label + r"\g<0>" + self.reset, s)
        s = _PROG.sub(self.prog + r"\g<0>" + self.reset, s)
        return s


def _visible_len(s: str) -> int:
    return len(_CSI.sub("", s))


@dataclass(frozen=True)
class Opt:
    """One option row: dest (must exist on the parsed namespace), the
    rendered flag column, and the description text."""

    dest: str
    flag: str
    text: str


@dataclass(frozen=True)
class Mode:
    dest: str
    flag: str
    category: str
    summary: str
    detail: str
    usage: str
    example: str
    own: tuple[Opt, ...] = ()
    shared: tuple[str, ...] = ()
    display: str = ""
    output_default: str = ""
    output_note: str = ""


CATEGORIES: tuple[tuple[str, str], ...] = (
    ("exports", "LIBRARY TREES & EXPORTS"),
    ("reports", "REPORTS & SNAPSHOTS"),
    ("integrity", "INTEGRITY (decode checks)"),
    ("artwork", "ARTWORK"),
    ("audits", "AUDITS (read-only reports)"),
    ("replaygain", "REPLAYGAIN (audit, verify, write)"),
    ("write", "WRITE MODES (dry-run by default; --apply writes)"),
)

SHARED: dict[str, Opt] = {
    "root": Opt(
        "root",
        "--root DIR",
        "Library root to scan; repeat --root to scan several libraries "
        "together (a repeated path is de-duped). Default: the configured "
        "library root, else the current directory.",
    ),
    "root_one": Opt(
        "root",
        "--root DIR",
        "The one library root to operate on (this mode takes exactly one "
        "tree). Default: the configured library root, else the current "
        "directory.",
    ),
    "output": Opt("output", "--output PATH", "Where the report is written."),
    "layout": Opt(
        "layout",
        "--layout PATTERN",
        "Directory pattern for extracting tags from paths (default: the "
        "layout config key, or {artist}/{album}). Use "
        "{genre}/{artist}/{album} for a genre-first library.",
    ),
    "where": Opt(
        "where",
        "--where EXPR",
        "Scope the run to tracks matching a playlist-rule expression (the "
        "--playlist grammar: rating, genre, artist, album, title, duration, "
        "bitrate). On targeting modes a rule matching any track selects the "
        "whole album. Accepted by: --library, --ai-library, --all-wings, "
        "--ai-wings, --stats, --auditTags, --auditAlbums, --auditBitrate, "
        "--auditReplayGain, --healthScore, --duplicates, --genreTidy-build, "
        "--genreTidy-apply, --genreMap, --replayGain; everywhere else --where "
        "is a usage error (exit 2).",
    ),
    "json": Opt(
        "json",
        "--json",
        "Machine-readable output: an audit/stats run writes the JSON report "
        "envelope {mode, root, findings, payload}, a write run ends with a "
        "JSON summary {mode, root, dry_run, counts}. .txt stays the default; "
        "--output - pipes. Unsupported modes refuse --json.",
    ),
    "fail_on_findings": Opt(
        "fail_on_findings",
        "--fail-on-findings",
        "Exit 1 when the audit found something (default 0), so a cron or CI "
        "job can gate on a clean report.",
    ),
    "apply": Opt(
        "apply",
        "--apply",
        "Write for real; without it this mode only previews (dry-run is the "
        "default). Every run is logged.",
    ),
    "dry_run": Opt(
        "dry_run",
        "--dry-run",
        "Preview without writing; explicit --dry-run wins even alongside --apply.",
    ),
    "quiet": Opt("quiet", "--quiet", "Minimize output."),
    "verbose": Opt("verbose", "--verbose", "Add per-finding detail to the report."),
}

_W = ("where", "json")  # shorthand only used in comments below

MODES: tuple[Mode, ...] = (
    # -- exports ------------------------------------------------------------
    Mode(
        dest="library",
        flag="--library",
        category="exports",
        summary="Album/track tree as a text report",
        detail="Walk the library and write the artist/album/track tree, with "
        "ratings and (with --genres) genres. The layout pattern decides how "
        "artist and album are recovered from paths; --where scopes the tree "
        "to matching tracks.",
        usage="lattice --library [ROOT] [--output PATH] [--genres] [--where EXPR]",
        example="lattice --library ~/Music --genres --output library.txt",
        shared=("root", "output", "layout", "where", "quiet"),
        output_default=DEFAULT_LIBRARY_OUTPUT,
        own=(Opt("genres", "--genres", "Include each album's genre in the tree."),),
    ),
    Mode(
        dest="ai_library",
        flag="--ai-library",
        category="exports",
        summary="Token-efficient export for AI prompts",
        detail="A flat, token-efficient rendering of the library meant to be "
        "pasted into LLM recommendation prompts: one line per album, minimal "
        "punctuation, no box art. --where scopes it like --library.",
        usage="lattice --ai-library [ROOT] [--output PATH] [--where EXPR]",
        example="lattice --ai-library ~/Music",
        shared=("root", "output", "layout", "where", "quiet"),
        output_default=DEFAULT_AI_LIBRARY_OUTPUT,
    ),
    Mode(
        dest="all_wings",
        flag="--all-wings",
        category="exports",
        summary="One library file per genre",
        detail="Write a separate library tree per genre into an output "
        "directory, so each wing of the library can be browsed on its own.",
        usage="lattice --all-wings [ROOT] [--output DIR] [--genres] [--paths]",
        example="lattice --all-wings ~/Music --paths --output wings",
        shared=("root", "output", "layout", "where", "quiet"),
        output_note="Directory for the per-genre files (default: wings).",
        own=(
            Opt("genres", "--genres", "Include each album's genre in the tree."),
            Opt(
                "paths",
                "--paths",
                "Include absolute directory paths at the album level.",
            ),
        ),
    ),
    Mode(
        dest="ai_wings",
        flag="--ai-wings",
        category="exports",
        summary="AI-friendly library file per genre",
        detail="The token-efficient export, split into one file per genre "
        "under an output directory.",
        usage="lattice --ai-wings [ROOT] [--output DIR] [--where EXPR]",
        example="lattice --ai-wings ~/Music --output wings_ai",
        shared=("root", "output", "layout", "where", "quiet"),
        output_note="Directory for the per-genre files (default: wings_ai).",
    ),
    # -- reports ------------------------------------------------------------
    Mode(
        dest="stats",
        flag="--stats",
        category="reports",
        summary="Format, bitrate, rating, genre breakdowns",
        detail="Library-wide statistics: format counts, bitrate distribution, "
        "ratings, genre and top-artist breakdowns. Combined across all given "
        "roots. Prints to the terminal unless --output names a file; --where "
        "describes the matching subset instead of the whole library.",
        usage="lattice --stats [ROOT] [--output PATH] [--where EXPR]",
        example="lattice --stats ~/Music --where \"genre == 'Classical'\"",
        shared=("root", "output", "layout", "where", "json", "quiet"),
        output_note="Path for the report; omit it to print to the terminal.",
    ),
    Mode(
        dest="snapshot",
        flag="--snapshot",
        category="reports",
        summary="Per-file TSV (the --diff baseline)",
        detail="Write one TSV row per audio file: path, size, mtime, and the "
        "key tags plus ReplayGain presence. This is the baseline format "
        "--diff replays; take one before a big reorganization.",
        usage="lattice --snapshot [ROOT] [--output PATH]",
        example="lattice --snapshot ~/Music --output before-reorg.tsv",
        shared=("root", "output", "quiet"),
        output_default=DEFAULT_SNAPSHOT_OUTPUT,
    ),
    Mode(
        dest="diff_snapshot",
        flag="--diff",
        category="reports",
        display="--diff SNAPSHOT",
        summary="Replay a snapshot against the current tree",
        detail="Read a --snapshot TSV and report what changed since: MOVED "
        "(size and mtime preserved at a new path), RETAGGED (per-field old -> "
        "new), RESIZED, ADDED, and REMOVED files.",
        usage="lattice --diff SNAPSHOT [ROOT] [--output PATH]",
        example="lattice --diff before-reorg.tsv ~/Music",
        shared=("root", "output", "quiet"),
        output_default=DEFAULT_SNAPSHOT_DIFF_OUTPUT,
    ),
    Mode(
        dest="playlist",
        flag="--playlist",
        category="reports",
        summary="Smart .m3u playlist from a rule",
        detail="Generate an .m3u from a rule in the playlist grammar "
        "(fields: rating, genre, artist, album, title, duration, bitrate; "
        "combinable with and/or and comparisons; there is no format field ("
        "formats are not tags; --stats breaks those down natively).",
        usage="lattice --playlist [ROOT] --rule EXPR [--output PATH]",
        example="lattice --playlist ~/Music --rule \"rating >= 4 and genre == 'Jazz'\"",
        shared=("root", "output", "layout", "quiet"),
        output_default=DEFAULT_PLAYLIST_OUTPUT,
        own=(
            Opt(
                "rule",
                "--rule EXPR",
                "The playlist rule, quoted, e.g. \"rating >= 4 and genre == 'Jazz'\".",
            ),
        ),
    ),
    Mode(
        dest="check_playlists",
        flag="--checkPlaylists",
        category="reports",
        summary="Verify the library's .m3u playlists",
        detail="Check every .m3u/.m3u8 under the root for a missing #EXTM3U "
        "header and entries whose target no longer exists; relative entries "
        "resolve against the playlist's own directory.",
        usage="lattice --checkPlaylists [ROOT] [--output PATH]",
        example="lattice --checkPlaylists ~/Music",
        shared=("root", "output", "verbose", "quiet"),
        output_default=DEFAULT_PLAYLIST_CHECK_OUTPUT,
    ),
    # -- integrity ----------------------------------------------------------
    Mode(
        dest="testFLAC",
        flag="--testFLAC",
        category="integrity",
        summary="Verify FLAC files (flac -t / ffmpeg)",
        detail="Decode-verify every FLAC file: the flac reference decoder by "
        "default (--prefer ffmpeg to flip), each file classified into a "
        "severity tier. The report lists problems plus a per-tier summary. "
        "An interrupted scan records verdicts that --resume picks up.",
        usage="lattice --testFLAC [ROOT] [--workers N] [--resume]",
        example="lattice --testFLAC ~/Music --workers 4 --resume",
        shared=("root", "output", "quiet"),
        output_default=DEFAULT_FLAC_OUTPUT,
        own=(
            Opt(
                "prefer",
                "--prefer {flac,ffmpeg}",
                "Verification tool for FLAC: the flac reference decoder "
                "(default) or ffmpeg.",
            ),
            Opt("workers", "--workers N", "Parallel decode workers (default 4)."),
            Opt("ffmpeg", "--ffmpeg PATH", "Path to the ffmpeg binary."),
            Opt(
                "resume",
                "--resume",
                "Reuse verdicts recorded by an interrupted run "
                "(<output>.progress.json) and scan only the remaining files; "
                "finishing a scan clears the state.",
            ),
        ),
    ),
    Mode(
        dest="testMP3",
        flag="--testMP3",
        category="integrity",
        summary="Decode-verify MP3 files via ffmpeg",
        detail="Decode every MP3 through ffmpeg (demuxer forced from the "
        "extension) and classify each file into a severity tier. Only "
        "errors and warnings are written unless --no-only-errors asks for "
        "every file. --resume picks up an interrupted scan.",
        usage="lattice --testMP3 [ROOT] [--workers N] [--resume]",
        example="lattice --testMP3 ~/Music --workers 8",
        shared=("root", "output", "quiet"),
        output_default=DEFAULT_MP3_OUTPUT,
        own=(
            Opt("workers", "--workers N", "Parallel decode workers (default 4)."),
            Opt("ffmpeg", "--ffmpeg PATH", "Path to the ffmpeg binary."),
            Opt(
                "only_errors",
                "--only-errors / --no-only-errors",
                "Record only errors and warnings (the default); "
                "--no-only-errors records every file scanned.",
            ),
            Opt(
                "resume",
                "--resume",
                "Reuse verdicts recorded by an interrupted run "
                "(<output>.progress.json) and scan only the remaining files; "
                "finishing a scan clears the state.",
            ),
        ),
    ),
    Mode(
        dest="testOpus",
        flag="--testOpus",
        category="integrity",
        summary="Decode-verify Opus files via ffmpeg",
        detail="Decode every Opus file through ffmpeg and classify each into "
        "a severity tier; only errors and warnings are written unless "
        "--no-only-errors asks for every file.",
        usage="lattice --testOpus [ROOT] [--workers N]",
        example="lattice --testOpus ~/Music",
        shared=("root", "output", "quiet"),
        output_default=DEFAULT_OPUS_OUTPUT,
        own=(
            Opt("workers", "--workers N", "Parallel decode workers (default 4)."),
            Opt("ffmpeg", "--ffmpeg PATH", "Path to the ffmpeg binary."),
            Opt(
                "only_errors",
                "--only-errors / --no-only-errors",
                "Record only errors and warnings (the default); "
                "--no-only-errors records every file scanned.",
            ),
        ),
    ),
    Mode(
        dest="testWAV",
        flag="--testWAV",
        category="integrity",
        summary="Decode-verify WAV files via ffmpeg",
        detail="Decode every WAV file through ffmpeg and classify each into "
        "a severity tier; only errors and warnings are written unless "
        "--no-only-errors asks for every file.",
        usage="lattice --testWAV [ROOT] [--workers N]",
        example="lattice --testWAV ~/Music",
        shared=("root", "output", "quiet"),
        output_default=DEFAULT_WAV_OUTPUT,
        own=(
            Opt("workers", "--workers N", "Parallel decode workers (default 4)."),
            Opt("ffmpeg", "--ffmpeg PATH", "Path to the ffmpeg binary."),
            Opt(
                "only_errors",
                "--only-errors / --no-only-errors",
                "Record only errors and warnings (the default); "
                "--no-only-errors records every file scanned.",
            ),
        ),
    ),
    Mode(
        dest="testWMA",
        flag="--testWMA",
        category="integrity",
        summary="Decode-verify WMA files via ffmpeg",
        detail="Decode every WMA file through ffmpeg and classify each into "
        "a severity tier; only errors and warnings are written unless "
        "--no-only-errors asks for every file.",
        usage="lattice --testWMA [ROOT] [--workers N]",
        example="lattice --testWMA ~/Music",
        shared=("root", "output", "quiet"),
        output_default=DEFAULT_WMA_OUTPUT,
        own=(
            Opt("workers", "--workers N", "Parallel decode workers (default 4)."),
            Opt("ffmpeg", "--ffmpeg PATH", "Path to the ffmpeg binary."),
            Opt(
                "only_errors",
                "--only-errors / --no-only-errors",
                "Record only errors and warnings (the default); "
                "--no-only-errors records every file scanned.",
            ),
        ),
    ),
    # -- artwork ------------------------------------------------------------
    Mode(
        dest="extractArt",
        flag="--extractArt",
        category="artwork",
        summary="Extract embedded cover art to folders",
        detail="For every album folder, pick the best embedded image by "
        "format priority and write it out as the folder cover. --dry-run "
        "lists what would be written.",
        usage="lattice --extractArt [ROOT] [--dry-run]",
        example="lattice --extractArt ~/Music --dry-run",
        shared=("root", "quiet"),
        own=(
            Opt(
                "dry_run",
                "--dry-run",
                "List what would be extracted without writing image files.",
            ),
        ),
    ),
    Mode(
        dest="missingArt",
        flag="--missingArt",
        category="artwork",
        summary="Report album folders with no cover art",
        detail="Report album folders that have neither embedded art nor a "
        "folder image (cover.jpg and friends, matched case-insensitively).",
        usage="lattice --missingArt [ROOT] [--output PATH]",
        example="lattice --missingArt ~/Music",
        shared=("root", "output", "quiet"),
        output_default=DEFAULT_MISSING_ART_OUTPUT,
    ),
    Mode(
        dest="auditArtQuality",
        flag="--auditArtQuality",
        category="artwork",
        summary="Folder covers below a resolution floor",
        detail="Report extracted and folder covers whose resolution is "
        "below the threshold; the natural follow-up to --missingArt once "
        "covers exist but are tiny.",
        usage="lattice --auditArtQuality [ROOT] [--min-art-res N]",
        example="lattice --auditArtQuality ~/Music --min-art-res 1000",
        shared=("root", "output", "quiet"),
        output_default=DEFAULT_ART_QUALITY_OUTPUT,
        own=(
            Opt(
                "min_art_res",
                "--min-art-res N",
                "Minimum cover resolution in pixels (default 500).",
            ),
        ),
    ),
    Mode(
        dest="audit_art_mismatch",
        flag="--auditArtMismatch",
        category="artwork",
        summary="Embedded art vs folder cover comparison",
        detail="Compare each album's embedded art against its folder cover "
        "and classify the pair: byte-identical, a same-pixels re-encode, or "
        "a real dimension mismatch. Folders carrying only one of the two are "
        "not findings.",
        usage="lattice --auditArtMismatch [ROOT] [--output PATH]",
        example="lattice --auditArtMismatch ~/Music --verbose",
        shared=("root", "output", "verbose", "quiet"),
        output_default=DEFAULT_ART_MISMATCH_OUTPUT,
    ),
    # -- audits -------------------------------------------------------------
    Mode(
        dest="duplicates",
        flag="--duplicates",
        category="audits",
        summary="Four-section duplicate-album report",
        detail="Duplicate detection keyed on tags: exact album dupes across "
        "directories, within-folder multi-format pairs, fuzzy similar-name "
        "candidates, and track-level cross-library duplicates filtered by "
        "duration. Detection only; --clean is the destructive companion for "
        "the quote/dash/case-variant subset.",
        usage="lattice --duplicates [ROOT] [--output PATH]",
        example="lattice --duplicates ~/Music",
        shared=("root", "output", "where", "json", "fail_on_findings", "quiet"),
        output_default=DEFAULT_DUPLICATES_OUTPUT,
    ),
    Mode(
        dest="audit_audio_dupes",
        flag="--auditAudioDupes",
        category="audits",
        summary="Content-hash duplicate detection",
        detail="Byte-level duplicate detection that never reads tags: exact "
        "sha256, audio-stream sha256 (container tag regions parsed out so a "
        "retag does not hide a dupe), and head/tail sampled matches for the "
        "remaining formats. Catches what the name-based --duplicates cannot.",
        usage="lattice --auditAudioDupes [ROOT] [--output PATH]",
        example="lattice --auditAudioDupes ~/Music",
        shared=("root", "output", "quiet"),
        output_default=DEFAULT_AUDIO_DUPES_OUTPUT,
    ),
    Mode(
        dest="auditTags",
        flag="--auditTags",
        category="audits",
        summary="Files with incomplete tags",
        detail="Report files missing any of: title, artist, track number, "
        "genre. The simplest lint pass over the library; pair with "
        "--fail-on-findings in cron.",
        usage="lattice --auditTags [ROOT] [--output PATH]",
        example="lattice --auditTags ~/Music --where \"genre == 'Classical'\"",
        shared=("root", "output", "where", "json", "fail_on_findings", "quiet"),
        output_default=DEFAULT_TAG_AUDIT_OUTPUT,
    ),
    Mode(
        dest="audit_junk_frames",
        flag="--auditJunkFrames",
        category="audits",
        summary="MP3 ID3v2 junk-frame audit",
        detail="Read-only audit of MP3 ID3v2 tags for junk frames: obsolete "
        "v2.3-era leftovers, empty text frames, duplicate unique frames, and "
        "nonstandard iTunes-era frames. Frames are loaded raw so mutagen's "
        "upgrade cannot hide them; --retag --strip-junk is the fix.",
        usage="lattice --auditJunkFrames [ROOT] [--output PATH]",
        example="lattice --auditJunkFrames ~/Music",
        shared=("root", "output", "verbose", "quiet"),
        output_default=DEFAULT_JUNK_FRAME_OUTPUT,
    ),
    Mode(
        dest="audit_albums",
        flag="--auditAlbums",
        category="audits",
        summary="Per-album consistency (codecs, tracks, year)",
        detail="Per-album consistency audit: mixed codecs in one folder, "
        "track-number gaps and duplicates over 1..max, and missing, partial, "
        "or divergent year tags.",
        usage="lattice --auditAlbums [ROOT] [--output PATH]",
        example="lattice --auditAlbums ~/Music",
        shared=(
            "root",
            "output",
            "where",
            "json",
            "fail_on_findings",
            "verbose",
            "quiet",
        ),
        output_default=DEFAULT_ALBUM_CONSISTENCY_OUTPUT,
    ),
    Mode(
        dest="auditBitrate",
        flag="--auditBitrate",
        category="audits",
        summary="Files below a bitrate floor",
        detail="Report every audio file whose bitrate falls below the floor; "
        "the floor and the report make the lossy side of the library "
        "auditable next to the lossless one.",
        usage="lattice --auditBitrate [ROOT] [--min-bitrate N]",
        example="lattice --auditBitrate ~/Music --min-bitrate 128",
        shared=(
            "root",
            "output",
            "where",
            "json",
            "fail_on_findings",
            "verbose",
            "quiet",
        ),
        output_default=DEFAULT_BITRATE_AUDIT_OUTPUT,
        own=(
            Opt(
                "min_bitrate",
                "--min-bitrate N",
                "Minimum bitrate in kbps (default 192).",
            ),
        ),
    ),
    Mode(
        dest="auditReplayGain",
        flag="--auditReplayGain",
        category="audits",
        summary="Per-album ReplayGain coverage",
        detail="Report per-album ReplayGain coverage: missing, partial, or "
        "present-but-no-album-gain. Opus R128 gain counts as tagged. "
        "Coverage only; --verifyReplayGain measures whether stored values "
        "are right.",
        usage="lattice --auditReplayGain [ROOT] [--output PATH]",
        example="lattice --auditReplayGain ~/Music",
        shared=(
            "root",
            "output",
            "where",
            "json",
            "fail_on_findings",
            "verbose",
            "quiet",
        ),
        output_default=DEFAULT_REPLAYGAIN_AUDIT_OUTPUT,
    ),
    Mode(
        dest="audit_strays",
        flag="--auditStrays",
        category="audits",
        summary="Wrong-depth audio, strays, hidden dirs",
        detail="Report audio outside the layout's album depth, loose tracks "
        "beside album folders, hidden-directory audio, and unrecognized "
        "non-audio files sitting in album folders. Depth awareness follows "
        "--layout, so genre-first libraries should pass their pattern.",
        usage="lattice --auditStrays [ROOT] [--layout PATTERN]",
        example='lattice --auditStrays ~/Music --layout "{genre}/{artist}/{album}"',
        shared=("root", "output", "layout", "quiet"),
        output_default=DEFAULT_STRAY_AUDIT_OUTPUT,
    ),
    Mode(
        dest="health_score",
        flag="--healthScore",
        category="audits",
        summary="Per-album health score out of 100",
        detail="Score every album out of 100 across tag completeness, "
        "ReplayGain coverage, art presence/resolution, and the bitrate "
        "floor, with per-bucket deductions so the fix is obvious from the "
        "row. Read-only, no decode scans; the full-ranking companion to "
        "--health.",
        usage="lattice --healthScore [ROOT] [--output PATH]",
        example="lattice --healthScore ~/Music",
        shared=(
            "root",
            "output",
            "where",
            "json",
            "fail_on_findings",
            "verbose",
            "quiet",
        ),
        output_default=DEFAULT_HEALTH_SCORE_OUTPUT,
        own=(
            Opt(
                "min_bitrate",
                "--min-bitrate N",
                "Bitrate floor the art/bitrate bucket scores against (default 192).",
            ),
            Opt(
                "min_art_res",
                "--min-art-res N",
                "Cover resolution the art bucket scores against (default 500).",
            ),
        ),
    ),
    Mode(
        dest="health",
        flag="--health",
        category="audits",
        summary="One-screen digest across all lenses",
        detail="One walk, one screen: finding counts from every existing "
        "lens (tag completeness, bitrate floor, ReplayGain coverage, art, "
        "strays, playlists, exact-duplicate albums, worst health scores), "
        "each file's tags read once and fed to every lens. Every row points "
        "at the full-report mode. Read-only and always exit 0; the audits "
        "are the gates.",
        usage="lattice --health [ROOT] [--output PATH] [--json]",
        example="lattice --health ~/Music",
        shared=("root", "output", "json", "verbose", "quiet"),
        output_default=DEFAULT_HEALTH_OUTPUT,
        own=(
            Opt(
                "min_bitrate",
                "--min-bitrate N",
                "Bitrate floor for the digest's bitrate lens (default 192).",
            ),
            Opt(
                "min_art_res",
                "--min-art-res N",
                "Cover resolution for the digest's art lens (default 500).",
            ),
        ),
    ),
)

# auditReplayGain sits under audits (it is one); the replaygain category
# carries the verify mode and the write mode so the trio stays discoverable.
MODES = MODES + (
    Mode(
        dest="verify_replaygain",
        flag="--verifyReplayGain",
        category="replaygain",
        summary="Stored ReplayGain vs a fresh rsgain measure",
        detail="Re-measure each album read-only through rsgain (the same "
        "engine the writer uses) and report stored gains that disagree with "
        "the fresh measurement by more than --tolerance. replaygain_* keys "
        "verify at the reference --target-lufs implies; Opus R128 keys "
        "verify at the R128 -23 LUFS baseline. Requires rsgain; nothing is "
        "written.",
        usage="lattice --verifyReplayGain [ROOT] [--target-lufs N] [--tolerance DB]",
        example="lattice --verifyReplayGain ~/Music --target-lufs -14",
        shared=("root", "output", "verbose", "quiet"),
        output_default=DEFAULT_REPLAYGAIN_VERIFY_OUTPUT,
        own=(
            Opt(
                "target_lufs",
                "--target-lufs N",
                "The reference the stored replaygain_* values are checked "
                "against (default -18, the ReplayGain 2.0 reference; verify a "
                "-14-targeted library at -14). Opus R128 keys verify at the "
                "R128 -23 LUFS baseline their format implies either way.",
            ),
            Opt(
                "tolerance",
                "--tolerance DB",
                "Allowed |stored - expected| difference in dB (default 0.5).",
            ),
        ),
    ),
    Mode(
        dest="replaygain",
        flag="--replayGain",
        category="replaygain",
        summary="Write ReplayGain 2.0 tags via rsgain",
        detail="Scan and write ReplayGain 2.0 gain/peak tags album-by-album "
        "through rsgain easy (album = one folder, rescanned whole, so album "
        "gain stays correct). --skip-tagged skips fully-tagged albums as a "
        "unit; --target-lufs switches to a custom target; --threads parallelizes "
        "the scan. Requires rsgain (exit 2 without it). Write mode: dry-run "
        "by default, logged.",
        usage="lattice --replayGain [ROOT] [--apply] [--skip-tagged]",
        example="lattice --replayGain ~/Music --apply --skip-tagged",
        shared=("root_one", "where", "json", "apply", "dry_run", "quiet"),
        own=(
            Opt(
                "skip_tagged",
                "--skip-tagged",
                "Skip albums already fully tagged (track + album gain on "
                "every file), as a unit.",
            ),
            Opt(
                "target_lufs",
                "--target-lufs N",
                "Write at a custom loudness target in LUFS instead of the "
                "89 dB / -18 LUFS standard; switches rsgain to custom mode "
                "(range -30 to -5, e.g. -14 for streaming loudness).",
            ),
            Opt(
                "threads",
                "--threads N",
                "Parallel scan threads passed to rsgain (-m); standard mode "
                "only, ignored with --target-lufs.",
            ),
        ),
    ),
    # -- write modes --------------------------------------------------------
    Mode(
        dest="clean",
        flag="--clean",
        category="write",
        summary="Consolidate fragmented album folders",
        detail="Find folders whose names normalize to the same key (curly "
        "vs straight quotes, dash variants, casing) and merge the fragments "
        "into the survivor, renamed to its canonical form. Opt-in passes "
        "extend this to renaming folders and files and to a library-wide "
        "typographic tag normalization. Audio collisions are kept, never "
        "deleted. Write mode: dry-run by default, logged to cleanup.log.",
        usage="lattice --clean ROOT [--apply] [--all]",
        example="lattice --clean ~/Music --all            # preview everything\n"
        "lattice --clean ~/Music --all --apply    # commit",
        shared=("root_one", "layout", "json", "apply", "dry_run", "quiet"),
        own=(
            Opt(
                "clean_all",
                "--all",
                "Run every normalization pass (--normalize-names, "
                "--normalize-filenames, --normalize-tags).",
            ),
            Opt(
                "normalize_names",
                "--normalize-names",
                "Also rename non-duplicate folders at every depth to their "
                "normalized form (curly quotes and broken hyphens folded to "
                "ASCII).",
            ),
            Opt(
                "normalize_filenames",
                "--normalize-filenames",
                "Also rename audio track files the same way (extension kept; "
                "a distinct change from --normalize-names).",
            ),
            Opt(
                "normalize_tags",
                "--normalize-tags",
                "Also run the library-wide typographic tag normalization "
                "pass (title/album mojibake and quote/dash repair; the words "
                "never change).",
            ),
        ),
    ),
    Mode(
        dest="apestrip",
        flag="--apestrip",
        category="write",
        summary="Strip stray APEv2 tags from MP3s",
        detail="Delete hidden APEv2 tags from MP3s (torrent rips carry them; "
        "players that read APEv2 merge its values over ID3, so stale genres "
        "reappear). By default a pure strip: ID3 is left byte for byte. "
        "--keep-metadata migrates sole-source APE fields into ID3 first "
        "(never genre, never ratings); --repair-malformed fixes tags mutagen "
        "cannot parse. Write mode: dry-run by default, logged.",
        usage="lattice --apestrip ROOT [--apply] [--keep-metadata]",
        example="lattice --apestrip ~/Music --keep-metadata --apply",
        shared=("root_one", "json", "apply", "dry_run", "quiet"),
        own=(
            Opt(
                "keep_metadata",
                "--keep-metadata",
                "Before stripping, migrate APE fields not already in ID3 "
                "into the matching ID3 frame (genre is never migrated; "
                "ratings are never written).",
            ),
            Opt(
                "repair_malformed",
                "--repair-malformed",
                "Also repair malformed APE tags mutagen cannot parse, by "
                "excising the tag bytes directly (verified and atomic).",
            ),
        ),
    ),
    Mode(
        dest="lyrics",
        flag="--lyrics",
        category="write",
        summary="Fetch synced .lrc lyrics from LRCLIB",
        detail="Match each track against the LRCLIB API and write its synced "
        "lyrics as a same-basename .lrc sidecar beside the audio. Tracks "
        "with a sidecar are skipped unless --lyrics-force; instrumental and "
        "plain-text-only matches are reported, never written. The package's "
        "one network touchpoint. Write mode: dry-run by default.",
        usage="lattice --lyrics ROOT [--apply] [--lyrics-force]",
        example="lattice --lyrics ~/Music --apply",
        shared=("root_one", "apply", "dry_run", "quiet"),
        own=(
            Opt(
                "lyrics_force",
                "--lyrics-force",
                "Re-fetch and overwrite existing .lrc sidecars (default: "
                "tracks with a sidecar are skipped).",
            ),
            Opt(
                "lyrics_sleep",
                "--lyrics-sleep SECONDS",
                "Seconds to sleep between LRCLIB requests (default 0.5).",
            ),
        ),
    ),
    Mode(
        dest="retag",
        flag="--retag",
        category="write",
        display="--retag",
        summary="Rewrite genre tags on one album directory",
        detail="Hard-overwrite the genre tag(s) on one album directory: "
        "--retag DIR GENRE [GENRE...]. The package's one genre-write path; "
        "dir-scoped (a positional directory, --root refused). Deleting any "
        "APEv2 tag comes with the rewrite (the deadbeef-won't-update trap). "
        "--strip-junk strips junk ID3 frames instead and takes no genres. "
        "Write mode: dry-run by default, logged to <dir>/retag.log.",
        usage="lattice --retag DIR GENRE [GENRE...] [--apply]",
        example='lattice --retag "~/Music/Alice Coltrane/Universal Consciousness" '
        '"Spiritual Jazz" --apply',
        shared=("json", "apply", "dry_run", "quiet"),
        own=(
            Opt(
                "strip_junk",
                "--strip-junk",
                "Strip junk ID3 frames (obsolete v2.3-era and empty text "
                "frames) instead of rewriting genres; takes no genre "
                "arguments.",
            ),
        ),
    ),
    Mode(
        dest="genre_tidy_build",
        flag="--genreTidy-build",
        category="write",
        summary="Write the artist-to-genre authority map",
        detail="Scan (read-only, through the shared scanner) and write the "
        "editable artist-to-genre TSV map: one line per artist listing "
        "every genre it currently uses, most-common first. Tidying happens "
        "by removing a stray genre from a line; --genreTidy-apply then "
        "reconciles the library. Re-runs preserve edits and append only new "
        "artists; compilation artists are flagged EXCLUDED, never enforced.",
        usage="lattice --genreTidy-build ROOT [--map FILE]",
        example="lattice --genreTidy-build ~/Music --map artist_genre_defaults.tsv",
        shared=("root_one", "layout", "where", "quiet"),
        own=(
            Opt(
                "map_path",
                "--map FILE",
                "Authority-map path (default: <root>/genre_map.tsv; the repo "
                "ships a maintained map as artist_genre_defaults.tsv).",
            ),
        ),
    ),
    Mode(
        dest="genre_tidy_apply",
        flag="--genreTidy-apply",
        category="write",
        summary="Retag albums to the authority map",
        detail="Re-tag every album whose genre is not on its artist's map "
        "line to the line's first (canonical) genre, through the package's "
        "one genre-write path. Write mode: dry-run by default, logged to "
        "<root>/genre_tidy.log.",
        usage="lattice --genreTidy-apply ROOT [--map FILE] [--apply]",
        example="lattice --genreTidy-apply ~/Music --dry-run",
        shared=("root_one", "layout", "where", "json", "apply", "dry_run", "quiet"),
        own=(
            Opt(
                "map_path",
                "--map FILE",
                "Authority-map path (default: <root>/genre_map.tsv; the repo "
                "ships a maintained map as artist_genre_defaults.tsv).",
            ),
        ),
    ),
    Mode(
        dest="genre_map",
        flag="--genreMap",
        category="write",
        summary="Restructure into Genre/Artist/Album",
        detail="Move each album folder under its dominant genre tag, turning "
        "a flat Artist/Album library into Genre/Artist/Album. Depth-aware, "
        "gated by the genre vocabulary already in use (new top-level folders "
        "need --allow-new-genre), with a staging-inbox convention for "
        "untagged dumps. mv-only on one filesystem; every move is appended "
        "to a manifest TSV that --revert replays in reverse. Write mode: "
        "dry-run by default.",
        usage="lattice --genreMap ROOT [--apply] [--only-genre GENRE]",
        example='lattice --genreMap ~/Music --only-genre "Jazz" --apply',
        shared=("root_one", "where", "json", "apply", "dry_run", "quiet"),
        own=(
            Opt(
                "revert",
                "--revert MANIFEST",
                "Replay a run's manifest TSV in reverse (dry-run by default; "
                "add --apply to execute the restore).",
            ),
            Opt(
                "only_genre",
                "--only-genre GENRE",
                "Restrict the plan to this genre (repeatable), for a staged rollout.",
            ),
            Opt(
                "staging",
                "--staging DIR",
                "Name of the top-level staging inbox filed into the real "
                "taxonomy instead of read as a genre (default 'Unfiltered'; "
                "an empty string disables it).",
            ),
            Opt(
                "refile_mismatched",
                "--refile-mismatched",
                "Move an already-organized album whose tag genre disagrees "
                "with its genre folder to the tag's folder (still gated by "
                "the existing genre vocabulary).",
            ),
            Opt(
                "allow_new_genre",
                "--allow-new-genre",
                "Permit creating a new top-level genre folder when an "
                "album's genre is not already in use.",
            ),
        ),
    ),
    Mode(
        dest="ingest",
        flag="--ingest",
        category="write",
        summary="Import ritual: genreMap, apestrip, clean, health",
        detail="Sequence the import stages over one root: genreMap, then "
        "apestrip, then clean --all, then the post-state health digest. "
        "Each stage keeps its own dry-run/apply contract and log, confirmed "
        "per stage; a declined stage is recorded and skipped, never fatal. "
        "One summary file points into the per-stage logs; --baseline adds a "
        "diff report against a pre-import snapshot. Sequencing only, no new "
        "write verb.",
        usage="lattice --ingest ROOT [--apply] [--baseline SNAPSHOT]",
        example="lattice --ingest ~/Music/incoming --baseline before.tsv --dry-run",
        shared=("root_one", "json", "apply", "dry_run", "quiet"),
        own=(
            Opt(
                "baseline",
                "--baseline SNAPSHOT",
                "A --snapshot TSV taken before the import; the summary then "
                "points at a diff report showing exactly what the ritual "
                "changed.",
            ),
            Opt(
                "staging",
                "--staging DIR",
                "Staging-inbox name passed through to the genreMap stage "
                "(default 'Unfiltered'; an empty string disables it).",
            ),
            Opt(
                "allow_new_genre",
                "--allow-new-genre",
                "Passed through to the genreMap stage: permit new top-level "
                "genre folders.",
            ),
            Opt(
                "output",
                "--output PATH",
                "Summary-file path (default: <root>/ingest_summary.txt).",
            ),
        ),
    ),
)

_FLAG_TO_DEST: dict[str, str] = {}
_TOPIC_ALIASES: dict[str, str] = {}


def _key(token: str) -> str:
    return re.sub(r"[^a-z0-9]", "", token.lower())


def _register(m: Mode) -> None:
    token = m.flag.split()[0]
    _FLAG_TO_DEST[token] = m.dest
    if m.display:
        _FLAG_TO_DEST[m.display.split()[0]] = m.dest
    _TOPIC_ALIASES[_key(m.dest)] = m.dest
    _TOPIC_ALIASES[_key(token)] = m.dest


for _m in MODES:
    _register(_m)


def resolve_topic(token: str) -> str | None:
    """Map a help topic ("clean", "--genreTidy-build", "AUDIT-TAGS") to a
    mode dest, or None when nothing matches."""
    return _TOPIC_ALIASES.get(_key(token))


EXIT_CODES = {
    "0": "clean",
    "1": "audit findings, only with --fail-on-findings",
    "2": "usage error or missing dependency",
    "130": "interrupted",
}

# The single rule grammar, stated once: help text, --where, --playlist, and
# the machine surface all describe these seven fields and no others.
RULE_FIELDS = ("rating", "genre", "artist", "album", "title", "duration", "bitrate")


def help_json() -> str:
    """The whole surface as machine-readable JSON, generated from the
    registry (the same source the rendered pages read). Never colored."""
    import json

    payload = {
        "tool": "lattice",
        "version": VERSION,
        "usage": "lattice MODE [ROOT] [options] | lattice help MODE",
        "exit_codes": EXIT_CODES,
        "rule_fields": list(RULE_FIELDS),
        "modes": [
            {
                "dest": m.dest,
                "flag": m.flag,
                "category": m.category,
                "summary": m.summary,
                "detail": " ".join(m.detail.split()),
                "usage": m.usage,
                "example": m.example,
            }
            for m in MODES
        ],
        "shared_options": [
            {"dest": key, "flag": opt.flag, "text": " ".join(opt.text.split())}
            for key, opt in SHARED.items()
        ],
    }
    return json.dumps(payload, indent=1, sort_keys=True)


def single_mode_dest(argv: list[str]) -> str | None:
    """The one mode flag present in argv, if exactly one is; None when none
    or several are (several would be a mutually-exclusive error anyway)."""
    found: set[str] = set()
    for token in argv:
        dest = _FLAG_TO_DEST.get(token)
        if dest is not None:
            found.add(dest)
    if len(found) == 1:
        return found.pop()
    return None


def _two_col(rows: list[tuple[str, str]], indent: str = "  ") -> list[str]:
    """Render (left, right) rows as aligned two-column text, wrapped at
    _WIDTH. Left cells may carry ANSI codes, so the gutter is computed on
    visible length; a left column longer than the gutter pushes its right
    text to the next line."""
    pad = min(max((_visible_len(left) for left, _ in rows), default=0) + 2, 26)
    lines: list[str] = []
    for left, right in rows:
        wrapped = textwrap.wrap(
            right, width=_WIDTH - pad, break_long_words=False, break_on_hyphens=False
        )
        if not wrapped:
            lines.append(indent + left)
            continue
        if _visible_len(left) + 2 > pad:
            lines.append(indent + left)
            lines.append(indent + " " * pad + wrapped[0])
        else:
            lines.append(indent + left + " " * (pad - _visible_len(left)) + wrapped[0])
        for extra in wrapped[1:]:
            lines.append(indent + " " * pad + extra)
    return lines


def render_index(color: bool | None = None) -> str:
    st = _Style(_can_color() if color is None else color)
    lines = [
        st.tokens("lattice") + f" {VERSION} - filesystem-first music library toolkit",
        "",
        st.heading
        + "usage:"
        + st.reset
        + " "
        + st.tokens("lattice MODE [ROOT] [options]"),
        "       " + st.tokens("lattice help MODE") + "    full help for one mode",
        "       "
        + st.tokens("lattice")
        + "              interactive TUI (no arguments)",
        "",
    ]
    for key, heading in CATEGORIES:
        rows = [
            (st.tokens(m.display or m.flag), m.summary)
            for m in MODES
            if m.category == key
        ]
        if not rows:
            continue
        lines.append(st.heading + heading + st.reset)
        lines.extend(_two_col(rows))
        lines.append("")
    option_rows = [
        (
            st.tokens("-h, --help"),
            'This index; "lattice help MODE" shows one mode.',
        ),
        (st.tokens("--version"), "Show the version."),
        (
            st.tokens(SHARED["root"].flag),
            "Library root; repeatable (default: config or current dir).",
        ),
        (
            st.tokens(SHARED["output"].flag),
            "Report path; each mode's default is in its help page.",
        ),
        (
            st.tokens(SHARED["where"].flag),
            "Scope to tracks matching a playlist rule (tag-reading modes).",
        ),
        (
            st.tokens(SHARED["layout"].flag),
            "Path pattern (default: {artist}/{album}).",
        ),
        (st.tokens(SHARED["json"].flag), "Machine-readable output where supported."),
        (
            st.tokens(SHARED["fail_on_findings"].flag),
            "Audits exit 1 on findings (cron/CI gating).",
        ),
        (
            st.tokens("--apply / --dry-run"),
            "Write modes: commit vs preview (preview is the default).",
        ),
        (st.tokens(SHARED["quiet"].flag), "Minimize output."),
        (
            st.tokens(SHARED["verbose"].flag),
            "Extra detail on the audits that support it.",
        ),
    ]
    lines.append(st.heading + "OPTIONS" + st.reset)
    lines.extend(_two_col(option_rows))
    lines.append("")
    lines.append("Every mode above has a full help page:")
    lines.append(
        "  "
        + st.tokens("lattice help MODE")
        + "    (try: "
        + st.tokens("lattice help clean")
        + ")"
    )
    lines.append(
        "  " + st.tokens("lattice help --json") + "    the whole surface as JSON"
    )
    lines.append("")
    lines.append(
        "exit codes: 0 clean; 1 audit findings (only with --fail-on-findings); "
        "2 usage or missing dependency; 130 interrupted"
    )
    return "\n".join(lines)


def render_mode(dest: str, color: bool | None = None) -> str:
    st = _Style(_can_color() if color is None else color)
    m = next(m for m in MODES if m.dest == dest)
    lines = [
        st.tokens(m.display or m.flag) + ": " + m.summary.lower(),
        "",
        st.heading + "usage:" + st.reset + " " + st.tokens(m.usage),
        "",
    ]
    lines.extend(textwrap.wrap(m.detail, width=_WIDTH))
    lines.append("")
    if m.own:
        lines.append(st.heading + "mode options:" + st.reset)
        lines.extend(_two_col([(st.tokens(o.flag), o.text) for o in m.own]))
        lines.append("")
    shared_rows: list[tuple[str, str]] = []
    for dest_name in m.shared:
        opt = SHARED[dest_name]
        if dest_name == "output":
            text = m.output_note or opt.text + (
                f" Default: {m.output_default}." if m.output_default else ""
            )
        else:
            text = opt.text
        shared_rows.append((st.tokens(opt.flag), text))
    if shared_rows:
        lines.append(st.heading + "options that apply:" + st.reset)
        lines.extend(_two_col(shared_rows))
        lines.append("")
    lines.append(st.heading + "example:" + st.reset)
    for example_line in m.example.splitlines():
        lines.append("  " + st.tokens(example_line))
    return "\n".join(lines)
