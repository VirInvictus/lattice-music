import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from lattice.config import (
    DEFAULT_MP3_OUTPUT,
    DEFAULT_OPUS_OUTPUT,
    DEFAULT_WAV_OUTPUT,
    DEFAULT_WMA_OUTPUT,
)
from lattice.tags import HAVE_MUTAGEN_MP3, MUTAGEN_MP3
from lattice.utils import (
    _make_pbar,
    as_roots,
    green,
    has_tool,
    red,
    relpath_under,
    run_proc,
    yellow,
)

# =====================================
# Decode-result classification
# =====================================

TIER_OK = "OK"
TIER_METADATA = "METADATA"
TIER_SUSPECT = "SUSPECT"
TIER_CORRUPT = "CORRUPT"
TIER_ORDER = (TIER_CORRUPT, TIER_SUSPECT, TIER_METADATA, TIER_OK)

_RE_FLAC_LOSTSYNC = re.compile(r"LOST_SYNC after processing (\d+) samples")

# Tag/container parse complaints — the audio stream is unaffected. (-vn already
# suppresses most embedded-cover lines before they reach us.)
_METADATA_MARKERS = (
    "Incorrect BOM value",
    "Error reading frame",
    "Error reading comment",
    "[png",
    "chunk too big",
)
# Decoder hiccups that also appear on files which play start to finish (a
# truncated MP3 and a healthy one can produce these identically), so on their
# own they are not evidence of damage.
_BENIGN_MARKERS = (
    "Header missing",
    "invalid new backstep",
)


def _matches(line: str, markers: tuple[str, ...]) -> bool:
    return any(m in line for m in markers)


def classify_decode(
    rc: int, stderr: str, declared_samples: int | None = None
) -> tuple[str, str]:
    """Map a decode tool's (exit code, stderr) into a severity tier and reason.

    Conservative by design. A decode that ran to completion (rc == 0) is never
    CORRUPT, however many decoder complaints it emitted. CORRUPT is reserved for
    a tool that could not decode through (rc != 0) or a FLAC that lost sync
    before its declared sample count (true truncation, which `flac -t` reports
    but ffmpeg cannot reliably detect for MP3)."""
    text = stderr or ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # FLAC sync loss carries the decoded sample count; compare it to the
    # header's declared total to separate truncation from a trailing-junk tail.
    m = _RE_FLAC_LOSTSYNC.search(text)
    if m:
        decoded = int(m.group(1))
        if declared_samples and decoded < declared_samples:
            return (
                TIER_CORRUPT,
                f"truncated: decoded {decoded} of {declared_samples} samples",
            )
        return (
            TIER_SUSPECT,
            f"trailing data after {decoded} samples (audio intact, not byte-clean)",
        )

    if rc != 0:
        return TIER_CORRUPT, (lines[0] if lines else f"decoder exit code {rc}")

    if not lines:
        return TIER_OK, "decode ok"

    # Completed decode with complaints. METADATA only if every line is a known
    # tag-parse or benign-decoder marker; an unknown line is treated as SUSPECT
    # rather than hidden.
    if all(_matches(ln, _METADATA_MARKERS + _BENIGN_MARKERS) for ln in lines):
        return TIER_METADATA, "tag/benign decoder warnings only; audio decodes"
    n = len(lines)
    return TIER_SUSPECT, f"{n} decoder warning{'s' if n != 1 else ''}; decoded to end"


# =====================================
# Mode: FLAC integrity
# =====================================


def _flac_declared_samples(filepath: str) -> int | None:
    """Total sample count from the FLAC STREAMINFO, used to tell a truncated
    file (decoded < declared) from a trailing-junk tail (decoded >= declared)."""
    try:
        from mutagen.flac import FLAC

        return FLAC(filepath).info.total_samples or None
    except Exception:
        return None


# ffmpeg's format autodetection can mis-probe a valid file (e.g. an MP3 with a
# large ID3v2 tag scored as RIFF), reporting a bogus decode failure. Forcing the
# demuxer from the extension sidesteps that. Files outside this map fall back to
# autodetection.
_FFMPEG_DEMUXER = {
    ".mp3": "mp3",
    ".opus": "ogg",
    ".ogg": "ogg",
    ".flac": "flac",
    ".wav": "wav",
    ".wma": "asf",
    ".m4a": "mov",
}


def _flac_verdict(
    filepath: str, *, use_flac: bool, ffmpeg_path: str | None
) -> tuple[str, str, str]:
    """Return (tool, tier, reason) for one FLAC. libFLAC is authoritative when
    available (its message carries the decoded sample count); ffmpeg is the
    fallback when flac is absent."""
    declared = _flac_declared_samples(filepath)
    if use_flac:
        rc, out, err = run_proc(["flac", "-t", "-s", str(filepath)])
        tier, reason = classify_decode(rc, err or out, declared)
        return "flac", tier, reason
    rc, stderr = _ffmpeg_decode_check(ffmpeg_path or "ffmpeg", Path(filepath))
    tier, reason = classify_decode(rc, stderr, declared)
    return "ffmpeg", tier, reason


# =====================================
# Progress persistence (--resume, FLAC/MP3)
# =====================================

# The state file exists only while the last scan of its report is unfinished:
# every scan writes through as it goes, a completed scan deletes it, and
# --resume reads it back so the rerun scans only the remainder. It is a
# transient cache beside the report, not an index; the filesystem stays the
# source of truth.

_PROGRESS_FORMAT = 1
_FLUSH_EVERY = 25


def _progress_file(output: str) -> Path:
    """State path for a scan's report: <output>.progress.json."""
    out = Path(output).expanduser()
    return out.with_name(out.name + ".progress.json")


def _load_progress(path: Path, scan_id: dict) -> dict[str, dict] | None:
    """Cached per-file results for this exact scan configuration, or None when
    there is nothing usable: no state file, a different configuration (so the
    verdicts would not be comparable), or state corrupted by the interrupt."""
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError, ValueError:
        return None
    if not isinstance(data, dict) or data.get("format") != _PROGRESS_FORMAT:
        return None
    if data.get("scan") != scan_id:
        return None
    results = data.get("results")
    if not isinstance(results, dict) or not results:
        return None
    return results


def _save_progress(path: Path, scan_id: dict, results: dict[str, dict]) -> None:
    """Atomically write the state so an interrupt keeps every flushed verdict.
    Best-effort: persistence must never fail the scan itself."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(
                {"format": _PROGRESS_FORMAT, "scan": scan_id, "results": results}, fh
            )
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _clear_progress(path: Path) -> None:
    """The scan finished; there is nothing left to resume."""
    try:
        path.unlink()
    except OSError:
        pass


def run_flac_mode(
    root: str | list[str],
    output: str,
    workers: int,
    prefer: str,
    *,
    ffmpeg: str | None = None,
    resume: bool = False,
    quiet: bool = False,
) -> int:
    roots = as_roots(root)
    flacs = _find_files_by_ext_path(roots, ".flac")
    total = len(flacs)

    if total == 0:
        if not quiet:
            print(f"No FLAC files found under: {', '.join(roots)}")
        return 0

    # An explicit --ffmpeg path wins over PATH lookup, same as the decode modes.
    ffmpeg_path = _find_ffmpeg(ffmpeg)
    have_flac = has_tool("flac")
    have_ffmpeg = ffmpeg_path is not None
    if not (have_flac or have_ffmpeg):
        if not quiet:
            print("ERROR: Neither 'flac' nor 'ffmpeg' found in PATH.", file=sys.stderr)
        return 2

    # libFLAC is preferred and authoritative; ffmpeg is the fallback.
    use_flac = (not have_ffmpeg) if prefer == "ffmpeg" else have_flac
    if not use_flac and prefer != "ffmpeg" and not quiet:
        print(
            "[warn] 'flac' not found; using ffmpeg for FLAC verification. "
            "ffmpeg's decoder is stricter and may flag valid files.",
            file=sys.stderr,
        )
    if prefer == "ffmpeg" and use_flac and not quiet:
        # An explicit preference that cannot be honored is reported, never
        # silent: the two decoders classify differently, so the switch is
        # a change in verdicts, not just in tools.
        where = f" at {ffmpeg}" if ffmpeg else ""
        print(
            f"[warn] --prefer ffmpeg but no usable ffmpeg was found{where}; "
            "falling back to flac.",
            file=sys.stderr,
        )

    # Persistence is always write-through; --resume only controls whether a
    # previous run's verdicts are reused. A verdict is reused only when the
    # verification tool is the same one.
    pfile = _progress_file(output)
    scan_id = {"kind": "flac", "use_flac": use_flac}
    state: dict[str, dict] = {}
    cached: dict[str, dict] = {}
    if resume:
        cached = _load_progress(pfile, scan_id) or {}
        if cached and not quiet:
            print(
                f"Resuming: {len(cached)} previously verified file(s) reused "
                f"from {pfile.name}"
            )
    pending = [p for p in flacs if str(p) not in cached]

    if not quiet:
        print(f"Found {total} FLAC files under: {', '.join(roots)}")

    counts = {tier: 0 for tier in TIER_ORDER}
    flagged: list[tuple[str, str, str, str]] = []  # (path, tool, tier, reason)
    # .get, not []: the state file is best-effort by contract, so a record
    # damaged by the interrupt degrades to an OK row instead of crashing the
    # resumed scan (the decode path below makes the same trade).
    for path_s, rec in sorted(cached.items()):
        rec_tier = rec.get("tier", TIER_OK)
        counts[rec_tier] = counts.get(rec_tier, 0) + 1
        if rec_tier in (TIER_CORRUPT, TIER_SUSPECT):
            flagged.append(
                (path_s, rec.get("tool", "?"), rec_tier, rec.get("reason", ""))
            )

    def worker(path: Path) -> tuple[str, str, str, str]:
        try:
            tool, tier, reason = _flac_verdict(
                str(path), use_flac=use_flac, ffmpeg_path=ffmpeg_path
            )
            return str(path), tool, tier, reason
        except KeyboardInterrupt:
            raise
        except Exception as e:
            return str(path), "exception", TIER_CORRUPT, repr(e)

    pbar = _make_pbar(total, "Testing FLACs", quiet)
    if cached:
        pbar.update(len(cached))
    ex: ThreadPoolExecutor | None = None
    futures: dict = {}
    try:
        ex = ThreadPoolExecutor(max_workers=max(1, workers))
        futures = {ex.submit(worker, p): p for p in pending}
        for fut in as_completed(futures):
            path, tool, tier, reason = fut.result()
            state[path] = {"tool": tool, "tier": tier, "reason": reason}
            counts[tier] = counts.get(tier, 0) + 1
            if tier in (TIER_CORRUPT, TIER_SUSPECT):
                flagged.append((path, tool, tier, reason))
            pbar.update(1)
            if len(state) % _FLUSH_EVERY == 0:
                _save_progress(pfile, scan_id, state)
    except KeyboardInterrupt:
        if not quiet:
            print("\nInterrupted by user. Cancelling FLAC checks...")
        if ex is not None:
            for f in futures:
                f.cancel()
            ex.shutdown(cancel_futures=True)
        _save_progress(pfile, scan_id, state)
        return 130
    finally:
        if ex is not None:
            ex.shutdown(wait=True)
        pbar.close()

    out_path = os.path.abspath(output)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("FLAC INTEGRITY REPORT\n")
        f.write(f"Root: {', '.join(roots)}\n")
        f.write(
            f"Scanned: {total}  OK: {counts[TIER_OK]}  "
            f"Metadata: {counts[TIER_METADATA]}  "
            f"Suspect: {counts[TIER_SUSPECT]}  Corrupt: {counts[TIER_CORRUPT]}\n"
        )
        f.write("=" * 60 + "\n\n")
        for tier in (TIER_CORRUPT, TIER_SUSPECT):
            rows = sorted((r for r in flagged if r[2] == tier), key=lambda r: r[0])
            if not rows:
                continue
            f.write(f"{tier} ({len(rows)})\n")
            f.write("-" * 40 + "\n")
            for i, (path, tool, _tier, reason) in enumerate(rows, 1):
                rel = relpath_under(path, roots)
                f.write(f"  {i:>3}. {rel}\n")
                f.write(f"       Tool: {tool}\n")
                f.write(f"       {reason}\n\n")

    if not quiet:
        if counts[TIER_CORRUPT] or counts[TIER_SUSPECT]:
            corrupt_s = f"Corrupt: {counts[TIER_CORRUPT]}"
            suspect_s = f"Suspect: {counts[TIER_SUSPECT]}"
            if counts[TIER_CORRUPT]:
                corrupt_s = red(corrupt_s)
            if counts[TIER_SUSPECT]:
                suspect_s = yellow(suspect_s)
            print(f"Scanned {total}. {corrupt_s}  {suspect_s}. Details: {out_path}")
        else:
            print(green("✅ All FLAC files passed integrity checks."))
    _clear_progress(pfile)
    return 1 if counts[TIER_CORRUPT] > 0 else 0


# =====================================
# Mode: MP3 decode check
# =====================================


def _find_ffmpeg(explicit_path: str | None) -> str | None:
    if explicit_path:
        p = Path(explicit_path)
        return str(p) if p.exists() else None
    return shutil.which("ffmpeg")


def _find_files_by_ext_path(roots, ext: str) -> list[Path]:
    """Collect files matching `ext` across one or more roots (each a directory or
    a single file), as Path objects. Roots are normalized to absolute paths so a
    later relpath against the same roots lines up.

    Hidden directories are pruned to match utils.iter_audio_dirs: every other
    mode skips them, so an integrity scan was the one place a `.testing/` copy
    of an album got decoded. Directories and filenames are walked in sorted
    order so the same library yields the same file list twice running."""
    out: list[Path] = []
    for r in as_roots(roots):
        p = Path(r)
        if p.is_file():
            if p.suffix.lower() == ext:
                out.append(p)
            continue
        for dirpath, dirs, files in os.walk(r):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for fn in sorted(files):
                if os.path.splitext(fn)[1].lower() == ext:
                    out.append(Path(dirpath) / fn)
    return out


def _mutagen_header_info(path: Path) -> dict[str, Any]:
    if not HAVE_MUTAGEN_MP3:
        return {}
    try:
        audio = MUTAGEN_MP3(path)
        info = getattr(audio, "info", None)
        if not info:
            return {}
        return {
            "duration_s": round(getattr(info, "length", 0.0) or 0.0, 3),
            "bitrate_kbps": int((getattr(info, "bitrate", 0) or 0) / 1000),
            "sample_rate_hz": getattr(info, "sample_rate", None),
            "mode": getattr(info, "mode", None),
            # mutagen's BitrateMode is a custom int-enum without .name; str()
            # gives "BitrateMode.CBR", so take the member after the dot.
            # __class__.__name__ rendered every mode as the literal
            # "BitrateMode". UNKNOWN (0) is falsy and stays None.
            "vbr_mode": str(bm).rpartition(".")[2]
            if (bm := getattr(info, "bitrate_mode", None))
            else None,
        }
    except Exception:
        return {}


def _ffmpeg_decode_check(ffmpeg_path: str, path: Path) -> tuple[int, str]:
    """Run a full decode and return (returncode, stderr). Judgment is left to
    classify_decode; this only produces the raw signal."""
    cmd = [ffmpeg_path, "-v", "error", "-nostats", "-hide_banner"]
    # Force the demuxer from the extension so format autodetection can't
    # mis-probe a valid file and report a false failure.
    demuxer = _FFMPEG_DEMUXER.get(path.suffix.lower())
    if demuxer:
        cmd += ["-f", demuxer]
    # -vn drops non-audio streams (e.g. an embedded cover) so a malformed
    # picture is never mistaken for an audio fault.
    cmd += ["-i", str(path), "-vn", "-f", "null", "-"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as e:
        return -1, f"FFmpeg invocation failed: {e!r}"
    return proc.returncode, (proc.stderr or "").strip()


def _scan_one_file(
    path: Path, ffmpeg_path: str | None, *, enrich: bool = False
) -> dict[str, Any]:
    """Scan a single audio file for decode errors. If enrich=True, also pull
    mutagen header info (bitrate, duration, sample rate, VBR mode)."""
    row: dict[str, Any] = {
        "path": str(path),
        "size_bytes": None,
        "tier": TIER_OK,
        "reason": "decode ok",
    }
    if enrich:
        row.update(
            {
                "duration_s": None,
                "bitrate_kbps": None,
                "sample_rate_hz": None,
                "mode": None,
                "vbr_mode": None,
            }
        )

    try:
        row["size_bytes"] = path.stat().st_size
    except Exception as e:
        row["tier"] = TIER_CORRUPT
        row["reason"] = f"stat failed: {e!r}"
        return row

    if enrich:
        row.update({k: v for k, v in _mutagen_header_info(path).items() if k in row})

    if not ffmpeg_path:
        # Cannot assess the audio without a decoder; do not flag it.
        row["reason"] = "decode check skipped (ffmpeg unavailable)"
        return row

    rc, stderr = _ffmpeg_decode_check(ffmpeg_path, path)
    row["tier"], row["reason"] = classify_decode(rc, stderr)
    return row


def _format_row_meta(row: dict[str, Any]) -> str:
    """Format metadata fields into a compact summary string."""
    parts: list[str] = []
    if row.get("bitrate_kbps"):
        parts.append(f"{row['bitrate_kbps']}kbps")
    if row.get("sample_rate_hz"):
        parts.append(f"{row['sample_rate_hz']}Hz")
    if row.get("duration_s"):
        parts.append(f"{row['duration_s']}s")
    if row.get("vbr_mode"):
        parts.append(row["vbr_mode"])
    return "  ".join(parts)


def _run_decode_scan(
    root: str | list[str],
    output: str,
    workers: int,
    ffmpeg: str | None,
    *,
    ext: str,
    report_title: str,
    default_output: str,
    enrich: bool,
    only_errors: bool,
    verbose: bool,
    quiet: bool,
    resume: bool = False,
) -> int:
    """Unified decode-check scanner for the ffmpeg-decoded formats (MP3, Opus,
    WAV, WMA). Only the MP3 entry point exposes --resume; the other formats
    never forward it."""
    roots = as_roots(root)
    ffmpeg_path = _find_ffmpeg(ffmpeg)

    if not ffmpeg_path:
        # No decoder means zero decodes can run. Proceeding once graded
        # every file OK ("decode check skipped") with exit 0, so a typo'd
        # --ffmpeg path produced an all-clean report that verified nothing;
        # refuse instead.
        if not quiet:
            print(
                f"ERROR: FFmpeg not found. Required for {ext.strip('.')} decode "
                "testing. Install it or pass --ffmpeg /path/to/ffmpeg",
                file=sys.stderr,
            )
        return 2

    targets = _find_files_by_ext_path(roots, ext)

    if not targets:
        if not quiet:
            print(f"No {ext} files found.", file=sys.stderr)
        return 0

    label = ext.strip(".").upper()
    started = time.time()
    counts = {tier: 0 for tier in TIER_ORDER}
    results: list[dict[str, Any]] = []

    # --verbose overrides --quiet before the progress bar is built, so the two
    # flags together don't produce a barless run that still prints the summary.
    if verbose:
        quiet = False

    # Same contract as the FLAC mode: write-through always, --resume only
    # decides whether previous verdicts are reused, and the state is deleted
    # when a scan completes.
    pfile = _progress_file(output)
    scan_id = {"kind": ext, "ffmpeg": ffmpeg_path}
    state: dict[str, dict] = {}
    cached: dict[str, dict] = {}
    if resume:
        cached = _load_progress(pfile, scan_id) or {}
        if cached and not quiet:
            print(
                f"Resuming: {len(cached)} previously scanned file(s) reused "
                f"from {pfile.name}"
            )
    pending = [p for p in targets if str(p) not in cached]

    pbar = _make_pbar(len(targets), f"Scanning {label}", quiet)
    if cached:
        pbar.update(len(cached))
    ex: ThreadPoolExecutor | None = None
    futures: dict = {}
    # CORRUPT and SUSPECT are always listed; METADATA and OK only when the user
    # asks (keeps a clean library's report short and bounds memory on big runs).
    list_benign = verbose or not only_errors

    # Cached verdicts fold into the counts unconditionally; their report rows
    # follow the same list_benign rule as fresh ones.
    for path_s, rec in sorted(cached.items()):
        rec_tier = rec.get("tier", TIER_OK)
        counts[rec_tier] = counts.get(rec_tier, 0) + 1
        row = dict(rec)
        row["path"] = path_s
        if rec_tier in (TIER_CORRUPT, TIER_SUSPECT) or list_benign:
            results.append(row)

    try:
        ex = ThreadPoolExecutor(max_workers=max(1, workers))
        futures = {
            ex.submit(_scan_one_file, p, ffmpeg_path, enrich=enrich): p for p in pending
        }

        for fut in as_completed(futures):
            row = fut.result()
            tier = row.get("tier", TIER_OK)
            state[row["path"]] = row
            counts[tier] = counts.get(tier, 0) + 1
            if tier in (TIER_CORRUPT, TIER_SUSPECT) or list_benign:
                results.append(row)
            pbar.update(1)
            if len(state) % _FLUSH_EVERY == 0:
                _save_progress(pfile, scan_id, state)

    except KeyboardInterrupt:
        if not quiet:
            print(f"\nInterrupted by user. Cancelling {label} scan…", file=sys.stderr)
        if ex is not None:
            for f in futures:
                f.cancel()
            ex.shutdown(cancel_futures=True)
        _save_progress(pfile, scan_id, state)
        return 130
    finally:
        if ex is not None:
            ex.shutdown(wait=True)
        pbar.close()

    elapsed = time.time() - started
    out_path = Path(output or default_output).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def _section(
        handle, tier: str, rows: list[dict[str, Any]], *, compact: bool = False
    ):
        if not rows:
            return
        handle.write(f"{tier} ({len(rows)})\n")
        handle.write("-" * 40 + "\n")
        # Sorted by path: rows arrive in as_completed order, so without this two
        # scans of an unchanged library produced differently-ordered reports and
        # could not be diffed. (run_flac_mode already sorted its sections.)
        for r in sorted(rows, key=lambda row: row["path"]):
            rel = relpath_under(r["path"], roots)
            meta = _format_row_meta(r) if enrich else ""
            if compact:
                handle.write(f"  {rel}{('  [' + meta + ']') if meta else ''}\n")
                continue
            handle.write(f"  {rel}\n")
            if r.get("reason"):
                handle.write(f"    {r['reason']}\n")
            if meta:
                handle.write(f"    {meta}\n")
            handle.write("\n")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"{report_title}\n")
        f.write(f"Root: {', '.join(roots)}\n")
        f.write(
            f"Scanned: {len(targets)}  OK: {counts[TIER_OK]}  "
            f"Metadata: {counts[TIER_METADATA]}  Suspect: {counts[TIER_SUSPECT]}  "
            f"Corrupt: {counts[TIER_CORRUPT]}\n"
        )
        f.write(f"Elapsed: {elapsed:.1f}s\n")
        if not list_benign and (counts[TIER_METADATA] or counts[TIER_OK]):
            f.write("(METADATA and OK omitted; re-run with --verbose to list them)\n")
        f.write("=" * 60 + "\n\n")

        by_tier = {t: [r for r in results if r["tier"] == t] for t in TIER_ORDER}
        _section(f, TIER_CORRUPT, by_tier[TIER_CORRUPT])
        _section(f, TIER_SUSPECT, by_tier[TIER_SUSPECT])
        if list_benign:
            _section(f, TIER_METADATA, by_tier[TIER_METADATA])
            _section(f, TIER_OK, by_tier[TIER_OK], compact=True)

    if not quiet:
        print(f"\nScanned: {len(targets)} files in {elapsed:.1f}s")
        suspect_s = f"suspect: {counts[TIER_SUSPECT]}"
        corrupt_s = f"corrupt: {counts[TIER_CORRUPT]}"
        if counts[TIER_SUSPECT]:
            suspect_s = yellow(suspect_s)
        if counts[TIER_CORRUPT]:
            corrupt_s = red(corrupt_s)
        print(
            f"{green('ok: ' + str(counts[TIER_OK]))}  "
            f"metadata: {counts[TIER_METADATA]}  {suspect_s}  {corrupt_s}"
        )
        print(f"Report written to: {out_path}")
    _clear_progress(pfile)
    return 1 if counts[TIER_CORRUPT] > 0 else 0


def run_mp3_mode(
    root: str | list[str],
    output: str,
    workers: int,
    ffmpeg: str | None,
    *,
    only_errors: bool,
    verbose: bool,
    quiet: bool,
    resume: bool = False,
) -> int:
    return _run_decode_scan(
        root,
        output,
        workers,
        ffmpeg,
        ext=".mp3",
        report_title="MP3 INTEGRITY REPORT",
        default_output=DEFAULT_MP3_OUTPUT,
        enrich=True,
        only_errors=only_errors,
        verbose=verbose,
        quiet=quiet,
        resume=resume,
    )


def run_opus_mode(
    root: str | list[str],
    output: str,
    workers: int,
    ffmpeg: str | None,
    *,
    only_errors: bool,
    verbose: bool,
    quiet: bool,
) -> int:
    return _run_decode_scan(
        root,
        output,
        workers,
        ffmpeg,
        ext=".opus",
        report_title="OPUS INTEGRITY REPORT",
        default_output=DEFAULT_OPUS_OUTPUT,
        enrich=False,
        only_errors=only_errors,
        verbose=verbose,
        quiet=quiet,
    )


def run_wav_mode(
    root: str | list[str],
    output: str,
    workers: int,
    ffmpeg: str | None,
    *,
    only_errors: bool,
    verbose: bool,
    quiet: bool,
) -> int:
    return _run_decode_scan(
        root,
        output,
        workers,
        ffmpeg,
        ext=".wav",
        report_title="WAV INTEGRITY REPORT",
        default_output=DEFAULT_WAV_OUTPUT,
        enrich=False,
        only_errors=only_errors,
        verbose=verbose,
        quiet=quiet,
    )


def run_wma_mode(
    root: str | list[str],
    output: str,
    workers: int,
    ffmpeg: str | None,
    *,
    only_errors: bool,
    verbose: bool,
    quiet: bool,
) -> int:
    return _run_decode_scan(
        root,
        output,
        workers,
        ffmpeg,
        ext=".wma",
        report_title="WMA INTEGRITY REPORT",
        default_output=DEFAULT_WMA_OUTPUT,
        enrich=False,
        only_errors=only_errors,
        verbose=verbose,
        quiet=quiet,
    )
