import argparse
import os
import sys

from lattice.config import (
    DEFAULT_AI_LIBRARY_OUTPUT,
    DEFAULT_ART_QUALITY_OUTPUT,
    DEFAULT_ALBUM_CONSISTENCY_OUTPUT,
    DEFAULT_AUDIO_DUPES_OUTPUT,
    DEFAULT_BITRATE_AUDIT_OUTPUT,
    DEFAULT_DUPLICATES_OUTPUT,
    DEFAULT_FLAC_OUTPUT,
    DEFAULT_HEALTH_SCORE_OUTPUT,
    DEFAULT_LIBRARY_OUTPUT,
    DEFAULT_MISSING_ART_OUTPUT,
    DEFAULT_MP3_OUTPUT,
    DEFAULT_OPUS_OUTPUT,
    DEFAULT_PLAYLIST_OUTPUT,
    DEFAULT_PLAYLIST_CHECK_OUTPUT,
    DEFAULT_REPLAYGAIN_AUDIT_OUTPUT,
    DEFAULT_REPLAYGAIN_VERIFY_OUTPUT,
    DEFAULT_STRAY_AUDIT_OUTPUT,
    DEFAULT_TAG_AUDIT_OUTPUT,
    DEFAULT_WAV_OUTPUT,
    DEFAULT_WMA_OUTPUT,
    VERSION,
    get_layout,
)
from lattice.modes.apestrip import run_apestrip
from lattice.modes.artwork import (
    run_art_quality_audit,
    run_extract_art,
    run_missing_art,
)
from lattice.modes.audit import (
    run_album_consistency,
    run_audio_dupes,
    run_bitrate_audit,
    run_duplicates,
    run_health_score,
    run_replaygain_audit,
    run_stray_audit,
    run_tag_audit,
    run_verify_replaygain,
)
from lattice.modes.clean import run_clean
from lattice.modes.integrity import (
    run_flac_mode,
    run_mp3_mode,
    run_opus_mode,
    run_wav_mode,
    run_wma_mode,
)
from lattice.modes.library import (
    write_ai_library,
    write_ai_wings,
    write_all_wings,
    write_music_library_tree,
)
from lattice.modes.playlists import generate_playlist, run_check_playlists
from lattice.modes.stats import run_stats
from lattice.tui import interactive_menu


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lattice",
        description="Filesystem-first music library toolkit: trees, integrity, "
        "audits, content-hash duplicate detection, health score, write modes",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--library", action="store_true", help="Generate library tree")
    group.add_argument(
        "--ai-library",
        dest="ai_library",
        action="store_true",
        help="Generate token-efficient library for AI recommendations",
    )
    group.add_argument(
        "--all-wings",
        dest="all_wings",
        action="store_true",
        help="Generate separate library files for each genre",
    )
    group.add_argument(
        "--ai-wings",
        dest="ai_wings",
        action="store_true",
        help="Generate separate AI-friendly library files for each genre",
    )
    group.add_argument("--testFLAC", action="store_true", help="Verify FLAC files")
    group.add_argument("--testMP3", action="store_true", help="Verify MP3 files")
    group.add_argument(
        "--testOpus", action="store_true", help="Verify Opus files via FFmpeg decode"
    )
    group.add_argument(
        "--testWAV", action="store_true", help="Verify WAV files via FFmpeg decode"
    )
    group.add_argument(
        "--testWMA", action="store_true", help="Verify WMA files via FFmpeg decode"
    )
    group.add_argument(
        "--extractArt", action="store_true", help="Extract embedded cover art to folder"
    )
    group.add_argument(
        "--missingArt", action="store_true", help="Report directories missing cover art"
    )
    group.add_argument(
        "--auditArtQuality",
        action="store_true",
        help="Report extracted/folder covers below a resolution threshold",
    )
    group.add_argument(
        "--duplicates",
        action="store_true",
        help="Four-section dupe report: exact albums, within-folder multi-format, similar names, track-level",
    )
    group.add_argument(
        "--auditAudioDupes",
        dest="audit_audio_dupes",
        action="store_true",
        help="Content-hash duplicate detection: exact sha256, audio-stream, "
        "and head/tail sampled matches (catches retagged or renamed dupes)",
    )
    group.add_argument(
        "--auditTags", action="store_true", help="Report files with incomplete tags"
    )
    group.add_argument(
        "--auditAlbums",
        dest="audit_albums",
        action="store_true",
        help="Per-album consistency audit: mixed codecs, track-number gaps and "
        "duplicates, and missing or divergent year tags",
    )
    group.add_argument(
        "--auditBitrate",
        action="store_true",
        help="Report files below a certain bitrate floor",
    )
    group.add_argument(
        "--auditReplayGain",
        action="store_true",
        help="Report per-album ReplayGain coverage (missing, partial, no album gain)",
    )
    group.add_argument(
        "--verifyReplayGain",
        dest="verify_replaygain",
        action="store_true",
        help="Verify stored ReplayGain values against a fresh read-only rsgain "
        "measurement (requires rsgain; nothing is written)",
    )
    group.add_argument(
        "--auditStrays",
        dest="audit_strays",
        action="store_true",
        help="Report audio outside the layout's album depth, loose tracks, "
        "hidden-dir audio, and unrecognized non-audio files in album folders",
    )
    group.add_argument(
        "--healthScore",
        dest="health_score",
        action="store_true",
        help="Per-album health score aggregating tag completeness, "
        "ReplayGain coverage, art, and the bitrate floor",
    )
    group.add_argument(
        "--playlist",
        action="store_true",
        help="Generate a smart .m3u playlist based on a rule",
    )
    group.add_argument(
        "--checkPlaylists",
        dest="check_playlists",
        action="store_true",
        help="Verify the library's .m3u playlists: missing #EXTM3U headers and "
        "entries whose target no longer exists",
    )
    group.add_argument(
        "--stats", action="store_true", help="Library-wide statistics summary"
    )
    group.add_argument(
        "--clean",
        action="store_true",
        help="Consolidate fragmented album folders; optionally normalize names "
        "and tags (write mode: dry-run by default, --apply to write)",
    )
    group.add_argument(
        "--apestrip",
        action="store_true",
        help="Strip stray APEv2 tags from MP3s (write mode: dry-run by "
        "default, --apply to write)",
    )

    p.add_argument(
        "--root",
        action="append",
        default=None,
        metavar="DIR",
        help="Root directory; repeat --root to scan several libraries together "
        "(default: read from config or current dir)",
    )
    p.add_argument(
        "pos_root", nargs="?", default=None, help="Root directory (positional fallback)"
    )
    p.add_argument("--output", default=None, help="Output path")
    p.add_argument(
        "--rule",
        default="",
        help="Smart playlist rule (e.g. \"rating >= 4 and genre == 'Jazz'\")",
    )
    p.add_argument(
        "--layout",
        default=None,
        help="Directory structure pattern for extracting tags from path "
        "(default: the `layout` config key, or {artist}/{album}). "
        "Use {genre}/{artist}/{album} for a genre-first library.",
    )
    p.add_argument(
        "--min-art-res",
        type=int,
        default=500,
        help="Minimum resolution in pixels for --auditArtQuality (default: 500)",
    )
    p.add_argument(
        "--min-bitrate",
        type=int,
        default=192,
        help="Minimum bitrate in kbps for --auditBitrate (default: 192)",
    )
    p.add_argument(
        "--target-lufs",
        dest="target_lufs",
        type=float,
        default=-18.0,
        help="Assumed ReplayGain write target in LUFS for --verifyReplayGain "
        "(default: -18, the ReplayGain 2.0 reference; verify a -14-targeted "
        "library at -14)",
    )
    p.add_argument(
        "--tolerance",
        type=float,
        default=0.5,
        help="Allowed |stored - expected| in dB for --verifyReplayGain (default: 0.5)",
    )
    p.add_argument(
        "--workers", type=int, default=4, help="Parallel workers (integrity modes)"
    )
    p.add_argument(
        "--prefer",
        choices=["flac", "ffmpeg"],
        default="flac",
        help="Preferred tool (FLAC mode)",
    )
    p.add_argument("--quiet", action="store_true", help="Minimize output")
    p.add_argument(
        "--genres", action="store_true", help="Include album genres in library tree"
    )
    p.add_argument(
        "--paths",
        action="store_true",
        help="Include absolute directory paths at the album level",
    )
    p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Preview changes without writing (extractArt, clean, apestrip)",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Write for real (clean, apestrip); without it these modes only preview",
    )
    p.add_argument(
        "--normalize-names",
        action="store_true",
        help="--clean: also rename non-duplicate folders at every depth with "
        "non-standard characters to their normalized form",
    )
    p.add_argument(
        "--normalize-filenames",
        action="store_true",
        help="--clean: also rename audio track files the same way (a distinct "
        "change from --normalize-names)",
    )
    p.add_argument(
        "--normalize-tags",
        action="store_true",
        help="--clean: library-wide typographic tag normalization (Pass 4)",
    )
    p.add_argument(
        "--all",
        dest="clean_all",
        action="store_true",
        help="--clean: run all normalization passes (--normalize-names, "
        "--normalize-filenames, --normalize-tags)",
    )
    p.add_argument(
        "--keep-metadata",
        action="store_true",
        help="--apestrip: before stripping, migrate APE fields not already in "
        "ID3 into the matching ID3 frame (genre is never migrated, ratings "
        "never written)",
    )
    p.add_argument(
        "--repair-malformed",
        action="store_true",
        help="--apestrip: also repair malformed APE tags mutagen cannot parse, "
        "by excising the tag bytes directly (verified + atomic)",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="--testFLAC / --testMP3: reuse the verdicts recorded by an "
        "interrupted run (<output>.progress.json) and scan only the remaining "
        "files; finishing a scan clears the state",
    )

    p.add_argument(
        "--only-errors",
        dest="only_errors",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write only errors/warns (MP3/Opus/WAV/WMA modes)",
    )

    p.add_argument("--ffmpeg", default=None, help="Path to ffmpeg")
    p.add_argument("--verbose", action="store_true", help="Verbose output")
    return p


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) == 0:
        return interactive_menu()

    try:
        args = build_parser().parse_args(argv)

        # Resolve the path-extraction layout: an explicit --layout wins,
        # otherwise fall back to the configured/default layout. (The mode flags
        # are a single argparse mutually-exclusive group, so picking more than
        # one mode is already rejected at parse time.)
        if args.layout is None:
            args.layout = get_layout()

        # Every named root (positional + each --root) is scanned together;
        # de-dupe so the same path passed twice isn't walked twice.
        raw_roots = list(args.root or [])
        if args.pos_root is not None:
            raw_roots.append(args.pos_root)

        if raw_roots:
            seen: set[str] = set()
            root: list[str] = []
            for r in raw_roots:
                ar = os.path.abspath(os.path.expanduser(r))
                if ar not in seen:
                    seen.add(ar)
                    root.append(ar)
        else:
            from lattice.config import get_library_roots, set_library_root

            # isdir, not exists: a root that is now a plain file would "scan"
            # zero files silently. The TUI already validates with isdir.
            config_roots = [r for r in get_library_roots() if r and os.path.isdir(r)]
            if config_roots:
                root = config_roots
            elif sys.stdin.isatty():
                print("First run: No library root configured.")
                raw_input_root = input(
                    "Enter path to your music library (or press Enter for current directory): "
                ).strip()
                if raw_input_root:
                    single = os.path.abspath(os.path.expanduser(raw_input_root))
                    set_library_root(single)
                    print(f"Library root saved to {single}")
                    root = [single]
                else:
                    root = [os.path.abspath(".")]
            else:
                root = [os.path.abspath(".")]

        if args.library:
            output = args.output or DEFAULT_LIBRARY_OUTPUT
            write_music_library_tree(
                root,
                output,
                layout=args.layout,
                quiet=args.quiet,
                show_genre=args.genres,
            )
            return 0

        if args.ai_library:
            output = args.output or DEFAULT_AI_LIBRARY_OUTPUT
            write_ai_library(root, output, layout=args.layout, quiet=args.quiet)
            return 0

        if args.all_wings:
            outdir = args.output or "wings"
            return write_all_wings(
                root,
                outdir,
                layout=args.layout,
                quiet=args.quiet,
                show_genre=args.genres,
                show_paths=args.paths,
            )

        if args.ai_wings:
            outdir = args.output or "wings_ai"
            return write_ai_wings(root, outdir, layout=args.layout, quiet=args.quiet)

        if args.testFLAC:
            output = args.output or DEFAULT_FLAC_OUTPUT
            return run_flac_mode(
                root,
                output,
                args.workers,
                args.prefer,
                ffmpeg=args.ffmpeg,
                resume=args.resume,
                quiet=args.quiet,
            )

        if args.testMP3:
            output = args.output or DEFAULT_MP3_OUTPUT
            return run_mp3_mode(
                root,
                output,
                args.workers,
                args.ffmpeg,
                only_errors=args.only_errors,
                verbose=args.verbose,
                quiet=args.quiet,
                resume=args.resume,
            )

        if args.testOpus:
            output = args.output or DEFAULT_OPUS_OUTPUT
            return run_opus_mode(
                root,
                output,
                args.workers,
                args.ffmpeg,
                only_errors=args.only_errors,
                verbose=args.verbose,
                quiet=args.quiet,
            )

        if args.testWAV:
            output = args.output or DEFAULT_WAV_OUTPUT
            return run_wav_mode(
                root,
                output,
                args.workers,
                args.ffmpeg,
                only_errors=args.only_errors,
                verbose=args.verbose,
                quiet=args.quiet,
            )

        if args.testWMA:
            output = args.output or DEFAULT_WMA_OUTPUT
            return run_wma_mode(
                root,
                output,
                args.workers,
                args.ffmpeg,
                only_errors=args.only_errors,
                verbose=args.verbose,
                quiet=args.quiet,
            )

        if args.extractArt:
            return run_extract_art(root, quiet=args.quiet, dry_run=args.dry_run)

        if args.missingArt:
            output = args.output or DEFAULT_MISSING_ART_OUTPUT
            return run_missing_art(root, output, quiet=args.quiet)

        if args.auditArtQuality:
            output = args.output or DEFAULT_ART_QUALITY_OUTPUT
            return run_art_quality_audit(
                root, output, args.min_art_res, quiet=args.quiet
            )

        if args.duplicates:
            output = args.output or DEFAULT_DUPLICATES_OUTPUT
            return run_duplicates(root, output, quiet=args.quiet)

        if args.audit_audio_dupes:
            output = args.output or DEFAULT_AUDIO_DUPES_OUTPUT
            return run_audio_dupes(root, output, quiet=args.quiet)

        if args.auditTags:
            output = args.output or DEFAULT_TAG_AUDIT_OUTPUT
            return run_tag_audit(root, output, quiet=args.quiet)

        if args.audit_albums:
            output = args.output or DEFAULT_ALBUM_CONSISTENCY_OUTPUT
            return run_album_consistency(
                root, output, verbose=args.verbose, quiet=args.quiet
            )

        if args.auditBitrate:
            output = args.output or DEFAULT_BITRATE_AUDIT_OUTPUT
            return run_bitrate_audit(root, output, args.min_bitrate, quiet=args.quiet)

        if args.auditReplayGain:
            output = args.output or DEFAULT_REPLAYGAIN_AUDIT_OUTPUT
            return run_replaygain_audit(
                root, output, verbose=args.verbose, quiet=args.quiet
            )

        if args.verify_replaygain:
            output = args.output or DEFAULT_REPLAYGAIN_VERIFY_OUTPUT
            return run_verify_replaygain(
                root,
                output,
                target_lufs=args.target_lufs,
                tolerance=args.tolerance,
                verbose=args.verbose,
                quiet=args.quiet,
            )

        if args.audit_strays:
            output = args.output or DEFAULT_STRAY_AUDIT_OUTPUT
            return run_stray_audit(root, output, layout=args.layout, quiet=args.quiet)

        if args.health_score:
            output = args.output or DEFAULT_HEALTH_SCORE_OUTPUT
            return run_health_score(
                root,
                output,
                min_kbps=args.min_bitrate,
                min_res=args.min_art_res,
                verbose=args.verbose,
                quiet=args.quiet,
            )

        if args.playlist:
            output = args.output or DEFAULT_PLAYLIST_OUTPUT
            return generate_playlist(
                root, output, args.rule, layout=args.layout, quiet=args.quiet
            )

        if args.check_playlists:
            output = args.output or DEFAULT_PLAYLIST_CHECK_OUTPUT
            return run_check_playlists(
                root, output, verbose=args.verbose, quiet=args.quiet
            )

        if args.stats:
            run_stats(root, args.output, layout=args.layout, quiet=args.quiet)
            return 0

        # The write modes operate on exactly one tree (like the companion
        # scripts they replace), not on an aggregated root list.
        if args.clean or args.apestrip:
            if len(root) != 1:
                print(
                    "error: this write mode needs exactly one library root; "
                    "pass one DIR (or configure a single library_root)",
                    file=sys.stderr,
                )
                return 2
            dry_run = args.dry_run or not args.apply
            if args.clean:
                return run_clean(
                    root[0],
                    dry_run=dry_run,
                    normalize_names=args.normalize_names or args.clean_all,
                    normalize_filenames=args.normalize_filenames or args.clean_all,
                    normalize_tags=args.normalize_tags or args.clean_all,
                    layout=args.layout,
                    quiet=args.quiet,
                )
            return run_apestrip(
                root[0],
                dry_run=dry_run,
                keep_metadata=args.keep_metadata,
                repair_malformed=args.repair_malformed,
                quiet=args.quiet,
            )

        build_parser().print_help()
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        return 130
