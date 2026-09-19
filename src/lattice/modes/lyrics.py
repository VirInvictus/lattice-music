"""Fetch synced lyrics from LRCLIB into .lrc sidecars (the lyrics write mode).

New with v5.7.0 and the package's third write mode (besides clean and
apestrip). Walks one library root, matches every audio file against the
LRCLIB API (https://lrclib.net — open, unauthenticated, GET-only), and
writes the synced lyrics as a `.lrc` sidecar beside the audio file (same
basename, the LRCGET/Jellyfin convention). Players and servers that read
sidecar lyrics — Jellyfin 10.9+ among them — then carry the lyrics with
the files, so a library that clones to several machines takes its lyrics
along instead of parking them in one server's database.

Matching: artist + title (+ album and duration when tagged) against
/api/get for an exact record, then /api/search as a fallback, preferring
the candidate with the closest duration inside a ±2s window. Only *synced*
lyrics are written; plain-only matches and LRCLIB's instrumental flag are
reported, not invented into files. Tracks that already carry a
same-basename .lrc are skipped unless --lyrics-force.

Dry-run is the default and performs the read-only lookups so the preview
can state real hit/miss counts; --apply gates every disk write, and every
written sidecar is recorded in an append-only timestamped log (default
<root>/lyrics.log). Requests are serial and paced (--lyrics-sleep, default
0.5s) per LRCLIB's fair-use etiquette. This is the package's first mode
that talks to the network, so spec.md §1/§5 carry the amended wording
(the v5.7.0 contract amendment).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from vir_tui import core as ui

from lattice.config import VERSION
from lattice.tags import get_all_tags
from lattice.utils import is_audio, iter_audio_dirs

_API = "https://lrclib.net"
_UA = f"lattice-music/{VERSION}"
_TIMEOUT_S = 30
_DURATION_TOLERANCE_S = 2.0

# Per-track outcomes. written/would-write are the matches (the only statuses
# that touch or would touch disk); everything else is reported, not acted on.
ST_WRITTEN = "written"
ST_WOULD_WRITE = "would-write"
ST_EXISTS = "exists"
ST_INSTRUMENTAL = "instrumental"
ST_PLAIN_ONLY = "plain-only"
ST_NOT_FOUND = "not-found"
ST_UNTAGGED = "untagged"
ST_ERROR = "error"

_MATCH_STATUSES = (ST_WRITTEN, ST_WOULD_WRITE)


@dataclass
class TrackResult:
    path: str
    status: str
    detail: str = ""
    content: str | None = None


def _fetch_json(url: str):
    """GET `url` and decode the JSON body. Raises urllib.error.HTTPError on
    non-2xx (callers distinguish 404); patched out in the test suite."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _lrclib_exact(artist, title, album, duration_s, fetch):
    """Exact /api/get lookup: None on a 404 (the endpoint's no-match signal),
    the record dict on a hit. Other HTTP errors propagate as errors."""
    params: dict[str, str | int] = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration_s:
        params["duration"] = int(round(duration_s))
    url = _API + "/api/get?" + urllib.parse.urlencode(params)
    try:
        return fetch(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _lrclib_search(artist, title, fetch):
    url = (
        _API
        + "/api/search?"
        + urllib.parse.urlencode({"track_name": title, "artist_name": artist})
    )
    results = fetch(url)
    return results if isinstance(results, list) else []


def _pick_search(results, duration_s, artist, title):
    """Best search hit carrying synced lyrics: closest duration inside the
    tolerance window first, then an exact case-insensitive artist+title
    match, else the first candidate (LRCLIB's ordering is deterministic)."""
    candidates = [r for r in results if (r.get("syncedLyrics") or "").strip()]
    if not candidates:
        return None
    if duration_s:
        near = [
            r
            for r in candidates
            if r.get("duration")
            and abs(r["duration"] - duration_s) <= _DURATION_TOLERANCE_S
        ]
        if near:
            return min(near, key=lambda r: abs(r["duration"] - duration_s))
    want_artist = (artist or "").casefold()
    want_title = (title or "").casefold()
    for r in candidates:
        if (r.get("artistName") or "").casefold() == want_artist and (
            r.get("trackName") or ""
        ).casefold() == want_title:
            return r
    return candidates[0]


def _lookup_one(path: str, fetch) -> TrackResult:
    """Look up one audio file: read its tags, query LRCLIB, and return the
    outcome with the synced text attached (never written here)."""
    try:
        tags = get_all_tags(path)
    except Exception as e:
        return TrackResult(path, ST_ERROR, detail=str(e))

    if not tags.artist or not tags.title:
        return TrackResult(path, ST_UNTAGGED, detail="no artist/title tags")

    try:
        rec = _lrclib_exact(tags.artist, tags.title, tags.album, tags.duration_s, fetch)
        if rec is None:
            results = _lrclib_search(tags.artist, tags.title, fetch)
            rec = _pick_search(results, tags.duration_s, tags.artist, tags.title)
    except urllib.error.URLError as e:
        # HTTPError is a URLError subclass; 404s were already handled as
        # no-match inside the exact lookup, so this is a network-level fail.
        reason = getattr(e, "reason", None) or e
        return TrackResult(path, ST_ERROR, detail=str(reason))
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return TrackResult(path, ST_ERROR, detail=str(e))
    if rec is None:
        return TrackResult(path, ST_NOT_FOUND)
    if rec.get("instrumental"):
        return TrackResult(path, ST_INSTRUMENTAL)
    synced = rec.get("syncedLyrics") or ""
    if not synced.strip():
        return TrackResult(path, ST_PLAIN_ONLY)
    content = synced if synced.endswith("\n") else synced + "\n"
    return TrackResult(path, ST_WOULD_WRITE, content=content)


def _sidecar_path(audio_path: str) -> str:
    return os.path.splitext(audio_path)[0] + ".lrc"


def _iter_audio_files(root: str):
    for _r, _dirpath, _dirs, files in iter_audio_dirs(root):
        for fn in sorted(files):
            if is_audio(fn):
                yield os.path.join(_dirpath, fn)


def run_lyrics(
    root,
    *,
    dry_run: bool = True,
    force: bool = False,
    sleep_s: float = 0.5,
    log_path=None,
    assume_yes: bool = False,
    quiet: bool = False,
    fetch=None,
    _title: str = "lattice lyrics - LRCLIB Sidecar Fetcher",
) -> int:
    """Run the LRCLIB lyrics fetch over one directory tree.

    The package write mode behind `lattice --lyrics`: dry-run by default,
    applying only on an explicit opt-in (dry_run=False, the CLI's --apply).
    The lookup pass runs in both faces — it is read-only, and the preview is
    worthless without it — while every disk write waits for the apply gate
    (the CLI's --apply, or the TUI's ask_yn via assume_yes; a non-TTY apply
    run proceeds, so pipelines keep working). Every written sidecar is
    recorded to an append-only timestamped log (default <root>/lyrics.log).
    Returns a process exit code: 1 when the root is unusable, the log is
    unopenable, or any file errored, else 0.
    """
    root = str(root)
    if not os.path.isdir(root):
        print(f"[!] Directory not found: {root}", file=sys.stderr)
        return 1

    if log_path is None:
        log_path = os.path.join(root, "lyrics.log")
    fetch = fetch or _fetch_json
    sleep_s = max(0.0, sleep_s)

    if not quiet:
        ui.print_header(f"{_title}{' [DRY RUN]' if dry_run else ''}")
        print(f"Target: {root}")
        print(f"Overwrite existing sidecars: {'Yes' if force else 'No'}\n")

    # Lookup pass: shared by dry-run and apply — read-only against the API
    # and the tree, so the worklist is real before anything is written.
    files = list(_iter_audio_files(root))
    results: list[TrackResult] = []
    if files:
        last = len(files) - 1
        for i, path in enumerate(ui.tqdm(files, desc=ui.info("Looking up lyrics"))):
            sidecar = _sidecar_path(path)
            if not force and os.path.exists(sidecar):
                results.append(TrackResult(path, ST_EXISTS))
            else:
                results.append(_lookup_one(path, fetch))
            if i < last and sleep_s:
                time.sleep(sleep_s)

    matches = [r for r in results if r.status in _MATCH_STATUSES]
    exists = sum(1 for r in results if r.status == ST_EXISTS)
    instrumental = [r for r in results if r.status == ST_INSTRUMENTAL]
    plain = [r for r in results if r.status == ST_PLAIN_ONLY]
    missing = [r for r in results if r.status == ST_NOT_FOUND]
    untagged = [r for r in results if r.status == ST_UNTAGGED]
    errors = [r for r in results if r.status == ST_ERROR]

    if not quiet:
        head = "[DRY RUN] " if dry_run else ""
        if matches:
            print(f"\n{head}Synced lyrics for {len(matches)} track(s) under {root}\n")
            for r in matches:
                rel = os.path.relpath(r.path, root)
                sidecar = os.path.basename(_sidecar_path(r.path))
                lines = r.content.count("\n") if r.content else 0
                print(f"  {rel} -> {sidecar} ({lines} lines)")
            print()
        for label, group in (
            ("inst", instrumental),
            ("plain", plain),
            ("miss", missing),
            ("untagged", untagged),
            ("!", errors),
        ):
            for r in group:
                rel = os.path.relpath(r.path, root)
                detail = f" ({r.detail})" if r.detail else ""
                print(f"  [{label}] {rel}{detail}")
        print(
            f"\n{head}{len(matches)} match(es), {exists} already sided,"
            f" {len(instrumental)} instrumental, {len(plain)} plain-only,"
            f" {len(missing)} not found, {len(untagged)} untagged,"
            f" {len(errors)} error(s)."
        )

    if dry_run:
        print("\nDry run: no files modified.")
        return 0

    if not matches:
        print(ui.success("Nothing to write."))
        return 1 if errors else 0

    log_fh = None
    try:
        try:
            log_fh = open(log_path, "a", encoding="utf-8")
        except OSError as e:
            print(ui.error(f"cannot open log file {log_path}: {e}"), file=sys.stderr)
            return 1

        def log(msg: str) -> None:
            ui.tqdm.write(msg)
            if log_fh is not None:
                ts = datetime.datetime.now().isoformat(timespec="seconds")
                log_fh.write(f"[{ts}] {msg}\n")  # uncolored to the log

        # Confirmation, apestrip style. assume_yes stands in for the TUI's
        # ask_yn gate; a non-TTY run proceeds so pipelines keep working.
        if not assume_yes and sys.stdin.isatty():
            try:
                ans = input("\nWrite these .lrc sidecars? [y/N] ").strip().lower()
            except EOFError:
                ans = ""
            if ans not in ("y", "yes"):
                log("Aborted by user; no files modified.")
                return 0

        log(f"Writing lyrics sidecars for {len(matches)} file(s) under {root}")
        wrote = 0
        write_errors = 0
        for r in matches:
            rel = os.path.relpath(r.path, root)
            sidecar = _sidecar_path(r.path)
            try:
                with open(sidecar, "w", encoding="utf-8") as fh:
                    fh.write(r.content or "")
            except OSError as e:
                write_errors += 1
                log(f"  [!] {rel}: {e}")
                continue
            wrote += 1
            r.status = ST_WRITTEN
            log(f"  {rel}: wrote {os.path.basename(sidecar)} (LRCLIB)")
        log(f"-> wrote {wrote} sidecar(s); {write_errors} write error(s).")
        if errors or write_errors:
            return 1
    finally:
        if log_fh is not None:
            log_fh.close()

    return 0


def main(argv: list[str] | None = None) -> int:
    """Direct CLI for the module, mirroring the other write modes' main()
    helpers: apply by default, --dry-run to opt out."""
    parser = argparse.ArgumentParser(
        description="Fetch synced lyrics from LRCLIB into .lrc sidecars."
    )
    parser.add_argument("directory", help="Directory to walk (album or library root)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the lookups and print the worklist; write nothing",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt (auto-skipped when stdin is not a TTY)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch and overwrite existing .lrc sidecars",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.5,
        help="Seconds between LRCLIB requests (default: 0.5)",
    )
    parser.add_argument(
        "--log",
        dest="log_path",
        default=None,
        help="Append a timestamped record to this file "
        "(default: <directory>/lyrics.log)",
    )
    args = parser.parse_args(argv)

    return run_lyrics(
        args.directory,
        dry_run=args.dry_run,
        force=args.force,
        sleep_s=args.sleep,
        log_path=args.log_path,
        assume_yes=args.yes,
        _title="lyrics.py - LRCLIB Sidecar Fetcher",
    )
