# lattice-music Application Specification

**Version:** 5.4.0  
**Language:** Python 3.14+  
**Dependencies:** `mutagen`, `tqdm`, `vir-tui`  
**License:** MIT

---

## 1. Mission Statement

lattice-music is a CLI toolkit for music collectors who manage their own libraries
outside of any player's database. It reads tags directly via mutagen, and is
player-agnostic by design. Every operation works from the filesystem and
embedded metadata, not from a proprietary database or cloud service.

Design philosophy: **one toolkit, every library maintenance task.** The standard collector
layout (`~/Music/ARTIST/ALBUM/01 - Track.flac`) is the default assumption. It is
configurable: the `layout` config key (or `--layout`) sets the pattern lattice-music
uses to recover artist/album/genre from a path, so a genre-first tree
(`{genre}/{artist}/{album}`) is fully supported.

---

## 2. Architecture

### 2.1 Layer-Based Package Design

The codebase is structured as a proper Python package (`src/lattice/`) managed by `pyproject.toml` (via Hatch). It is split by logical layers:
- `cli.py`: Command routing and argparse definitions.
- `tui.py`: Full-screen interactive curses interface.
- `tags.py`: Extraction logic (`TagBundle`) over mutagen.
- `norm.py`: Pure name and tag normalization rules (quote/dash folding, CP1252 mojibake repair, tag deduplication); zero I/O, shared by the write modes.
- `utils.py`: Shared utilities (progress bars, terminal formatting).
- `config.py`: Default constants, the default path-extraction `layout`, the tag-read worker count (`tag_workers`), and persistent library root configuration (`~/.config/lattice/config.json`).
- `modes/`: The individual operation features (e.g., `library.py`, `integrity.py`, `artwork.py`, `clean.py`, `apestrip.py`).

### 2.2 Tag Reading

All tag extraction runs through `get_all_tags()`, which returns a `TagBundle`
named tuple from a single `MutagenFile()` open. Format-specific tag field
mapping (ID3 for MP3, VorbisComment for FLAC/Opus/OGG, MP4 atoms for M4A)
is handled internally. Ratings are read from POPM, TXXX, or Vorbis comment
fields, compatible with foobar2000's `foo_quicktag` and most other taggers.
The canonical POPM bytes (1/64/128/196/255) map to whole stars regardless of
the frame's rater email; `FMPS_Rating`-style tags are read on their 0.0-1.0
scale; other values fall back to a magnitude heuristic (0-5, 0-10, 0-100,
0-255).
A file may carry several rating-ish keys at once (`rating` alongside
`album rating`, `love rating`, or a tagger's private key). Selection is
ranked, never first-match: the standard names win, an album-level rating ranks
last because it is not the track's rating, and ties break alphabetically. This
is a correctness requirement, not a preference: mutagen builds a Vorbis
comment's key list from a set, so first-match selection made the reported
rating depend on the interpreter's hash seed and vary between runs.
When a file is missing an artist, album, or genre tag, that field is recovered
from the file's path according to the configured `layout`.

Tag reads are serial by default. Threading them is a measured regression
(mutagen decodes in Python, so the GIL is held for everything but the file
open), but it can pay off on storage where an open genuinely blocks, so the
worker count is tunable: the `tag_workers` config key or the
`LATTICE_TAG_WORKERS` environment variable, as an integer or `auto`. This is
independent of `--workers`, which sizes the subprocess pool in the integrity
modes and is unaffected.

### 2.3 Supported Formats

`.mp3` · `.flac` · `.ogg` · `.opus` · `.m4a` · `.wav` · `.wma` · `.aac`

### 2.4 Standalone Binary

lattice-music can be compiled into a standalone native executable using **PyInstaller**.
This encapsulates the Python interpreter, dependencies (`mutagen`, `tqdm`, `vir-tui`), and the package code into a single self-contained binary, eliminating the need for end-users to install Python or configure `pip`.

### 2.5 Interactive TUI

When run with no arguments, the tool launches a full-screen curses TUI with:
- Arrow-key navigation with highlighted selection cursor
- Color-coded section groups (Library, Integrity, Artwork, Metadata)
- Styled Unicode box drawing for menus, prompts, and pause screens
- Fallback to typed numbered input if curses is unavailable

### 2.6 CLI Interface

Every mode is accessible via flags (`--library`, `--testFLAC`, etc.) for
scripting and automation. All modes accept `--root`, `--output`, `--workers`,
`--quiet`, and `--verbose` where applicable. `--root` is repeatable: several
libraries scan together in one pass and results aggregate across them (a
repeated path is de-duped). An optional `library_roots` array in the config
supplies default roots; the first-run prompt persists only the single
`library_root`.

---

## 3. Modes

| Mode | Flag | Description |
|------|------|-------------|
| Library tree | `--library` | Formatted text tree with artist/album/track/rating/genre |
| AI export | `--ai-library` | Token-efficient flat export for LLM recommendation prompts |
| AI wings | `--ai-wings` | Separate flat library files per genre for AI processing |
| Genre wings | `--all-wings` | Separate library tree file per genre |
| Statistics | `--stats` | Format breakdown, bitrate, ratings, genres, top artists |
| FLAC integrity | `--testFLAC` | Verify via `flac -t` (authoritative) or FFmpeg; classify each file into a severity tier |
| MP3 integrity | `--testMP3` | Decode via FFmpeg (demuxer forced from extension); classify into severity tiers |
| Opus integrity | `--testOpus` | Decode via FFmpeg; classify into severity tiers |
| WAV integrity | `--testWAV` | Decode via FFmpeg; classify into severity tiers |
| WMA integrity | `--testWMA` | Decode via FFmpeg; classify into severity tiers |
| Cover extraction | `--extractArt` | Extract embedded art with format priority ranking |
| Missing art | `--missingArt` | Report directories with no cover art |
| Art quality audit | `--auditArtQuality` | Report folder/embedded covers below a resolution threshold |
| Duplicates | `--duplicates` | Four-section report: exact album dupes (cross-directory), within-folder multi-format pairs, fuzzy similar-name candidates, and track-level cross-library duplicates filtered by duration |
| Content-hash duplicate audit | `--auditAudioDupes` | Byte-level duplicate detection, tags never read: exact-file sha256, audio-stream sha256 (container tag regions parsed out: ID3v2/ID3v1/APEv2 on MP3, FLAC metadata blocks, MP4 mdat boxes), and raw first/last 64KB sampled matches for the remaining formats |
| Library health score | `--healthScore` | Per-album score out of 100 aggregating tag completeness, ReplayGain coverage, art presence/resolution, and the bitrate floor, with per-bucket deductions; read-only, no decode scans |
| Tag audit | `--auditTags` | Report files missing title, artist, track number, or genre |
| Bitrate audit | `--auditBitrate` | Report files below a configurable bitrate floor |
| ReplayGain audit | `--auditReplayGain` | Report per-album ReplayGain coverage (missing / partial / no album gain / OK); Opus R128 gain counts as tagged |
| Stray-file audit | `--auditStrays` | Report audio at the wrong depth for the configured layout, loose tracks beside album folders, hidden-dir audio, and unrecognized non-audio files in album folders |
| Smart playlist | `--playlist` | Generate an `.m3u` from a dynamic rule (e.g. `rating >= 4 and genre == 'Jazz'`) |
| Clean | `--clean` | Consolidate fragmented album folders (quote/dash/case variants) with opt-in name (`--normalize-names`, `--normalize-filenames`) and tag (`--normalize-tags`) normalization passes; write mode, dry-run by default |
| APEv2 strip | `--apestrip` | Remove stray APEv2 tags from MP3s (`--keep-metadata` to migrate sole-source fields first, `--repair-malformed` for tags mutagen cannot parse); write mode, dry-run by default |

---

## 4. Output

All output modes write `.txt` reports (not CSV). Results are grouped by
severity or category with headers and relative paths. Designed for human
reading, not spreadsheet import.

### 4.1 Integrity Severity Tiers

The integrity scanners classify every file rather than emitting a binary
pass/fail, because a decoder complaint is not by itself evidence of damaged
audio (ffmpeg reports decode errors on files that play start to finish):

- **CORRUPT**: could not decode through (tool exited non-zero / could not open),
  or a FLAC that lost sync *before* its declared sample count (true truncation).
- **SUSPECT**: decoded to the end but the tool complained; usually plays. Also
  a FLAC that decoded every declared sample and then hit trailing data.
- **METADATA**: only tag/container parse warnings; the audio is unaffected.
- **OK**: clean decode.

CORRUPT and SUSPECT are always listed; METADATA and OK are summarized and listed
only with `--verbose`. The process exit code is `1` only when at least one file
is CORRUPT.

The FLAC and MP3 integrity scans support `--resume`: each run persists its
verdicts to a transient `<output>.progress.json` beside the report, an
interrupted run leaves that state behind, and a resumed run reuses recorded
verdicts (only when the verification tool is unchanged) and scans only the
remainder, still producing the full report. A completed scan deletes the state.

---

## 5. What lattice-music Is Not

- **Not a player.** It reads tags; it does not play audio.
- **Not a tagger.** It reads metadata; it writes metadata only via the explicit
  `--clean`/`--apestrip` write modes (opt-in via `--apply`, logged, dry-run by
  default). Every other mode stays read-only.
- **Not a database.** It walks the filesystem every time; there is no index.
- **Not a sync tool.** It does not interact with cloud services or devices.

## 6. Companion Scripts

Seven destructive operations remain standalone Python scripts in the `scripts/`
directory (they are not installed by `pip` or `pipx`, and are run directly).
`cleaner.py` and `apestrip.py` were promoted into the package as the
`--clean`/`--apestrip` write modes in 5.0.0; the scripts by those names survive
as thin launchers over the package implementation, preserving the
apply-by-default CLI they always had.

Notable scripts include:
- `slipcover.py`: Embeds existing folder cover art (`cover.jpg`, etc.) into audio files lacking embedded art, maintaining parity between the filesystem and embedded metadata.
- `flac2opus.py`: Converts FLAC files to Opus 128kbps, guaranteeing tag and duration parity before cleanly deleting the original FLAC.
- `retag.py`, `genre_tidy.py`, `rerate.py`, `genre_foldermap.py`, `replaygain.py`: Various other destructive and state-mutating utilities documented in `CLAUDE.md` and `README.md`. (`cleaner.py` and `apestrip.py` are now launchers over the package's `--clean`/`--apestrip` write modes.)
