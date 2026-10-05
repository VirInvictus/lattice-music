"""The import ritual, packaged (the ingest mode).

One command sequences what the shell history shows being typed by hand after
every import: the genreMap reorganization, the APEv2 strip, then the clean
mode with every normalization pass (--clean --all), plus a post-state health
digest. Ingest sequences; it never re-implements: each stage is the package
mode function with its own dry-run/apply contract and its own log
(<root>/genre_foldermap.manifest.tsv, <root>/apestrip.log, <root>/cleanup.log),
and the write modes' single-root refusal is respected by sequencing one root
across the stages, never aggregating.

Dry-run is the default: without --apply every stage only previews (its own
plan, its own [DRY] log lines). --apply runs the stages in order, each behind
its own confirmation prompt on a TTY (auto-proceeding when stdin is not a
TTY, the house convention), so a stage can be declined without skipping the
rest. Not a new write verb: the stages are the sanctioned write modes, so no
contract amendment beyond the ones those modes already carry.

The summary is one top-level file (default <root>/ingest_summary.txt)
recording each stage's exit and pointing into the per-stage logs, plus the
post-state digest (lattice --health, in process) when applied. With a
--snapshot baseline the summary also points at a diff report, so the whole
import is a before/after record.
"""

import sys
from datetime import datetime
from pathlib import Path

from lattice.modes.apestrip import run_apestrip
from lattice.modes.audit import run_health
from lattice.modes.clean import run_clean
from lattice.modes.foldermap import STAGING_DIR, run_genremap
from lattice.modes.library import diff_snapshot
from lattice.utils import json_summary
from vir_tui import core as ui


def _confirm(stage: str) -> bool:
    """One gate per stage, asked only on an interactive terminal (the house
    convention: cron and pipelines proceed, a human confirms)."""
    if not sys.stdin.isatty():
        return True
    try:
        ans = input(f"Proceed with the {stage} stage? [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def run_ingest(
    root,
    *,
    apply: bool = False,
    snapshot: str | None = None,
    staging: str | None = STAGING_DIR,
    allow_new_genre: bool = False,
    log_path=None,
    quiet: bool = False,
    json_mode: bool = False,
    _title: str = "lattice ingest - Import Ritual",
) -> int:
    """Run the import ritual over one library root.

    Dry-run by default: every stage previews. With apply=True the stages run
    in order behind per-stage confirmations (auto-proceeded on a non-TTY),
    followed by the post-state health digest and, when a snapshot baseline is
    given, a diff report. The summary file (default <root>/ingest_summary.txt)
    records every stage's exit and log path. Returns 1 when any stage failed,
    else 0; a declined stage is recorded and skipped, not an error."""
    directory = Path(root).resolve()
    if not directory.is_dir():
        print(f"error: {directory} is not a directory", file=sys.stderr)
        return 1

    staging = staging or None
    summary_path = Path(log_path) if log_path else directory / "ingest_summary.txt"

    if not quiet:
        ui.print_header(f"{_title}{' [DRY RUN]' if not apply else ''}")
        print(f"Target: {directory}")
        print(f"Summary: {summary_path}")
        print("Stages: genreMap -> apestrip -> clean --all -> health digest\n")

    def stage_genremap() -> int:
        return run_genremap(
            directory,
            apply=apply,
            staging=staging,
            allow_new_genre=allow_new_genre,
            quiet=quiet,
        )

    def stage_apestrip() -> int:
        # The ingest gate confirmed the stage; apestrip's own prompt would
        # double-ask, so it runs with assume_yes and keeps its apply contract.
        return run_apestrip(directory, dry_run=not apply, assume_yes=True, quiet=quiet)

    def stage_clean() -> int:
        return run_clean(
            directory,
            dry_run=not apply,
            normalize_names=True,
            normalize_filenames=True,
            normalize_tags=True,
            quiet=quiet,
        )

    stages = [
        ("genreMap (reorganize into Genre/Artist/Album)", stage_genremap),
        ("apestrip (strip stray APEv2 tags from MP3s)", stage_apestrip),
        ("clean --all (merge + normalize names/files/tags)", stage_clean),
    ]

    results: list[tuple[str, int, str]] = []
    for name, fn in stages:
        if apply and not _confirm(name.split(" (")[0]):
            results.append((name, -1, "declined by user; skipped"))
            continue
        if not quiet:
            print(f"\n--- STAGE: {name} ---")
        rc = fn()
        note = "ok" if rc == 0 else f"exit {rc}"
        results.append((name, rc, note))

    digest_note = "skipped (dry run: the post-state would be hypothetical)"
    if apply:
        digest_path = summary_path.parent / "ingest_health.txt"
        rc = run_health(
            directory,
            str(digest_path),
            layout="{artist}/{album}",
            quiet=True,
        )
        digest_note = f"exit {rc}; report: {digest_path}"

    diff_note = "no --snapshot baseline passed"
    if snapshot is not None:
        diff_path = summary_path.parent / "ingest_diff.txt"
        rc = diff_snapshot([str(directory)], str(snapshot), str(diff_path), quiet=True)
        diff_note = f"exit {rc}; report: {diff_path}"

    failures = [r for r in results if r[1] not in (0, -1)]
    exit_code = 1 if failures else 0

    lines: list[str] = []
    lines.append("INGEST SUMMARY")
    lines.append(f"Root: {directory}")
    lines.append(f"Mode: {'APPLY' if apply else 'DRY RUN'}")
    lines.append(f"Date: {datetime.now().isoformat(timespec='seconds')}")
    lines.append("=" * 64)
    for name, rc, note in results:
        label = name.split(" (")[0]
        lines.append(f"  {label}: {note}")
    lines.append(f"  health digest: {digest_note}")
    lines.append(f"  snapshot diff: {diff_note}")
    lines.append("")
    lines.append("Per-stage records live in the stage logs:")
    lines.append(f"  genreMap manifest: {directory / 'genre_foldermap.manifest.tsv'}")
    lines.append(f"  apestrip log:      {directory / 'apestrip.log'}")
    lines.append(f"  clean log:         {directory / 'cleanup.log'}")
    summary = "\n".join(lines) + "\n"

    summary_path.write_text(summary, encoding="utf-8")

    if json_mode:
        print(
            json_summary(
                "ingest",
                directory,
                not apply,
                {
                    "stages": {
                        name.split(" (")[0]: {"exit": rc, "note": note}
                        for name, rc, note in results
                    },
                    "summary": str(summary_path),
                },
            ),
            end="",
        )
    elif not quiet:
        print()
        print(summary)
        print(f"Summary written to: {summary_path}")

    return exit_code
