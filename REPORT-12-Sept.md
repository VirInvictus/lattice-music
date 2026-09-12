# REPORT-12-Sept: the companion fold + the music-toolset landscape (lattice-music)

Research date: 2026-09-10/11. Method: a read-only research pass reading
scripts/apestrip.py (838 lines, v1.2.0) and scripts/cleaner.py (1,208
lines, v1.5.0) line-by-line including their test suites
(tests/test_apestrip.py: 12 classes, ~50 tests; tests/test_cleaner.py:
~30 classes, ~85 tests), the main CLI's mode registry and contracts
(src/lattice/, spec.md §5-6, CLAUDE.md), and a landscape comparison
against the music-management state of the art (beets and its plugin
ecosystem, MusicBrainz Picard, foobar2000-style library views).

## Verdict on the fold

The promotion is **feasible, mechanical, and worth its L effort**. Both
scripts are self-contained brains (pure rules + virtual-filesystem
safety rails) already presenting through vir-tui, which the package
depends on. The fold follows the house mode-flag style (`lattice` has
no subcommands), promotes the pure rules into `src/lattice/norm.py`,
lands the two passes as package modes behind opt-in `--apply`, and
leaves the scripts as argparse-preserving shims so muscle memory and
any cron/aliases survive intact.

## A. What the two scripts actually are

- **scripts/apestrip.py**: the APEv2 brain (`classify_ape_field`
  :140, `plan_file` :521, presence-level redundancy via
  `id3_has_equivalent` :202; genre never migrated and rating never
  written per :29-35) plus byte-surgery repair (`_parse_raw_ape` :348,
  `repair_file` :419 with temp+fsync+decode-verify+`os.replace`+
  `copymode` :477-498), driven by a two-phase main (:714-828: dry
  plan -> worklist -> confirm with non-TTY auto-skip -> apply -> exit 1
  on errors).
- **scripts/cleaner.py**: the pure rules engine (`normalize_name`
  :153, `canonical_render` :163, `tag_fold` :211 with CP1252-mojibake
  repair, `tag_dedupe` :219, `canon_track_artist` :261 with the
  feat-preserving restamp), four passes (artist merge -> album merge ->
  bottom-up `normalize_tree` renames -> library-wide `normalize_tags`
  with the global artist authority map), safety rails (64KiB
  head/tail sample :513, `.from-fragment` keeps :560-576,
  higher-res-cover-wins :577-608, NTFS `is_legal_name` :149), and the
  dry-run virtual filesystem (`Run.removed/created`, `_exists/_move/
  _rename`, `_survives`) that makes `--dry-run` predict apply exactly
  — pinned by dedicated test classes. The "fancy" presentation is
  already vir_tui.core (print_header/print_summary/color/tqdm).

## B. The fold design (house mode-flag style, no subcommands)

1. New library modules:
   - `src/lattice/norm.py`: the pure rules engine (normalize_name,
     canonical_render, tag_fold, tag_dedupe, is_legal_name, the fold
     tables, `_FEAT_RE`, canon_track_artist). Zero I/O; reusable by
     genre tooling and future modes.
   - `src/lattice/modes/clean.py`: the `Run` virtual filesystem + four
     passes as `run_clean(root, *, dry_run, normalize_names,
     normalize_filenames, normalize_tags, layout, log_path, quiet)`.
   - `src/lattice/modes/apestrip.py`: classify/plan/apply/repair as
     `run_apestrip(root, *, dry_run, keep_metadata, repair_malformed,
     log_path, quiet)`. A new mode-group file (like artwork.py) is
     justified.
2. CLI: `lattice --clean [DIR] [--normalize-names|
   --normalize-filenames|--normalize-tags|--all] [--layout]` and
   `lattice --apestrip [DIR] [--keep-metadata] [--repair-malformed]`
   (argparse entries cli.py:58ff, dispatch cli.py:254ff, plus the
   mandatory TUI entries per CLAUDE.md:57).
3. Presentation ports verbatim: both scripts' output layers are
   already vir_tui.core calls; `Run.log`'s colorized PASS/SKIP/TAG
   lines move as-is.
4. Compat shims: scripts/cleaner.py and scripts/apestrip.py become
   argparse-preserving wrappers delegating to the package (the
   slipcover.py pattern, slipcover.py:39-63). Flags, log defaults
   (`<dir>/cleanup.log`, `<dir>/apestrip.log`), append-only log
   format, idempotency, and non-TTY auto-confirm all preserved.
   Tests retarget the package modules (deleting the sys.path hack at
   test_apestrip.py:17).

## C. The one deliberate break: inside `lattice`, write is opt-in

Today `cleaner.py DIR` applies immediately and `--dry-run` opts out
(cleaner.py:1039-1043). Folded in, `run_clean` defaults to dry-run and
requires `--apply` (or the TUI's `ask_yn` confirm): apply-by-default
behind a menu entry is the Enter-through-prompt risk the roadmap's T2
history documents. The shims keep the old apply-by-default behavior so
existing aliases and cron do not change. This is the only
incompatibility.

## D. Contract handling (the read-only boundary)

The boundary is stated three ways: spec.md §5 ("Not a tagger"),
README.md:16, CLAUDE.md:69 ("Don't fold any of them into the `lattice`
package without asking") and :89. Folding is exactly that asking:
amend spec.md §5 to "reads tags; writes metadata only via the explicit
`--clean`/`--apestrip` write modes (opt-in, logged, dry-run by
default)"; update README.md:16 and CLAUDE.md:69 to name the two
exceptions instead of the blanket ban; patchnotes entry + version bump
per CLAUDE.md:92. The spirit survives: one gated, logged,
dry-run-defaulted write path, and the remaining seven companions stay
put.

Risks: (a) contract-erosion precedent — hold the line at "fold only
with an explicit contract amendment"; (b) the PyInstaller binary
(hatch run build-bin) would ship mutation — acceptable, but the TUI
menu entry must confirm before applying; (c) shims must re-export, not
copy, or the two implementations drift; (d) the tag pass writes
ID3v2.3+v1 (cleaner.py:835) — same semantics as retag.py, but now a
package behavior worth a spec sentence.

## E. Fold effort

**L** (the mechanical moves are M; the contract/UX work makes it L):
rules -> norm.py; passes+Run -> modes/clean.py; ape brain ->
modes/apestrip.py; 2 CLI flags + 2 dispatch branches; 2 TUI entries +
confirms; 2 shims; ~135 tests migrated; spec/README/CLAUDE/roadmap/
patchnotes/config.py VERSION. No new deps (vir-tui and mutagen are
already package deps).

## F. The landscape gaps (ranked; beets/Picard/foobar comparison)

Companions already cover: replaygain writing, APE stripping, genre
authority, rating reconcile, art embed+fetch, FLAC->Opus,
restructuring. The real gaps:

1. **Content-hash audio duplicate detection** (`--duplicates` today is
   tag/name/size-based; audit.py:334/:303; no hashlib anywhere in
   modes/). sha256 exact + head/tail sampling catches retagged/renamed
   dupes. Audit mode; M; high value.
2. **Library health score**: the six audits exist separately; one mode
   aggregating tag completeness, RG coverage, art, bitrate, and decode
   errors into a per-album/per-root score. Audit mode; S; high
   curator value.
3. **Unimported/stray-file audit** (beets `unimported` analog): audio
   at wrong depth, non-audio junk, loose tracks outside albums, hidden
   dirs. Audit mode; S.
4. **m3u verification / foobar2000 interop**: dead paths, non-#EXTM3U
   compliance, relative-vs-absolute. Audit mode (`--checkPlaylists`);
   S.
5. **Album-consistency audit**: mixed codec within an album, missing
   year/label (TPUB), tracknumber gaps. Audit mode; S.
6. **MusicBrainz/Picard hand-off**: TXXX `MusicBrainz ...` coverage
   audit (`--auditMbids`) + a Picard-friendly needs-tagging TSV from
   `--auditTags`. Audit mode; S.
7. **ReplayGain verification**: tags exist (audit.py:654) but nothing
   confirms stored gain matches a fresh measurement; rsgain/ffmpeg
   ebur128 read-only analysis. Audit mode; M.
8. **Junk-frame scrub**: CP1252-era junk (iTunNORM COMM blobs, private
   TXXX) reported in audit (`--auditJunkFrames`, S); the strip belongs
   in a new companion or a retag.py extension. M.
9. **Art mismatch audit**: embedded art vs folder art divergence, and
   per-track art. Audit mode; S.
10. **Library snapshot + diff**: a tag+path fingerprint pair for change
    detection between curation runs (no DB by design). Audit mode; S.
11. **Lyrics** (USLT embedder companion, slipcover's pattern): L,
    lower priority than 1-7.

Deliberately skipped: beets mbsync/import (needs a DB — anti-filesystem-
as-truth), convert (flac2opus.py covers), edit (retag/genre_tidy
cover), fetchart (slipcover --fetch covers).

## G. Routing

The fold (A-E) and gaps 1-7, 9-10 are boxed in roadmap.md ("Research
2026-09-12"). Gap 8's strip and gap 11's lyrics are new-companion
candidates for later lanes. Nothing here touches the read-only
contract without the spec amendment the fold itself carries.
