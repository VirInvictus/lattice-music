#!/usr/bin/env python3
"""replaygain.py — scan and write ReplayGain 2.0 tags album-by-album (launcher).

Since lattice-music 6.0.0 this script is a thin launcher over the package
implementation in lattice.modes.replaygain; nothing is copied here anymore, so
the writer and the package's --auditReplayGain/--verifyReplayGain readers
cannot drift. The tool's behavior is unchanged: it wraps `rsgain easy` (or
`rsgain custom` with --target-lufs) to compute and write per-track and
per-album gain/peak tags, one album folder at a time, with the values read back
and logged after each album.

The companion-script contract is unchanged, so aliases and cron keep working:
apply by default (--dry-run to preview), a worklist and confirmation prompt
before a real write (--yes to skip; auto-skipped when stdin is not a TTY), an
append-only timestamped log at <directory>/replaygain.log (--log to override),
idempotent re-runs. Note that the packaged mode inverts the default on
purpose: `lattice --replayGain` dry-runs unless you pass --apply.

Requires `rsgain` on PATH (not bundled; Linux: `dnf install rsgain`, macOS:
`brew install rsgain`, Windows: `winget install rsgain`).

Usage:
    ./replaygain.py /mnt/SharedData/Music --dry-run
    ./replaygain.py /mnt/SharedData/Music
    ./replaygain.py ~/Music --skip-tagged --threads 4 --yes
    ./replaygain.py ~/Music --target-lufs -14        # louder than 89 dB
"""

import sys

__version__ = "1.2.2"


def _import_lattice():
    """The implementation lives in the lattice package. Imported lazily so the
    script gives a useful hint instead of a bare traceback when run outside an
    install / PYTHONPATH=src."""
    try:
        from lattice.modes import replaygain
    except ImportError as e:
        print(
            f"error: could not import lattice ({e}).\n"
            "Install it (pip install -e . / pipx install .) or run with "
            "PYTHONPATH=src.",
            file=sys.stderr,
        )
        sys.exit(2)
    return replaygain


def __getattr__(name):
    # Re-export the live implementation (split_rsgain_supported, find_album_dirs,
    # coverage_label, album_coverage, read_gain_strings, scan_album, main, ...)
    # instead of copying any of it.
    return getattr(_import_lattice(), name)


def main() -> int:
    return _import_lattice().main()


if __name__ == "__main__":
    sys.exit(main())
