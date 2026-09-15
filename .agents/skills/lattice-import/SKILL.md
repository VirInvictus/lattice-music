---
name: lattice-import
description: >-
  Use this skill when the user asks to process, clean, or import a swath of new albums into the lattice-music library. It provides the standard circuit of destructive scripts to run on newly imported music before integrating it into the main library.
---

# lattice-music Import Circuit

When the user imports new albums (typically into a staging folder like `/mnt/SharedData/Music/Unfiltered`), run this exact sequence of tools to ensure the files meet the library's strict standards.

## The Standard Circuit

Run these commands in order on the target directory (e.g., `/mnt/SharedData/Music/Unfiltered`). 
*Note: Always use `-y` or `--yes` to bypass prompts when running these programmatically.*

1. **Transcode Lossless to Opus**
   Convert any FLAC files to Opus to save space while preserving metadata and quality.
   `./scripts/flac2opus.py /mnt/SharedData/Music/Unfiltered -y`

2. **Strip Malformed APEv2 Tags**
   Remove hidden or malformed APEv2 tags from MP3s that can cause metadata conflicts.
   `./scripts/apestrip.py /mnt/SharedData/Music/Unfiltered --yes`

3. **Clean and Normalize (Crucial Step)**
   Consolidate fragmented albums, remove junk files, and normalize folder names, filenames, and tags.
   *Important: You must explicitly pass the `--normalize` flags, otherwise cleaner.py will only consolidate directories.*
   `./scripts/cleaner.py /mnt/SharedData/Music/Unfiltered --normalize-names --normalize-tags --normalize-filenames`

4. **Fetch and Embed Cover Art**
   Download missing covers from the iTunes API and embed folder art directly into the audio files.
   `./scripts/slipcover.py /mnt/SharedData/Music/Unfiltered --fetch -y`

5. **Apply ReplayGain**
   Calculate and write ReplayGain 2.0 volume normalization tags using `rsgain`.
   `./scripts/replaygain.py /mnt/SharedData/Music/Unfiltered -y`

## Whole Library Maintenance

After the unfiltered albums are processed, they can be merged into the main library (`/mnt/SharedData/Music`). Periodically, the user may want to run maintenance on the entire library.

The most common whole-library maintenance command is the cleaner:
`./scripts/cleaner.py /mnt/SharedData/Music --normalize-names --normalize-tags --normalize-filenames`

Always explicitly explain *why* you are running each step to the user so they understand the pipeline.
