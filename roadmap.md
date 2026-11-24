# lattice-music Roadmap

> Collapsed 2026-11-24, first application of the every-pass cleanup rule:
> shipped work lives in this ledger, in patchnotes.md, and in the git tags;
> the full per-phase prose this replaced is one commit back in git history.
> The "Open / parked" section is the only living part of this file.

## Shipped

| Batch | Shipped | What it gave the tool |
|---|---|---|
| Phases 1-4: foundation, integrity, reporting, interface | v2-v4 era | TagBundle reader over 8 formats, the integrity scanners and audits, trees/wings/stats/playlists, the curses TUI, persistent config, the PyInstaller binary |
| Audit 2026-07-01 + cquarry carry-backs | v4.8.x | TUI/UX and companion-script audit batch |
| Bug sweeps 2026-08-07 and 08-09 | v4.10.2, v4.11.0 | ranked rating selection (hash-seed determinism fix), the threading-is-a-regression measurement |
| Phase 15: the vir-tui fold | v4.17.0-v5.2.0 | shared TUI primitives (`vir-tui`) replace the hand-mirrored plumbing |
| Phase 16: rating strip | v6.4.0 | `--stripRatings` through tags.py's shared rating-source helpers; `--ingest` gains the staging-scoped strip stage |
| Phase 17: rating protection | v6.4.1 | `rating_protected_roots` guard after the 2026-10-10 hand-ratings incident |
| Research 2026-09-12: the companion fold (REPORT-12-Sept.md) | v5.0.0, v6.0.0 | cleaner/apestrip folded (5.0.0); retag, genre_tidy, genre_foldermap, replaygain folded and `lattice.vfs` unified (6.0.0) |
| Audit season 2026-09-12..14 (six-lens, final audit, blitz) | v5.2.x-v5.5.0 | TUI kwargs-parity suite, publish hardening, and the B-queue modes: `--verifyReplayGain`, `--checkPlaylists`, `--auditAlbums`, `--snapshot`/`--diff`, `--auditArtMismatch`, `--auditJunkFrames` |
| Lyrics | v5.7.0 | `--lyrics`, the LRCLIB sidecar writer |
| Expansion program 2026-10-05: folds A1-A4 + capabilities B1-B4 | v6.0.0 | `--genreMap` + the spec §5 folder-move amendment, `--where`, `--json`/exit parity, `--health`, `--ingest` packaged |
| Machine-surface passes | v6.3.0-v6.4.3 | truthful exit taxonomy, `--output -`, `help --json`, contract violations closed, analysis-correctness fixes |
| Report discipline | v6.5.0 | stdout-by-default reports, `resolve_output`, the anti-clobber default names retired |
| Database export | v6.6.0 | `--exportDb`, the library as a SQLite snapshot |

## Open / parked

- Parked: the spindle-threading measurement (tag-read worker scaling on
  network or spinning storage; serial is measured-correct on local disk).
- Parked: the `--lyrics` USLT embedded-tag companion (sidecars won: they
  travel with cloned libraries and never touch the audio files).
- Parked, conditional: a health-score ReplayGain-correctness bucket, only if
  `--verifyReplayGain`'s first real-library run had shown materially wrong
  gains (recorded 2026-09-14; the trigger never fired).
- Nothing else is open. New work starts as a dated section here, ships, and
  collapses back into the ledger the same pass.
