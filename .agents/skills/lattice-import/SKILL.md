---
name: lattice-import
description: >-
  Use this skill when the user asks to process, clean, or import a swath of new albums into the lattice-music library. It provides the standard circuit of destructive scripts to run on newly imported music before integrating it into the main library.
---

# lattice-music Import Circuit

When the user imports new albums (typically into a staging folder like `/mnt/SharedData/Music/Unfiltered`), run this exact sequence of tools to ensure the files meet the library's strict standards.

## The Standard Circuit

Run these commands in order on the target directory (e.g., `/mnt/SharedData/Music/Unfiltered`).
*Note: most of the circuit is the packaged write modes (dry-run unless `--apply`); pass `--apply` for real. The two remaining hand-run scripts take `-y`/`--yes` to bypass prompts.*

1. **Transcode Lossless to Opus**
   Convert any FLAC files to Opus to save space while preserving metadata and quality.
   `./scripts/flac2opus.py /mnt/SharedData/Music/Unfiltered -y`

2. **Strip stray APEv2 Tags**
   Remove hidden or malformed APEv2 tags from MP3s that can cause metadata conflicts.
   `lattice --apestrip /mnt/SharedData/Music/Unfiltered --apply`

3. **Clean and Normalize (Crucial Step)**
   Consolidate fragmented albums, consolidate fragmented folders and apply the opt-in rename/tag-fold passes, and normalize folder names, filenames, and tags.
   *Important: `--all` turns on the `--normalize-names`, `--normalize-tags`, and `--normalize-filenames` passes; without it `--clean` only consolidates directories.*
   `lattice --clean /mnt/SharedData/Music/Unfiltered --all --apply`

4. **Fetch and Embed Cover Art**
   Download missing covers from the iTunes API and embed folder art directly into the audio files.
   `./scripts/slipcover.py /mnt/SharedData/Music/Unfiltered --fetch -y`

5. **Apply ReplayGain**
   Calculate and write ReplayGain 2.0 volume normalization tags using `rsgain`.
   `lattice --replayGain /mnt/SharedData/Music/Unfiltered --apply`

## Whole Library Maintenance

After the unfiltered albums are processed, they can be merged into the main library (`/mnt/SharedData/Music`). Periodically, the user may want to run maintenance on the entire library.

The most common whole-library maintenance command is the clean mode:
`lattice --clean /mnt/SharedData/Music --all --apply`

Always explicitly explain *why* you are running each step to the user so they understand the pipeline.
