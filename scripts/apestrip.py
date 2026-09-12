#!/usr/bin/env python3
"""apestrip.py — remove stray APEv2 tags from MP3 files, losslessly (launcher).

Since lattice-music 5.0.0 this script is a thin launcher over the package
implementation in lattice.modes.apestrip; nothing is copied here anymore, so
the two can't drift. The tool's behavior is unchanged: by default it deletes
the APEv2 block and leaves ID3 byte for byte (genre and rating are always
reported, never migrated or written); --keep-metadata migrates sole-source APE
fields into the right ID3 frames first; --repair-malformed excises malformed
APE tags mutagen cannot parse, via verified atomic byte surgery.

The companion-script contract is unchanged, so aliases and cron keep working:
apply by default (--dry-run to preview), a worklist and confirmation prompt
before a real write (--yes to skip; auto-skipped when stdin is not a TTY), an
append-only timestamped log at <directory>/apestrip.log (--log to override),
idempotent re-runs. Note that the packaged mode inverts the default on
purpose: `lattice --apestrip` dry-runs unless you pass --apply.

Usage:
    ./apestrip.py /path/to/album --dry-run
    ./apestrip.py "/mnt/SharedData/Music"
    ./apestrip.py /path/to/album --keep-metadata --yes --log ~/apestrip.log
"""

import sys

__version__ = "1.3.0"


def _import_lattice():
    """The implementation lives in the lattice package. Imported lazily so the
    script gives a useful hint instead of a bare traceback when run outside an
    install / PYTHONPATH=src."""
    try:
        from lattice.modes import apestrip
    except ImportError as e:
        print(
            f"error: could not import lattice ({e}).\n"
            "Install it (pip install -e . / pipx install .) or run with "
            "PYTHONPATH=src.",
            file=sys.stderr,
        )
        sys.exit(2)
    return apestrip


def __getattr__(name):
    # Re-export the live implementation (classify_ape_field, plan_file,
    # process_file, repair_file, main, ...) instead of copying any of it.
    return getattr(_import_lattice(), name)


def main() -> int:
    return _import_lattice().main()


if __name__ == "__main__":
    sys.exit(main())
