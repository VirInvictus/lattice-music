# lattice-music Patch Notes

## v5.2.1 (2026-09-12)
- **Fix (release hygiene):** the v5.2.0 commit missed the pyproject half of the version sync set (config.VERSION said 5.2.0, pyproject still said 5.1.0). The tagged v5.2.0 build therefore produced artifacts named `5.1.0`, which raced the real v5.1.0 publish and won: **PyPI's 5.1.0 files are the v5.2.0-commit build** (internally `lattice --version` prints 5.2.0 and `Requires-Dist` is the newer `vir-tui>=2.3.0`), the true v5.1.0 build lost the race with a 400, and no 5.2.0 was ever uploaded. PyPI forbids re-uploading a filename, so that 5.1.0 record is permanent; it is functionally complete but its embedded version string lags. This release restores the sync set and ships the real 5.2.1 to PyPI. No code changes.

## v5.2.0 (2026-09-12)
- **Dependency:** the `vir-tui` floor moves to `>=2.3.0` (mouse support, type-to-filter in menus of 15+ items, `configure_theme`). The lockfile has resolved 2.3.0 since the v4.17.0 refresh, so installs already receive it; the floor now says so.
- **Chore:** dropped `pytest-asyncio` from the dev dependency group. The suite is stdlib `unittest` and the repo carries no asyncio anywhere; `pytest` stays as an optional runner.

## v5.1.0 (2026-09-12)
- **New mode: `--auditStrays`** (the stray-file audit from the 2026-09-12 landscape research). One read-only pass reports everything that does not fit the library's own layout: audio at the wrong depth for the configured `--layout` (a flat `Artist/Album` stray in a genre-first tree, a root-level file), loose tracks sitting directly in a folder at the artist slot, audio hidden in dot-directories and dot-names that the scanners and integrity walks prune silently, and unrecognized non-audio files inside album folders. Recognized furniture (cover images, playlists, cue sheets, checksum sidecars, and the companion tools' own logs) is ignored via documented extension sets; anything else is listed for curation. Writes `stray_audit.txt` by default; wired into the CLI and the TUI's Metadata section. Twelve new tests; suite 501 to 513.
- **Cleanup:** `audit.py`'s hand-mirrored quote/dash fold table (kept because the cleaner used to live outside the package) now imports `lattice.norm`, removing the last copy of the fold rules.

## v5.0.0 (2026-09-12)
- **New write modes (the fold):** `scripts/cleaner.py` and `scripts/apestrip.py` are promoted into the `lattice` package as `lattice --clean` and `lattice --apestrip`, per the commissioned fold in REPORT-12-Sept.md. The pure name/tag rules engine now lives in `lattice.norm` (zero I/O), the four consolidation/normalization passes and their dry-run virtual filesystem in `lattice.modes.clean`, and the APEv2 classify/plan/apply/repair brain (including the verified atomic malformed-tag repair) in `lattice.modes.apestrip`.
- **Contract amendment:** spec.md §5 now reads "reads tags; writes metadata only via the explicit `--clean`/`--apestrip` write modes (opt-in, logged, dry-run by default)". Every other mode remains read-only, and the remaining seven companion scripts stay put.
- **Behavior change (the one deliberate break):** inside `lattice`, the write modes dry-run by default and write only on `--apply` (`--dry-run` always wins). The scripts keep their historical apply-by-default contract, so aliases and cron are unaffected.
- **New:** `lattice --clean DIR [--normalize-names|--normalize-filenames|--normalize-tags|--all] [--layout]` consolidates fragmented album folders and optionally normalizes names and tags. Every change is logged (default `<root>/cleanup.log`) and the dry-run preview predicts the real run exactly.
- **New:** `lattice --apestrip DIR [--keep-metadata] [--repair-malformed]` strips stray APEv2 tags from MP3s. A real run prints the worklist and asks for confirmation (auto-skipped when stdin is not a TTY); the log defaults to `<root>/apestrip.log`. Both write modes take exactly one root; an aggregated `--root` list is refused.
- **TUI:** a new Maintenance section exposes both write modes behind yes/no confirms; answering No to the apply question runs the preview instead.
- **Companion scripts:** `cleaner.py` (1.6.0) and `apestrip.py` (1.3.0) are now thin launchers that re-export the package implementation, so flags, log formats, idempotency, and the non-TTY auto-confirm are unchanged while the two implementations cannot drift.
- **Tests:** test_cleaner.py and test_apestrip.py retarget the package modules wholesale (the `scripts/` sys.path hack is gone; the dry-run fidelity classes moved whole), and new test_write_modes.py pins the CLI/TUI write-mode contract. Suite: 489 to 501, green.

## v4.17.0 (2026-08-28)
- **Refactor**: Dropped lattice-music's hand-mirrored TUI progress implementation — `_TUIPbar`, the `_TUI_BOX_W`/color-pair id mirrors, and the `_SHARED_SCREEN`/`set_shared_screen` plumbing (~110 lines) are replaced by `vir_tui.progress_box()` (vir-tui 2.2.0's first-class, session-screen-aware progress widget). Rendering can no longer drift from the TUI's style when vir-tui restyles.
- **Refactor**: `interactive_menu()` delegates the curses session lifecycle (open, degrade-to-text, close, KeyboardInterrupt cleanup) to vir-tui 2.2.0's `interactive_session()` context manager; the `utils.IN_TUI` flag is now set from the yielded session screen.
- **Chore**: Report footers use vir-tui's public `out_note()` instead of a local copy.
- **Chore**: Removed the stale tracked `tests/test_tui.py.bak` (referenced the removed API).
- **Dependency**: `vir-tui` tracks `@main`; requires 2.2.0+.

## v4.16.1 (2026-08-25)
- **Fix**: TUI library-tree scan crashed with `TypeError` when the output prompt was left blank; blank now renders the tree to the results pager like Library Statistics (`write_music_library_tree` accepts `output_file=None`).
- **Fix**: Progress boxes are back in the TUI. The vir-tui migration dropped the `IN_TUI`/shared-screen wiring from `interactive_menu`, so scans silently fell back to tqdm bars swallowed by the output capture and the screen sat frozen until the pager opened. Uses `session_screen()` from vir-tui 2.1.0.

## v4.16.0 (2026-08-24)
- **Refactor**: Eradicated 1,267 lines of duplicated TUI framework code. lattice-music now delegates completely to `vir-tui` v2.0.0.
- **Dependency**: Pointed `vir-tui` reference to local path for editable testing, then restored remote URL on 2.0.0 bump.

## v4.15.0 (2026-08-23)
- **TUI Extraction (`vir-tui`):** The generic CLI/curses interface has been extracted into a shared library, `vir-tui`. lattice-music now delegates to `vir-tui` for all interactive menus and text prompts, sharing this layer with `CalibreQuarry`.


## v4.14.0 (2026-08-21)

- `cleaner.py` now supports an `--all` flag to run all normalization passes concurrently.

## flac2opus.py v1.0.0 (2026-08-21)
- **New Companion Script:** Added `flac2opus.py` to recursively find FLAC files, encode them to Opus at 128kbps using `ffmpeg`, verify output quality (duration match) and seamlessly copy metadata tags via Mutagen, and delete the original FLAC files.

## slipcover.py v1.1.0 (2026-08-21)
- **Feature:** Added `--fetch` to query the iTunes Search API and automatically download high-resolution cover art for albums completely missing art.
- **Feature:** Added `--report` to find and report albums completely missing cover art.

## slipcover.py v1.0.0 (2026-08-21)
- **New Companion Script:** Added `slipcover.py` to embed existing folder cover art (`cover.jpg`, etc.) into audio files that lack embedded art. Supports MP3, FLAC, Opus, Ogg, and M4A. Idempotent and dry-run capable.

## v4.13.0 (2026-08-21)

- Added tag deduplication to `cleaner.py` (Pass 4). Tags displaying as `A / A` or `A, A` (often remnants of flattened multi-value tags) are now automatically merged down to their unique values during `--normalize-tags`. Substrings are also resolved (e.g. `Title (feat. Artist) / Title` correctly becomes `Title (feat. Artist)`). This cleanly fixes cases where ffmpeg or other tools accidentally join ID3v1 and ID3v2 tags.

## cleaner.py v1.6.0 (2026-08-21)
- **Feature:** Added `--all` flag to easily enable all normalization passes at once (--normalize-names, --normalize-filenames, --normalize-tags).

## cleaner.py v1.5.0 (2026-08-21)
- **Feature:** Added `tag_dedupe` function to properly clean up and deduplicate identical or substring multi-value tags (e.g. `Title / Title`) during tag normalization.
## v4.12.0 (2026-08-15)

- Added global artist tag authority logic to `cleaner.py` (Pass 4). The tag normalization pass now scans all artist-level folders across the entire library to build a global canonical map. Any track whose artist tag normalizes to a known canonical artist folder (handling case, punctuation, and guest credits correctly via a new `_base_artist` helper) will have its artist and albumartist tags rewritten to perfectly match the casing of the canonical folder. This unifies artist names even across different genre boundaries.
- `cleaner.py` now explicitly outputs a summary line (`Cleanup run complete (APPLY). See ... for details.`) on stdout when it finishes, resolving silent completion behavior.

## cleaner.py v1.4.0 (2026-08-15)
- **Feature:** Tag normalization (Pass 4) uses a global map of artist folders as the tag authority. Tracks featuring guest credits (e.g. `Artist feat. Guest`) are correctly matched to the base `Artist` folder.
- **Fix:** Added a terminal `print` notification when the script concludes, ensuring the user is informed of completion rather than seeing a silent exit.
## v4.11.0 (2026-08-09)

Second full-repo sweep on top of v4.10.2 (suite 487 to 520). One fix corrects a defect that made report values non-reproducible between runs, and one performance finding reverses a long-standing default; both are called out first.

- **Behavior change: ratings are selected by rank, not by whichever key the container happened to yield first.** A file can carry several rating-ish keys at once (`rating` beside `album rating`, `love rating`, or a tagger's private `_custom_rating`). `get_all_tags` took the first match and stopped, and mutagen builds a Vorbis comment's key list from a `set`, whose iteration order is randomized per process by Python's hash seed. **The same FLAC/Opus file therefore reported a different rating on different runs**: 41 files in a 9,589-file library were affected, so `--playlist "rating >= 4"` and the `--stats` rating histogram silently disagreed with themselves between invocations. Candidates are now ranked (the standard names first, an album-level rating last because it is not the track's rating, alphabetical ties), which also fixes `album rating` outranking a real track rating. Applied to all four container branches (ID3 TXXX, MP4, Vorbis, ASF); verified stable across eight hash seeds and byte-identical across whole-library runs.
- **Behavior change: tag reads are serial by default, and the worker count is now tunable.** The concurrent tag read was never a win: mutagen decodes in Python, so the GIL is held for everything except the file open, and the per-task handoff costs more than the overlap saves. Measured with hyperfine on a 9,589-file NVMe library, serial is 1.41x faster for `--stats` (9.6s vs 15.3s user CPU) and 1.49x faster for `--auditTags`; `--stats` wall clock drops from 16.3s to 11.4s. The pool is kept rather than removed, because the picture inverts where an open genuinely blocks (SMB/NFS shares, spinning disks): set the `tag_workers` config key or the `LATTICE_TAG_WORKERS` environment variable to an integer or `auto` to opt back in. Gated on the storage rather than on a file count, since concurrency loses at every library size on fast local disk. `--workers` in the integrity modes is a different pool (flac/ffmpeg subprocesses, which do release the GIL) and is unchanged.
- **Fix: integrity modes no longer scan hidden directories.** `--testFLAC`, `--testMP3`, `--testOpus`, `--testWAV`, and `--testWMA` walked with a bare `os.walk`, while every other mode goes through `iter_audio_dirs` and prunes dot-directories. A scratch tree with a `.hidden/` copy measured 7 files found against the package's own 3, so a `.testing/` working copy of an album was decoded on every integrity run. The walk now prunes and is sorted.
- **Fix: MP3/Opus/WAV/WMA report rows are ordered by path.** Rows were appended in `as_completed` order and written unsorted, so two scans of an unchanged library produced differently-ordered reports that could not be diffed. `run_flac_mode` already sorted its sections; the decode scanner now matches.
- **Fix: a tie in a directory's dominant artist/album/genre breaks the same way every run.** `_scan_album_dirs` iterated `os.walk`'s raw readdir order, so a directory whose files split evenly between two artists picked its headline name by coin flip. Filenames are now sorted before the tally.
- **Fix: `--auditArtQuality` picks the same cover twice running.** A directory holding more than one recognized cover name took whichever `os.listdir` returned first. The lookup is now shared with `_has_cover_file` through `utils._find_cover_file` and is sorted.
- Fix: an empty-list tag value no longer raises. The rating readers did `val[0] if isinstance(val, list) else val`, which raised `IndexError` on an empty list rather than reading as absent.
- Cleanup: `stats.py` had four copies of `import lattice.utils as utils` inside one function (hoisted to module scope, which preserves the deferred `IN_TUI` lookup they existed for), a dead `fsize = 0` assignment, and an inline extension test where `is_audio` already exists.
- Tests: 33 new, covering hidden-directory pruning in the integrity walk, decode-report ordering (the guard forces reverse-completion order rather than trusting scheduling luck), rating rank selection and its permutation-invariance, the tag-worker knob, the shared cover lookup, and scan tie-break determinism. Suite at 520.
- Verification: `--library`, `--ai-library`, `--all-wings`, `--playlist`, `--stats`, and `--auditTags` were diffed against a v4.10.2 baseline over the real 9,589-file library and are byte-identical apart from the intended rating corrections.

## apestrip.py v1.2.0 (2026-08-09)

- **Behavior change: hidden directories are pruned.** The walk descended into dot-directories, unlike `rerate.py` and `replaygain.py`, which both prune and document `.testing/` album copies as the reason. This is the destructive companion, so a hidden working copy was the worst place for the inconsistency to sit. Directory and file order is now sorted too, so a run's worklist is deterministic.
- Fix: an unwritable log path is reported instead of raising. `open()` on the log was unguarded, so a bad `--log` produced a traceback where `rerate.py` and `replaygain.py` print `error: cannot open log file` and exit 1.

## rerate.py v1.1.0 (2026-08-09)

- **Fix: a dry-run reports the same read errors as the apply run.** `read_remaps` caught every exception and returned a bare `[]`, so an unreadable MP3 counted as "no changes" and the dry-run summary claimed zero errors on files the apply run then reported as failures. It now returns `(changes, error)`, the same shape `rerate_file` returns, and distinguishes a missing ID3 header (not an error) from a genuine read failure. The two paths share one `_popm_changes` helper so they cannot drift again.
- Note for callers: `read_remaps` returns a tuple now, not a list.

## genre_tidy.py v1.3.0 (2026-08-09)

- **Behavior change: `apply` exits 1 when any album failed to retag.** It returned 0 unconditionally, so a wrapper script could not tell a clean run from one where every write failed. `retag.py`, `rerate.py`, and `replaygain.py` all already returned nonzero on error; `apply` now matches.
- Fix: `build`'s "N artist(s) carry multiple genres" count is accurate. It counted every row beginning with `#`, which also swept up the `EXCLUDED` compilation comments and the "no genre tags found" comments, so the number bore little relation to the artists actually worth reviewing. It is now counted from the reduced artists that genuinely carry more than one genre.

## cleaner.py v1.3.5 (2026-08-09)

- Fix: an unwritable log path is reported instead of raising. `Run` opens the log in its constructor, so a bad `--log` produced a traceback; it now prints `error: cannot open log file` and exits 1, matching the other companions.

## retag.py v1.1.4 (2026-08-09)

- Fix: an unwritable `--log` path is reported instead of raising, matching the other companions.

## v4.10.2 (2026-08-07)

Full-repo bug sweep (package, TUI, and all seven companions reviewed; every fix below is regression-tested; suite 462 to 487). Three fixes change report values on purpose, called out first.

- **Behavior change: canonical POPM bytes read as whole stars for any rater email.** The bytes 1/64/128/196/255 (the WMP convention that foobar2000, Winamp, MusicBee, and `rerate.py` all write) only mapped to 1-5 stars when the frame's email was exactly "Windows Media Player 9 Series"; any other email fell into the linear 0-255 scale, so byte 196 read as 3.84 stars and a `rating >= 4` playlist missed every 4-star MP3 rerate.py had just fixed. The canonical bytes now map to whole stars regardless of email; non-canonical bytes still use the linear scale.
- **Behavior change: FMPS_Rating tags read on their real 0-1 scale.** `FMPS_Rating=0.8` (4 of 5 stars, a documented Vorbis/TXXX convention) was read as 0.8 stars by the magnitude heuristic. Keys containing `fmps_` now scale 0.0-1.0 by five; everything else is unchanged.
- **Behavior change: multi-valued ID3 title/artist/album keep every value.** The readers passed `frame.text` into `_first_text`, whose list branch took the first element before the "/" join could run, so a multi-valued TPE1 read as "Artist One" instead of "Artist One/Artist Two" (TCON already joined; now they agree).
- **Fix: `--verbose` MP3 rows print the real bitrate mode.** The row metadata took the enum's class name, so every CBR/VBR/ABR file printed the literal "BitrateMode"; it now prints the member ("CBR", "VBR", "ABR").
- **Fix: smart-playlist rules that divide by a field validate correctly.** `validate_rule` probes the rule against all-zero dummy metadata, so `bitrate / duration > 200` died with "division by zero" before the walk even though real tracks never carry zero duration. Division by the dummy zeros is now treated as data-dependent, not structural.
- **Fix: `--all-wings --paths` lists every directory of a duplicated album.** When the same (artist, album) lives in two folders their songs merge into one wing entry, but the path annotation was last-wins, misattributing the merged track list to a single folder. It now lists all contributing paths ("; "-joined).
- **Fix: layout-aware album counting in `--stats`.** Albums were counted by raw path depth, which undercounted flat layouts and counted a loose file's artist folder as an album on a `{genre}/{artist}/{album}` tree. A directory now counts when the layout's `{album}` slot is actually filled.
- **Fix: a file directly at the scan root no longer defeats the "Unknown Artist" fallback.** `parse_layout` filled the first layout slot with a present-but-empty string for root-level files, so `parsed.get("artist", "Unknown Artist")` returned "" instead of falling back.
- **Fix: `relpath_under` handles a root of "/".** The prefix check built `"//"` and never matched, so scanning from the filesystem root returned absolute paths where every other root returns relative ones.
- **Fix: `--quiet --verbose` shows the progress bar.** The decode scanners built the bar before `--verbose` flipped `quiet` off, producing a barless run that still printed the full verbose report.
- **Fix: `--extractArt --dry-run --quiet` is quiet.** The dry-run announcement line ignored `--quiet`, unlike its real-run sibling.
- **Fix: a hand-edited `library_root` expands `~` like the plural form.** `get_library_root()` and the singular fallback in `get_library_roots()` returned the raw string, so a hand-written `"~/Music"` failed the CLI/TUI existence filters and the app re-prompted as if unconfigured.
- **Fix: the CLI validates configured roots with `isdir`, matching the TUI.** `os.path.exists` accepted a root that had become a plain file, silently "scanning" zero files where the TUI correctly re-prompts.
- Cleanup: dead `DEFAULT_STATS_OUTPUT` constant and unused `_prompt_path` TUI helper removed; an impossible `!= "None"` string guard dropped; the always-true `hasattr(args, "layout")` condition simplified.
- Known limitations noted, deliberately unchanged: the audit's fuzzy-duplicate normalizer does not strip nested-bracket suffixes like `(Deluxe [Remastered])`, and blind non-TTY confirmation auto-skip in replaygain/apestrip remains the documented convention.

## retag.py v1.1.3 (2026-08-07)

- **Fix: a stale ID3v1 genre byte now defeats the noop check.** `is_noop` verified TCON, APEv2, and `TXXX:GENRE` but never the trailing ID3v1 block, one of the "hidden genre spots" the tool exists to clear: a file whose TCON already matched kept its stale v1 genre forever. The v1 genre byte is now compared against what the refresh would write; a file with no v1 tag at all is still a noop (nothing stale to clear, and a write would only mint a v1 tag nobody asked for).

## cleaner.py v1.3.4 (2026-08-07)

- **Fix: a Pass 1 survivor rename no longer hides the artist from Passes 2-4 in a dry-run.** The renamed survivor's old path lands in the virtual `removed` set, and Passes 2 (album consolidation), 3 (name normalization), and 4 (tag scan) all skipped it, so `--dry-run` under-reported work `--apply` then performed (the headline `JAY‐Z` example triggered it). Rename origins are now recognized as surviving (their bytes live on under the new name) and previewed at their current on-disk path; a new end-to-end test drives `main()` dry and apply over identical trees and asserts identical stats.
- **Fix: Pass 4's dry-run no longer tag-scans files an earlier pass dropped.** The tag walk ignored the virtual `removed` set, so an exact-duplicate file (`DROP DUPE`) was still previewed and counted, inflating the dry-run's scanned/rewritten stats over the apply run's.
- **Fix: an unparseable image can no longer win a cover collision on byte size.** When exactly one colliding cover parses as an image, the parseable one now wins; the larger-bytes fallback applies only when both parse (the pixel comparison) or neither does.

## genre_foldermap.py v1.4.1 (2026-08-07)

- **Fix: the dry-run now predicts cross-device refusals.** The mv-only guard (refuse instead of letting `shutil.move` degrade to copy+delete) only ran on `--apply`, so a library spanning a mount point previewed clean "would move" lines for moves the real run then refused. The check runs in both modes, against the destination's nearest existing ancestor.
- **Fix: a refused cross-device move leaves no stray directories.** The destination tree was `mkdir`-ed before the refusal check; the check now runs first and `mkdir` only happens for a real, accepted move.
- **Fix: a multi-disc album's destination collision is flagged once, not once per disc.** The COLLISION branch now records the rejection like the DEST EXISTS branch always did.
- **Fix: a genre folder emptied by `--refile-mismatched` is pruned.** Moving the last artist out of a genre left the empty genre folder behind forever (only the artist level was pruned, unlike revert's upward prune). Emptied genre folders are now removed; the library root and the staging inbox (documented to stay in place) never are.

## replaygain.py v1.2.2 (2026-08-07)

- Docs: the `-y/--yes` help now states the confirmation prompt is auto-skipped when stdin is not a TTY (the shared companion convention apestrip.py already documented).

## genre_foldermap.py v1.4.0 (2026-08-06)

Built for the library-wide genre audit: retag first, then make the folders follow.

- **New: `--refile-mismatched`.** An already-organized `Genre/Artist/Album` album whose tag genre disagrees with its genre folder used to be reported as a `NOTE` and left in place: correct behavior for a conversion run, a dead end after a retag pass. With the flag, such albums move to their tag's genre folder through the same machinery as strays: vocabulary gate (unknown target genres flagged unless `--allow-new-genre`), existing-folder spelling reuse, never-overwrite, manifest rows for `--revert`, source-artist pruning, and sidecars following the artist. Disc-subfolder records still collapse to their parent album, which moves as one unit. Default behavior without the flag is unchanged.
- **Fix: a multi-genre tag can no longer become one folder name.** A single tag value joining genres with `;` (tagger-joined TCON) or `/` (the library's own multi-genre convention) passed through `sanitize_component` intact, since `;` is NTFS-legal, and once produced a real `Drill;East Coast Hip Hop;…/` folder. `split_multi_genre` now takes the first component for placement and flags the rest as a `MULTI-GENRE TAG` issue so the tag gets fixed at the source; a separators-only value falls through to `NO GENRE`.
- Tests: 13 new (`MultiGenreTagTests`, `RefileMismatchedTests`) covering the gate interplay, spelling reuse, disc-unit moves, dest-exists skips, sidecar follow, and the unchanged default. Suite at 462.

## v4.10.1 (2026-07-03)

Carry-backs from the cquarry review: cquarry finished porting the 2026-07-01 audit, and its adversarially verified code review confirmed four defects in the shared curses skeleton that v4.10.0 still carried. The fixes mirror cquarry's (CalibreQuarry `0304cfa`). One deliberate behavior change is called out below.

- **Fix: the "Report written to ..." footer no longer appears after an error or cancel.** `_run_with_capture` appended the success footer unconditionally, so the pager could show `[Error]` plus a traceback (or `[Cancelled]`) followed by a footer claiming a file that was never finished. The footer is now suppressed whenever the mode raised or was cancelled.
- **Behavior change: Ctrl-C at the text-fallback menu exits 130, not 0.** `_fallback_input` swallowed `KeyboardInterrupt` into a clean Quit, so a wrapper script checking for the documented 130 saw success. The interrupt now propagates to the session handler (which already maps it to 130), matching the curses menu; EOF remains a quiet Quit. Verified end-to-end: SIGINT at the degraded text menu exits 130.
- **Fix: the prompt and pause curses-failure fallbacks delegate instead of duplicating.** `_tui_prompt_str`'s `curses.error` handler re-implemented the text prompt inline (and both copies rendered a `None` default as `[None]`); it now degrades and calls `_prompt_str`, which renders an empty default as `[]`. `_tui_pause` likewise delegates to `_pause`.
- **Cleanup: widget color init runs once per screen, not per widget.** Every widget `_run` re-called `_init_tui_colors()` although the session screen already initialized colors; the one-shot init now lives in `_with_screen`'s wrapper branch (session screens get it from `_open_screen`), and the four per-widget calls are gone. No behavior change.
- **Found and fixed while verifying: the library submenu broke the persistent screen with `stty sane`.** `_library_submenu` ran `_reset_terminal()` unconditionally after every selection; under the v4.10.0 session screen that re-enabled echo and canonical mode, so the very next prompt echoed keystrokes over the TUI and only saw input after Enter. The v4.10.0 pty verification drove a main-menu flow and missed it. Fixed at the source, matching cquarry's shape: `_reset_terminal` now returns early while a session screen is published (`utils._SHARED_SCREEN`), since a live curses session owns the terminal state. CLI use is unchanged (no session screen there). Re-verified under a pty: menu → submenu → prompt → Esc-cancel → quit works with one alternate-screen enter/leave.
- Tests extended (`tests/test_tui.py` + `tests/test_utils.py`, 449 total across the suite): footer suppressed on error and cancel (still shown on success), `_fallback_input` propagating Ctrl-C and quietly quitting on EOF, the prompt/pause fallbacks routing through their text counterparts, and `_reset_terminal` staying quiet while a session screen is live.

## v4.10.0 (2026-07-02)

The persistent-curses-screen rework: audit item T7, the last parked entry from the 2026-07-01 TUI/UX audit. Purely a lifecycle change; no menu, prompt, or mode behavior differs.

- **One curses screen per session.** Every menu, prompt, pause, and pager used to be its own `curses.wrapper` init/teardown, so multi-prompt flows visibly flashed to the shell between widgets. `interactive_menu` now opens the screen once and every widget draws into it (`_with_screen`); a widget invoked outside a session still gets its own one-shot wrapper, so nothing changes for direct callers. Measured under a pty: a full menu → prompt → cancel → mode → pager → quit session enters the terminal's alternate screen exactly once, where the same flow on v4.9.0 entered it seven times.
- **The in-mode progress bar joins the session.** `_TUIPbar` draws into the shared screen the session publishes (via `utils.set_shared_screen`) instead of `initscr()`-ing a screen of its own, and its `close()` no longer tears down terminal state that isn't its to tear down. Standalone use (no session) keeps the old start-and-end-a-screen behavior.
- **One degradation path.** A mid-session curses failure (terminal died, capability lost) funnels through `_degrade_to_text`: the screen is suspended once and the rest of the session runs the text fallback. Session startup failure does the same before the first menu. All the v4.8.2 guarantees (no stuck terminal, no silent exit) hold.
- **Ctrl-C is coherent everywhere.** At a prompt it cancels exactly like Esc; in the pager it closes the pager; at the menu it ends the session cleanly with exit code 130 (previously an unhandled traceback).
- Tests extended (`tests/test_tui.py` + `tests/test_utils.py`, 441 total): session-screen routing, wrapper fallback, degradation clearing every piece of session state, the 130 exit, and the progress bar leaving a shared screen alone.

## v4.9.0 (2026-07-02)

TUI parity and UX release, closing the 2026-07-01 audit's T1 to T6 (T7, the persistent-curses-screen rework, stays deliberately parked). One deliberate behavior change is called out below.

- **Multi-root configs are visible to the TUI (T1).** A `library_roots`-only config used to read as a first run: the TUI prompted and overwrote the user's intent with a single `library_root`. The session check now consults both keys, the menu title shows `[N roots]`, and every mode receives the full roots list exactly as the CLI passes it. "Change library root" edits only `library_root` and says so when a roots list exists.
- **Behavior change: Esc in a prompt cancels (T2).** Esc used to mean "accept default", so selecting a scan by accident and mashing Esc *launched* it. Esc (or Ctrl-C at a text prompt) now unwinds the whole prompt chain back to the menu with nothing launched; bare Enter still accepts the default. The hint bar reads "⏎ Accept  Esc Cancel  Ctrl-U Clear".
- **Library-root changes are validated before persisting (T3).** A typo'd root was saved over the good one, every retry of the recovery loop was labelled "First run:", and Esc or a blank answer at the true first run silently made the launch directory the permanent root. Paths are now checked with `isdir` before `set_library_root`; a bad answer shows "Not a directory" and keeps the old root; a missing configured root gets its own "Configured root missing" wording; a blank first-run answer re-prompts (type `.` to choose the CWD explicitly).
- **Output-path prompts expand `~` (T4).** `~/reports/x.txt` used to create a literal `./~/reports/`. Every output prompt now expands the tilde (paths otherwise stay relative), and the results pager ends with "Report written to /abs/path" so the report's location answers itself.
- **CLI parity in the prompts (T5).** The decode-scan modes gained an "ffmpeg path (blank = auto)" prompt (the CLI's `--ffmpeg` had no TUI equivalent); stats gained the layout prompt its siblings have; the FLAC prefer-tool prompt re-asks until it gets `flac` or `ffmpeg`; the OK-rows question is labelled "(verbose report)" since one answer drives both knobs; and the unreachable `or "{artist}/{album}"` arms after layout prompts (which would have shadowed a configured layout if ever reached) are gone.
- **TUI niceties (T6).** Bad numeric input re-prompts with a note instead of silently using the default; menus taller than the terminal scroll to keep the selection visible; the pager pans horizontally (←→/h/l) with `…` truncation markers and no longer recomputes its width on every keypress; Ctrl-U clears a prompt field; the in-mode progress box is throttled to ~10 redraws/s and shows `n/total · Ctrl-C cancels`; the Quit/Change-root result tuples are named constants. Mode-level: a bad smart-playlist rule is now rejected once, up front (`validate_rule`), instead of printing one error per track — the CLI benefits too.
- Tests extended (`tests/test_tui.py` + `tests/test_playlists.py`, 436 total across the suite): a hermetic menu harness (config stubbed, pager captured) covering cancel chains, root validation, multi-root passthrough and titling, prefer-tool re-prompting, `~` expansion, integer re-prompts, and the rule validator.

## cleaner.py v1.3.3 (2026-07-02)

Polish release from the 2026-07-01 audit (roadmap L-cleaner batch); one behavior change, strictly safer.

- **Fix: "identical" duplicates are verified by content sample, not size alone.** A colliding file was dropped as a DROP DUPE purely on byte count, so a same-size re-encode or retag of a different take was deleted. The drop now also requires equal first/last 64 KiB (`head_tail_equal`); a same-size file with differing sampled bytes takes the collision path instead, so audio survives as `.from-fragment`. A mid-file-only difference within same-size files can still slip through the sample; the log line says "sampled bytes" for that reason.
- **Fix: an MP3 with no ID3 header (tags only in APEv2/ID3v1) is reported and counted** (`SKIP (no ID3 header ...)`, new `tag_no_id3_skipped` stat) instead of silently skipped; the `.wav` path was already the model.
- **Fix: the ASF tag write deletes case-variant keys first.** The `.wma` branch read keys case-insensitively but wrote canonical case beside a variant original, leaving two keys; it now clears variants like the Vorbis branch.
- **Fix: pass headers no longer orphan the log timestamp.** `log("\n--- PASS ---")` put the timestamp prefix on the blank line; the blank line is emitted separately now.
- **Fix: a merge survivor that also gets renamed is one tag target, not two.** The rename hook and the group both recorded the artist; the group's record (which also carries the merged sources) is now the only one for that case, while Pass 3 sweep renames still record their own.
- Cleanups: `_normalize_folder_name`/`_normalize_file_name` share one `_rename_to` guard helper (H3's fix no longer has to be made twice), `consolidate_group` computes each folder's file count once instead of walking twice, and `file_count` no longer uses `_` as a load-bearing variable.
- Tests extended (`tests/test_cleaner.py`, 82 total): same-size-different-bytes collisions, `head_tail_equal` edge cases, the headerless-MP3 report, the ASF case-variant delete, the log format, and tag-target dedupe (merge+rename vs sweep).

## genre_foldermap.py v1.3.3 (2026-07-02)

Polish release from the 2026-07-01 audit (roadmap L-genre_foldermap batch). Also corrects a housekeeping miss: yesterday's v1.3.2 changes shipped without bumping `__version__` (it still said 1.3.1); this release moves it straight to 1.3.3.

- **Fix: the genre gate matches case-insensitively and reuses the existing folder's spelling.** A stray tagged `hip hop` against an existing `Hip Hop` folder was flagged UNKNOWN GENRE, and `--allow-new-genre` would then mint a case-variant duplicate top-level folder. A case-variant match now files into the existing folder (artist-level sidecars follow the same spelling), and an organized album whose tag differs from its folder only by case is no longer a NOTE.
- **Fix: cross-device moves are refused.** `shutil.move` silently degrades to copy+delete across filesystems, against the documented mv-only contract (audio bytes are never rewritten); the Runner now compares device numbers and refuses with a `CROSS-DEVICE (refused)` line and its own stats counter.
- **Fix: paths containing a tab or newline are refused, not moved.** Such a path would corrupt the manifest TSV, making the move unrevertable; it is flagged `UNSAFE NAME` (rename the folder first) and its bookkeeping is skipped.
- **Docs honest about revert and reversibility.** Every `--revert` mention now says it is a dry-run by default (add `--apply` to execute, same as a forward run), including the hint the manifest header writes; the "reversible" claim is scoped to files and moved folders (source folders pruned empty are not recreated); the vocabulary is documented as record-derived (a genre folder with no readable audio that run drops out of the gate). Comment fixes: the `Move.kind` comment no longer claims pruning behavior, the `dst == src` guard is annotated with its only reachable case (genre folder named like the staging inbox), and the prune comment describes the real single deepest-first pass.
- Tests extended (`tests/test_genre_foldermap.py`, 55 total): case-variant gate reuse (with and without `--allow-new-genre`), the case-variant NOTE suppression, unsafe-name refusal, and the cross-device refusal.

## genre_tidy.py v1.2.3 (2026-07-02)

Polish release from the 2026-07-01 audit (roadmap L-genre_tidy batch).

- **New: `--layout` on both subcommands.** The scanner was pinned to `{artist}/{album}`, so on the genre-foldered library genre_foldermap produces, an untagged file's artist fell back to the genre folder name. Pass `--layout "{genre}/{artist}/{album}"` to recover artist/genre from the right path levels, exactly like cleaner.py.
- **Fix: tabs and newlines in tag values no longer corrupt the map.** A genre like `Rock<TAB>Pop` was written as two TSV columns, so it read back as two allowed genres and a fresh map was not a no-op (`apply` rewrote the tag). Generated fields fold tab/newline to a space; `norm()` collapses whitespace the same way, so the sanitized row still matches the raw tag at compliance time.
- **Fix: silent last-wins on normalized-key collisions.** Two hand-edited rows that normalize to the same artist (`Jay-Z` vs a curly-dash `Jay‐Z`) silently dropped the earlier row; the overwrite warns on stderr now.
- **Docs: the EXCLUDED trade-off is stated.** A real artist actually named "Various" or "VA" is permanently unenforceable; the comment block above `EXCLUDED_ARTISTS` acknowledges it.
- Tests extended (`tests/test_genre_tidy.py`, 40 total): TSV sanitization round-trips, the collision warning, and the layout passthrough.

## retag.py v1.1.2 (2026-07-02)

Polish release from the 2026-07-01 audit (roadmap L-retag batch); no new features. RT4 (the "subdirectories not descended" note) had already landed with M17.

- **Fix: direct invocation is byte-idempotent.** A file already carrying exactly the target genre was still fully rewritten on every run (APEv2 deleted, v2.4 tag re-saved as v2.3, ID3v1 minted). A per-file no-op guard now skips the write and logs `unchanged`; for MP3 the guard also checks the hidden genre spots retag exists to clear, so a matching TCON with a stray APEv2 tag or bare `TXXX:GENRE` frame still gets the write. Dry-run predicts the same skips. (genre_tidy already gated via compliance; this hardens direct use.)
- **Fix: an unreadable Vorbis-family file reports its failure.** `mutagen.File()` returning None for a corrupt file used to return False *silently*, invisible next to every other failure path's `[!] Failed` line; it prints one now.
- **Fix: WMA dry-runs show the real old genre.** `read_genres` used `easy=True`, which has no ASF wrapper, so `.wma` always read back `[]`; `WM/Genre` is read directly now.
- **Docs: dead code dropped.** The Vorbis branch popped both `"genre"` and `"GENRE"` with a comment about clearing "both cases"; mutagen's comment dict is case-insensitive, so the second pop could never do anything and is gone.
- Tests extended (`tests/test_retag.py`, 24 total): byte-identical second run, the stray-APE and bare-TXXX force paths, dry-run unchanged prediction, the WMA read, and the unreadable-file report.

## rerate.py v1.0.2 (2026-07-02)

Polish release from the 2026-07-01 audit (roadmap L-rerate batch); no new features. RR4 (error accounting) had already landed with M10.

- **Fix: hidden directories are pruned from the walk.** `.testing/` album copies (and any dot-directory) were rerated along with the library; the walker now prunes them, matching replaygain.py.
- **Fix: a bad `--log` path is a clean error** (exit 1 with a message, not a traceback), matching the directory validation.
- **Docs: the REMAP comment no longer claims extra entries are harmless.** They are not: the map is deliberately closed because only DeaDBeeF's 2★ (127) and 4★ (254) have a byte both players agree on. Byte 64 is a fixpoint collision (DeaDBeeF 1★ and foobar 2★ share it, unfixable by byte rewrite alone) and DeaDBeeF 3★ (190) has no byte both players read as 3 stars. The comment now says so, and warns that any candidate entry needs verifying in both players first.
- **Docs: the save is described honestly.** "Rewrites the POPM byte in place" undersold it; the save re-serializes the whole ID3 tag (v2.4 comes back v2.3, ID3v1 refreshed, matching retag.py), audio untouched.
- Tests extended (`tests/test_rerate.py`, 12 total): hidden-dir pruning and the guarded log open.

## replaygain.py v1.2.1 (2026-07-02)

Polish release from the 2026-07-01 audit (roadmap L-replaygain batch); no new features.

- **Fix: an album rsgain silently declined is a NO-OP, not "scanned".** Easy mode exits 0 with "No files were scanned" when it finds nothing it can tag; the run counted it scanned and logged nothing unusual. The album's gains are now read back before it counts: no message and no gains means a distinct `NO-OP` log line and its own summary counter.
- **Fix: M4A gains read back as text.** `MP4FreeForm` is a bytes subclass, so the post-scan verification logged `b'-6.66 dB'`; bytes values are decoded now.
- **Fix: every run leaves a log trail.** A run where everything was skipped (`--skip-tagged` on a fully tagged library) or that was declined at the confirmation prompt wrote nothing at all to the log; both now write a proper `RG RUN START`/summary/`END` block saying why nothing happened.
- **Fix: a bad `--log` path is a clean error.** The log open was unguarded, so an unwritable path was a traceback; it now validates like `directory` does and exits 1 with a message.
- **Docs: `--skip-tagged` target-blindness stated.** It checks only that gain tags exist, not what target they were computed against, so an album tagged at the default 89 dB reads as tagged under `--target-lufs -14`; the docstring says to rescan without `--skip-tagged` when changing targets. Also tidied: the worklist filter unpacks its tuples instead of indexing `w[5]`.
- Tests extended (`tests/test_replaygain.py`, 27 total): NO-OP vs scanned accounting, the bytes decode, skip-everything and declined-prompt log blocks, and the guarded log open; the `main` harness is now shared between the dry-run and apply test classes.

## apestrip.py v1.1.2 (2026-07-02)

Polish release from the 2026-07-01 audit (roadmap L-apestrip batch); no new features.

- **Fix: unmigratable APE items are reported and skipped, never written as junk frames.** Under `--keep-metadata`, a binary item bound for a TXXX passthrough became an *empty* TXXX frame, an external cover reference became an `APIC` with zero bytes of image data, and a cover in an unrecognized image format was mislabeled `image/jpeg`. All three now land in a per-file `[skip]` report (with the reason) and are dropped with the tag; the preview summary counts them.
- **Fix: a described COMM frame no longer blocks a real comment migration.** The redundancy check counted any `COMM*` frame, so an iTunes `COMM:iTunNORM:eng` normalization blob made a sole-source APE Comment read as redundant and silently dropped it; only an unqualified comment (empty description) counts now.
- **Fix: the raw item parser bounds each value length.** A malformed item whose declared length overran the footer would slurp footer bytes into its value; the parse now stops at such an item instead.
- **Fix: repair-path fsync ordering.** With migrations, `id3.save(tmp)` rewrote the temp file *after* the flush+fsync, so the atomic swap could install a half-flushed tag on power loss; the file is synced again after the save.
- **Docs honest about the redundancy check.** The docstring promised value-level preservation ("so nothing is lost") but the check is presence-only: a field ID3 already has, whatever its value, is treated as authoritative and not overwritten. Now says so. Also tidied: the garbled `_RawAPEValue` docstring, a narration comment, the mid-file `AUDIO_EXT` constant, and `plan_file`'s annotation (it also accepts raw-parsed items).
- Tests extended (`tests/test_apestrip.py`, 53 total): binary/external/unknown-format skip paths, the iTunNORM redundancy trap, and the overflowing item length.

## apestrip.py v1.1.1 (2026-07-01)

Bugfix release from the 2026-07-01 audit (roadmap items M1 to M6); no new features.

- **Fix: `--keep-metadata` on an ID3v1-only MP3 no longer clobbers the v1 values.** Without seeing ID3v1, every APE field looked sole-source and the eventual `save(v1=2)` rebuilt ID3v1 from the sparse v2 frames, blanking the old title/artist (exactly the old rips most likely to carry APE tags). Migration planning now seeds from the trailing v1 block when there is no v2 tag. On mutagen 1.46+ (which reads v1 into `ID3()` itself) the bug never fired; the seed covers older mutagen and the test pins the behavior either way.
- **Fix: multi-value APE text items migrate as separate values.** They were flattened to one NUL-joined string, and an embedded NUL truncates a v2.3 UTF-16 frame so players showed only the first value. Values now stay separate through the migration (the v2.3 save renders them slash-joined, all values visible); labels render them readably.
- **Fix: `--repair-malformed` preserves file permissions.** The atomic-swap temp file (mode 0600) was installed verbatim, silently stripping group/other access on shared-mount libraries; the original mode is copied before the swap.
- **Fix: a repair-path I/O error is a per-file error, not a crash.** An unreadable file or read-only directory raised out of the unguarded repair body and killed a library-wide run mid-write-pass; it now returns an error result the loop's accounting handles.
- **Fix: the repair excision recognizes trailing structures.** Everything between the APE footer and a strictly terminal ID3v1 used to be cut as junk: a Lyrics3v2 block was deleted, and an ID3v1 followed by stray padding was itself excised (the end-anchor missed it). Lyrics3v2 and a terminal ID3v1 are now preserved byte for byte; junk before a terminal ID3v1 (the shape that motivated the flag) is still excised; anything else refuses the repair and reports instead of guessing.
- **Fix: honest audit log and exit code.** A file whose APE tag vanished between the planning and write passes was logged as "stripped APEv2 tag"; it now logs "no APEv2 tag at write time (skipped)". `main` exits 1 when any file errored (was always 0).
- Tests extended (`tests/test_apestrip.py`, 46 total): v1-only preservation, multi-value migration, permission preservation, read-only-dir error path, the three trailer shapes, and first coverage of the `main` execution loop (happy path, vanished tag, error exit).

## replaygain.py v1.2.0 (2026-07-01)

Bugfix release from the 2026-07-01 audit (roadmap items M7 to M9); no new features.

- **Fix: custom mode no longer fails a whole album over one rsgain-unsupported file.** `rsgain custom` rejects its entire file list when one entry is unsupported (verified live: a raw ADTS `.aac` bonus track failed a 12-FLAC album under `--target-lufs` while default easy mode sailed through). The custom branch now filters to the formats rsgain 3.6 can write (per `rsgain --help`), logs each `SKIP (rsgain-unsupported)` file, and skips an album with nothing left.
- **Fix: dry-run honors `--skip-tagged`.** The preview printed "Would scan" over the unfiltered worklist while apply filtered it; on a mostly-tagged library it predicted hundreds of albums where apply would scan a handful. The scan set is now computed once, before the dry/apply split; the dry-run marks skipped rows and summarizes "Would scan X of Y".
- **Fix: nested album folders are no longer rescanned via rsgain easy's recursion.** `rsgain easy` scans a directory recursively, so a parent album dir with a loose track beside `CD1/` meant CD1 was scanned twice and, with `--skip-tagged`, rewritten even when skipped. Such parents are excluded from easy-mode runs and reported (`NESTED ALBUM (skipped): ... scan the subfolders or flatten`); custom mode passes explicit direct files and is unaffected. The fuller option (scanning the parent's direct files as their own unit via custom mode) is deliberately not taken yet; flagged as an open decision.
- Tests extended (`tests/test_replaygain.py`): supported-set filtering and argv exclusion, nested-parent detection (including the sibling-string-prefix trap), and first `main` coverage via dry-run runs; the test file now imports `AUDIO_EXTENSIONS` from `lattice.config` instead of carrying a copy.

## rerate.py v1.0.1 (2026-07-01)

Bugfix release from the 2026-07-01 audit (roadmap items M10, M11); no new features.

- **Fix: one bad file no longer crashes the whole run.** `tags.save` sat outside the try in a function documented "never raises", so a read-only file or full disk killed the walk with no log line and no SUMMARY. `rerate_file` now returns `(changes, error)`; `main` logs an `ERR` line per failure, counts errors into the SUMMARY, and exits 1 when any occurred.
- **Documented decision (M11): the remap stays byte-only, with no POPM email filter.** Safe in a DeaDBeeF/foobar-only library; the docstring now states the assumption prominently and points at the dry-run output (which prints each frame's email) for libraries touched by other taggers. Revisit with an email gate only if a foreign tagger actually shows up.
- Tests extended (`tests/test_rerate.py`): the error result on a read-only file, and first `main` coverage (error accounting, exit codes).

## retag.py v1.1.1 (2026-07-01)

Bugfix release from the 2026-07-01 audit (roadmap items M16, M17); no new features. Exit-code contract change: **`main` now returns 1 when any file failed to write** (known callers: `genre_tidy.py` apply, which wants exactly this, and manual use).

- **Fix: failures are visible to callers.** Per-file failure messages go to stderr (they were buried in stdout), and a run where files failed exits nonzero, so `genre_tidy.py` can count the album as an error instead of a successful retag.
- **Fix: unwritable audio in a mixed album is named.** Files retag cannot write (`.wav`, `.aac`, `.alac`, `.ape`, `.wv`, `.aiff`) were silently skipped; each now logs `skip (unsupported): <name>`, and the "No valid audio files found" note mentions that subdirectories are not descended.
- Tests extended (`tests/test_retag.py`): stderr routing, nonzero exit with a read-only file, full-success exit 0, and the mixed-album skip line.

## genre_tidy.py v1.2.2 (2026-07-01)

Bugfix release from the 2026-07-01 audit (roadmap items M14 to M17); no new features.

- **Fix: artists whose name starts with `#` are data, not comments.** `#1 Dad` used to be discarded by the map parser, could never be enforced, and was re-appended as a "new" artist on every rebuild. The comment rule is now explicit (`#` + space, dash, or end of line); generated comments always match it, so a `#`-leading artist survives the round-trip. The rule is documented in the map header.
- **Fix: EXCLUDED (Various Artists) entries no longer re-append on every rebuild.** They intentionally have no data row, so the "already present" check could never see them and every `build` appended a fresh dated marker plus a duplicate EXCLUDED comment; they are now filtered from the new-artist diff and the "No new artists" path is reachable.
- **Fix: a failed retag counts as an error, not a retag.** `apply` counted an album into `retagged` before invoking retag and retag always exited 0; with retag v1.1.1's exit code, a nonzero run now increments `errors` (with the captured stderr logged) and `retagged` only counts successes.
- **Fix: albums retag cannot write are skipped with a reason.** A `.wav`-only album with a stray genre was "retagged" forever (retag skipped every file, exited 0, nothing converged). `apply` now checks the album's extensions against retag's writable set (imported from `retag.py`, so the two cannot drift), logs `UNSUPPORTED FORMAT (skipped)` once, and counts it under a new `unsupported` stat.
- Tests extended (`tests/test_genre_tidy.py`): the comment rule and round-trip, rebuild no-op with a VA comp, and first `cmd_build`/`cmd_apply` coverage (success, failure-counts-as-error, unsupported-format convergence) with the real retag subprocess.

## genre_foldermap.py v1.3.2 (2026-07-01)

Bugfix release from the 2026-07-01 audit (roadmap items M12, M13); no new features.

- **Fix: a rejected album move no longer triggers its sidecar bookkeeping.** When an album was skipped (`DEST EXISTS`/collision), its artist-level sidecars (cover art, .nfo) were still relocated to the target genre, stranding the un-moved album without its artwork, and the source dir was still queued for pruning. Bookkeeping is now gated on the move actually being planned.
- **Fix: `--revert` runs through the Runner.** Reverts moved as much as an apply but bypassed the context manager: no audit trail, no replay manifest (the `.revert.tsv` was named but never written), and a dry-run revert always predicted `pruned=0` because nothing fed the virtual-removed set. Each restore now goes through `do_move` (kind `revert`), so the manifest, stats, and dry-run prune prediction all come for free; the Runner also models virtual *creations*, so a dry-run revert sees restored albums as children of their original parents and predicts exactly the prunes a real revert performs.
- Tests extended (`tests/test_genre_foldermap.py`): rejected-move bookkeeping, the replayable `.revert.tsv`, dry-vs-real revert prune parity, and dry-run-revert-touches-nothing.

## cleaner.py v1.3.2 (2026-07-01)

Bugfix release from the 2026-07-01 audit (roadmap items M18 to M20); no new features.

- **Fix: multi-valued tags keep all their values through the typographic fold.** The tag pass read only the first value and wrote back a singleton, so a FLAC with `artist=["Artist A’s Band", "Artist B"]` lost the co-artist whenever the first value needed folding. Every value is now folded and written back as the full list. The authority restamp deliberately still collapses to the single survivor name (that is its semantic), now stated in a comment.
- **Fix: dry-run models creations, not just removals.** Collision and rename decisions consulted the raw disk, so previews diverged from apply in several shapes: a three-way merge where both sources carry the same extra track previewed two plain moves where apply performs one move plus an `AUDIO COLLISION`; two siblings folding to the same rendered name previewed two renames where apply performs one. The Run now tracks virtual creations (mapping each virtual destination to the real path holding its bytes, so size/kind checks still read real data), every existence/size check in the merge and rename paths goes through the virtual-aware views, and Pass 2/3 walks skip folders an earlier pass virtually merged away. Dry-run stats now match apply on all the audited fixtures; the residual limitation (contents of virtually-moved folders are not modeled recursively) is noted in the docstring.
- **Docs: the module docstring's stale v1.2.0 tag-pass text is gone.** Two passes were both numbered "4." and the survivor described the pass as MP3-only and merged-folders-scoped, contradicting shipped v1.3.0 behavior; one accurate pass description remains, and the misleading "Pass 3" comment on the tag-target hook (which also fires for Pass 1/2 survivor renames) is corrected.
- Tests extended (`tests/test_cleaner.py`, 72 total): multi-value fold and authority collapse, dry-vs-apply parity for the three-way merge and same-render siblings, and virtually-removed folders being skipped by the group scan.

## v4.8.2 (2026-07-01)

Bugfix release for the TUI, from the 2026-07-01 audit (roadmap items H6 and H7); no new features.

- **Fix: a mode error no longer kills the TUI with the terminal stuck in curses mode.** `_run_with_capture` had no exception boundary, so a mode failure (say, an unwritable output path after a completed scan) escaped as a raw traceback, lost the captured results, and left the screen in curses mode because the in-mode progress bar had started a screen nothing tore down. Mode exceptions are now caught and paged under an `[Error]` heading with the full traceback plus whatever output was captured; Ctrl-C pages a `[Cancelled]` notice the same way. The curses screen is explicitly ended and the terminal reset before paging, and `_TUIPbar.close()` now ends the screen it started, so well-behaved modes hand back a sane terminal too.
- **Fix: a plain-text fallback session no longer routes progress to the curses bar.** `interactive_menu` set `utils.IN_TUI` unconditionally, so a session that had already degraded to the typed-input menu (curses missing, or stdin not a TTY) still handed progress to `_TUIPbar`, which could crash on the missing module or hijack the screen mid-session. `IN_TUI` now tracks `_USE_CURSES`, re-evaluated every menu pass.
- **Fix: a curses init failure no longer reads as "Quit".** On capability-poor terminals (`TERM=vt100`, dumb terminals) `curs_set`/color setup raised `curses.error`, which the menu treated as the user choosing Quit: the TUI silently exited 0 even though the text fallback works. Cosmetic capabilities (colors, cursor visibility) are now non-fatal, so such terminals get a monochrome TUI; a real `curses.wrapper` failure flips the session to the text fallback menu instead of exiting.
- New tests in `tests/test_tui.py` pin all three behaviors (paged tracebacks, fallback sessions leaving `IN_TUI` unset, the fallback sentinel re-entering the menu loop).

## cleaner.py v1.3.1 (2026-07-01)

Bugfix release from the 2026-07-01 audit (roadmap items H1, H2, H3); no new features.

- **Fix: `feat.` detection no longer matches inside words.** The guest-credit regex used a zero-width `\s*` boundary, so the `ft` ending "Left", "Swift", or "Croft" read as a feat marker: `--normalize-tags` under a merged/renamed artist folder rewrote a clean "Left Boy" tag to "Left Boy feat. Boy", and every further pass appended another "feat. Boy" (non-idempotent corruption). The marker now requires a real token boundary. Also fixed in the same change: a parenthesised credit like "A (feat. B)" no longer leaves the stray closing paren on the guest.
- **Fix: `.wma` collisions are treated as audio.** `.wma` was in `TAGGABLE_EXT` but missing from `AUDIO_EXT`, so a differing-size `.wma` collision during a merge fell into the non-audio branch and the source copy was deleted, violating the "audio is never overwritten or deleted" guarantee. It now survives as a `.from-fragment.wma` copy, and `--normalize-filenames` reaches `.wma` tracks. `AUDIO_EXT` is now documented against the package's `AUDIO_EXTENSIONS` (`.mp4` stays deliberately excluded as ambiguous with video).
- **Fix: dry-run now predicts the survivor rename.** The rename collision guard checked only the disk, where the merged-away source folder still exists during a dry-run, so the flagship scenario (canonical `Drive‐By Truckers` with a U+2010 hyphen absorbing `Drive-By Truckers`) previewed `RETAIN NAME` while apply performed the rename, and the tag pass preview consequently missed the authority restamps. The guard now consults the run's virtual removals, so dry-run and apply report identical stats and tag targets.
- Tests extended (`tests/test_cleaner.py`): mid-word `ft` cases, paren stripping, feat idempotency, `.wma` collision and rename, and a dry-vs-apply parity check on the survivor-rename fixture.

## genre_foldermap.py v1.3.1 (2026-07-01)

Bugfix release from the 2026-07-01 audit (roadmap item H4); no new features.

- **Fix: multi-disc album subfolders no longer poison the genre vocabulary.** The scanner emits one record per audio-bearing directory, so `Artist/Album/CD1` in a flat library arrived as a depth-3 record and classified as "organized", which put the artist's name into the genre vocabulary, turned the placement gate on, and flagged every ordinary stray `UNKNOWN GENRE`: one disc folder broke the whole flat-to-genre conversion, and the disc album itself was never filed. Disc subfolders (`CD1`, `Disc 2`, `disk_3`, `DVD 1`, `Side 1`, `Vinyl 2`, ...) now collapse to their parent album, which moves as one unit with the discs inside; CD1+CD2 records dedupe to a single move, and discs whose genre tags disagree keep the first and flag a `DISC GENRE MISMATCH` issue instead of planning two moves of one folder.
- **Fix: organized disc albums no longer spam `TOO DEEP`.** A legit `Genre/Artist/Album/CD1` is recognized as that album (in place, no issue) instead of producing perpetual wrong-root noise.
- **Fix: inbox content can never read as "organized".** `Unfiltered/Artist/Album/CD1` strips to the album and is filed like any stray; anything deeper or stranger under the inbox is flagged `STAGED TOO DEEP (left in inbox)` for manual review rather than being silently treated as an organized album stuck in the inbox forever.
- Tests extended (`tests/test_genre_foldermap.py`): disc classification (flat, organized, staged, and an album genuinely named like a disc), one-unit end-to-end move with the gate off, vocabulary hygiene, disc genre disagreement, and the staged-deep flag.

## genre_tidy.py v1.2.1 (2026-07-01)

Bugfix release from the 2026-07-01 audit (roadmap item H5); no new features. `retag.py` is unchanged.

- **Fix: a slash canonical no longer causes a permanent retag loop.** `apply` used to split `"Emo / Orgcore"` on `/` into two genre values; retag wrote them as a multi-value tag that never read back equal to the map (lattice reads only the first Vorbis genre field; MP3's v2.3 save slash-joins without spaces), so every `apply` re-ran retag on the same albums forever: files rewritten, APEv2 stripped, mtimes churned, dry-run never converging. The canonical is now passed verbatim as one genre value, so the tag written is exactly the string the compliance check reads back and the run converges. This treats a slash genre as one genre (which is what DeaDBeeF/foobar2000 display anyway); nothing in the shipped `artist_genre_defaults.tsv` used a slash genre, so no live behavior changes.
- Tests updated (`tests/test_genre_tidy.py`): the single-argument invocation is pinned, and a new end-to-end check writes the slash canonical through `retag.apply_genres` on FLAC and MP3 fixtures and asserts the re-read tag is compliant (a second apply would plan zero retags).

## genre_foldermap.py v1.3.0 (2026-06-21)

New feature (no package change): a **staging inbox**, so albums dumped outside the organized tree can be filed without being mistaken for a genre.

- **The problem.** A tagger such as MusicBrainz Picard drops fresh `Artist/Album` folders into a holding area to keep them out of the organized root. If that holding folder lives at the library root (e.g. `Music/Unfiltered/`), the old depth logic read `Unfiltered/Artist/Album` as an already-organized album under a genre literally named "Unfiltered" and refused to move it out.
- **What it does.** A staging folder name (default `Unfiltered`) is stripped from the front of a path before classification, so `Unfiltered/Artist/Album` classifies as a flat stray and is filed into the real taxonomy at the root, exactly like `Artist/Album`. The genre vocabulary gate still applies, so a staged album only lands in a genre the library already uses (or pass `--allow-new-genre`).
- **The inbox is kept.** Each staged album's per-artist source folder is pruned once emptied, but the `Unfiltered/` inbox itself is never treated as a source dir, so it survives the run for next time.
- **New `--staging DIR`** sets the inbox name (default `Unfiltered`); pass `--staging ""` to disable the behavior entirely. The wrong-root `TOO DEEP` guard is unaffected.
- Tests extended (`tests/test_genre_foldermap.py`): classify stripping for album and loose shapes, the unchanged no-staging behavior, end-to-end filing into the taxonomy with the cover carried along and the inbox kept, and the vocabulary gate still applying to staged albums.

## cleaner.py v1.3.0 (2026-06-21)

Extends the v1.2.0 tag work into a general, library-wide typographic normalizer covering names at every depth and the title/album/artist tags across all formats.

- **`--normalize-tags` is now library-wide and multi-format.** It walks every audio file, not just merged/renamed artist folders. **Title and album** get a pure typographic fold (CP1252 mojibake, curly quotes, and broken hyphens cleaned; the words never change, so the folder is never an authority for them). **Artist and albumartist** fold the same way everywhere, except under a merged or renamed artist folder, where they are still restamped to the surviving folder name (guest credits preserved). All formats are handled (MP3/FLAC/Ogg/Opus/m4a/WMA), each written in its native fields; ID3 stays v2.3 + refreshed v1. Only changed fields are touched, so a clean file is a no-op and the pass is idempotent.
- **Correct typography is preserved.** The tag fold now matches the folder rename's philosophy: en/em dashes and the ellipsis are kept (`Selected Ambient Works 85–92` keeps its en dash), and a CP1252-mojibake dash is repaired to the real dash rather than flattened to a hyphen. Genuinely broken hyphens (U+2010-2012/2015) are still folded to ASCII.
- **`--normalize-names` now recurses to every folder depth** (it was capped at two), so album folders under a `Genre/Artist/Album` library are reached.
- **New `--normalize-filenames`** renames audio track files the same way (extension kept verbatim). It is separate from `--normalize-names` because renaming files is a distinct change (filenames are referenced by playlists and cue sheets); the two compose.
- **Behavior-change note.** `--normalize-names` reaching all depths and `--normalize-tags` sweeping the whole library are both broader than v1.2.0. Both remain off by default and dry-run-previewable; the v1.2.0 single-folder behavior is a subset of the new one.
- Tests extended (`tests/test_cleaner.py`): multi-format writer (FLAC + MP3, case-insensitive Vorbis keys, authority vs plain fold, no-op, absent-field), library-wide folding with and without a merge, deep folder + filename renames with collision/illegal guards, dry-run fidelity, and a run-twice idempotency check.

## cleaner.py v1.2.0 (2026-06-20)

New feature (no package change): **`--normalize-tags`** teaches `cleaner.py` to rewrite embedded artist/albumartist tags so they match the surviving folder name after a merge or rename.

- **The problem.** The merge passes consolidate variant artist folders (`Bonnie 'Prince' Billy` / `Bonnie Prince Billy` / a CP1252-mojibake `Bonnie \x93Prince\x94 Billy`) into one folder, but the tags inside still carry the old spellings. Players that show the artist tag, not the folder, still see three artists.
- **What it does.** With `--normalize-tags`, a fourth pass rewrites the MP3 `artist`/`albumartist` tags under every merged or `--normalize-names`-renamed artist folder to the survivor's name, which is the naming authority. CP1252 mojibake and curly punctuation fold to straight ASCII; a trailing guest credit is preserved (`... feat. Tim O'Brien`). A file already correct is left untouched.
- **Artist level from `--layout`.** A new `--layout` option (default `{artist}/{album}`) tells the pass which path depth names an artist, so a genre or album folder is never restamped as one. On a genre-first library, pass `--layout '{genre}/{artist}/{album}'`.
- **MP3-only, same guarantees as the other companions.** Non-MP3 audio is reported and left alone (its tags are already authoritative); writes are ID3v2.3 + refreshed ID3v1, matching `retag.py`. Off by default, `--dry-run` previews every change, and the run is idempotent. Run `apestrip.py` first if a stray APEv2 tag is in play (APEv2 overrides ID3 on the players that read it).
- Tests extended (`tests/test_cleaner.py`): the fold/feat helpers directly, plus merge restamping, dry-run fidelity, the clean-file no-op count, and the artist-depth gate.

## apestrip.py v1.1.0 (2026-06-15)

Behavior change (no package change): **the default is now a pure strip.** apestrip deletes the APEv2 block and leaves ID3 byte for byte; it no longer absorbs APE fields into ID3 unless you ask.

- **Why.** Migrating APE values into ID3 by default was backwards. The whole reason to run apestrip is to drop stray APE values (the genre above all), so copying them into ID3 is exactly how a bad APE genre ends up baked into the real tags. The old default forced manual genre cleanup afterward.
- **`--keep-metadata` restores the old behavior.** With the flag, every APE field whose value is not already in ID3 is migrated into the correct frame before the strip, exactly as before (genre still never migrated, ratings still report-only). Without it, nothing is migrated.
- **Always reported.** APE `Genre` and `Rating` are scanned and reported in both modes, so the worklist still shows what is being dropped (and still warns when stripping an APE genre would leave a file with no genre at all).
- **Repair too.** `--repair-malformed` follows the same rule: the malformed block is always excised, but sole-source fields are migrated into ID3 only when `--keep-metadata` is also given.
- Tests extended for both modes (`tests/test_apestrip.py`).

## apestrip.py v1.0.0 (2026-06-15)

New companion script (no package change): a lossless stripper for stray **APEv2 tags on MP3s**.

- **The problem.** Some MP3s (commonly torrent rips) carry a hidden APEv2 tag alongside their ID3 tags. Players that read APEv2 on MP3 (foobar2000, DeaDBeeF) merge the APE values over the ID3 ones, so a stray APE genre such as `Trash Metal` keeps showing up as `Trash Metal, Metal` no matter how often the ID3 genre is corrected, and ordinary tag editors never touch the APEv2 block. `retag.py` already deletes APEv2 as a side effect of rewriting the genre, but only the genre; `apestrip.py` is the general fix.
- **Lossless by design.** Before deleting the APEv2 tag, every APE field whose value is not already in ID3 is migrated into the correct frame: core text to its native frame (`Year`→`TDRC`, `Title`/`Artist`/`Album`→`TIT2`/`TPE1`/`TALB`, `Album Artist`→`TPE2`, etc.), `Comment`→`COMM`, `Cover Art (Front)`→`APIC`, `Unsynced lyrics`→`USLT`, sort orders to `TSO*`, and anything else (MusicBrainz IDs, ISRC, barcode, ReplayGain, ...) to a `TXXX:<key>` passthrough.
- **Two deliberate exceptions.** Genre is never migrated (ID3 stays authoritative; the APE genre is the value being removed; a file with no ID3 genre is reported, not invented). Rating is never written, because APE and `POPM` use different scales and an auto-conversion would corrupt star counts (the hazard `rerate.py` exists for); APE ratings are reported instead.
- **Guarded like the other companions.** `--dry-run` previews the full worklist and writes nothing; the real run prints the worklist and asks for confirmation (`--yes` to bypass, auto-bypassed when stdin is not a TTY); an append-only timestamped log is written (default `<directory>/apestrip.log`); the operation is idempotent. Recursive over the given directory, so it handles one album or a whole library. MP3-only (the APEv2-over-ID3 conflict is specific to MP3). Migrations are saved as ID3v2.3 + refreshed ID3v1, matching `retag.py`. Tested by `tests/test_apestrip.py`.
- **Malformed-tag handling (`--repair-malformed`).** Some rips carry a structurally broken APEv2 tag (footer with the `IS_HEADER` bit wrongly set, junk bytes before a trailing ID3v1) that mutagen refuses to load. By default these are **reported, not silently skipped**, so the run is honest about what it could not touch. `--repair-malformed` opts into fixing them: the tag is parsed straight from the bytes, but only after proving the footer sits exactly where the header's size field points (so the excision boundary is a real tag edge, not a chance signature in the audio); sole-source fields are migrated first, then the APE block is cut out via direct byte surgery, written to a temp file, verified (still decodes, no APE signature left), and atomically swapped in. Audio frames and the trailing ID3v1 are preserved byte for byte; any failed check leaves the original untouched.

## v4.8.1 (2026-06-10)

Bugfix release from a full code review; no new features.

- **Fix: the no-curses fallback menu dispatched the wrong modes.** The typed-input menu shown when curses is unavailable (or stdin is not a TTY) had a hand-maintained key map that drifted as modes were added: "6" was labelled "Test WAV files" but ran Extract cover art (which writes `cover.jpg` files if the dry-run prompt is declined), "7"–"9" were each shifted one mode group over, "10"–"13" were rejected as invalid, and the WAV/WMA tests, art-quality audit, bitrate audit, and ReplayGain audit were unreachable by number. The fallback listing and key map are now both generated from the same `_MAIN_SECTIONS`/`_LIB_SECTIONS` data the arrow-key menu renders, so they cannot drift again, and new word aliases (`wav`, `wma`, `quality`, `bitrate`, `rg`/`replaygain`) cover the previously unreachable modes. The curses menu itself was never affected. Pinned by the new `tests/test_tui.py`.
- **Fix: smart-playlist AND/OR no longer rewrites string literals.** The SQL-style convenience was a plain substring replace, so `genre == 'Drum AND Bass'` was silently rewritten to compare against `'Drum and Bass'` and could never match. The fold is now word-bounded and applied only outside quoted segments; as a side effect, `(rating >= 4)AND(...)` without padding spaces now also works.
- **Fix: art-quality audit no longer truncates folder covers.** Folder art was read 8 KB at a time "for the header", but a large EXIF/ICC block can push the JPEG size marker past any fixed prefix; such covers parsed as unreadable and were silently skipped instead of flagged. The whole file is read now (embedded art always was).
- **Fix: FLAC report header now includes the Metadata tier count**, matching the other integrity reports, so the per-tier counts always sum to Scanned (previously METADATA-tier files, mostly from the ffmpeg fallback path, were invisible in the report).
- **Hardening/cleanup:** `_parse_track_number` tolerates a malformed MP4 `trkn` atom containing `None` (previously an uncaught `TypeError` aborted the rest of that file's tag parse); the rule evaluator's comparison walk uses `zip(strict=True)`; an unreachable duplicated `return` in `run_replaygain_audit` was removed; `.gitignore` now covers the default report outputs so runs from the repo root stay out of `git status`.
- **Known limitation (deferred):** the curses prompt accepts ASCII-only typed input, so a non-ASCII path or output name cannot be typed into the TUI (the CLI and the prompt defaults are unaffected).

## genre_foldermap.py v1.2.0 (2026-06-01)

Companion-script fix and hardening (no package change):

- **Wrong-root guard.** `classify` collapsed any directory to its last two path components regardless of depth, and its docstring's promise to "flag" deeper directories was never implemented. Pointing the tool at the parent of an already-organized `Genre/Artist/Album` library (e.g. `/mnt/SharedData` instead of `/mnt/SharedData/Music`) therefore read every album one level too high, discarded the genre level, and planned to move and prune the entire tree. A directory deeper than `Genre/Artist/Album` is now flagged `TOO DEEP` and skipped. On a real ~1810-album library the parent-root dry-run went from 1820 planned moves plus 930 prunes to zero moves and a wall of `TOO DEEP` flags.
- **Placement gated by the library's existing genres.** The tool now learns the genre vocabulary from the folders that already hold a `Genre/Artist/Album` tree and only files a stray into one of those. A stray whose dominant tag genre isn't already in use is flagged `UNKNOWN GENRE` and skipped instead of spawning a new top-level folder from a typo or junk tag; `--allow-new-genre` lifts the gate. A flat library with no genre folders yet has an empty vocabulary, so the gate is off and the original `Artist/Album` → `Genre/Artist/Album` conversion is unchanged. The same gate is what makes a wrong-root run inert: nothing matches the (absent) vocabulary.
- **Organized albums are never silently re-filed.** An album already at `Genre/Artist/Album` whose folder genre disagrees with its tags is reported as a `NOTE` and left in place, rather than being moved across genre folders without warning.
- Tested by `tests/test_genre_foldermap.py` (depth classification, gating known/unknown/greenfield/`--allow-new-genre`, in-place skip and mismatch NOTE, the too-deep flag).

## replaygain.py v1.1.1 (2026-06-01)

Companion-script hardening (no package change):

- **Cross-platform robustness.** The rsgain subprocess output is now decoded with `errors="replace"`, so a stray undecodable byte (cp1252 on Windows, or a non-UTF-8 Linux locale) is logged rather than crashing the run. The "rsgain not found" message now points to the right installer per OS (`dnf`/package manager, `brew`, `winget`/scoop/choco) instead of Fedora only. The script was already portable Python (paths via `os.path`, no shell, UTF-8 logging); these close the two remaining rough edges. Documented under the script's new "Cross-platform" note in the README.

## v4.8.0 (2026-06-01)

- **Feature: ReplayGain audit (`--auditReplayGain`).** A new read-only mode reports per-album ReplayGain coverage, sorting every album into one of four buckets: MISSING (no track tagged), PARTIAL (some tracks tagged, some bare), NO ALBUM GAIN (every track has track gain but album gain is absent, so album-mode playback has nothing to apply), and OK (fully tagged, summarized and listed only with `--verbose`). It is format-aware: Opus stores gain as `R128_TRACK_GAIN`/`R128_ALBUM_GAIN` rather than the `replaygain_*_gain` text tags MP3 and FLAC use, and both are recognized, so an R128-tagged Opus album is not mis-flagged as untagged. Added to the CLI, the TUI (Metadata section), `spec.md`, and the test suite (format detection and bucket classification as pure-helper tests, plus an end-to-end mode test over a generated FLAC library). New `read_replaygain` reader in `tags.py` and a `DEFAULT_REPLAYGAIN_AUDIT_OUTPUT` constant.
- **Companion script: `replaygain.py` v1.1.0.** The writing counterpart to the audit. It wraps `rsgain` (libebur128, ReplayGain 2.0, the -18 LUFS / 89 dB reference foobar2000 uses) to scan and write track + album gain/peak tags album-by-album, the way foobar's "Scan selection as album" does. Album = one folder, rescanned as a whole so album gain is correct; `--skip-tagged` skips a fully-tagged album as a unit (never half-skips, which would corrupt album gain). `--dry-run` lists every album and its current coverage without invoking rsgain; a real run prints the worklist and asks for confirmation before writing (`--yes` to skip). After each album the written tags are read back and logged. `--target-lufs N` (v1.1.0) targets a louder or quieter result than the 89 dB / -18 LUFS standard (e.g. -14 ≈ 93 dB, the streaming-loudness range): it switches rsgain to custom mode and writes standard replaygain_* tags for every format including Opus (the R128 convention is fixed at -23 LUFS and cannot carry a custom target, so it is not used for that path); valid range -30 to -5, default stays the 89 dB standard. Lives in `scripts/` (outside the package's read-only contract, like the other companions) and imports `lattice` for the format-aware ReplayGain reader. Requires `rsgain` on PATH (not bundled). Tested by `tests/test_replaygain.py` (pure helpers; the rsgain call is mocked).
- **Cleanup**: `read_tags_concurrent` now delegates to a small general `map_concurrent` helper in `utils.py` (the ReplayGain audit reuses it); behavior is unchanged. `run_bitrate_audit` now returns `0` explicitly to match its `-> int` annotation and the sibling audits (the exit code was already 0).

## v4.7.1 (2026-05-30)

Hardening pass from a full code audit.

- **Security: smart-playlist rules no longer use `eval`.** `--playlist` rules were evaluated with `eval(rule, {"__builtins__": {}}, ...)`, which is not a sandbox: a rule like `genre.__class__.__mro__[-1].__subclasses__()` escapes it to arbitrary code. Rules are now evaluated by walking a restricted AST (comparisons, boolean and arithmetic operators, the exposed field names, literals); attribute access, calls, and subscripts are refused. Same rule syntax, no behavior change for valid rules.
- **Fix: `--stats` artist count on a genre-first tree.** The artist tally was derived from the top-level directory, so on a Genre/Artist/Album layout it counted genres, not artists. `run_stats` now takes the path `layout` (like the other modes) and reads the artist from the correct component. Album counts were already layout-independent.
- **Performance: read-heavy modes read tags concurrently.** New `read_tags_concurrent` helper (a small thread pool, since tag reads are I/O-bound) now backs duplicate detection, the tag and bitrate audits, and statistics, matching the integrity scanners' existing concurrency. Duplicate detection also drops a redundant whole-library tag cache, roughly halving its peak memory.
- **Fix: bitrate audit default output path** now uses `DEFAULT_BITRATE_AUDIT_OUTPUT` instead of string-rewriting the tag-audit constant.
- **`retag.py` (v1.1.0)** now writes genres to `.wma` (ASF `WM/Genre`); raw ADTS `.aac` stays unsupported (no tag container) and is documented as such.
- **`genre_foldermap.py` (v1.1.0)**: artist-level sidecar files (e.g. an `Artist/cover.jpg` beside album subfolders) now follow the artist to its dominant genre instead of being orphaned in an emptied folder; the dry-run now predicts directory pruning instead of reading the unchanged disk.
- **Cleanups**: `rerate.py` shares one read-only scan between preview and apply; lazier POPM rating fallback in `tags.py`; `cleaner.py`/`rerate.py` exception handlers collapsed to a single base class where one subsumed the other.

## v4.7.0 (2026-05-30)

- **Feature: configurable path-extraction layout.** The layout lattice-music uses to recover artist/album/genre from a file's path (when a tag is missing) is now settable. A new `layout` key in `~/.config/lattice/config.json` becomes the default for every scanning mode, and `--layout` overrides it per-run. A genre-first library can pin `"{genre}/{artist}/{album}"` once instead of passing the flag each time. The default remains `{artist}/{album}`, so existing setups are unaffected. New `config.DEFAULT_LAYOUT` constant and `config.get_layout()` helper back this.
- **Feature: genre falls back to the path.** The library scanner already recovered a missing artist/album from the directory layout; it now does the same for a missing genre (`t.genre or parsed.get("genre")`). With a `{genre}/...` layout, an untagged file is still placed in the right wing.
- **Companion script: `genre_foldermap.py` v1.0.0.** A new destructive tool in `scripts/` that restructures a flat `Artist/Album` library into `Genre/Artist/Album`, moving each album folder under its dominant genre (read through lattice-music's scanner). Dry-run by default, `--apply` to perform, an append-only manifest with `--revert`, and `--only-genre` for a staged rollout. Loose single tracks are wrapped in a `Singles/` folder, and artist-level cover art follows the artist to its genre rather than being orphaned. `mv`-only on one filesystem, so audio bytes and embedded ratings are never rewritten.

## v4.6.1 (2026-05-29)

- **Fixes**: Fixed a flaky `test_plain_when_not_a_tty` test that failed when the suite was run under a pseudoterminal.
- **Typing**: Added a missing `type: ignore` for `tqdm` in `utils.py` and correctly scoped the ignore for `OggOpus` in `tags.py` to ensure `mypy` runs completely clean.

## Companion script: `genre_tidy.py` v1.2.0 (2026-05-27)

*(Companion-script change; no package version bump.)*

---

- **Compilations are excluded from the genre authority.** An album whose album-artist is `Various Artists` (or `VA`/`Various`) has no single canonical genre, so enforcing one would wrongly flatten the disc. `build` now writes such artists as a flagged `# ... EXCLUDED (compilation)` comment instead of an enforceable row, and `apply` hard-skips them even if a stale row exists. The shipped `artist_genre_defaults.tsv` had its `Various Artists` row converted to the exclusion comment. (lattice-music's tag layer already prefers the album-artist, so this keys off album-artist.)

## Companion script: `rerate.py` (2026-05-27)

*(Companion-script addition; no package version bump.)*

---

- **New `scripts/rerate.py`: reconcile DeaDBeeF and foobar2000 MP3 ratings.** The two players store MP3 ratings in an ID3 POPM byte on different scales, so a rating set in DeaDBeeF reads one star too high in foobar: 2★ (byte 127) shows as 3★, 4★ (byte 254) shows as 5★. `rerate.py` rewrites those bytes to the foobar values that *both* players read identically (`127→64`, `254→196`), so DeaDBeeF ratings sift correctly in foobar without changing DeaDBeeF's own display (byte 196 reads 4★ in both, verified on real files). MP3/POPM-only; foobar's own bytes, MusicBee's `186/242`, unrated, and non-MP3 files are left alone, and Vorbis/Opus ratings already agree. `--dry-run`, append-only `rerate.log` recording every change (so a run is auditable and reversible), idempotent. Covered by `tests/test_rerate.py`.

## Companion script: `cleaner.py` v1.1.1 (2026-05-27)

*(Companion-script change; no package version bump. Refinements learned from a real-library run.)*

---

- **Accurate `--dry-run`.** A dry-run now tracks virtual removals, so its `RMDIR`/retain lines and summary counts match the real run instead of misreporting emptied folders as retained.
- **Survivor names are normalized.** The folder with the most files still wins as canonical, but its name is now rewritten to a normalized form afterward (so merging a unicode-hyphen `Drive‐By Truckers` no longer leaves the non-standard name as the survivor). Uses a new narrow `canonical_render` fold deliberately kept **filesystem-safe** for NTFS/exFAT targets shared with Windows: only broken hyphens (U+2010/11/12/15) and curly single-quotes/apostrophes go to ASCII. En/em dashes (correct in ranges like `85–92`), curly double quotes (straight `"` is forbidden on Windows), the ellipsis glyph (`...` would end a name in dots, which NTFS rejects), and prime marks are all preserved. Distinct from `normalize_name`, which stays aggressive for duplicate matching.
- **Rename safety.** A rename whose target would be illegal on Windows/NTFS/exFAT (forbidden `<>:"/\|?*`, or a trailing `.`/space) is skipped and logged; any `OSError` during a rename is caught and logged so a single bad name can never abort the run mid-way.
- **Higher-resolution cover wins.** On a cover-image collision (`.jpg`/`.png`), the higher-resolution file is kept rather than blindly keeping the canonical folder's copy (ties or unparseable images fall back to larger bytes). Dimensions are read with a stdlib JPEG/PNG parser ported from the package. Other non-audio collisions are unchanged.
- **`--normalize-names` (opt-in).** A third pass renames lone, non-duplicate folders whose names carry non-standard characters (e.g. `At the Drive‐In` → `At the Drive-In`) to the same normalized form. Off by default; preview with `--dry-run`. Covered by expanded `tests/test_cleaner.py`.

## Companion script: `genre_tidy.py` (2026-05-27)

*(Companion-script addition; no package version bump.)*

---

- **New `scripts/genre_tidy.py`: artist→genre authority + reconciler.** A two-phase companion for libraries whose genre tags have drifted. `build` scans (read-only, through lattice) and writes an editable tab-separated `genre_map.tsv`: one `Artist<TAB>Genre<TAB>Genre2<TAB>...` line per artist listing every genre that artist currently uses (most-common first), with `#` comments giving per-genre counts for multi-genre artists. Every listed genre is allowed, so a fresh map makes `apply` a no-op; you tidy by removing a stray genre from a line, and `apply` then calls `retag.py` to collapse that artist's albums tagged with the removed genre to the first (canonical) genre. Reorder to retarget, blank a line to skip an artist. `--dry-run` and append-only logging like the other companions, idempotent. The lattice package stays read-only; every write goes through `retag.py`. Keys on the normalized artist tag, not folder name. A real ~877-artist map ships at `artist_genre_defaults.tsv` as a worked example and maintained authority (re-run `build` to append new artists; hand-edit to accept new genres). Covered by `tests/test_genre_tidy.py` (21 cases); the same work backfilled `tests/test_retag.py` and `tests/test_cleaner.py`, so all three `scripts/` companions now have tests.

## v4.6.0 (2026-05-27)

---

### Multi-root scanning

- **`--root` is now repeatable.** `lattice --duplicates --root /mnt/A --root /mnt/B` walks both libraries in one pass; a path passed twice is de-duped. Every mode (trees, wings, stats, audits, art, playlists) aggregates across the roots.
- **Cross-library duplicate detection.** With more than one root, an album that lives in two libraries is grouped as a single exact duplicate. Each entry is prefixed by its root's basename (`Music/...` vs `Rin's Music/...`) so the two copies are distinguishable; single-root reports are unchanged.
- **Optional config array.** A `library_roots` array in `~/.config/lattice/config.json` supplies default roots when no `--root` is given. The first-run prompt still saves only the single `library_root`, so a throwaway `--root` is never persisted.

### Color output

- **Colorized status summaries.** Integrity summaries print a green all-clear, yellow suspect counts, and red corrupt counts. Color is gated on an interactive terminal: off inside the TUI, off when piped or redirected, off under `NO_COLOR`, so report files and pipes stay byte-clean.

### Companion scripts

- **`retag.py`: stale genres fully cleared (the deadbeef trap).** A genre can hide in more than the standard ID3 `TCON` frame: an APEv2 tag, the ID3v1 genre byte, and a bare custom `TXXX:GENRE` frame. The old `EasyID3` path left those overrides in place, so players like deadbeef kept showing the old value. The MP3 path now clears all of them and writes one clean `TCON` (v2.3, refreshed ID3v1), while deliberately preserving qualified `TXXX` frames (AcousticBrainz `AB:*`, `ALBUMGENRE`, MusicBrainz).
- **`cleaner.py`: apostrophe-fold fix.** `normalize_name` now strips apostrophes and collapses whitespace after the existing NFKC/dash/quote/case folding, so `Director's Cut` and `Directors Cut` consolidate. (Closes the 2026-05-25 found bug.)

## v4.5.0 (2026-05-26)

---

### TUI
- **Richer report pager.** The in-TUI viewer every report passes through now supports PageUp/PageDown, Home/End, and vim g/G on top of line scrolling, uses the full terminal height, and widens to the longest line (up to the terminal width) instead of a fixed 80 columns so long paths are no longer truncated. Folded in from the CalibreQuarry TUI.

### Internal
- **Python 3.14 modernization.** With the floor at 3.14, legacy `typing` generics were dropped for builtin generics and PEP 604 unions (`Dict`/`List`/`Tuple[...]` to `dict`/`list`/`tuple[...]`, `Optional[X]` to `X | None`) across the package and scripts; the redundant `from __future__ import annotations` was removed from `cleaner.py` and one `subprocess.run` simplified to `capture_output=True`.
- **Expanded test suite.** Added a committed fixture library (`tests/fixtures/`) and integration tests (`tests/test_modes.py`) that run the report modes end to end, plus decode-classifier tests. 78 stdlib `unittest` tests; run with `python -m unittest discover`.

### Repository
- **Companion scripts moved to `scripts/`.** Invoke `cleaner.py` and `retag.py` as `./scripts/cleaner.py` and `./scripts/retag.py`. They remain outside the `lattice` package (spec §5).
- **Documentation pass.** Recast em-dashes out of the prose, trimmed marketing language, fixed an unclosed README code fence, and condensed the per-mode sections.

## v4.4.2 (2026-05-26)

---

### Integrity scanning: fewer false alarms, severity tiers

Running every mode against a real 8,400-file library exposed the integrity
scanners crying wolf: they treated any line ffmpeg wrote to stderr as a failure,
producing roughly 187 flags where essentially no file had damaged audio. This
release reworks them.

- **Forced demuxer.** `--testMP3`, `--testOpus`, `--testWAV`, `--testWMA`, and the
  FLAC ffmpeg fallback now force the demuxer from the file extension. ffmpeg's
  format autodetection had been mis-probing valid MP3s with large ID3v2 tags as
  RIFF and reporting bogus failures; forcing `-f mp3` (and so on) eliminates the
  whole class. On the test library this turned dozens of false positives into
  zero.
- **Cover art ignored.** `-vn` drops non-audio streams, so a malformed embedded
  image is no longer decoded and counted as an audio fault.
- **Severity tiers.** Each file is classified CORRUPT / SUSPECT / METADATA / OK
  instead of pass/fail. CORRUPT means the decoder could not get through the file,
  or a FLAC lost sync before its declared sample count (true truncation). SUSPECT
  means it decoded to the end but the tool complained (these usually play).
  METADATA means only tag-parse warnings; the audio is fine. CORRUPT and SUSPECT
  are always listed; METADATA and OK are summarized and listed only with
  `--verbose`.
- **Exit code change.** Integrity modes now exit `1` only when a file is CORRUPT,
  not whenever the decoder printed anything. Scripts relying on the old "exit 1
  means any complaint" behavior should read the tier counts in the report
  instead.
- **FLAC reporting.** A failed verification now shows the preferred tool's
  message (libFLAC's "decoded N of M samples" rather than ffmpeg's terse "invalid
  sync code"), and the mode warns when `flac` is absent and the stricter ffmpeg
  fallback is used. The FLAC report is now always written, not only on failure.

### Tests
- Added `tests/test_integrity.py` covering the decode classifier across all four
  tiers using real ffmpeg and libFLAC stderr signatures.

## v4.4.1 (2026-05-26)

---

### Bug Fixes
- **Latent `Tuple` import in the art-quality audit:** `modes/artwork.py` annotated `_get_image_size` with `Tuple` without importing it. The code ran only because Python 3.14 defers annotation evaluation; `typing.get_type_hints`, a type checker, or any pre-3.14 interpreter would have raised `NameError`. The import is now present.

### Internal
- **Test suite added.** A stdlib `unittest` suite under `tests/` (no third-party dependencies) covers the pure helpers: rating and key normalization, duration clustering, JPEG/PNG header parsing, filename cleanup, and track-number parsing. Run it from the repo root with `python -m unittest discover`.
- **Library and stats refactor.** The three duplicated walk-and-aggregate loops and two identical tree-writers in `modes/library.py` were unified behind `_scan_album_dirs`, `_song_display_name`, and `_write_tree`; the file shrank by roughly a third with byte-identical output. The repeated rating-bucketing block in `modes/stats.py` was replaced with a single label helper. Output across every mode was verified unchanged against a captured baseline.
- **Formatting and lint.** The package was run through `ruff format`, unused imports were removed, and the misplaced mid-file imports in `tui.py` were moved to the top.

### Documentation
- `spec.md` now lists all shipped modes; `--testWAV`, `--testWMA`, `--auditArtQuality`, `--auditBitrate`, and `--playlist` were missing from its table. A stale `python lattice-music.py` example in the README was corrected, and the test suite is now documented.

## v4.4.0 (2026-05-14)

---

### New Features
- **Expanded `--duplicates`:** The duplicate detection mode was rewritten to emit a four-section report instead of a single album-level list. Section 1 (exact album duplicates) continues to flag the same artist/album pair across multiple directories, but now aggregates the most-common artist and album across every track in the folder rather than sampling the first audio file; per-location lines include the format breakdown, average bitrate, and total size to support keep/discard decisions. Section 2 (within-directory multi-format) reports folders that hold the same track in two or more formats (e.g., `01 - Track.flac` next to `01 - Track.mp3`), listed track-by-track so partial overlaps stay visible. Section 3 (similar-name candidates) flags within-artist album pairs whose names match at a `difflib` ratio of 0.85 or higher after stripping trailing parentheticals (`(Deluxe Edition)`, `(Remastered)`) and `feat.` clauses; this catches cases like `Domestica` vs `Domestica (Deluxe Edition)` that exact matching misses. Section 4 (track-level duplicates) reports the same artist + title appearing in two or more directories, partitioned into duration-clusters within a 2-second window so a studio cluster and a live cluster for the same song surface as separate rows instead of being lumped together (or one of them silently dropped).
- **Quote / dash normalization in matching:** Album and artist keys now apply NFKC normalization plus the same curly-quote and dash-variant fold table that `cleaner.py` uses (`'` → `'`, `‐` / `–` / `—` → `-`, etc.). `JAY‐Z` and `Jay-Z` collapse to the same key, so the two are reported together instead of slipping past as separate albums.

### Requirements
- **Minimum Python is now 3.14.** lattice-music was previously declared `>=3.9`. The bump is for runtime quality, not language sugar: end users get faster CLI cold starts (cumulative ~25% startup improvement since 3.11, with continued specializing-interpreter gains through 3.14), fine-grained tracebacks (PEP 657) so tag-read or subprocess failures point at the exact column rather than just the line, and faster general bytecode performance from the 3.11+ specializing interpreter. No 3.14-specific language features (template strings, free-threading, tail-call interpreter, etc.) were adopted because they either require a non-default build or have no use case in lattice-music's read-walk-report workload.

### Bug Fixes
- **`run_duplicates` first-file bias:** Reading album/artist tags from only the first audio file in a directory would mis-key entire folders when track 1 had bad tags, was a hidden track, or the album was a compilation with per-track artists. The new aggregation reads tags from every file and takes the mode across the directory.
- **Empty-album false positives:** The exact-duplicate section accepted directories with an empty album key (e.g., singles or weirdly tagged folders), grouping every album-less folder for an artist together as "duplicates." Both `norm_artist` and `norm_album` are now required to be non-empty for inclusion in the exact group.
- **Multi-format display title lowercasing:** When tracks lacked title tags, the within-folder multi-format section fell back to the normalized lookup key for display, which had been lowercased. Filename stems are now carried alongside the key and used as the display fallback, so case is preserved.
- **Track-level cluster dropping:** Track-level dupe detection previously returned only the single largest duration cluster per `(artist, title)` key, silently discarding a second valid cluster when the same title legitimately existed as both a studio version (in two albums) and a live version (in two more). Replaced with a partitioning helper that returns every cluster with 2+ entries spanning 2+ directories.
- **Removed dead code:** `_DirInfo.track_count` was computed but never read; removed. `_fmt_size` had an unreachable final `return` after a loop that always returned in its last iteration; loop refactored to only iterate over B/KB/MB with GB as the natural fallthrough. `argparse.BooleanOptionalAction` fallback in `cli.py` predates 3.9 and was dead even at the prior 3.9 floor; removed.

---

## Repo addition (2026-05-04)

---

**Companion Script: `cleaner.py`.** Added a standalone consolidator for fragmented album folders to the repository. It detects sibling directories whose names differ only in quote rendering (`'` vs `'`), dash/hyphen variant, case, or whitespace (the typical artifact of inconsistent metadata across import sources), and merges them via filesystem `mv` only. Audio collisions where sizes differ keep both copies (source renamed with a `.from-fragment` suffix), never overwriting user audio. Includes a `--dry-run` preview mode and per-file logging to `<directory>/cleanup.log`. Intentionally narrow scope: it does not rewrite tags or re-encode audio. Lives outside the `lattice` package alongside `retag.py`, preserving the package's read-only contract.

---

## v4.3.4 (2026-04-14)

---

### Bug Fixes
- **TUI Artwork Submenu:** Restored missing "Audit art quality" option to the interactive TUI menu.
- **TUI Integrity Submenu:** Added missing "Test WAV files" and "Test WMA files" options to the interactive TUI menu.
- **TUI Metadata Submenu:** Added missing "Audit bitrates" option to the interactive TUI menu.
- **WAV/WMA Integrity Modes:** Fixed a crash caused by missing `DEFAULT_WAV_OUTPUT` and `DEFAULT_WMA_OUTPUT` imports during `--testWAV` and `--testWMA` runs.
- **Genre Split Formatting:** Refined genre splitting logic in `write_all_wings` to correctly extract multiple genres without splitting literal paths or bracketed tags when saving library files.

---

## v4.3.3 (2026-04-14)

---

### New Features
- **Dual-Genre Wing Splitting:** The `--ai-wings` and `--all-wings` modes now intelligently split dual-tagged items (e.g., `Coke Rap/Midwest Rap`). Instead of creating a single, combined `.txt` file for the multi-genre string, lattice-music now separates the genres and correctly filters the album into *both* respective genre text files, ensuring accurate categorization across the library.

---

## v4.3.2 (2026-04-14)

---

### Bug Fixes
- **Retag Tool Overhaul:** Fixed an issue where `retag.py` was duplicating genre tags instead of replacing them. The tool now safely clears APEv2 tags from MP3s, correctly pops all existing standard/custom genre keys across formats, and explicitly forces ID3v1 synchronization to prevent ghost tags in older media players.
- **Null Byte Sanitization:** Fixed an issue in all library generation modes (`--library`, `--ai-library`, `--ai-wings`, `--all-wings`) where non-legible null bytes (`\x00`) from ID3v2.4 multi-value frames were being printed into the text output. Multiple values (e.g., dual genres) are now properly joined with a slash (`/`).
- **Wing File Names:** Improved file name generation for `--ai-wings` and `--all-wings` to preserve word boundaries when encountering slashes or special characters (e.g., `Coke_Rap_Midwest_Rap_AI.txt` instead of `Coke_RapMidwest_Rap_AI.txt`).

---

## v4.3.1 (2026-04-13)

---

### Bug Fixes
- **Album Overcounting Fix:** Resolved an issue where tracks in "Various Artists" or soundtrack directories were being counted as separate albums. All library generation modes (`--library`, `--ai-library`, `--all-wings`, `--ai-wings`) now correctly group tracks by their containing directory.
- **Improved Metadata Consolidation:** For each directory, the toolkit now automatically determines the most frequent artist, album title, and genre to use for headers, ensuring accurate representation even when track-level tags vary.

---

## v4.3.0 (2026-04-13)

---

### New Features
- **AI Wings:** Added `--ai-wings` to generate separate, token-efficient library files for each genre. These files hide individual songs and only include Artist, Album, Genre, and Directory Location, making them ideal for large-scale LLM processing or quick library overviews.
- **TUI Submenu Expansion:** The Library Tree & Exports submenu now includes both "Generate AI wings" and the previously omitted "Generate smart playlist" options.

---

## v4.2.2 (2026-04-13)

---

### Bug Fixes
- **TUI Persistence Fix:** Fixed an issue where the TUI would exit or "blink" back to the menu when running background tasks (like Stats). This was caused by the progress bar calling `curses.endwin()`, which terminated the curses session prematurely.
- **Improved Progress Bar:** The TUI progress bar now correctly updates within the existing curses session without corrupting the terminal state.

---

## v4.2.1 (2026-04-13)

---

### Bug Fixes
- **Stats Page Fix:** Fixed a `NameError` in the statistics module where `genre_ratings` was not properly initialized.
- **Missing Report:** Properly implemented the "Rating Distribution per Genre" report section in `--stats` which was previously omitted.
- **Version Synchronization:** Corrected version mismatches across the repository.

---

## v4.2.0 (2026-04-13)

---

### Major Overhaul: Configurable Layout & Smart Playlists

lattice-music now supports dynamic directory structures via the `--layout` flag, completely decoupling library generation from the strict `ARTIST/ALBUM` assumption. You can now generate `.m3u` playlists using rule-based filters.

### New Features & Improvements
- **Configurable Layout:** A new `--layout` argument specifies your directory structure (e.g. `{genre}/{artist}/{album}`). `write_music_library_tree`, `write_ai_library`, and `write_all_wings` now intelligently parse paths according to this structure if tags are missing. They no longer fail or produce garbage output on flat folders.
- **Smart Playlists:** Generate `.m3u` playlists based on dynamic evaluation rules using `--playlist` and `--rule` (e.g. `"rating >= 4 and genre == 'Jazz'"`).
- **WAV & WMA Support:** Extended the unified FFmpeg decode scanner to verify WAV (`--testWAV`) and WMA (`--testWMA`) files.
- **Art Quality Audit:** Added `--auditArtQuality` (with configurable `--min-art-res`) to parse and report extracted or embedded covers falling below a minimum resolution threshold (default: 500x500).
- **Bitrate Floor Audit:** Added `--auditBitrate` (with configurable `--min-bitrate`) to report audio files falling below a designated kbps floor (default: 192).
- **Rating Distribution per Genre:** The library statistics page (`--stats`) now cross-tabulates rating distributions (e.g., 5-star vs 1-star spread) independently per genre.

### Bug Fixes
- **TUI Close Button Fix:** Fixed an indexing error in the interactive menu where selecting "Quit" would accidentally trigger the "Change library root" prompt due to a missing settings group in the main `_MAIN_SECTIONS` list.

---

## v4.1.3 (2026-04-12)

---

### Bug Fixes
- Fixed an issue in the TUI main menu where selecting "Quit" (or pressing 'q') would unintentionally trigger the "Change library root" prompt due to a mismatched menu array index.

---

## v4.1.2 (2026-04-12)

---

### Bug Fixes & Improvements
- **Fully Immersive TUI:** Addressed an issue where background operations (such as cover art extraction or tree generation) would write their output directly to the terminal stdout and pause, which dropped the user out of the full-screen curses environment.
  - The TUI now features a global output capture wrapper (`_run_with_capture`) using an `io.StringIO` buffer.
  - Standard output and error output are automatically intercepted while a background task executes, allowing progress bars to draw undisturbed.
  - Upon task completion, any logged output (e.g., dry-run details, success messages, errors) is formatted and displayed within the `_tui_page` viewer, ensuring the user never leaves the curses application.

---

## v4.1.1 (2026-04-12)

---

### Bug Fixes
- Fixed a rendering bug where `_TUIPbar` did not erase the screen on its first draw, causing overlapping text from previous prompts in the curses interface.
- Fixed a crash (`ValueError: embedded null character`) when scrolling through the library statistics TUI page by sanitizing null bytes from the output report.

---

## v4.1.0 (2026-04-12)

---

### New Features & Improvements
- **First-Run Configuration:** Added a persistent configuration file stored at `~/.config/lattice/config.json`.
  - The CLI and TUI now save the root music library location upon first run, eliminating the need to repeatedly specify `--root` or manually enter the path in the interactive menu.
  - A new "Change library root" option has been added under the `SETTINGS` section in the TUI main menu.
  - If no `--root` is provided, the CLI gracefully falls back to the configured location (or prompts if unconfigured).
- **TUI Immersion Enhancements:**
  - Progress bars now render inside a stylized curses box when running from the TUI, preventing screen tearing and keeping the interface consistent.
  - The library statistics page now displays its full report in an integrated, scrollable curses pager (`_tui_page`), rather than dropping you back into standard terminal output.

---

## v4.0.2 (2026-04-12)

---

### Bug Fixes & Improvements
- **PyInstaller Multiprocessing Fix:** Fixed an issue where the standalone binary would crash (`unrecognized arguments: -B -S -I -c`) on Python 3.14 due to the `multiprocessing.resource_tracker` trying to spawn a new process using the executable as the Python interpreter. The executable now properly intercepts `-c` command strings from the tracker.
- **Positional Root Argument:** The CLI now supports providing the root directory as an optional positional argument. You can run commands like `lattice --library .` instead of explicitly using `--root .`.

---

## v4.0.0 (2026-04-11)

---

### Major Overhaul: Package Restructure & Standalone Binary

lattice-music has been completely refactored from a single ~2500-line monolithic script (`lattice-music.py`) into a proper, modern Python package architecture.

**Layer-Based Package Design.** The codebase is now housed in `src/lattice/` and split by logical functionality (`cli.py`, `tui.py`, `tags.py`, `utils.py`, `config.py`, and a `modes/` directory for individual feature operations). This dramatically improves maintainability while preserving the exact same functionality and CLI interface.

**Modern Build System (Hatch).** lattice-music now uses `pyproject.toml` managed by Hatch, replacing the need for manual `pip install mutagen tqdm` commands. You can now cleanly install lattice-music via `pipx install .` and have the `lattice` command available globally in your terminal.

**Standalone Native Executable.** We have integrated **PyInstaller** support to compile lattice-music into a self-contained standalone binary. This means end-users no longer need to install Python or external packages (like `mutagen`) on their machines. The compiled binary (`lattice`) can be dropped into any directory in your PATH.

---

## v3.1.0 (2026-04-09)

---

### Enhancements

**Absolute Paths for Genre Wings.** The `--all-wings` mode now accepts a `--paths` flag. When enabled, the absolute directory path is appended to the album header in the generated text files (e.g., `ALBUM: Jane Doe [/path/to/Music/Converge/Jane Doe]`). 
- This bridges the gap between visualization and execution. It eliminates the need to write brittle shell scripts that guess file locations by scraping artist and album strings. You can now pipe the generated wing files directly into command-line tagging utilities.
- The interactive TUI's Library submenu has been updated to prompt for path inclusion (`Include paths? (y/N)`) when generating genre wings.
**Companion Script: `retag.py`.** Added a standalone universal genre tagger to the repository. It abstracts away container-specific tagging differences (ID3, Vorbis, Apple atoms) and is designed to cleanly consume the absolute paths generated by the `--all-wings --paths` flag. This allows for safe, bulk-overwriting of genres at the album-directory level.

## v3.0.1 (2026-04-08)

---

### Bug Fixes

**Album Artist Prioritization.** Fixed an issue where albums were being split up due to featured artists on individual tracks. The tag extractor now consistently prioritizes "Album Artist" over "Artist" across all supported formats:
- MP3: `TPE2` > `TPE1`
- FLAC/Ogg/Opus: `albumartist` > `artist`
- MP4/M4A: `aART` > `\xa9ART`
- ASF: `wm/albumartist` > `author`

---

## v3.0.0 (2026-04-06)

---

### Consistent Full-Screen TUI

The entire interactive experience now stays in curses. Previously, selecting a
menu item dropped to raw `input()` calls for parameter prompts (root directory,
output file, worker count, etc.) and the post-operation pause, breaking the
visual flow. All prompts and the pause screen now render inside the same styled
Unicode boxes as the menus.

**Curses prompts.** `_tui_prompt_str` draws a centered box with a yellow header
label and a cursor-visible input field. Typing, backspace, Enter to confirm,
Esc to accept the default, all within the curses session. Since `_prompt_path`
and `_prompt_int` call `_prompt_str` internally, every parameter prompt in the
interactive menu gets the TUI treatment automatically.

**Curses pause.** `_tui_pause` replaces the raw `input("Press Enter…")` with a
styled box. Accepts Enter, q, or Esc to dismiss.

**Fallback preserved.** If curses is unavailable or stdin is not a TTY,
prompts and pause fall back to plain `input()`, same as before.

All CLI flags (`--library`, `--ai-library`, `--all-wings`, etc.) are unchanged.

---

## v2.4.0 (2026-04-06)

---

### TUI Overhaul: Arrow-Key Navigation

The interactive menu is now a full-screen curses TUI with arrow-key navigation,
color-coded sections, and a highlighted selection cursor (`►`). No more typing
numbers; just `↑`/`↓` to move, `Enter` to select, `q` or `Esc` to quit.

The menu is drawn as a centered Unicode box with labeled section groups:
**Library** (yellow), **Integrity**, **Artwork**, and **Metadata**, separated
by ruled dividers. The selected item is highlighted in bold cyan reverse video.
A hint bar at the bottom shows available controls.

**Library submenu.** AI-readable library export and genre wings (all-wings)
are now nested under a "Library tree & exports" submenu (marked with `→`)
alongside the standard library tree builder. Selecting it opens a second
curses menu; `Esc` returns to the main menu. This trims the top-level menu
from 11 flat items to 10 navigable entries and groups the three library-output
modes where they logically belong.

**Curses colors:**
- Cyan box frame
- Bold yellow section headers
- Bold cyan-on-black highlight for selected item
- Dim hint bar

**Fallback path.** If `curses` is unavailable (e.g. `windows-curses` not
installed) or stdin is not a TTY, the menu falls back to a static boxed
text display with numbered options and typed input, the same layout, just without
arrow-key navigation.

**Post-operation pause.** Every mode now waits for Enter before redrawing
the menu, so results aren't immediately scrolled off screen.

**Indented prompts.** All interactive prompts are visually aligned with the
menu box for a tighter feel.

All CLI flags (`--library`, `--ai-library`, `--all-wings`, etc.) are unchanged.

---

## v2.3.0 (2026-04-05)

---

### New Feature: Genre Wings

**`--all-wings`** scans genre tags across the entire library, groups albums by
genre, and writes a separate library tree file for each genre into an output
directory, analogous to virtual library wings in Calibre's getBooks.

```bash
lattice --all-wings --root ~/Music --output wings/
```

Produces files like `Alternative_Rock_Library.txt`, `East_Coast_Rap_Library.txt`,
etc. Albums with no genre tag land in `Uncategorized_Library.txt`. Each file
uses the same tree format as `--library`. Pass `--genres` to include the genre
label in album headers. Available from both CLI and interactive menu (option 11).

### AI Library: Removed Album Artist Fallback

The `--ai-library` export no longer overrides the directory-based artist name
with tag data. Previously, the artist field fell back through TPE1 → TPE2
(ALBUMARTIST) from tags, which added noise without value; the AI export
doesn't distinguish album artist from track artist, and the directory name is
the canonical artist in a well-organized library. This keeps the output cleaner
and more predictable.

---

## v2.2.0 (2026-04-05)

---

### New Feature: AI-Readable Library Export

**`--ai-library`** generates a flat, token-efficient summary of the music
library for use in LLM recommendation prompts. One line per album in
pipe-delimited format:

```
Artist | Album | Genre | Rating | Tracks
--------------------------------------------------
Converge | Jane Doe | Metalcore | 4.8 | 12
```

- **Rating** is the average of all rated tracks in the album, rounded to one
  decimal. Blank if no tracks are rated.
- **Tracks** is the number of audio files surviving in the album directory:
  the post-cull headcount. An AI reading `5.0 | 1` vs `4.6 | 12` gets the
  density signal without extra framing.
- Genre is sampled from the first track with a genre tag.
- Output defaults to `library_ai.txt`. Available from both CLI and interactive
  menu (option 10).

### Performance

**`get_all_tags` reduced to a single `MutagenFile` open per file.** The v2.1.0
unified reader still opened each file twice, once via the EasyID3 abstraction
pass, once via the full format-specific path (because rating, duration, and
bitrate aren't available through the easy interface). The easy pass is now
eliminated entirely; all tag extraction runs against the single full object.
The MP3 branch also had a separate `ID3(file_path)` call on top of the
`MutagenFile` open; that call is now removed, and tags are read from `audio.tags` directly.

On a 6,300-track library, this eliminates ~12,600 redundant file opens per
full-library mode.

**`TagBundle` extended with `duration_s` and `bitrate_kbps`.** These fields are
extracted from `audio.info` during the same single open. `run_stats` previously
opened every file a second time just to read duration and bitrate; that
redundant open is gone.

**First-song double-read eliminated in `--library --genres`.** Genre was read
from the first song before the per-track loop, then the loop re-read the same
file. The album header is now deferred until the first track's tags are available
inside the loop.

**`count_audio_files` was called twice in `--ai-library`.** Once for the console
message, once for the progress bar. Now called once, result reused.

**`_has_embedded_art` duplicated `_extract_best_art`'s directory scan logic.**
Collapsed to a one-liner: `return _extract_best_art(directory) is not None`.

**Low-quality bitrate count in `--stats`** used a list comprehension just to
call `len()`. Replaced with a generator sum.

### Bug Fixes

**`--root ~/Music` didn't work from the CLI.** `main()` was missing
`os.path.expanduser()`; tilde expansion only worked in the interactive menu.

**`--library --output subdir/file.txt` crashed.** `write_music_library_tree`
opened the output file directly without creating parent directories, unlike
every other mode. Added `os.makedirs`.

**`_find_files_by_ext_path` matched false extensions.** Used
`filename.endswith('.flac')` which would match a hypothetical file named
`notflac`. Replaced with `os.path.splitext` for exact extension matching.

**Rating bucketing in `--stats` used Python's `round()` (banker's rounding).**
A 4.5 rating rounded to 4, but so did 3.5. Replaced with `int()` (truncate)
for consistent behavior matching the star display logic in `format_rating`.

### Structural Improvements

**Unified MP3/Opus decode scanner.** `_scan_one_mp3`, `_scan_one_opus`,
`run_mp3_mode`, `run_opus_mode`, and `_format_mp3_meta` collapsed into three
shared functions: `_scan_one_file`, `_run_decode_scan`, and `_format_row_meta`.
Format-specific behavior is parameterized via `ext`, `enrich`, and
`ffmpeg_required` flags. Adding a new format is now a three-line wrapper.

**`_FallbackProgress` class replaces all `if pbar:` conditionals.** `_make_pbar`
now always returns an object with `.update()` and `.close()`, whether tqdm is
installed or not. Eliminated 6 conditional blocks and 4 dead counter variables
(`current_file`, `checked`, `scanned_count`, `current`) that existed only to
feed the manual fallback.

**`is_audio()` helper.** Replaced 6 inline
`os.path.splitext(f)[1].lower() in AUDIO_EXTENSIONS` patterns. Two callsites
where `ext` was already extracted for other purposes were left as-is.

**`_looks_numeric()` helper.** Replaced 4 inline
`str(val).replace('.', '').isdigit()` patterns in rating extraction code.

**`_prompt_path()` helper.** Consolidated 8 identical
`os.path.abspath(os.path.expanduser(_prompt_str(...)))` patterns in the
interactive menu.

**`test_flac` simplified.** Two mirrored if/elif branches (one per tool
preference) collapsed into a priority-ordered tool list with a single loop.

### Dead Code Removed

**Four standalone tag functions removed (~190 lines).** `get_title_artist_track`,
`get_album`, `get_genre`, `get_rating`, all superseded by `get_all_tags` in
v2.1.0 but left in the codebase. No internal callers remained.

**`_get_cover_file_path`**: defined but never called by any mode.

**`_scan_one_mp3`, `_scan_one_opus`, `_format_mp3_meta`**: replaced by the
unified scanner.

**`ID3` and `ID3NoHeaderError` imports**: no longer needed after the MP3
branch was rewritten to use `audio.tags` from `MutagenFile`.

**Stale `(NEW)` markers** removed from section headers.

---

## v2.1.0 (2026-04-04)

---

### Bug Fixes

**Opus mode wrote to `mp3_scan_results.csv`.** Copy-paste bug in `_write_header`
hardcoded `DEFAULT_MP3_OUTPUT` as the fallback filename for all modes. Opus scans
silently wrote results to the wrong file.

**`_extract_art_from_mp3` called `MUTAGEN_MP3` without checking
`HAVE_MUTAGEN_MP3`.** If `mutagen` installed but `mutagen.mp3` failed to import,
art extraction threw `NameError`.

**`verbose` flag mutated `only_errors` and `quiet` inside the MP3 scan loop.**
Dead writes on every iteration after the first. Moved above the loop. Opus mode
now has the same `verbose` behavior for consistency.

**Terminal corruption after subprocess modes.** Running FLAC/MP3/Opus integrity
checks from the interactive menu left the terminal with `icrnl` disabled; Enter
sent `^M` instead of newline, and input froze. Caused by `run_proc` using raw
bytes mode while `flac -t` wrote binary diagnostic data to stderr, colliding
with tqdm's cursor manipulation. Fixed with `_reset_terminal()` (`stty sane`)
called at the top of every menu loop, and in a `finally` block on CLI exit.

### Structural Improvements

**Unified tag reader: `get_all_tags()` → `TagBundle`.** The old code opened each
file up to 4× via independent `MutagenFile()` calls (`get_title_artist_track`,
`get_album`, `get_genre`, `get_rating`). Consolidated into a single function
returning a `TagBundle` named tuple. ~19,000 fewer file opens per `--library`
run on a 6,300-track library. Callers updated: `write_music_library_tree`,
`run_tag_audit`, `run_duplicates`. Original standalone functions preserved for
any external imports but no longer called internally.

**Duplicate file-finder eliminated.** `find_files_by_ext` (string generator) and
`_find_files_by_ext_path` (Path list) did the same job. Removed the former,
pointed FLAC mode at the latter.

**Vestigial `paths` list removed from `run_mp3_mode`.** Leftover from a
multi-root design that never shipped. Replaced with a plain `root_path`.

**`Counter` import consolidated.** Was at module level via `defaultdict` but then
re-imported locally in `run_tag_audit`. One import, one location.

**Removed unused `Iterable` from typing imports.**

### Output Format: CSV → Formatted Text

All output modes now write `.txt` reports instead of `.csv`. None of these
outputs were destined for spreadsheets; they're checklists and diagnostics
read by one person, and the format now respects that.

- **FLAC/MP3/Opus integrity**: Header with scan totals, results grouped by
  severity (ERRORS → WARNINGS → OK). Relative paths, tool/error details,
  compact metadata where relevant (bitrate, sample rate, duration).
- **Missing art**: Two sections: no art at all, embedded only. Relative paths
  with file counts.
- **Duplicates**: Grouped by artist/album pair with directories nested
  underneath showing format sets.
- **Tag audit**: Grouped by directory, each file showing format and missing
  fields. Header includes field-level breakdown counts.

### Dead Code Removed

**`_write_header` / `_close_writer` / `_rotated_path`**: Entire CSV writer
infrastructure gone. These managed `csv.DictWriter` lifecycle via a
monkey-patched `_file_handle` attribute. With text output, file writes are
straightforward `open()` calls.

**`import csv`**: No longer imported.

---

## v2.0.0 (2026-03-15)

---

lattice-music.py is now a single unified toolkit. The standalone
`extract_opus_art.py` and `extract_mp3_art.py` scripts are retired; their
functionality lives in the main script as `--extractArt`, with improvements.

### New Modes

- **`--testOpus`**: Opus file integrity checking via FFmpeg decode (same
  pattern as `--testMP3`).
- **`--extractArt`**: Extract embedded cover art to `cover.jpg` with format
  priority ranking (FLAC > Opus > M4A > MP3) and `--dry-run` support.
- **`--missingArt`**: Report directories with no cover art (distinguishes
  "no art at all" from "embedded only").
- **`--duplicates`**: Detect same artist+album appearing across multiple
  directories or formats.
- **`--auditTags`**: Report files missing title, artist, track number, or
  genre with a summary breakdown.

### Bug Fixes

- **Fixed cover.jpg collision**: Cover detection is now case-insensitive.
  Running art extraction in a folder with both Opus and MP3 files no longer
  produces both `cover.jpg` and `Cover.jpg`.

### Improvements

- Art extraction prefers front cover (type 3) over generic embedded images.
- Art extraction supports four formats: FLAC, Opus/OGG, M4A, MP3.
- Interactive menu updated with all eight modes.
- All existing CLI invocations remain backward-compatible.

### Removed

- `extract_opus_art.py` (folded into `--extractArt`).
- `extract_mp3_art.py` (folded into `--extractArt`).
les no longer
  produces both `cover.jpg` and `Cover.jpg`.

### Improvements

- Art extraction prefers front cover (type 3) over generic embedded images.
- Art extraction supports four formats: FLAC, Opus/OGG, M4A, MP3.
- Interactive menu updated with all eight modes.
- All existing CLI invocations remain backward-compatible.

### Removed

- `extract_opus_art.py` (folded into `--extractArt`).
- `extract_mp3_art.py` (folded into `--extractArt`).
rt`).
