#!/usr/bin/env python3
"""retag.py — universal genre rewriter for one album directory (launcher).

Since lattice-music 6.0.0 this script is a thin launcher over the package
implementation in lattice.modes.retag; nothing is copied here anymore, so the
script and the package's --retag write mode cannot drift. The tool's behavior
is unchanged: hard-overwrites the genre tag(s) on every audio file in a
directory, hiding the per-container differences (ID3, Vorbis, Apple atoms),
clearing the hidden MP3 genre spots (APEv2, TXXX:GENRE, the ID3v1 byte) as it
goes. --strip-junk converts-or-drops junk ID3 frames instead, through the same
classifier --auditJunkFrames reports with.

The companion-script contract is unchanged, so aliases and cron keep working:
apply by default (--dry-run to preview), the same flags, and the log stays
--log opt-in (no default log file, as it always was). Note that the packaged
mode inverts the default on purpose: `lattice --retag` dry-runs unless you
pass --apply, and it logs to <dir>/retag.log.

Usage:
    ./retag.py /path/to/album "Genre One" "Genre Two"
    ./retag.py /path/to/album "Alternative Rap" --dry-run
    ./retag.py /path/to/album "Jazz" --log ~/retag.log
"""

import sys

__version__ = "1.2.0"


def _import_lattice():
    """The implementation lives in the lattice package. Imported lazily so the
    script gives a useful hint instead of a bare traceback when run outside an
    install / PYTHONPATH=src."""
    try:
        from lattice.modes import retag
    except ImportError as e:
        print(
            f"error: could not import lattice ({e}).\n"
            "Install it (pip install -e . / pipx install .) or run with "
            "PYTHONPATH=src.",
            file=sys.stderr,
        )
        sys.exit(2)
    return retag


def __getattr__(name):
    # Re-export the live implementation (read_genres, is_noop, apply_genres,
    # strip_junk_frames, retag_directory, run_junk_strip, main, ...) instead of
    # copying any of it.
    return getattr(_import_lattice(), name)


def main() -> int:
    return _import_lattice().main()


if __name__ == "__main__":
    sys.exit(main())
