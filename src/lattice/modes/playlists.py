import ast
import operator
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lattice.config import DEFAULT_PLAYLIST_CHECK_OUTPUT
from lattice.tags import get_all_tags
from lattice.utils import (
    _make_pbar,
    as_roots,
    count_audio_files,
    is_audio,
    iter_audio_dirs,
    parse_layout,
    relpath_under,
)

# =====================================
# Mode: Playlist generation (.m3u)
# =====================================


# Smart-playlist rules are evaluated by walking a whitelisted AST, never by
# eval(). eval with `{"__builtins__": {}}` is NOT a sandbox — a rule like
# `genre.__class__.__mro__[-1].__subclasses__()` escapes it to arbitrary code.
# Here only comparisons, boolean/arithmetic operators, the exposed field names,
# and literal constants are allowed; attribute access, calls, and subscripts
# raise, so a rule can read the fields and nothing else.
_CMP_OPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}
_BIN_OPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
}
_UNARY_OPS: dict[type, Callable[[Any], Any]] = {
    ast.Not: operator.not_,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


class RuleError(Exception):
    """A rule referenced an unknown field or used an unsupported construct."""


def _eval_node(node: ast.AST, names: dict):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, names)
    if isinstance(node, ast.BoolOp):
        vals = [_eval_node(v, names) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand, names))
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](
            _eval_node(node.left, names), _eval_node(node.right, names)
        )
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, names)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            if type(op) not in _CMP_OPS:
                raise RuleError(f"operator {type(op).__name__} not allowed")
            right = _eval_node(comparator, names)
            if not _CMP_OPS[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Name):
        if node.id in names:
            return names[node.id]
        raise RuleError(f"unknown field '{node.id}'")
    if isinstance(node, ast.Constant):
        return node.value
    raise RuleError(f"unsupported expression: {type(node).__name__}")


# SQL-style AND/OR are folded to Python's and/or, but only outside quoted
# strings — a plain str.replace would corrupt a literal like 'Drum AND Bass'.
# re.split with a capturing group keeps the quoted segments at odd indices.
_QUOTED_SEGMENT = re.compile(r"('[^']*'|\"[^\"]*\")")
_SQL_AND = re.compile(r"\bAND\b")
_SQL_OR = re.compile(r"\bOR\b")


def _pythonize_rule(rule: str) -> str:
    parts = _QUOTED_SEGMENT.split(rule)
    for i in range(0, len(parts), 2):
        parts[i] = _SQL_OR.sub("or", _SQL_AND.sub("and", parts[i]))
    return "".join(parts)


def validate_rule(rule: str) -> str | None:
    """One-shot check of a smart rule against dummy metadata, so a rule that
    can never evaluate (syntax error, unknown field, type mismatch) is one
    error before the walk instead of one stderr line per track. Returns the
    error message, or None when the rule is usable."""
    if not rule or not rule.strip():
        return None
    names = {
        "rating": 0.0,
        "genre": "",
        "artist": "",
        "album": "",
        "title": "",
        "duration": 0.0,
        "bitrate": 0,
    }
    try:
        _eval_node(ast.parse(_pythonize_rule(rule), mode="eval"), names)
    except ZeroDivisionError:
        # Dividing by the dummy zeros is a data-dependent outcome, not a
        # structural error: `bitrate / duration > 200` is a valid rule (real
        # tracks never carry duration 0; a per-track failure still reports).
        return None
    except Exception as e:
        return str(e)
    return None


def _evaluate_rule(rule: str, t, parsed_layout: dict) -> bool:
    """Evaluate a dynamic smart playlist rule against a track's metadata, using
    a restricted AST walker (see _eval_node) rather than eval()."""
    if not rule or not rule.strip():
        return True

    names = {
        "rating": t.rating or 0.0,
        "genre": t.genre or "",
        "artist": t.artist or parsed_layout.get("artist", ""),
        "album": t.album or parsed_layout.get("album", ""),
        "title": t.title or parsed_layout.get("title", ""),
        "duration": t.duration_s or 0.0,
        "bitrate": t.bitrate_kbps or 0,
    }

    try:
        # Accept SQL-style AND/OR as a convenience for Python's and/or.
        return bool(_eval_node(ast.parse(_pythonize_rule(rule), mode="eval"), names))
    except Exception as e:
        print(f"Error evaluating rule '{rule}': {e}", file=sys.stderr)
        return False


def generate_playlist(
    root_dir: str | list[str],
    output_file: str,
    rule: str,
    layout: str = "{artist}/{album}",
    quiet: bool = False,
) -> int:
    """Generate an .m3u playlist based on a smart rule filter."""
    rule_error = validate_rule(rule)
    if rule_error is not None:
        print(f"Invalid rule '{rule}': {rule_error}", file=sys.stderr)
        return 1

    roots = as_roots(root_dir)
    total_files = count_audio_files(roots)

    if total_files == 0:
        if not quiet:
            print(f"No audio files found under: {', '.join(roots)}")
        return 0

    if not quiet:
        print(f"Scanning {total_files} files for playlist generation...")

    pbar = _make_pbar(total_files, "Building playlist", quiet)

    playlist_entries: list[str] = []

    for src_root, dirpath, _dirs, files in iter_audio_dirs(roots):
        # Sort files to keep album tracks in order
        audio = sorted(f for f in files if is_audio(f))
        if not audio:
            continue

        # Read serially, like _scan_album_dirs: a thread pool here measured
        # worse, not better (mutagen's parsing holds the GIL, so only the file
        # opens overlap and the per-task handoff dominates).
        for f in audio:
            filepath = os.path.join(dirpath, f)
            rel_path = os.path.relpath(filepath, src_root)
            parsed = parse_layout(rel_path, layout)
            t = get_all_tags(filepath)

            if _evaluate_rule(rule, t, parsed):
                # For .m3u, we can write #EXTINF if we have duration and title
                duration = int(t.duration_s) if t.duration_s else -1
                artist = t.artist or parsed.get("artist", "Unknown")
                title = t.title or f
                display = f"{artist} - {title}" if artist != "Unknown" else title

                playlist_entries.append(f"#EXTINF:{duration},{display}")
                # Use absolute paths for the playlist
                playlist_entries.append(filepath)

            pbar.update(1)

    pbar.close()

    if not playlist_entries:
        if not quiet:
            print(f"No tracks matched the rule: {rule}")
        return 0

    out_path = os.path.abspath(output_file)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            f.writelines(f"{entry}\n" for entry in playlist_entries)
    except OSError as e:
        print(f"Failed to write playlist: {e}", file=sys.stderr)
        return 1

    if not quiet:
        track_count = len(playlist_entries) // 2
        print(f"\nWrote playlist with {track_count} tracks to: {out_path}")

    return 0


# =====================================
# Mode: Playlist check
# =====================================

PLAYLIST_EXTENSIONS = (".m3u", ".m3u8")


def _find_playlists(roots: list[str]) -> list[Path]:
    """Every .m3u/.m3u8 under the roots (or a single playlist passed as a
    root), walked sorted with hidden directories pruned like the audio walk."""
    out: list[Path] = []
    for r in roots:
        p = Path(r)
        if p.is_file():
            if p.suffix.lower() in PLAYLIST_EXTENSIONS:
                out.append(p)
            continue
        for dirpath, dirs, files in os.walk(r):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for fn in sorted(files):
                if os.path.splitext(fn)[1].lower() in PLAYLIST_EXTENSIONS:
                    out.append(Path(dirpath) / fn)
    return out


def check_playlist(path: Path) -> tuple[bool, int, int, list[tuple[int, str]]]:
    """One playlist -> (has_extm3u, n_entries, n_missing, [(line_no, detail)]).
    Comment and directive lines (#) are skipped; every other non-blank line is
    an entry, resolved against the playlist's own directory so hand-made
    relative playlists check the same as the absolute ones --playlist writes.
    Missing detection is os.path.exists, nothing more: a path that exists but
    is unreadable is not this mode's finding."""
    has_header = False
    entries = 0
    missing: list[tuple[int, str]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line_no, raw in enumerate(f, 1):
                line = raw.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    if line.upper().startswith("#EXTM3U"):
                        has_header = True
                    continue
                entries += 1
                target = Path(line)
                if not target.is_absolute():
                    target = (path.parent / target).resolve()
                if not os.path.exists(target):
                    missing.append((line_no, str(target)))
    except OSError as e:
        missing.append((0, f"unreadable playlist: {e}"))
    return has_header, entries, len(missing), missing


def run_check_playlists(
    root: str | list[str],
    output: str,
    *,
    verbose: bool = False,
    quiet: bool = False,
) -> int:
    """Verify the library's playlists against the filesystem. --playlist
    writes absolute-path .m3us that the movers (clean, genre_foldermap,
    flac2opus) routinely orphan; this walks the roots for playlists and
    reports, per playlist, the entries whose target no longer exists plus a
    missing #EXTM3U header. Read-only; --verbose also lists the clean ones."""
    roots = as_roots(root)
    playlists = _find_playlists(roots)

    if not quiet:
        print(f"Checking playlists under: {', '.join(roots)}")

    results: list[tuple[Path, bool, int, int, list[tuple[int, str]]]] = []
    for p in playlists:
        has_header, n_entries, n_missing, missing = check_playlist(p)
        results.append((p, has_header, n_entries, n_missing, missing))

    n_entries = sum(r[2] for r in results)
    n_missing = sum(r[3] for r in results)
    n_noheader = sum(1 for r in results if not r[1] and r[2] + r[3] > 0)
    dirty = [r for r in results if r[3] or (not r[1] and r[2] + r[3] > 0)]

    out_path = os.path.abspath(output or DEFAULT_PLAYLIST_CHECK_OUTPUT)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("PLAYLIST CHECK REPORT\n")
        f.write(f"Root: {', '.join(roots)}\n")
        f.write(
            f"Playlists: {len(playlists)}   Entries: {n_entries}   "
            f"Missing targets: {n_missing}   Without #EXTM3U: {n_noheader}\n"
        )
        f.write("=" * 64 + "\n\n")

        if not results:
            f.write("No .m3u/.m3u8 playlists found under the root(s).\n\n")
        for p, has_header, n_entries_p, n_missing_p, missing in dirty:
            f.write(
                f"{relpath_under(str(p), roots)} ({n_missing_p} of {n_entries_p} missing)\n"
            )
            if not has_header:
                f.write("    no #EXTM3U header line\n")
            for line_no, detail in missing:
                where = f"line {line_no}: " if line_no else ""
                f.write(f"    {where}{detail}\n")
            f.write("\n")
        clean = [r for r in results if r not in dirty]
        if verbose:
            f.write(f"CLEAN ({len(clean)})\n")
            f.write("-" * 40 + "\n")
            for p, _h, n_entries_p, n_missing_p, _m in clean:
                f.write(f"  {relpath_under(str(p), roots)} ({n_entries_p} entries)\n")
            f.write("\n")

    if not quiet:
        print(f"\nChecked {len(playlists)} playlist(s), {n_entries} entries.")
        print(f"  Missing targets: {n_missing}")
        print(f"  Without #EXTM3U: {n_noheader}")
        print(f"Results written to: {out_path}")

    return 0
