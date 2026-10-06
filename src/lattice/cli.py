import argparse
import os
import sys
from pathlib import Path

from lattice import help as lattice_help
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
    get_layout,
)
from lattice.modes.apestrip import run_apestrip
from lattice.modes.artwork import (
    run_art_mismatch_audit,
    run_art_quality_audit,
    run_extract_art,
    run_missing_art,
)
from lattice.modes.audit import (
    run_album_consistency,
    run_audio_dupes,
    run_bitrate_audit,
    run_duplicates,
    run_health,
    run_health_score,
    run_junk_frame_audit,
    run_replaygain_audit,
    run_stray_audit,
    run_tag_audit,
    run_verify_replaygain,
)
from lattice.modes.clean import run_clean
from lattice.modes.foldermap import revert as revert_genremap
from lattice.modes.foldermap import run_genremap
from lattice.modes.genretidy import run_genre_tidy_apply, run_genre_tidy_build
from lattice.modes.ingest import run_ingest
from lattice.modes.integrity import (
    run_flac_mode,
    run_mp3_mode,
    run_opus_mode,
    run_wav_mode,
    run_wma_mode,
)
from lattice.modes.library import (
    diff_snapshot,
    write_ai_library,
    write_ai_wings,
    write_all_wings,
    write_music_library_tree,
    write_snapshot,
)
from lattice.modes.lyrics import run_lyrics
from lattice.modes.playlists import (
    RuleError,
    compile_where,
    generate_playlist,
    run_check_playlists,
)
from lattice.modes.replaygain import run_replaygain
from lattice.modes.retag import run_retag
from lattice.modes.stats import run_stats
from lattice.tui import interactive_menu

# Mode-flag dests that accept --where: the scanner-driven exports, the
# tag-reading audits, and the album-granular write targeting. Anything not
# listed refuses --with an explicit error, so a new mode opts in by adding its
# dispatch here, never by silently ignoring the rule.
_WHERE_MODES = frozenset(
    {
        "library",
        "ai_library",
        "all_wings",
        "ai_wings",
        "stats",
        "auditTags",
        "audit_albums",
        "auditBitrate",
        "auditReplayGain",
        "health_score",
        "duplicates",
        "genre_tidy_build",
        "genre_tidy_apply",
        "genre_map",
        "replaygain",
    }
)

# Mode-flag dests that accept --json: the tag-reading audits, --stats, and the
# write modes (JSON run summary). Everything else refuses, like --where.
_JSON_MODES = frozenset(
    {
        "ingest",
        "health",
        "stats",
        "auditTags",
        "audit_albums",
        "auditBitrate",
        "auditReplayGain",
        "health_score",
        "duplicates",
        "clean",
        "apestrip",
        "replaygain",
        "retag",
        "genre_tidy_apply",
        "genre_map",
    }
)

# The audits whose findings can gate --fail-on-findings.
_FAIL_MODES = frozenset(
    {
        "auditTags",
        "audit_albums",
        "auditBitrate",
        "auditReplayGain",
        "health_score",
        "duplicates",
    }
)


class _Parser(argparse.ArgumentParser):
    # format_help renders the registry index; main() intercepts -h/--help
    # before parse_args, so argparse's flat listing is never what a user
    # sees. Help text lives in lattice/help.py, not on these arguments.
    def format_help(self) -> str:
        from lattice import help as lattice_help

        return lattice_help.render_index()


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog="lattice",
        description="Filesystem-first music library toolkit: trees, integrity, "
        "audits, content-hash duplicate detection, health score, write modes",
        usage="lattice MODE [ROOT] [options]  (lattice --help for the index)",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--library", action="store_true")
    group.add_argument("--ai-library", dest="ai_library", action="store_true")
    group.add_argument("--all-wings", dest="all_wings", action="store_true")
    group.add_argument("--ai-wings", dest="ai_wings", action="store_true")
    group.add_argument("--testFLAC", action="store_true")
    group.add_argument("--testMP3", action="store_true")
    group.add_argument("--testOpus", action="store_true")
    group.add_argument("--testWAV", action="store_true")
    group.add_argument("--testWMA", action="store_true")
    group.add_argument("--extractArt", action="store_true")
    group.add_argument("--missingArt", action="store_true")
    group.add_argument("--auditArtQuality", action="store_true")
    group.add_argument(
        "--auditArtMismatch", dest="audit_art_mismatch", action="store_true"
    )
    group.add_argument("--duplicates", action="store_true")
    group.add_argument(
        "--auditAudioDupes", dest="audit_audio_dupes", action="store_true"
    )
    group.add_argument("--auditTags", action="store_true")
    group.add_argument(
        "--auditJunkFrames", dest="audit_junk_frames", action="store_true"
    )
    group.add_argument("--auditAlbums", dest="audit_albums", action="store_true")
    group.add_argument("--auditBitrate", action="store_true")
    group.add_argument("--auditReplayGain", action="store_true")
    group.add_argument(
        "--verifyReplayGain", dest="verify_replaygain", action="store_true"
    )
    group.add_argument("--auditStrays", dest="audit_strays", action="store_true")
    group.add_argument("--healthScore", dest="health_score", action="store_true")
    group.add_argument("--health", action="store_true")
    group.add_argument("--playlist", action="store_true")
    group.add_argument("--checkPlaylists", dest="check_playlists", action="store_true")
    group.add_argument("--stats", action="store_true")
    group.add_argument("--snapshot", action="store_true")
    group.add_argument("--diff", dest="diff_snapshot", metavar="SNAPSHOT", default=None)
    group.add_argument("--clean", action="store_true")
    group.add_argument("--apestrip", action="store_true")
    group.add_argument("--lyrics", action="store_true")
    group.add_argument("--replayGain", dest="replaygain", action="store_true")
    group.add_argument("--retag", action="store_true")
    group.add_argument(
        "--genreTidy-build", dest="genre_tidy_build", action="store_true"
    )
    group.add_argument(
        "--genreTidy-apply", dest="genre_tidy_apply", action="store_true"
    )
    group.add_argument("--ingest", action="store_true")
    group.add_argument("--genreMap", dest="genre_map", action="store_true")

    p.add_argument("--root", action="append", default=None, metavar="DIR")
    p.add_argument("pos_root", nargs="?", default=None)
    p.add_argument("retag_genres", nargs="*", default=[])
    p.add_argument("--output", default=None)
    p.add_argument("--rule", default="")
    p.add_argument("--where", default=None, metavar="EXPR")
    p.add_argument("--layout", default=None)
    p.add_argument("--min-art-res", type=int, default=500)
    p.add_argument("--min-bitrate", type=int, default=192)
    p.add_argument(
        "--target-lufs", dest="target_lufs", type=float, default=None, metavar="N"
    )
    p.add_argument("--tolerance", type=float, default=0.5)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--threads", type=int, default=1)
    p.add_argument("--skip-tagged", action="store_true")
    p.add_argument("--prefer", choices=["flac", "ffmpeg"], default="flac")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--fail-on-findings", dest="fail_on_findings", action="store_true")
    p.add_argument("--genres", action="store_true")
    p.add_argument("--paths", action="store_true")
    p.add_argument("--dry-run", dest="dry_run", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--normalize-names", action="store_true")
    p.add_argument("--normalize-filenames", action="store_true")
    p.add_argument("--normalize-tags", action="store_true")
    p.add_argument("--all", dest="clean_all", action="store_true")
    p.add_argument("--keep-metadata", action="store_true")
    p.add_argument("--repair-malformed", action="store_true")
    p.add_argument("--strip-junk", action="store_true")
    p.add_argument("--map", dest="map_path", default=None, metavar="FILE")
    p.add_argument("--baseline", metavar="SNAPSHOT", default=None)
    p.add_argument("--revert", metavar="MANIFEST", default=None)
    p.add_argument("--only-genre", action="append", metavar="GENRE")
    p.add_argument("--staging", default=None, metavar="DIR")
    p.add_argument("--refile-mismatched", action="store_true")
    p.add_argument("--allow-new-genre", action="store_true")
    p.add_argument("--lyrics-force", action="store_true")
    p.add_argument("--lyrics-sleep", type=float, default=0.5)
    p.add_argument("--resume", action="store_true")
    p.add_argument(
        "--only-errors",
        dest="only_errors",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    p.add_argument("--ffmpeg", default=None)
    p.add_argument("--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) == 0:
        return interactive_menu()

    # The help surface is intercepted before argparse: the index and the
    # per-mode pages render from the registry in lattice/help.py, their only
    # home. `lattice help MODE` and `MODE --help` land here; bare -h/--help
    # prints the index (exit 0, argparse's own convention).
    if argv[0].lower() == "help":
        if len(argv) == 1:
            print(lattice_help.render_index())
            return 0
        if argv[1] == "--json":
            # the machine surface: the whole registry as JSON, never colored
            print(lattice_help.help_json())
            return 0
        dest = lattice_help.resolve_topic(argv[1])
        if dest is None:
            print(
                f'error: unknown help topic "{argv[1]}"; '
                '"lattice --help" lists the modes',
                file=sys.stderr,
            )
            return 2
        print(lattice_help.render_mode(dest))
        return 0
    if "-h" in argv or "--help" in argv:
        h = "-h" if "-h" in argv else "--help"
        hi = argv.index(h)
        dest = lattice_help.single_mode_dest(argv)
        if dest is None and hi + 1 < len(argv) and not argv[hi + 1].startswith("-"):
            # `--help MODE` is a per-mode form too: the token after the help
            # flag is a topic candidate, and a non-topic word is a typo worth
            # surfacing rather than silently printing the index
            candidate = argv[hi + 1]
            dest = lattice_help.resolve_topic(candidate)
            if dest is None:
                print(
                    f'error: unknown help topic "{candidate}"; '
                    '"lattice --help" lists the modes',
                    file=sys.stderr,
                )
                return 2
        if dest is not None:
            print(lattice_help.render_mode(dest))
        else:
            print(lattice_help.render_index())
        return 0

    try:
        args = build_parser().parse_args(argv)

        # The trailing positionals exist for --retag (DIR GENRE [GENRE...]);
        # every other mode must not silently swallow them.
        if args.retag_genres and not args.retag:
            print(
                "error: extra arguments are only valid with --retag "
                "(--retag DIR GENRE [GENRE...])",
                file=sys.stderr,
            )
            return 2

        # --json / --fail-on-findings are opt-in machine shapes with the same
        # explicit-support discipline as --where: a mode not listed refuses
        # rather than silently ignoring the flag.
        if args.json and not any(getattr(args, d) for d in _JSON_MODES):
            print(
                "error: --json does not apply to this mode",
                file=sys.stderr,
            )
            return 2
        if args.fail_on_findings and not any(getattr(args, d) for d in _FAIL_MODES):
            print(
                "error: --fail-on-findings applies to the audit modes only",
                file=sys.stderr,
            )
            return 2

        # --where scopes the tag-reading modes only; a rule handed to a mode
        # that never reads tags (or whose targeting is not tag-based) is a
        # caller mistake, refused rather than silently ignored.
        where_pred = None
        if args.where:
            chosen = [dest for dest in _WHERE_MODES if getattr(args, dest)]
            if not chosen:
                print(
                    "error: --where does not scope this mode (it never reads "
                    "the tags the rule evaluates, or its targeting is not "
                    "tag-based)",
                    file=sys.stderr,
                )
                return 2
            try:
                where_pred = compile_where(args.where)
            except RuleError as e:
                print(f"error: invalid --where rule: {e}", file=sys.stderr)
                return 2

        # Where-scoped dispatch passes the compiled predicate; the other
        # branches never see it.
        where = where_pred

        # Resolve the path-extraction layout: an explicit --layout wins,
        # otherwise fall back to the configured/default layout. (The mode flags
        # are a single argparse mutually-exclusive group, so picking more than
        # one mode is already rejected at parse time.)
        if args.layout is None:
            args.layout = get_layout()

        # --retag is the one dir-scoped write mode: its positional is a single
        # album directory, not a root walk. It dispatches before the root
        # resolution so a bare `--retag` can never fall into the config-root
        # / first-run prompt path, and --root (let alone a repeated one) is a
        # caller mistake rather than an input.
        if args.retag:
            if args.root:
                print(
                    "error: --retag takes one directory positional "
                    "(--retag DIR GENRE [GENRE...]), not --root",
                    file=sys.stderr,
                )
                return 2
            if args.pos_root is None:
                print(
                    "error: --retag needs a directory: --retag DIR GENRE [GENRE...]",
                    file=sys.stderr,
                )
                return 2
            if args.strip_junk and args.retag_genres:
                print(
                    "error: --strip-junk takes no genre arguments",
                    file=sys.stderr,
                )
                return 2
            if not args.strip_junk and not args.retag_genres:
                print(
                    "error: --retag needs one or more genres (or --strip-junk)",
                    file=sys.stderr,
                )
                return 2
            return run_retag(
                args.pos_root,
                args.retag_genres,
                dry_run=args.dry_run or not args.apply,
                strip_junk=args.strip_junk,
                log_path=os.path.join(args.pos_root, "retag.log"),
                quiet=args.quiet,
                json_mode=args.json,
            )

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
                where=where,
            )
            return 0

        if args.ai_library:
            output = args.output or DEFAULT_AI_LIBRARY_OUTPUT
            write_ai_library(
                root, output, layout=args.layout, quiet=args.quiet, where=where
            )
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
                where=where,
            )

        if args.ai_wings:
            outdir = args.output or "wings_ai"
            return write_ai_wings(
                root, outdir, layout=args.layout, quiet=args.quiet, where=where
            )

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

        if args.audit_art_mismatch:
            output = args.output or DEFAULT_ART_MISMATCH_OUTPUT
            return run_art_mismatch_audit(
                root, output, verbose=args.verbose, quiet=args.quiet
            )

        if args.duplicates:
            output = args.output or DEFAULT_DUPLICATES_OUTPUT
            return run_duplicates(
                root,
                output,
                quiet=args.quiet,
                where=where,
                json_mode=args.json,
                fail_on_findings=args.fail_on_findings,
            )

        if args.audit_audio_dupes:
            output = args.output or DEFAULT_AUDIO_DUPES_OUTPUT
            return run_audio_dupes(root, output, quiet=args.quiet)

        if args.auditTags:
            output = args.output or DEFAULT_TAG_AUDIT_OUTPUT
            return run_tag_audit(
                root,
                output,
                quiet=args.quiet,
                where=where,
                json_mode=args.json,
                fail_on_findings=args.fail_on_findings,
            )

        if args.audit_albums:
            output = args.output or DEFAULT_ALBUM_CONSISTENCY_OUTPUT
            return run_album_consistency(
                root,
                output,
                verbose=args.verbose,
                quiet=args.quiet,
                where=where,
                json_mode=args.json,
                fail_on_findings=args.fail_on_findings,
            )

        if args.audit_junk_frames:
            output = args.output or DEFAULT_JUNK_FRAME_OUTPUT
            return run_junk_frame_audit(
                root, output, verbose=args.verbose, quiet=args.quiet
            )

        if args.auditBitrate:
            output = args.output or DEFAULT_BITRATE_AUDIT_OUTPUT
            return run_bitrate_audit(
                root,
                output,
                args.min_bitrate,
                quiet=args.quiet,
                where=where,
                json_mode=args.json,
                fail_on_findings=args.fail_on_findings,
            )

        if args.auditReplayGain:
            output = args.output or DEFAULT_REPLAYGAIN_AUDIT_OUTPUT
            return run_replaygain_audit(
                root,
                output,
                verbose=args.verbose,
                quiet=args.quiet,
                where=where,
                json_mode=args.json,
                fail_on_findings=args.fail_on_findings,
            )

        if args.verify_replaygain:
            output = args.output or DEFAULT_REPLAYGAIN_VERIFY_OUTPUT
            return run_verify_replaygain(
                root,
                output,
                # None (no --target-lufs) means the standard -18 reference.
                target_lufs=-18.0 if args.target_lufs is None else args.target_lufs,
                tolerance=args.tolerance,
                verbose=args.verbose,
                quiet=args.quiet,
            )

        if args.audit_strays:
            output = args.output or DEFAULT_STRAY_AUDIT_OUTPUT
            return run_stray_audit(root, output, layout=args.layout, quiet=args.quiet)

        if args.health:
            output = args.output or DEFAULT_HEALTH_OUTPUT
            return run_health(
                root,
                output,
                layout=args.layout,
                min_kbps=args.min_bitrate,
                min_res=args.min_art_res,
                verbose=args.verbose,
                quiet=args.quiet,
                json_mode=args.json,
            )

        if args.health_score:
            output = args.output or DEFAULT_HEALTH_SCORE_OUTPUT
            return run_health_score(
                root,
                output,
                min_kbps=args.min_bitrate,
                min_res=args.min_art_res,
                verbose=args.verbose,
                quiet=args.quiet,
                where=where,
                json_mode=args.json,
                fail_on_findings=args.fail_on_findings,
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
            run_stats(
                root,
                args.output,
                layout=args.layout,
                quiet=args.quiet,
                where=where,
                json_mode=args.json,
            )
            return 0

        if args.snapshot:
            output = args.output or DEFAULT_SNAPSHOT_OUTPUT
            return write_snapshot(root, output, quiet=args.quiet)

        if args.diff_snapshot:
            output = args.output or DEFAULT_SNAPSHOT_DIFF_OUTPUT
            return diff_snapshot(root, args.diff_snapshot, output, quiet=args.quiet)

        # The write modes operate on exactly one tree (like the companion
        # scripts they replace), not on an aggregated root list. The
        # genreTidy pair joins the guard: build writes the map beside the
        # root and apply retags under it, so both are one-root modes.
        # --genreMap's --revert path is exempt: it reads a manifest, not a
        # tree, and the script shape never took a directory for it.
        if args.genre_map and args.revert:
            return revert_genremap(
                Path(args.revert).resolve(),
                dry_run=args.dry_run or not args.apply,
                quiet=args.quiet,
            )
        if (
            args.clean
            or args.apestrip
            or args.lyrics
            or args.replaygain
            or args.genre_tidy_build
            or args.genre_tidy_apply
            or args.genre_map
            or args.ingest
        ):
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
                    json_mode=args.json,
                )
            if args.apestrip:
                return run_apestrip(
                    root[0],
                    dry_run=dry_run,
                    keep_metadata=args.keep_metadata,
                    repair_malformed=args.repair_malformed,
                    quiet=args.quiet,
                    json_mode=args.json,
                )
            if args.replaygain:
                return run_replaygain(
                    root[0],
                    dry_run=dry_run,
                    skip_tagged=args.skip_tagged,
                    # None (no --target-lufs) is the standard rsgain easy pass.
                    target_lufs=args.target_lufs,
                    threads=args.threads,
                    quiet=args.quiet,
                    where=where,
                    json_mode=args.json,
                )
            if args.genre_tidy_build:
                return run_genre_tidy_build(
                    root[0],
                    map_path=args.map_path,
                    layout=args.layout,
                    quiet=args.quiet,
                    where=where,
                )
            if args.genre_tidy_apply:
                return run_genre_tidy_apply(
                    root[0],
                    dry_run=dry_run,
                    map_path=args.map_path,
                    layout=args.layout,
                    quiet=args.quiet,
                    where=where,
                    json_mode=args.json,
                )
            if args.ingest:
                return run_ingest(
                    root[0],
                    apply=args.apply and not args.dry_run,
                    snapshot=args.baseline,
                    # None here means the default 'Unfiltered' inbox; an
                    # explicit empty string disables staging.
                    staging="Unfiltered" if args.staging is None else args.staging,
                    allow_new_genre=args.allow_new_genre,
                    log_path=args.output,
                    quiet=args.quiet,
                    json_mode=args.json,
                )
            if args.genre_map:
                return run_genremap(
                    root[0],
                    apply=args.apply and not args.dry_run,
                    only_genres=args.only_genre,
                    # None here means the default 'Unfiltered' inbox; an
                    # explicit empty string disables staging.
                    staging="Unfiltered" if args.staging is None else args.staging,
                    refile_mismatched=args.refile_mismatched,
                    allow_new_genre=args.allow_new_genre,
                    quiet=args.quiet,
                    where=where,
                    json_mode=args.json,
                )
            return run_lyrics(
                root[0],
                dry_run=dry_run,
                force=args.lyrics_force,
                sleep_s=args.lyrics_sleep,
                quiet=args.quiet,
            )

        build_parser().print_help()
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        return 130
