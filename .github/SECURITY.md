# Security Policy

## Supported versions

Only the latest release line receives security fixes. `lattice --version`
prints what you are running; PyPI's latest matches `main`.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting
(**Security → Report a vulnerability** on this repository) rather than a
public issue. You should hear back within a week.

## Scope notes

- lattice-music is a local, offline tool: it reads the audio files you point
  it at and writes reports next to your cwd (or into the library only via the
  explicit `--clean`/`--apestrip` write modes, which are dry-run by default).
- The integrity modes shell out to `flac`/`ffmpeg` binaries found on `PATH`
  (or an explicit `--ffmpeg` path). The companion scripts in `scripts/` shell
  out to `ffmpeg`/`rsgain` the same way and, unlike the package, modify files
  in place.
- Smart-playlist rules are evaluated by a whitelisted AST walker, never by
  `eval`; attribute access, calls, and subscripts in a rule are rejected.
- No network access, no telemetry, no auto-updates anywhere in the package.
  (The `slipcover.py` companion queries the iTunes API for missing covers,
  but that is opt-in via `--fetch` and lives outside the package.)
