#!/usr/bin/env python3
"""genre_foldermap.py — restructure a library into Genre/Artist/Album/Song
(launcher).

Since lattice-music 6.0.0 this script is a thin launcher over the package
implementation in lattice.modes.foldermap; nothing is copied here anymore, so
the script and the package's --genreMap write mode cannot drift, and both plan
against the same shared dry-run virtual filesystem as the clean mode. The
tool's behavior is unchanged: reorganizes a flat Artist/Album/Song tree into
Genre/Artist/Album/Song, moving each album folder under its dominant genre
(read through lattice's scanner), mv-only on one filesystem, destinations
never overwritten, emptied source folders pruned, with the depth-aware
classify, the live genre-vocabulary gate, and the Unfiltered/ staging-inbox
semantics intact.

The companion-script contract is unchanged, so aliases and cron keep working:
dry-run is the DEFAULT (--apply performs it and writes the manifest TSV that
--revert replays in reverse), the same flags, the same
<library>/genre_foldermap.manifest.tsv default. The package mode
(`lattice --genreMap`) runs the same defaults -- this is the one fold that
needed no default inversion.

Usage:
    ./genre_foldermap.py /mnt/SharedData/Music
    ./genre_foldermap.py /mnt/SharedData/Music --only-genre "Comedy Rock" --apply
    ./genre_foldermap.py /mnt/SharedData/Music --apply --allow-new-genre
    ./genre_foldermap.py /mnt/SharedData/Music --apply --log ~/foldermap.tsv
    ./genre_foldermap.py --revert ~/foldermap.tsv --apply
"""

import sys

__version__ = "1.4.1"


def _import_lattice():
    """The implementation lives in the lattice package. Imported lazily so the
    script gives a useful hint instead of a bare traceback when run outside an
    install / PYTHONPATH=src."""
    try:
        from lattice.modes import foldermap
    except ImportError as e:
        print(
            f"error: could not import lattice ({e}).\n"
            "Install it (pip install -e . / pipx install .) or run with "
            "PYTHONPATH=src.",
            file=sys.stderr,
        )
        sys.exit(2)
    return foldermap


def __getattr__(name):
    # Re-export the live implementation (sanitize_component, split_multi_genre,
    # classify, build_plan, Runner, execute, revert, parse_manifest, main, ...)
    # instead of copying any of it.
    return getattr(_import_lattice(), name)


def main() -> int:
    return _import_lattice().main()


if __name__ == "__main__":
    sys.exit(main())
