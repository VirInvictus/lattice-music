lattice 6.6.0 - filesystem-first music library toolkit

usage: lattice MODE [ROOT] [options]
       lattice help MODE    full help for one mode
       lattice              interactive TUI (no arguments)

LIBRARY TREES & EXPORTS
  --library     Album/track tree as a text report
  --ai-library  Token-efficient export for AI prompts
  --all-wings   One library file per genre
  --ai-wings    AI-friendly library file per genre
  --exportDb    Library as a SQLite database

REPORTS & SNAPSHOTS
  --stats           Format, bitrate, rating, genre breakdowns
  --snapshot        Per-file TSV (the --diff baseline)
  --diff SNAPSHOT   Replay a snapshot against the current tree
  --playlist        Smart .m3u playlist from a rule
  --checkPlaylists  Verify the library's .m3u playlists

INTEGRITY (decode checks)
  --testFLAC  Verify FLAC files (flac -t / ffmpeg)
  --testMP3   Decode-verify MP3 files via ffmpeg
  --testOpus  Decode-verify Opus files via ffmpeg
  --testWAV   Decode-verify WAV files via ffmpeg
  --testWMA   Decode-verify WMA files via ffmpeg

ARTWORK
  --extractArt        Extract embedded cover art to folders (WRITES by default:
                      the only mode whose default is to write; --dry-run
                      previews; --apply is silently ignored here)
  --missingArt        Report album folders with no cover art
  --auditArtQuality   Folder covers below a resolution floor
  --auditArtMismatch  Embedded art vs folder cover comparison

AUDITS (read-only reports)
  --duplicates       Four-section duplicate-album report
  --auditAudioDupes  Content-hash duplicate detection
  --auditTags        Files with incomplete tags
  --auditJunkFrames  MP3 ID3v2 junk-frame audit
  --auditAlbums      Per-album consistency (codecs, tracks, year)
  --auditBitrate     Files below a bitrate floor
  --auditReplayGain  Per-album ReplayGain coverage
  --auditStrays      Wrong-depth audio, strays, hidden dirs
  --healthScore      Per-album health score out of 100
  --health           One-screen digest across all lenses

REPLAYGAIN (audit, verify, write)
  --verifyReplayGain  Stored ReplayGain vs a fresh rsgain measure
  --replayGain        Write ReplayGain 2.0 tags via rsgain

WRITE MODES (dry-run by default; --apply writes)
  --clean            Consolidate fragmented album folders
  --apestrip         Strip stray APEv2 tags from MP3s
  --stripRatings     Delete rating tags carrying a nonzero rating
  --lyrics           Fetch synced .lrc lyrics from LRCLIB
  --retag            Rewrite genre tags on one album directory
  --genreTidy-build  Write the artist-to-genre authority map
  --genreTidy-apply  Retag albums to the authority map
  --genreMap         Restructure into Genre/Artist/Album
  --ingest           Import ritual: stripRatings, genreMap, apestrip, clean,
                     health

OPTIONS
  -h, --help           This index; "lattice help MODE" shows one mode.
  --version            Show the version.
  --root DIR           Library root; repeatable (default: config or current
                       dir).
  --output PATH        Report path; each mode's default is in its help page.
  --where EXPR         Scope to tracks matching a playlist rule (tag-reading
                       modes).
  --layout PATTERN     Path pattern (default: {artist}/{album}).
  --json               Machine-readable output where supported.
  --fail-on-findings   Audits exit 1 on findings (cron/CI gating).
  --apply / --dry-run  Write modes: commit vs preview (preview is the default).
  --quiet              Minimize output.
  --verbose            Extra detail on the audits that support it.

Every mode above has a full help page:
  lattice help MODE    (try: lattice help clean)
  lattice help --json    the whole surface as JSON

exit codes: 0 clean (an export with no matches is still 0); 1 findings or failure: audits only with --fail-on-findings; the integrity scans exit 1 on any CORRUPT file without a flag; write modes on run failure; 2 usage error or missing dependency (including --where/--json/--fail-on-findings on a mode that refuses them, --replayGain --apply without rsgain, and an invalid --where/--playlist rule); 130 interrupted
