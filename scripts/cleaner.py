#!/usr/bin/env python3
"""cleaner.py — consolidate fragmented album folders (compat launcher).

Since lattice-music 5.0.0 this script is a thin launcher over the package
implementation: the pure name/tag rules live in lattice.norm and the four
passes (artist merge, album merge, name normalization, tag normalization) in
lattice.modes.clean. Nothing is copied here anymore, so the two can't drift.

The companion-script contract is unchanged, so aliases and cron keep working:
apply by default (--dry-run to preview), the same flags, an append-only
timestamped log at <directory>/cleanup.log (--log to override), idempotent
re-runs. Note that the packaged mode inverts the default on purpose:
`lattice --clean` dry-runs unless you pass --apply.

Usage:
    ./cleaner.py /mnt/SharedData/Music
    ./cleaner.py /mnt/SharedData/Music --dry-run
    ./cleaner.py ~/Music --log /tmp/music-cleanup.log
"""

import sys

__version__ = "1.6.0"


def _import_lattice():
    """The implementation lives in the lattice package. Imported lazily so the
    script gives a useful hint instead of a bare traceback when run outside an
    install / PYTHONPATH=src."""
    try:
        from lattice.modes import clean
    except ImportError as e:
        print(
            f"error: could not import lattice ({e}).\n"
            "Install it (pip install -e . / pipx install .) or run with "
            "PYTHONPATH=src.",
            file=sys.stderr,
        )
        sys.exit(2)
    return clean


def __getattr__(name):
    # Re-export the live implementation (Run, find_groups, consolidate_group,
    # normalize_tree, normalize_tags, main, ...) instead of copying any of it.
    return getattr(_import_lattice(), name)


def main() -> int:
    return _import_lattice().main()


if __name__ == "__main__":
    sys.exit(main())
