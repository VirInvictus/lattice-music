import os

from vir_tui import (
    CancelledError,
    ask,
    ask_yn,
    interactive_session,
    notify,
    out_note,
    prompt_int,
    prompt_out,
    reset_terminal,
    run_with_capture,
    tui_select,
)

from lattice import utils
from lattice.config import (
    DEFAULT_AI_LIBRARY_OUTPUT,
    DEFAULT_PLAYLIST_OUTPUT,
    get_layout,
    get_library_root,
    get_library_roots,
    set_library_root,
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
    diff_snapshot,
    write_ai_library,
    write_ai_wings,
    write_all_wings,
    write_music_library_tree,
    write_snapshot,
)
from lattice.modes.playlists import generate_playlist, run_check_playlists
from lattice.modes.stats import run_stats

# TUI default output names, deliberately prefixed lattice_ so an interactive
# run never silently overwrites the file a CLI default (config.py's
# unprefixed list) wrote in the same cwd. Defaults that match config.py
# exactly are imported from there; the wings directories match the CLI's
# own inline defaults.
DEFAULT_FLAC_OUTPUT = "lattice_flac_errors.txt"
DEFAULT_MP3_OUTPUT = "lattice_mp3_errors.txt"
DEFAULT_OPUS_OUTPUT = "lattice_opus_errors.txt"
DEFAULT_WAV_OUTPUT = "lattice_wav_errors.txt"
DEFAULT_WMA_OUTPUT = "lattice_wma_errors.txt"
DEFAULT_MISSING_ART_OUTPUT = "lattice_missing_art.txt"
DEFAULT_ART_QUALITY_OUTPUT = "lattice_art_quality.txt"
DEFAULT_ART_MISMATCH_OUTPUT = "lattice_art_mismatch.txt"
DEFAULT_DUPLICATES_OUTPUT = "lattice_duplicates.txt"
DEFAULT_AUDIO_DUPES_OUTPUT = "lattice_audio_dupes.txt"
DEFAULT_HEALTH_SCORE_OUTPUT = "lattice_health_score.txt"
DEFAULT_TAG_AUDIT_OUTPUT = "lattice_tag_audit.txt"
DEFAULT_BITRATE_AUDIT_OUTPUT = "lattice_bitrate_audit.txt"
DEFAULT_REPLAYGAIN_AUDIT_OUTPUT = "lattice_replaygain_audit.txt"
DEFAULT_REPLAYGAIN_VERIFY_OUTPUT = "lattice_replaygain_verify.txt"
DEFAULT_PLAYLIST_CHECK_OUTPUT = "lattice_playlist_check.txt"
DEFAULT_ALBUM_CONSISTENCY_OUTPUT = "lattice_album_consistency.txt"
DEFAULT_STRAY_AUDIT_OUTPUT = "lattice_stray_audit.txt"
DEFAULT_SNAPSHOT_OUTPUT = "lattice_library_snapshot.tsv"
DEFAULT_SNAPSHOT_DIFF_OUTPUT = "lattice_snapshot_diff.txt"


_MAIN_SECTIONS = [
    (
        "LIBRARY",
        [
            "Library tree & exports                  \u2192",
            "Library statistics",
        ],
    ),
    (
        "INTEGRITY",
        [
            "Test FLAC files",
            "Test MP3 files",
            "Test Opus files",
            "Test WAV files",
            "Test WMA files",
        ],
    ),
    (
        "ARTWORK",
        [
            "Extract cover art",
            "Report missing art",
            "Audit art quality",
            "Audit art mismatch",
        ],
    ),
    (
        "METADATA",
        [
            "Find duplicate albums",
            "Find duplicate audio (content hash)",
            "Audit tags",
            "Audit bitrates",
            "Audit ReplayGain",
            "Verify ReplayGain (rsgain)",
            "Check playlists",
            "Audit album consistency",
            "Audit stray files",
            "Library health score",
        ],
    ),
    (
        "MAINTENANCE",
        [
            "Consolidate fragmented albums (clean)",
            "Strip APEv2 tags (apestrip)",
        ],
    ),
    (
        "SETTINGS",
        [
            "Change library root",
        ],
    ),
    ("", ["Quit"]),
]

_LIB_SECTIONS = [
    (
        "",
        [
            "Build music library tree",
            "AI-readable library export",
            "Generate all wings (per-genre)",
            "Generate AI wings (per-genre flat)",
            "Generate smart playlist (.m3u)",
            "Write library snapshot",
            "Diff against a snapshot",
        ],
    ),
    ("", ["Back to main menu"]),
]

# Items that get a letter key instead of a number in the fallback menu.
# Matched on the cleaned label so the mapping follows the sections.
_LETTER_KEYS = {
    "Quit": ("q", None),
    "Back to main menu": ("b", None),
    "Change library root": ("s", "self"),  # "self": maps to its own (si, ii)
}

_MAIN_ALIASES: dict[str, tuple | None] = {
    "l": (0, 0),
    "lib": (0, 0),
    "library": (0, 0),
    "stats": (0, 1),
    "flac": (1, 0),
    "mp3": (1, 1),
    "opus": (1, 2),
    "wav": (1, 3),
    "wma": (1, 4),
    "art": (2, 0),
    "extract": (2, 0),
    "missing": (2, 1),
    "quality": (2, 2),
    "mismatch": (2, 3),
    "dup": (3, 0),
    "dupes": (3, 0),
    "adupes": (3, 1),
    "audiodupes": (3, 1),
    "tags": (3, 2),
    "audit": (3, 2),
    "bitrate": (3, 3),
    "rg": (3, 4),
    "replaygain": (3, 4),
    "verify": (3, 5),
    "rgverify": (3, 5),
    "playlists": (3, 6),
    "pl": (3, 6),
    "consistency": (3, 7),
    "albums": (3, 7),
    "strays": (3, 8),
    "health": (3, 9),
    "clean": (4, 0),
    "apestrip": (4, 1),
    "ape": (4, 1),
    "settings": (5, 0),
    "config": (5, 0),
    "c": (5, 0),
    "quit": None,
    "exit": None,
}

_LIB_ALIASES: dict[str, tuple | None] = {
    "tree": (0, 0),
    "lib": (0, 0),
    "ai": (0, 1),
    "wings": (0, 2),
    "ai-wings": (0, 3),
    "playlist": (0, 4),
    "snapshot": (0, 5),
    "diff": (0, 6),
    "back": None,
    "": None,
}


_SEL_CHANGE_ROOT = (5, 0)
_SEL_QUIT = (6, 0)
_SEL_LIB_BACK = (1, 0)


def _select_main(title: str) -> tuple | None:
    return tui_select(
        title, _MAIN_SECTIONS, aliases=_MAIN_ALIASES, letter_keys=_LETTER_KEYS
    )


def _select_library() -> tuple | None:
    return tui_select(
        "Library Tree & Exports",
        _LIB_SECTIONS,
        hints="↑↓ Navigate  ⏎ Select  Esc Back",
        aliases=_LIB_ALIASES,
        letter_keys=_LETTER_KEYS,
    )


def interactive_menu() -> int:
    try:
        with interactive_session() as scr:
            # IN_TUI switches modes from captured tqdm bars (invisible until
            # the pager opens) to vir_tui's curses progress box, which draws
            # into the session's own screen instead of starting one.
            utils.IN_TUI = scr is not None
            return _menu_session()
    except KeyboardInterrupt:
        return 130
    finally:
        utils.IN_TUI = False


def _integrity_prompts() -> tuple[int, bool, bool]:
    workers = prompt_int("Workers", 4)
    ffmpeg = ask_yn("Use ffmpeg instead of native tools? (y/N)")
    include_ok = ask_yn("List passing files too? (y/N)")
    return workers, ffmpeg, include_ok


def _library_submenu(roots: list[str]) -> None:
    while True:
        reset_terminal()
        result = _select_library()
        if result == "fallback":
            continue
        if result == "invalid":
            continue
        if result is None or result == _SEL_LIB_BACK:
            return

        try:
            if result == (0, 0):
                output = prompt_out("Output file (leave blank for screen)", "").strip()
                output = os.path.expanduser(output) if output else None
                show_genre = ask_yn("Include album genres? (y/N)")
                run_with_capture(
                    "Build music library tree",
                    write_music_library_tree,
                    roots,
                    output,
                    layout=get_layout(),
                    show_genre=show_genre,
                    quiet=False,
                    footer=out_note(output),
                )
            elif result == (0, 1):
                output = prompt_out("Output file", DEFAULT_AI_LIBRARY_OUTPUT)
                run_with_capture(
                    "AI-readable library export",
                    write_ai_library,
                    roots,
                    output,
                    layout=get_layout(),
                    quiet=False,
                    footer=out_note(output),
                )
            elif result == (0, 2):
                outdir = prompt_out("Output directory", "wings")
                show_genre = ask_yn("Include album genres? (y/N)")
                show_paths = ask_yn("Include absolute album paths? (y/N)")
                run_with_capture(
                    "Generate all wings",
                    write_all_wings,
                    roots,
                    outdir,
                    layout=get_layout(),
                    show_genre=show_genre,
                    show_paths=show_paths,
                    quiet=False,
                    footer=f"Wings written to {os.path.abspath(outdir)}",
                )
            elif result == (0, 3):
                outdir = prompt_out("Output directory", "ai_wings")
                run_with_capture(
                    "Generate AI wings",
                    write_ai_wings,
                    roots,
                    outdir,
                    layout=get_layout(),
                    quiet=False,
                    footer=f"AI Wings written to {os.path.abspath(outdir)}",
                )
            elif result == (0, 4):
                output = prompt_out("Output file", DEFAULT_PLAYLIST_OUTPUT)
                rule = ask("Smart rule (e.g. \"rating >= 4 and genre == 'Jazz'\")", "")
                run_with_capture(
                    "Generate smart playlist (.m3u)",
                    generate_playlist,
                    roots,
                    output,
                    rule,
                    layout=get_layout(),
                    quiet=False,
                    footer=out_note(output),
                )
            elif result == (0, 5):
                output = prompt_out("Snapshot file", DEFAULT_SNAPSHOT_OUTPUT)
                run_with_capture(
                    "Write library snapshot",
                    write_snapshot,
                    roots,
                    output,
                    quiet=False,
                    footer=out_note(output),
                )
            elif result == (0, 6):
                snapshot = prompt_out("Snapshot file to diff against", "")
                output = prompt_out("Report file", DEFAULT_SNAPSHOT_DIFF_OUTPUT)
                run_with_capture(
                    "Diff against a snapshot",
                    diff_snapshot,
                    roots,
                    snapshot,
                    output,
                    quiet=False,
                    footer=out_note(output),
                )
        except CancelledError:
            continue


def _menu_session() -> int:
    while True:
        # Multi-root configs (a `library_roots` array) scan together, exactly
        # as cli.py passes its roots list to the modes; a single configured
        # root that no longer exists stops the session rather than scanning
        # silently.
        configured = get_library_roots()
        roots = [r for r in configured if os.path.isdir(r)]
        if not roots:
            if configured:
                notify(f"Configured library root not found: {configured[0]}")
            return 1
        single = get_library_root()

        if len(roots) > 1:
            title = f"lattice-music ({len(roots)} roots)"
        else:
            title = f"lattice-music (root: {roots[0]})"

        reset_terminal()
        result = _select_main(title)

        if result == "fallback":
            continue

        if result == "invalid":
            continue

        if result is None or result == _SEL_QUIT:
            return 0

        try:
            if result == _SEL_CHANGE_ROOT:
                note = (
                    " — edits library_root only; the library_roots list is untouched"
                    if len(get_library_roots()) > 1
                    else ""
                )
                try:
                    raw = ask(
                        f"Change library root (current: {single}){note}", single or ""
                    )
                except CancelledError:
                    raw = None
                if raw is None or not raw.strip():
                    continue
                new_root = os.path.abspath(os.path.expanduser(raw.strip()))
                if not os.path.isdir(new_root):
                    notify(f"Not a directory: {new_root} — root unchanged.")
                    continue
                set_library_root(new_root)
                continue

            if result == (0, 0):
                _library_submenu(roots)

            elif result == (0, 1):
                output = ask("Output file (leave blank for screen)", "").strip()
                output = os.path.expanduser(output) if output else None
                layout = ask("Path extraction layout", get_layout())
                run_with_capture(
                    "Library Statistics",
                    run_stats,
                    roots,
                    output,
                    layout=layout,
                    quiet=False,
                    footer=out_note(output),
                )

            elif result == (1, 0):
                output = prompt_out("Output file", DEFAULT_FLAC_OUTPUT)
                workers = prompt_int("Workers", 4)
                pref = ask("Preferred tool (flac/ffmpeg)", "flac").strip().lower()
                while pref not in ("flac", "ffmpeg"):
                    pref = (
                        ask("Preferred tool must be flac or ffmpeg", "flac")
                        .strip()
                        .lower()
                    )
                ffmpeg_path = ""
                if pref == "ffmpeg":
                    ffmpeg_path = ask("ffmpeg path (blank = PATH)", "").strip()
                resume = ask_yn("Resume an interrupted scan? (y/N)")
                run_with_capture(
                    "Test FLAC files",
                    run_flac_mode,
                    roots,
                    output,
                    workers,
                    pref,
                    ffmpeg=ffmpeg_path or None,
                    resume=resume,
                    quiet=False,
                    footer=out_note(output),
                )

            elif result == (1, 1):
                output = prompt_out("Output file", DEFAULT_MP3_OUTPUT)
                workers, ffmpeg, include_ok = _integrity_prompts()
                resume = ask_yn("Resume an interrupted scan? (y/N)")
                run_with_capture(
                    "Test MP3 files",
                    run_mp3_mode,
                    roots,
                    output,
                    workers,
                    ffmpeg,
                    only_errors=not include_ok,
                    verbose=include_ok,
                    resume=resume,
                    quiet=False,
                    footer=out_note(output),
                )

            elif result == (1, 2):
                output = prompt_out("Output file", DEFAULT_OPUS_OUTPUT)
                workers, ffmpeg, include_ok = _integrity_prompts()
                run_with_capture(
                    "Test Opus files",
                    run_opus_mode,
                    roots,
                    output,
                    workers,
                    ffmpeg,
                    only_errors=not include_ok,
                    verbose=include_ok,
                    quiet=False,
                    footer=out_note(output),
                )

            elif result == (1, 3):
                output = prompt_out("Output file", DEFAULT_WAV_OUTPUT)
                workers, ffmpeg, include_ok = _integrity_prompts()
                run_with_capture(
                    "Test WAV files",
                    run_wav_mode,
                    roots,
                    output,
                    workers,
                    ffmpeg,
                    only_errors=not include_ok,
                    verbose=include_ok,
                    quiet=False,
                    footer=out_note(output),
                )

            elif result == (1, 4):
                output = prompt_out("Output file", DEFAULT_WMA_OUTPUT)
                workers, ffmpeg, include_ok = _integrity_prompts()
                run_with_capture(
                    "Test WMA files",
                    run_wma_mode,
                    roots,
                    output,
                    workers,
                    ffmpeg,
                    only_errors=not include_ok,
                    verbose=include_ok,
                    quiet=False,
                    footer=out_note(output),
                )

            elif result == (2, 0):
                dry = ask_yn("Dry run? (y/N)")
                run_with_capture(
                    "Extract cover art",
                    run_extract_art,
                    roots,
                    quiet=False,
                    dry_run=dry,
                )

            elif result == (2, 1):
                output = prompt_out("Output file", DEFAULT_MISSING_ART_OUTPUT)
                run_with_capture(
                    "Report missing art",
                    run_missing_art,
                    roots,
                    output,
                    quiet=False,
                    footer=out_note(output),
                )

            elif result == (2, 2):
                output = prompt_out("Output file", DEFAULT_ART_QUALITY_OUTPUT)
                min_res = prompt_int("Minimum resolution floor", 500)
                run_with_capture(
                    "Audit art quality",
                    run_art_quality_audit,
                    roots,
                    output,
                    min_res,
                    quiet=False,
                    footer=out_note(output),
                )

            elif result == (2, 3):
                output = prompt_out("Output file", DEFAULT_ART_MISMATCH_OUTPUT)
                include_ok = ask_yn("List matched albums too? (y/N)")
                run_with_capture(
                    "Audit art mismatch",
                    run_art_mismatch_audit,
                    roots,
                    output,
                    verbose=include_ok,
                    quiet=False,
                    footer=out_note(output),
                )

            elif result == (3, 0):
                output = prompt_out("Output file", DEFAULT_DUPLICATES_OUTPUT)
                run_with_capture(
                    "Find duplicate albums",
                    run_duplicates,
                    roots,
                    output,
                    quiet=False,
                    footer=out_note(output),
                )

            elif result == (3, 1):
                output = prompt_out("Output file", DEFAULT_AUDIO_DUPES_OUTPUT)
                run_with_capture(
                    "Find duplicate audio (content hash)",
                    run_audio_dupes,
                    roots,
                    output,
                    quiet=False,
                    footer=out_note(output),
                )

            elif result == (3, 2):
                output = prompt_out("Output file", DEFAULT_TAG_AUDIT_OUTPUT)
                run_with_capture(
                    "Audit tags",
                    run_tag_audit,
                    roots,
                    output,
                    quiet=False,
                    footer=out_note(output),
                )

            elif result == (3, 3):
                output = prompt_out("Output file", DEFAULT_BITRATE_AUDIT_OUTPUT)
                min_kbps = prompt_int("Minimum bitrate floor (kbps)", 192)
                run_with_capture(
                    "Audit bitrates",
                    run_bitrate_audit,
                    roots,
                    output,
                    min_kbps,
                    quiet=False,
                    footer=out_note(output),
                )

            elif result == (3, 4):
                output = prompt_out("Output file", DEFAULT_REPLAYGAIN_AUDIT_OUTPUT)
                include_ok = ask_yn("List fully-tagged albums? (y/N)")
                run_with_capture(
                    "Audit ReplayGain",
                    run_replaygain_audit,
                    roots,
                    output,
                    verbose=include_ok,
                    quiet=False,
                    footer=out_note(output),
                )

            elif result == (3, 5):
                output = prompt_out("Output file", DEFAULT_REPLAYGAIN_VERIFY_OUTPUT)
                target_lufs = prompt_int("Target loudness (LUFS)", -18)
                tol_raw = ask("Tolerance in dB", "0.5").strip()
                try:
                    tolerance = float(tol_raw)
                except ValueError:
                    tolerance = 0.5
                include_ok = ask_yn("List fully-correct albums? (y/N)")
                run_with_capture(
                    "Verify ReplayGain (rsgain)",
                    run_verify_replaygain,
                    roots,
                    output,
                    target_lufs=float(target_lufs),
                    tolerance=tolerance,
                    verbose=include_ok,
                    quiet=False,
                    footer=out_note(output),
                )

            elif result == (3, 6):
                output = prompt_out("Output file", DEFAULT_PLAYLIST_CHECK_OUTPUT)
                include_ok = ask_yn("List playlists with no problems? (y/N)")
                run_with_capture(
                    "Check playlists",
                    run_check_playlists,
                    roots,
                    output,
                    verbose=include_ok,
                    quiet=False,
                    footer=out_note(output),
                )

            elif result == (3, 7):
                output = prompt_out("Output file", DEFAULT_ALBUM_CONSISTENCY_OUTPUT)
                include_ok = ask_yn("List albums with no findings? (y/N)")
                run_with_capture(
                    "Audit album consistency",
                    run_album_consistency,
                    roots,
                    output,
                    verbose=include_ok,
                    quiet=False,
                    footer=out_note(output),
                )

            elif result == (3, 8):
                output = prompt_out("Output file", DEFAULT_STRAY_AUDIT_OUTPUT)
                layout = ask("Path layout", get_layout())
                run_with_capture(
                    "Audit stray files",
                    run_stray_audit,
                    roots,
                    output,
                    layout=layout,
                    quiet=False,
                    footer=out_note(output),
                )

            elif result == (3, 9):
                output = prompt_out("Output file", DEFAULT_HEALTH_SCORE_OUTPUT)
                min_kbps = prompt_int("Minimum bitrate floor (kbps)", 192)
                min_res = prompt_int("Minimum cover resolution (px)", 500)
                verbose = ask_yn("List perfect-score albums too? (y/N)")
                run_with_capture(
                    "Library health score",
                    run_health_score,
                    roots,
                    output,
                    min_kbps=min_kbps,
                    min_res=min_res,
                    verbose=verbose,
                    quiet=False,
                    footer=out_note(output),
                )

            elif result == (4, 0):
                # Write mode: every question defaults to no, and the last
                # confirm decides dry-run vs apply. With all passes declined
                # the mode still runs the two merge passes (the script's
                # base behavior). Like the CLI, a write mode refuses a
                # multi-root config rather than silently picking one tree.
                if len(roots) != 1:
                    notify(
                        "The clean mode needs exactly one library root; "
                        f"{len(roots)} are configured (library_roots)."
                    )
                    continue
                norm_names = ask_yn(
                    "Pass 3: rename folders with non-standard characters? (y/N)"
                )
                norm_files = ask_yn("Pass 3: rename audio track files too? (y/N)")
                norm_tags = ask_yn("Pass 4: normalize tags library-wide? (y/N)")
                layout = ask("Path layout", get_layout())
                apply = ask_yn("APPLY changes now? No = dry-run preview only (y/N)")
                run_with_capture(
                    "Consolidate fragmented albums (clean)",
                    run_clean,
                    roots[0],
                    dry_run=not apply,
                    normalize_names=norm_names,
                    normalize_filenames=norm_files,
                    normalize_tags=norm_tags,
                    layout=layout,
                    quiet=False,
                )

            elif result == (4, 1):
                # Write mode: the ask_yn confirm below is the gate, so the
                # mode itself runs with assume_yes (its own input() prompt
                # would fire inside the captured output).
                if len(roots) != 1:
                    notify(
                        "The apestrip mode needs exactly one library root; "
                        f"{len(roots)} are configured (library_roots)."
                    )
                    continue
                keep = ask_yn("Migrate APE fields into ID3 before stripping? (y/N)")
                repair = ask_yn("Also repair malformed APE tags? (y/N)")
                apply = ask_yn("APPLY strip now? No = dry-run preview only (y/N)")
                run_with_capture(
                    "Strip APEv2 tags (apestrip)",
                    run_apestrip,
                    roots[0],
                    dry_run=not apply,
                    keep_metadata=keep,
                    repair_malformed=repair,
                    assume_yes=True,
                    quiet=False,
                )
        except CancelledError:
            continue
