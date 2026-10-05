#!/usr/bin/env python3
"""genre_tidy.py — build an artist→genre authority and reconcile a library to it
(launcher).

Since lattice-music 6.0.0 this script is a thin launcher over the package
implementation in lattice.modes.genretidy; nothing is copied here anymore, so
the script and the package's --genreTidy-build/--genreTidy-apply modes cannot
drift. The tool's behavior is unchanged: `build` scans (read-only, through
lattice's scanner) and writes the editable TSV map; `apply` retags albums that
disagree with the map, with every write going through the package's retag
writer (lattice.modes.retag) in process.

The companion-script contract is unchanged, so aliases and cron keep working:
build/apply subcommands, apply is destructive immediately (--dry-run to
preview), an append-only timestamped log at <library>/genre_tidy.log (--log to
override), idempotent re-runs, re-running build preserves your map edits. Note
that the packaged apply mode inverts the default on purpose:
`lattice --genreTidy-apply` dry-runs unless you pass --apply.

Usage:
    ./genre_tidy.py build /mnt/SharedData/Music
    ./genre_tidy.py apply /mnt/SharedData/Music --dry-run
    ./genre_tidy.py apply /mnt/SharedData/Music --map ~/genres.tsv --log ~/tidy.log
    ./genre_tidy.py build /library --layout "{genre}/{artist}/{album}"
"""

import sys

__version__ = "1.3.0"


def _import_lattice():
    """The implementation lives in the lattice package. Imported lazily so the
    script gives a useful hint instead of a bare traceback when run outside an
    install / PYTHONPATH=src."""
    try:
        from lattice.modes import genretidy
    except ImportError as e:
        print(
            f"error: could not import lattice ({e}).\n"
            "Install it (pip install -e . / pipx install .) or run with "
            "PYTHONPATH=src.",
            file=sys.stderr,
        )
        sys.exit(2)
    return genretidy


def __getattr__(name):
    # Re-export the live implementation (norm, parse_map, is_compliant,
    # reduce_artists, build_rows, run_genre_tidy_build, run_genre_tidy_apply,
    # main, ...) instead of copying any of it.
    return getattr(_import_lattice(), name)


def main() -> int:
    return _import_lattice().main()


if __name__ == "__main__":
    sys.exit(main())
