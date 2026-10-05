"""The dry-run virtual filesystem shared by the folder-moving write modes.

Extracted (6.0.0) from the two implementations that had grown the same fixes
from the same audits: lattice.modes.clean's ``Run`` and the foldermap Runner
(scripts/genre_foldermap.py before its fold). Both plan real moves against a
virtual removed/created state so a dry-run's decisions and stats match the
apply run exactly; this module is that one core, and neither mode keeps a
second copy.

``created`` maps each virtual destination to the real on-disk path currently
holding its bytes, so size/kind checks against a not-yet-moved file still read
real data. During an apply run (dry_run=False) the guarded ops perform the real
filesystem call and leave the virtual state empty, so the views are identical
to the plain calls.
"""

import shutil
from pathlib import Path


class VirtualFS:
    """Virtual removed/created state plus the guarded ops and existence-aware
    views the moving modes share."""

    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        # Paths (virtually) removed/created this run. `created` maps each
        # virtual destination to the real on-disk path currently holding its
        # bytes (the source, for a not-yet-performed move).
        self.removed: set[Path] = set()
        self.created: dict[Path, Path] = {}

    # ------- existence-aware views (identical to the plain calls during an
    # apply run; a dry-run sees the state the apply run would have produced) --

    def real(self, p: Path) -> Path:
        """The on-disk path currently holding p's bytes (p itself unless p is
        a virtual destination of this dry-run)."""
        return self.created.get(p, p)

    def exists(self, p: Path) -> bool:
        return p in self.created or (p.exists() and p not in self.removed)

    def is_file(self, p: Path) -> bool:
        origin = self.created.get(p)
        if origin is not None:
            return origin.is_file()
        return p.is_file() and p not in self.removed

    def is_dir(self, p: Path) -> bool:
        origin = self.created.get(p)
        if origin is not None:
            return origin.is_dir()
        return p.is_dir() and p not in self.removed

    def size(self, p: Path) -> int:
        return self.real(p).stat().st_size

    def effective_children(self, p: Path) -> list[Path]:
        """Children of p adjusted for this run's virtual removals/creations,
        so a dry-run predicts whether p would really be empty."""
        try:
            kids = [c for c in p.iterdir() if c not in self.removed]
        except OSError:
            return []
        kids += [c for c in self.created if c.parent == p and c not in kids]
        return kids

    def survives(self, p: Path) -> bool:
        """Dry-run: does p's content still exist somewhere after the virtual
        ops so far? True for untouched paths and for rename/move origins (the
        bytes live on under a new name, so later passes must still preview
        them, at their current on-disk path); False for merged-away or
        unlinked paths. Always True during an apply run (both sets empty)."""
        lineage = (p, *p.parents)
        if not any(q in self.removed for q in lineage):
            return True
        alive = set(self.created.values())
        return any(q in alive for q in lineage)

    # ------- guarded ops (record virtual state in a dry-run, perform for real
    # otherwise) -------

    def move(self, src: Path, dst: Path) -> None:
        if self.dry_run:
            origin = self.created.pop(src, src)
            self.removed.add(src)
            self.created[dst] = origin
            return
        shutil.move(str(src), str(dst))

    def unlink(self, p: Path) -> None:
        if self.dry_run:
            self.removed.add(p)
            self.created.pop(p, None)
            return
        p.unlink()

    def rmdir(self, p: Path) -> bool:
        if self.dry_run:
            if self.effective_children(p):
                return False
            self.removed.add(p)
            return True
        try:
            p.rmdir()
            return True
        except OSError:
            return False

    def rename(self, src: Path, dst: Path) -> None:
        if self.dry_run:
            origin = self.created.pop(src, src)
            self.removed.add(src)
            self.created[dst] = origin
            return
        src.rename(dst)
