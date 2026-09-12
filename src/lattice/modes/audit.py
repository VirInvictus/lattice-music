import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Sequence
from difflib import SequenceMatcher
from typing import NamedTuple

from lattice.config import (
    AUDIO_EXTENSIONS,
    DEFAULT_BITRATE_AUDIT_OUTPUT,
    DEFAULT_DUPLICATES_OUTPUT,
    DEFAULT_REPLAYGAIN_AUDIT_OUTPUT,
    DEFAULT_STRAY_AUDIT_OUTPUT,
    DEFAULT_TAG_AUDIT_OUTPUT,
    get_layout,
)
from lattice.norm import QUOTE_DASH_FOLD as _NORM_QUOTE_DASH_FOLD
from lattice.tags import HAVE_MUTAGEN_BASE, TagBundle, read_replaygain
from lattice.utils import (
    _make_pbar,
    as_roots,
    count_audio_files,
    is_audio,
    iter_audio_dirs,
    map_concurrent,
    read_tags_concurrent,
    relpath_under,
)

# =====================================
# Mode: Duplicate detection
# =====================================

# The duplicate key folds quote/dash variants through the shared rules engine
# (lattice.norm, promoted from the cleaner in 5.0.0; this file used to carry a
# hand-mirrored copy of the table).
_QUOTE_DASH_FOLD = _NORM_QUOTE_DASH_FOLD

_WS_RUN = re.compile(r"\s+")
_PAREN_TAIL = re.compile(r"\s*[\(\[][^\(\[\)\]]*[\)\]]\s*$")
_FEAT = re.compile(r"\s+(?:feat\.?|featuring|ft\.?)\s+.+$", re.IGNORECASE)


def _norm_key(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    for k, v in _QUOTE_DASH_FOLD.items():
        s = s.replace(k, v)
    return _WS_RUN.sub(" ", s).strip().lower()


def _loose_key(s: str | None) -> str:
    """`_norm_key` plus stripping of trailing parentheticals and 'feat.' clauses;
    used only for fuzzy similarity matching, not exact lookup."""
    s = _norm_key(s)
    if not s:
        return ""
    s = _FEAT.sub("", s)
    while True:
        new = _PAREN_TAIL.sub("", s).strip()
        if new == s:
            break
        s = new
    return s


class _DirInfo(NamedTuple):
    path: str
    artist: str
    album: str
    norm_artist: str
    norm_album: str
    loose_album: str
    total_bytes: int
    formats: dict[str, int]
    fmt_bitrate: dict[str, int]
    files: list[tuple[str, TagBundle, int]]


def _aggregate_dir(
    dirpath: str, audio_files: list[str], tag_cache: dict[str, TagBundle]
) -> _DirInfo:
    files: list[tuple[str, TagBundle, int]] = []
    artists: Counter = Counter()
    albums: Counter = Counter()
    formats: Counter = Counter()
    fmt_kbps: dict[str, list[int]] = defaultdict(list)
    total_bytes = 0

    for fname in audio_files:
        fpath = os.path.join(dirpath, fname)
        t = tag_cache[fpath]
        try:
            sz = os.path.getsize(fpath)
        except OSError:
            sz = 0
        ext = os.path.splitext(fname)[1].lower()
        total_bytes += sz
        files.append((fname, t, sz))
        formats[ext] += 1
        if t.bitrate_kbps:
            fmt_kbps[ext].append(t.bitrate_kbps)
        if t.artist:
            artists[t.artist] += 1
        if t.album:
            albums[t.album] += 1

    artist = (
        artists.most_common(1)[0][0]
        if artists
        else os.path.basename(os.path.dirname(dirpath))
    )
    album = albums.most_common(1)[0][0] if albums else os.path.basename(dirpath)

    fmt_bitrate = {ext: int(sum(v) / len(v)) for ext, v in fmt_kbps.items() if v}

    return _DirInfo(
        path=dirpath,
        artist=artist,
        album=album,
        norm_artist=_norm_key(artist),
        norm_album=_norm_key(album),
        loose_album=_loose_key(album),
        total_bytes=total_bytes,
        formats=dict(formats),
        fmt_bitrate=fmt_bitrate,
        files=files,
    )


def _fmt_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB"):
        if f < 1024:
            return f"{int(f)} B" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} GB"


def _fmt_duration(secs: float | None) -> str:
    if not secs:
        return "--:--"
    m, s = divmod(int(secs), 60)
    return f"{m}:{s:02d}"


def _fmt_dir_summary(d: _DirInfo, roots: list[str]) -> str:
    rel = relpath_under(d.path, roots)
    parts = []
    for ext in sorted(d.formats):
        count = d.formats[ext]
        br = d.fmt_bitrate.get(ext)
        tag = ext.lstrip(".")
        parts.append(f"{tag}×{count} {br}kbps" if br else f"{tag}×{count}")
    fmt_str = ", ".join(parts)
    return f"       {rel}/  [{fmt_str}]  {_fmt_size(d.total_bytes)}"


def _section_exact(dirs: list[_DirInfo], roots: list[str], out) -> tuple[int, set]:
    groups: dict[tuple[str, str], list[_DirInfo]] = defaultdict(list)
    for d in dirs:
        # Require both keys non-empty: grouping folders by ("metallica", "")
        # would mass-match every album-less folder for that artist.
        if not d.norm_artist or not d.norm_album:
            continue
        groups[(d.norm_artist, d.norm_album)].append(d)
    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    if not dupes:
        out.write("[EXACT ALBUM DUPLICATES]    (none)\n\n")
        return 0, set()
    total_dirs = sum(len(v) for v in dupes.values())
    out.write(
        f"[EXACT ALBUM DUPLICATES]    ({len(dupes)} album(s), {total_dirs} directories)\n\n"
    )
    for i, (_, locs) in enumerate(
        sorted(dupes.items(), key=lambda kv: (kv[0][0], kv[0][1])), 1
    ):
        first = locs[0]
        out.write(f"  {i}. {first.artist} — {first.album}\n")
        for d in sorted(locs, key=lambda x: x.path):
            out.write(_fmt_dir_summary(d, roots) + "\n")
        out.write("\n")
    return len(dupes), set(dupes.keys())


def _section_multiformat(dirs: list[_DirInfo], roots: list[str], out) -> int:
    # Each value: ext -> (filename, size, tag_title, original_stem)
    hits: list[
        tuple[
            _DirInfo,
            dict[
                tuple[int | None, str],
                dict[str, tuple[str, int, str | None, str]],
            ],
        ]
    ] = []
    for d in dirs:
        if len(d.formats) < 2:
            continue
        by_key: dict[
            tuple[int | None, str], dict[str, tuple[str, int, str | None, str]]
        ] = defaultdict(dict)
        for fname, t, sz in d.files:
            ext = os.path.splitext(fname)[1].lower()
            stem = os.path.splitext(fname)[0]
            title_for_key = t.title or stem
            key = (t.trackno, _norm_key(title_for_key))
            by_key[key][ext] = (fname, sz, t.title, stem)
        matched = {k: v for k, v in by_key.items() if len(v) > 1}
        if matched:
            hits.append((d, matched))

    if not hits:
        out.write("[WITHIN-DIRECTORY MULTI-FORMAT]    (none)\n\n")
        return 0

    out.write(f"[WITHIN-DIRECTORY MULTI-FORMAT]    ({len(hits)} directories)\n\n")
    for i, (d, matched) in enumerate(sorted(hits, key=lambda x: x[0].path), 1):
        rel = relpath_under(d.path, roots)
        out.write(f"  {i}. {rel}/\n")
        for (trackno, _title_key), fmts in sorted(
            matched.items(),
            key=lambda x: (x[0][0] if x[0][0] is not None else 9999, x[0][1]),
        ):
            # Prefer a tag title; otherwise fall back to one of the original
            # filename stems (case-preserved), never the lowercased key.
            display_title = (
                next((info[2] for info in fmts.values() if info[2]), None)
                or next(iter(fmts.values()))[3]
            )
            tn = f"{trackno:02d}" if trackno else "--"
            out.write(f"       track {tn}  {display_title}\n")
            for ext in sorted(fmts):
                fname, sz, _, _ = fmts[ext]
                out.write(
                    f"           {ext.lstrip('.'):<5} {fname}  ({_fmt_size(sz)})\n"
                )
        out.write("\n")
    return len(hits)


def _section_similar(
    dirs: list[_DirInfo],
    exact_keys: set,
    roots: list[str],
    out,
    threshold: float = 0.85,
) -> int:
    by_artist: dict[str, list[_DirInfo]] = defaultdict(list)
    for d in dirs:
        if not d.norm_artist:
            continue
        if (d.norm_artist, d.norm_album) in exact_keys:
            continue
        if not d.loose_album:
            continue
        by_artist[d.norm_artist].append(d)

    pairs: list[tuple[float, _DirInfo, _DirInfo]] = []
    for items in by_artist.values():
        if len(items) < 2:
            continue
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                if a.loose_album == b.loose_album:
                    ratio = 1.0
                else:
                    ratio = SequenceMatcher(None, a.loose_album, b.loose_album).ratio()
                if ratio >= threshold:
                    pairs.append((ratio, a, b))

    if not pairs:
        out.write(
            f"[SIMILAR-NAME CANDIDATES]    (threshold ≥ {threshold:.2f}, none)\n\n"
        )
        return 0

    pairs.sort(key=lambda x: (-x[0], x[1].norm_artist, x[1].norm_album))
    out.write(
        f"[SIMILAR-NAME CANDIDATES]    "
        f"(threshold ≥ {threshold:.2f}, {len(pairs)} pair(s))\n\n"
    )
    for i, (ratio, a, b) in enumerate(pairs, 1):
        out.write(f"  {i}. [{ratio:.2f}]  {a.artist}\n")
        out.write(f'       "{a.album}"  ({relpath_under(a.path, roots)})\n')
        out.write(f'       "{b.album}"  ({relpath_under(b.path, roots)})\n')
        out.write("\n")
    return len(pairs)


def _cluster_by_duration(
    entries: Sequence[tuple[_DirInfo, str, TagBundle]], delta: float
) -> list[list[tuple[_DirInfo, str, TagBundle]]]:
    """Partition `entries` into duration-clusters where each cluster's spread
    fits within `delta` seconds. Greedy: a new cluster starts when the next
    entry exceeds `delta` past the cluster's first entry. Entries with no
    duration form one best-effort cluster. Returns only clusters with 2+
    entries spanning 2+ distinct directories — so a studio cluster and a
    live cluster for the same title each surface separately."""
    durs = [(e, e[2].duration_s) for e in entries if e[2].duration_s is not None]
    no_dur = [e for e in entries if e[2].duration_s is None]

    clusters: list[list[tuple[_DirInfo, str, TagBundle]]] = []
    if durs:
        durs.sort(key=lambda x: x[1])
        current = [durs[0][0]]
        anchor = durs[0][1]
        for entry, dur in durs[1:]:
            if dur - anchor <= delta:
                current.append(entry)
            else:
                clusters.append(current)
                current = [entry]
                anchor = dur
        clusters.append(current)
    if len(no_dur) >= 2:
        clusters.append(no_dur)

    return [c for c in clusters if len(c) >= 2 and len({e[0].path for e in c}) >= 2]


def _section_track_dupes(
    dirs: list[_DirInfo], roots: list[str], out, duration_delta: float = 2.0
) -> int:
    track_map: dict[tuple[str, str], list[tuple[_DirInfo, str, TagBundle]]] = (
        defaultdict(list)
    )
    for d in dirs:
        for fname, t, _sz in d.files:
            artist_src = t.artist or d.artist
            title_src = t.title
            if not artist_src or not title_src:
                continue
            key = (_norm_key(artist_src), _norm_key(title_src))
            if not key[0] or not key[1]:
                continue
            track_map[key].append((d, fname, t))

    hits: list[tuple[tuple[str, str], list[tuple[_DirInfo, str, TagBundle]]]] = []
    for key, entries in track_map.items():
        if len({e[0].path for e in entries}) < 2:
            continue
        for cluster in _cluster_by_duration(entries, duration_delta):
            hits.append((key, cluster))

    if not hits:
        out.write(
            f"[TRACK-LEVEL DUPLICATES]    "
            f"(duration delta ≤ {duration_delta:.0f}s, none)\n\n"
        )
        return 0

    hits.sort(key=lambda x: (x[0][0], x[0][1]))
    out.write(
        f"[TRACK-LEVEL DUPLICATES]    "
        f"(duration delta ≤ {duration_delta:.0f}s, {len(hits)} track(s))\n\n"
    )
    for i, (_, entries) in enumerate(hits, 1):
        first_d, first_fname, first_t = entries[0]
        artist_display = first_t.artist or first_d.artist
        title_display = first_t.title or os.path.splitext(first_fname)[0]
        out.write(
            f"  {i}. {artist_display} — {title_display}  ({len(entries)} copies)\n"
        )
        for d, fname, t in sorted(entries, key=lambda e: e[0].path):
            rel = relpath_under(os.path.join(d.path, fname), roots)
            dur = _fmt_duration(t.duration_s)
            br = f"{t.bitrate_kbps}kbps" if t.bitrate_kbps else "--"
            out.write(f"       {rel}    {dur}  {br}\n")
        out.write("\n")
    return len(hits)


def run_duplicates(root: str | list[str], output: str, *, quiet: bool = False) -> int:
    """Detect duplicate albums, within-folder multi-format duplicates, similar
    album names, and track-level cross-library duplicates. Emits a single
    sectioned text report. With several roots, duplicates are detected across
    them (e.g. the same album living in two separate libraries)."""
    if not HAVE_MUTAGEN_BASE:
        print("ERROR: mutagen is required for duplicate detection.", file=sys.stderr)
        return 2

    roots = as_roots(root)
    if not quiet:
        print(f"Scanning for duplicates under: {', '.join(roots)}")

    total = count_audio_files(roots)
    pbar = _make_pbar(total, "Reading tags", quiet)

    dirs: list[_DirInfo] = []
    for _src_root, dirpath, _subdirs, files in iter_audio_dirs(roots):
        audio_files = sorted(f for f in files if is_audio(f))
        if not audio_files:
            continue
        paths = [os.path.join(dirpath, fname) for fname in audio_files]
        # Read this directory's tags concurrently. The dict is local to the
        # iteration, so the library's tags are never held twice — they live on
        # only in each _DirInfo (which the later sections need anyway).
        tags = read_tags_concurrent(paths, pbar=pbar)
        dirs.append(_aggregate_dir(dirpath, audio_files, tags))

    pbar.close()

    out_path = os.path.abspath(output or DEFAULT_DUPLICATES_OUTPUT)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("DUPLICATE REPORT\n")
        f.write(f"Root: {', '.join(roots)}\n")
        f.write(f"Directories: {len(dirs)}    Audio files: {total}\n")
        f.write("=" * 70 + "\n\n")

        exact_count, exact_keys = _section_exact(dirs, roots, f)
        mf_count = _section_multiformat(dirs, roots, f)
        sim_count = _section_similar(dirs, exact_keys, roots, f)
        trk_count = _section_track_dupes(dirs, roots, f)

    if not quiet:
        print(f"\nReport written to: {out_path}")
        print(f"  Exact album duplicates:       {exact_count}")
        print(f"  Within-folder multi-format:   {mf_count}")
        print(f"  Similar-name candidates:      {sim_count}")
        print(f"  Track-level duplicates:       {trk_count}")
    return 0


# =====================================
# Mode: Tag audit
# =====================================


def run_tag_audit(root: str | list[str], output: str, *, quiet: bool = False) -> int:
    """Report audio files missing title, artist, track number, or genre."""
    if not HAVE_MUTAGEN_BASE:
        print("ERROR: mutagen is required for tag auditing.", file=sys.stderr)
        return 2

    roots = as_roots(root)
    issues: list[dict[str, str]] = []

    if not quiet:
        print(f"Auditing tags under: {', '.join(roots)}")

    total = count_audio_files(roots)
    pbar = _make_pbar(total, "Auditing tags", quiet)

    paths = [
        os.path.join(dirpath, f)
        for _src_root, dirpath, _dirs, files in iter_audio_dirs(roots)
        for f in sorted(files)
        if os.path.splitext(f)[1].lower() in AUDIO_EXTENSIONS
    ]
    tags = read_tags_concurrent(paths, pbar=pbar)
    pbar.close()

    for filepath in paths:
        t = tags[filepath]
        ext = os.path.splitext(filepath)[1].lower()

        missing_fields: list[str] = []
        if not t.title:
            missing_fields.append("title")
        if not t.artist:
            missing_fields.append("artist")
        if t.trackno is None:
            missing_fields.append("tracknumber")
        if not t.genre:
            missing_fields.append("genre")

        if missing_fields:
            issues.append(
                {
                    "path": filepath,
                    "format": ext.strip("."),
                    "missing": ", ".join(missing_fields),
                }
            )

    out_path = os.path.abspath(output or DEFAULT_TAG_AUDIT_OUTPUT)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    # Build breakdown counts
    field_counts: Counter = Counter()
    for issue in issues:
        for field in issue["missing"].split(", "):
            field_counts[field] += 1

    # Group issues by directory for readability
    by_dir: dict[str, list[dict[str, str]]] = defaultdict(list)
    for issue in issues:
        parent = os.path.dirname(issue["path"])
        by_dir[parent].append(issue)

    with open(out_path, "w", encoding="utf-8") as out_file:
        out_file.write("TAG AUDIT REPORT\n")
        out_file.write(f"Root: {', '.join(roots)}\n")
        out_file.write(f"Scanned: {total}  Incomplete: {len(issues)}\n")
        if field_counts:
            breakdown = "  ".join(
                f"{field}: {count}" for field, count in field_counts.most_common()
            )
            out_file.write(f"Breakdown: {breakdown}\n")
        out_file.write("=" * 60 + "\n\n")

        for directory in sorted(by_dir.keys()):
            rel_dir = relpath_under(directory, roots)
            out_file.write(f"  {rel_dir}/\n")
            for issue in by_dir[directory]:
                filename = os.path.basename(issue["path"])
                out_file.write(
                    f"    {filename}  [{issue['format']}]  missing: {issue['missing']}\n"
                )
            out_file.write("\n")

    if not quiet:
        print(f"\nAudited {total} files. Found {len(issues)} with incomplete tags.")
        print(f"Results written to: {out_path}")
        if field_counts:
            print("  Breakdown:")
            for field, count in field_counts.most_common():
                print(f"    {field}: {count}")

    return 0


# =====================================
# Mode: Bitrate floor audit
# =====================================


def run_bitrate_audit(
    root: str | list[str], output: str, min_kbps: int, *, quiet: bool = False
) -> int:
    """Report audio files falling below a specified bitrate floor."""
    if not HAVE_MUTAGEN_BASE:
        print("ERROR: mutagen is required for bitrate auditing.", file=sys.stderr)
        return 2

    roots = as_roots(root)
    issues: list[dict[str, str]] = []

    if not quiet:
        print(f"Auditing bitrates (< {min_kbps} kbps) under: {', '.join(roots)}")

    total = count_audio_files(roots)
    pbar = _make_pbar(total, "Auditing bitrates", quiet)

    paths = [
        os.path.join(dirpath, f)
        for _src_root, dirpath, _dirs, files in iter_audio_dirs(roots)
        for f in sorted(files)
        if os.path.splitext(f)[1].lower() in AUDIO_EXTENSIONS
    ]
    tags = read_tags_concurrent(paths, pbar=pbar)
    pbar.close()

    for filepath in paths:
        t = tags[filepath]
        if (
            t.bitrate_kbps is not None
            and t.bitrate_kbps > 0
            and t.bitrate_kbps < min_kbps
        ):
            issues.append(
                {
                    "path": filepath,
                    "format": os.path.splitext(filepath)[1].lower().strip("."),
                    "bitrate": str(t.bitrate_kbps),
                }
            )

    out_path = os.path.abspath(output or DEFAULT_BITRATE_AUDIT_OUTPUT)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    by_dir: dict[str, list[dict[str, str]]] = defaultdict(list)
    for issue in issues:
        parent = os.path.dirname(issue["path"])
        by_dir[parent].append(issue)

    with open(out_path, "w", encoding="utf-8") as out_file:
        out_file.write("BITRATE AUDIT REPORT\n")
        out_file.write(f"Root: {', '.join(roots)}\n")
        out_file.write(f"Floor: < {min_kbps} kbps\n")
        out_file.write(f"Scanned: {total}  Below floor: {len(issues)}\n")
        out_file.write("=" * 60 + "\n\n")

        for directory in sorted(by_dir.keys()):
            rel_dir = relpath_under(directory, roots)
            out_file.write(f"  {rel_dir}/\n")
            for issue in by_dir[directory]:
                filename = os.path.basename(issue["path"])
                out_file.write(
                    f"    {filename}  [{issue['format']}]  {issue['bitrate']} kbps\n"
                )
            out_file.write("\n")

    if not quiet:
        print(f"\nAudited {total} files. Found {len(issues)} below {min_kbps} kbps.")
        print(f"Results written to: {out_path}")

    return 0


# =====================================
# Mode: ReplayGain audit
# =====================================


def _rg_bucket(n_track: int, n_album: int, n_total: int) -> str:
    """Classify an album by its per-track ReplayGain coverage.

    MISSING: no track carries track gain.
    PARTIAL: some tracks tagged, some bare — the worst case for playback.
    NO_ALBUM_GAIN: every track has track gain, but not every track has album
    gain (album-mode replay has nothing to apply).
    OK: every track has both track and album gain."""
    if n_track == 0:
        return "MISSING"
    if n_track < n_total:
        return "PARTIAL"
    if n_album < n_total:
        return "NO_ALBUM_GAIN"
    return "OK"


def _rg_section(out, title: str, entries: list, roots: list[str], kind: str) -> None:
    out.write(f"[{title}]    ({len(entries)} album(s))\n")
    for dirpath, n_track, n_album, n_total in sorted(entries, key=lambda e: e[0]):
        rel = relpath_under(dirpath, roots)
        if kind == "missing":
            detail = f"0/{n_total} tracks tagged"
        elif kind == "partial":
            detail = f"{n_track}/{n_total} tracks tagged"
        elif kind == "noalbum":
            detail = f"{n_track}/{n_total} track gain, {n_album}/{n_total} album gain"
        else:
            detail = f"{n_total} track(s)"
        out.write(f"  {rel}/    ({detail})\n")
    out.write("\n")


def run_replaygain_audit(
    root: str | list[str],
    output: str,
    *,
    verbose: bool = False,
    quiet: bool = False,
) -> int:
    """Report per-album ReplayGain coverage. Format-aware: Opus R128 gain tags
    count as ReplayGain, so an album tagged the R128 way is not mis-flagged as
    untagged. Fully tagged albums are summarized and listed only with verbose."""
    if not HAVE_MUTAGEN_BASE:
        print("ERROR: mutagen is required for ReplayGain auditing.", file=sys.stderr)
        return 2

    roots = as_roots(root)
    if not quiet:
        print(f"Auditing ReplayGain under: {', '.join(roots)}")

    total = count_audio_files(roots)
    pbar = _make_pbar(total, "Auditing ReplayGain", quiet)

    # (dirpath, n_track_gain, n_album_gain, n_total) per album directory.
    albums: list[tuple[str, int, int, int]] = []
    for _src_root, dirpath, _subdirs, files in iter_audio_dirs(roots):
        audio_files = sorted(f for f in files if is_audio(f))
        if not audio_files:
            continue
        paths = [os.path.join(dirpath, f) for f in audio_files]
        statuses = map_concurrent(read_replaygain, paths, pbar=pbar)
        n_track = sum(1 for p in paths if statuses[p].has_track_gain)
        n_album = sum(1 for p in paths if statuses[p].has_album_gain)
        albums.append((dirpath, n_track, n_album, len(paths)))

    pbar.close()

    by_bucket: dict[str, list] = defaultdict(list)
    for dirpath, n_track, n_album, n_total in albums:
        by_bucket[_rg_bucket(n_track, n_album, n_total)].append(
            (dirpath, n_track, n_album, n_total)
        )

    n_ok = len(by_bucket["OK"])
    n_noalbum = len(by_bucket["NO_ALBUM_GAIN"])
    n_partial = len(by_bucket["PARTIAL"])
    n_missing = len(by_bucket["MISSING"])

    out_path = os.path.abspath(output or DEFAULT_REPLAYGAIN_AUDIT_OUTPUT)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("REPLAYGAIN AUDIT REPORT\n")
        f.write(f"Root: {', '.join(roots)}\n")
        f.write(f"Albums: {len(albums)}    Audio files: {total}\n")
        f.write(
            f"OK: {n_ok}   No album gain: {n_noalbum}   "
            f"Partial: {n_partial}   Missing: {n_missing}\n"
        )
        f.write("=" * 64 + "\n\n")

        _rg_section(f, "MISSING REPLAYGAIN", by_bucket["MISSING"], roots, "missing")
        _rg_section(f, "PARTIAL REPLAYGAIN", by_bucket["PARTIAL"], roots, "partial")
        _rg_section(f, "NO ALBUM GAIN", by_bucket["NO_ALBUM_GAIN"], roots, "noalbum")
        if verbose:
            _rg_section(f, "FULLY TAGGED", by_bucket["OK"], roots, "ok")

    if not quiet:
        print(f"\nAudited {len(albums)} albums ({total} files).")
        print(f"  Fully tagged:   {n_ok}")
        print(f"  No album gain:  {n_noalbum}")
        print(f"  Partial:        {n_partial}")
        print(f"  Missing:        {n_missing}")
        print(f"Results written to: {out_path}")

    return 0


# =====================================
# Mode: Stray-file audit
# =====================================

# Routine album-folder furniture that is neither audio nor a cover image:
# player/encoder sidecars, playlist formats, checksums, and the companion
# tools' own logs. Non-audio files inside an album folder whose extension is
# outside these sets are reported as likely import junk.
SIDECAR_IGNORE_EXT = {
    ".log",  # cleaner/apestrip/rerate/replaygain/genre_tidy/slipcover logs
    ".m3u",
    ".m3u8",
    ".pls",
    ".wpl",
    ".asx",  # playlists
    ".cue",  # cue sheets
    ".txt",
    ".nfo",
    ".url",
    ".csv",  # liner notes and download metadata
    ".accurip",
    ".md5",
    ".sfv",  # checksum sidecars
    ".xml",
    ".json",
    ".ini",
    ".db",  # player sidecars
}

IMAGE_SIDECAR_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}


def _layout_depths(layout: str) -> tuple[int, int]:
    """(artist_depth, album_depth): the 1-based component positions of
    {artist} and {album} in a layout pattern. Raises ValueError when either
    is missing."""
    parts = [p for p in layout.replace("\\", "/").split("/") if p]

    def slot(key: str) -> int | None:
        return next(
            (i + 1 for i, p in enumerate(parts) if p.strip("{}").lower() == key),
            None,
        )

    artist = slot("artist")
    album = slot("album")
    if artist is None or album is None:
        raise ValueError(f"layout {layout!r} must name {{artist}} and {{album}}")
    return artist, album


def classify_stray(
    rel_parts: tuple[str, ...], artist_depth: int, album_depth: int
) -> str:
    """Bucket one audio file by how it sits in the tree; rel_parts is the
    file's path below its library root, split into components.

    hidden       some component (folder or file) is a dot-name: the scanner
                 and the integrity walks prune these silently, so nothing
                 official ever saw the file
    loose        directly inside a folder at the layout's artist slot: with
                 the default layout that is the artist folder itself, and on
                 a genre-first layout it also catches flat Artist/Album
                 strays, whose files sit one slot short of the album depth
    placed       inside (or below) a folder at the layout's album depth, so
                 multi-disc subfolders deeper than album depth still count
    wrong-depth  everywhere else: audio at or above the genre/root slots, or
                 otherwise short of both the artist and album depths
    """
    if any(part.startswith(".") for part in rel_parts):
        return "hidden"
    depth = len(rel_parts) - 1  # containing folder's depth below the root
    if depth == artist_depth:
        return "loose"
    if depth >= album_depth:
        return "placed"
    return "wrong-depth"


def run_stray_audit(
    root: str | list[str],
    output: str,
    *,
    layout: str | None = None,
    quiet: bool = False,
) -> int:
    """Report files that don't fit the library's own layout: audio outside the
    configured album depth, loose tracks sitting beside album folders, audio
    hidden in dot-directories the scanners prune silently, and unrecognized
    non-audio files inside album folders (the beets 'unimported' analog).
    Read-only; the report is the whole point."""
    roots = as_roots(root)
    if layout is None:
        layout = get_layout()
    artist_depth, album_depth = _layout_depths(layout)

    strays: dict[str, list[str]] = {"wrong-depth": [], "loose": [], "hidden": []}
    junk: list[str] = []
    scanned = 0

    for src_root in roots:
        # One unpruned walk per root: unlike the scanner and integrity walks,
        # this mode exists to surface what they silently skip, hidden
        # directories and files included.
        for dirpath, dirnames, filenames in os.walk(src_root):
            dirnames[:] = sorted(dirnames)
            rel_dir = os.path.relpath(dirpath, src_root)
            dir_parts = tuple(p for p in rel_dir.split(os.sep) if p and p != ".")
            dir_is_hidden = any(p.startswith(".") for p in dir_parts)
            audio = sorted(f for f in filenames if is_audio(f))
            scanned += len(audio)
            for f in audio:
                bucket = classify_stray(dir_parts + (f,), artist_depth, album_depth)
                if bucket != "placed":
                    strays[bucket].append(
                        relpath_under(os.path.join(dirpath, f), roots)
                    )
            if audio and not dir_is_hidden:
                for f in sorted(filenames):
                    ext = os.path.splitext(f)[1].lower()
                    if (
                        is_audio(f)
                        or ext in IMAGE_SIDECAR_EXT
                        or ext in SIDECAR_IGNORE_EXT
                    ):
                        continue
                    junk.append(
                        f"{ext or '(none)'}  "
                        f"{relpath_under(os.path.join(dirpath, f), roots)}"
                    )

    total_strays = sum(len(v) for v in strays.values())
    out_path = os.path.abspath(output or DEFAULT_STRAY_AUDIT_OUTPUT)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as out_file:
        out_file.write("STRAY-FILE AUDIT\n")
        out_file.write(f"Root: {', '.join(roots)}\n")
        out_file.write(
            f"Layout: {layout}  (artist depth {artist_depth}, "
            f"album depth {album_depth})\n"
        )
        out_file.write(
            f"Scanned: {scanned} audio  Strays: {total_strays}  "
            f"Unrecognized files: {len(junk)}\n"
        )
        out_file.write("=" * 60 + "\n\n")

        sections = [
            (
                "WRONG-DEPTH AUDIO",
                strays["wrong-depth"],
                "short of both the artist and album slots (root or genre level)",
            ),
            (
                "LOOSE TRACKS",
                strays["loose"],
                "directly inside a folder at the layout's artist slot; on a "
                "genre-first layout this includes flat Artist/Album strays",
            ),
            (
                "HIDDEN-DIR AUDIO",
                strays["hidden"],
                "dot-names the scanners and integrity walks prune silently",
            ),
        ]
        for title, rows, note in sections:
            out_file.write(f"== {title} ({len(rows)}) ==\n")
            out_file.write(f"   ({note})\n")
            for row in rows:
                out_file.write(f"  {row}\n")
            out_file.write("\n")

        out_file.write(f"== NON-AUDIO FILES IN ALBUM FOLDERS ({len(junk)}) ==\n")
        out_file.write("   (outside the audio, image, and sidecar ignore sets)\n")
        for row in junk:
            out_file.write(f"  {row}\n")

    if not quiet:
        print(f"\nAudited {scanned} audio files under: {', '.join(roots)}")
        print(
            f"  Wrong-depth: {len(strays['wrong-depth'])}  "
            f"Loose: {len(strays['loose'])}  "
            f"Hidden: {len(strays['hidden'])}  "
            f"Unrecognized: {len(junk)}"
        )
        print(f"Results written to: {out_path}")

    return 0
