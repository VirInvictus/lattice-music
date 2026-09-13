import hashlib
import os
import re
import struct
import sys
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Sequence
from difflib import SequenceMatcher
from typing import NamedTuple

from lattice.config import (
    AUDIO_EXTENSIONS,
    DEFAULT_AUDIO_DUPES_OUTPUT,
    DEFAULT_BITRATE_AUDIT_OUTPUT,
    DEFAULT_DUPLICATES_OUTPUT,
    DEFAULT_HEALTH_SCORE_OUTPUT,
    DEFAULT_REPLAYGAIN_AUDIT_OUTPUT,
    DEFAULT_STRAY_AUDIT_OUTPUT,
    DEFAULT_TAG_AUDIT_OUTPUT,
    get_layout,
)
from lattice.modes.artwork import _get_image_size, _has_embedded_art
from lattice.norm import QUOTE_DASH_FOLD as _NORM_QUOTE_DASH_FOLD
from lattice.tags import HAVE_MUTAGEN_BASE, ReplayGainStatus, TagBundle, read_replaygain
from lattice.utils import (
    _find_cover_file,
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
# Mode: Content-hash audio duplicates
# =====================================

# Raw head/tail sample size, and the streaming chunk size.
DUPES_SAMPLE_BYTES = 64 * 1024
_DUPES_CHUNK = 1024 * 1024

# Extensions whose audio region can be located exactly by parsing (never
# trusting) the tag containers: MP3 skips ID3v2/ID3v1/APEv2, FLAC skips its
# metadata blocks, M4A hashes only mdat boxes. Everything else falls back to
# raw head/tail byte samples, which survive a rename but only some retags.
_AUDIO_REGION_EXTS = {".mp3", ".flac", ".m4a"}

_APE_HAS_HEADER = 0x80000000


class _FileSig(NamedTuple):
    path: str
    size: int
    full: str  # sha256 over the whole file
    stream: str  # sha256 over the audio region(s); "" when not computable
    head: str  # sha256 over the raw first DUPES_SAMPLE_BYTES bytes
    tail: str  # sha256 over the last bytes, trailing tag containers stripped
    error: str  # "" or why the file could not be read


class _BufReader:
    """seek/read over an in-memory buffer, so the tail-sample strip runs
    through the same code as the file-backed one."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def seek(self, off: int) -> None:
        self.pos = off

    def read(self, n: int) -> bytes:
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out


def _trailing_tags_len(reader, size: int) -> int:
    """Byte length of the well-formed ID3v1/APEv2 containers stacked at the
    end of `size` bytes (ID3v1 last, APEv2 before it, either optional; both
    are the tags this mode exists to see past). Every claim is end-anchored
    and bounded by `size`; a malformed or oversized claim simply counts as
    audio and returns shorter, so the strip can never cut into real audio
    beyond the bounds it proved."""
    cut = 0
    for _ in range(4):  # at most a couple of stacked containers
        if size - cut >= 128:
            reader.seek(size - cut - 128)
            if reader.read(3) == b"TAG":
                cut += 128
                continue
        if size - cut >= 32:
            reader.seek(size - cut - 32)
            footer = reader.read(32)
            if footer[:8] == b"APETAGEX":
                tag_size, _items, flags = struct.unpack("<III", footer[12:24])
                total = tag_size + (32 if flags & _APE_HAS_HEADER else 0)
                if 32 < total <= size - cut:
                    cut += total
                    continue
        break
    return cut


def _audio_regions(fh, ext: str, size: int) -> list[tuple[int, int]] | None:
    """(start, end) byte ranges holding the audio stream, or None when the
    container cannot be trusted enough to locate them (the caller then falls
    back to the raw head/tail samples). Every parse is bounded by `size`; a
    malformed claim means None, never a wrong range."""
    try:
        if ext == ".mp3":
            fh.seek(0)
            head = fh.read(10)
            if len(head) < 10 or head[:3] != b"ID3":
                return None
            end = 10 + (head[6] << 21) + (head[7] << 14) + (head[8] << 7) + head[9]
            if head[5] & 0x10:
                end += 10  # a footer mirrors the header
            if not 10 < end < size:
                return None
            audio_end = size - _trailing_tags_len(fh, size)
            if audio_end <= end:
                return None
            return [(end, audio_end)]
        if ext == ".flac":
            fh.seek(0)
            if fh.read(4) != b"fLaC":
                return None
            pos = 4
            for _ in range(64):  # block count is small; longer is malformed
                fh.seek(pos)
                header = fh.read(4)
                if len(header) < 4:
                    return None
                pos += 4 + int.from_bytes(header[1:4], "big")
                if pos >= size:
                    return None  # metadata reaches EOF: no audio to hash
                if header[0] & 0x80:
                    return [(pos, size)]
            return None
        if ext == ".m4a":
            regions: list[tuple[int, int]] = []
            pos = 0
            for _ in range(64):
                if pos >= size:
                    break
                fh.seek(pos)
                header = fh.read(16)
                if len(header) < 8:
                    return None
                box = int.from_bytes(header[:4], "big")
                hdr = 8
                if box == 1:
                    if len(header) < 16:
                        return None
                    box = int.from_bytes(header[8:16], "big")
                    hdr = 16
                elif box == 0:
                    box = size - pos  # a to-EOF box
                if box < hdr or pos + box > size:
                    return None
                if header[4:8] == b"mdat":
                    regions.append((pos + hdr, pos + box))
                pos += box
            else:
                return None  # walk hit its bound before EOF: malformed
            return regions or None
    except OSError:
        return None
    return None


def _fingerprint_file(path: str) -> _FileSig:
    """One pass over the file for the full sha256, the head sample, the tail
    sample, and (when the container is one we can parse) the audio-stream
    sha256 over the located region(s). Read errors come back in `error`
    rather than raising, so one unreadable file cannot kill a library run."""
    ext = os.path.splitext(path)[1].lower()
    try:
        size = os.path.getsize(path)
        regions: list[tuple[int, int]] | None = None
        if ext in _AUDIO_REGION_EXTS and size > 0:
            with open(path, "rb") as fh:
                regions = _audio_regions(fh, ext, size)
            if regions and sum(end - start for start, end in regions) == 0:
                regions = None

        h_full = hashlib.sha256()
        h_stream = hashlib.sha256() if regions else None
        h_head = hashlib.sha256()
        tail_buf = bytearray()
        pos = 0
        with open(path, "rb") as fh:
            while chunk := fh.read(_DUPES_CHUNK):
                h_full.update(chunk)
                if pos < DUPES_SAMPLE_BYTES:
                    h_head.update(chunk[: DUPES_SAMPLE_BYTES - pos])
                tail_buf.extend(chunk)
                if len(tail_buf) > DUPES_SAMPLE_BYTES:
                    del tail_buf[: len(tail_buf) - DUPES_SAMPLE_BYTES]
                if h_stream is not None:
                    chunk_end = pos + len(chunk)
                    while regions and regions[0][1] <= pos:
                        regions.pop(0)
                    for start, end in regions or ():
                        if start >= chunk_end:
                            break
                        lo = max(pos, start)
                        hi = min(chunk_end, end)
                        if hi > lo:
                            h_stream.update(chunk[lo - pos : hi - pos])
                pos += len(chunk)

        tail = bytes(tail_buf)
        strip = _trailing_tags_len(_BufReader(tail), len(tail))
        h_tail = hashlib.sha256(tail[: len(tail) - strip])
        return _FileSig(
            path=path,
            size=size,
            full=h_full.hexdigest(),
            stream=h_stream.hexdigest() if h_stream else "",
            head=h_head.hexdigest(),
            tail=h_tail.hexdigest(),
            error="",
        )
    except OSError as e:
        return _FileSig(
            path=path, size=0, full="", stream="", head="", tail="", error=str(e)
        )


def _sig_section(
    out, title: str, groups: list[list[_FileSig]], roots
) -> tuple[int, int]:
    files = sum(len(g) for g in groups)
    if not groups:
        out.write(f"[{title}]    (none)\n\n")
        return 0, 0
    out.write(f"[{title}]    ({len(groups)} group(s), {files} file(s))\n\n")
    for i, group in enumerate(sorted(groups, key=lambda g: g[0].path), 1):
        first = min(group, key=lambda s: s.path)
        out.write(f"  {i}. {relpath_under(os.path.dirname(first.path), roots)}/\n")
        for s in sorted(group, key=lambda x: x.path):
            out.write(
                f"       {relpath_under(s.path, roots)}    ({_fmt_size(s.size)})\n"
            )
        out.write("\n")
    return len(groups), files


def run_audio_dupes(root: str | list[str], output: str, *, quiet: bool = False) -> int:
    """Content-hash duplicate detection: the byte-level complement to
    `--duplicates` (which keys on tags, names, and sizes). Tags are never
    read; the report is purely byte-level, so it also works without mutagen.

    Three matching tiers, each a sha256:
      exact   the whole file byte for byte (a renamed or copied dupe);
      stream  the audio region only, located by parsing the container's tag
              boundaries (MP3: ID3v2 head plus trailing ID3v1/APEv2; FLAC:
              metadata blocks; M4A: the mdat boxes), so files that differ
              only in their tags match here;
      sample  for the formats without a parsed region, the raw first and
              last 64KB: a cheap fingerprint that survives a rename, and
              survives a retag when the tags stay out of the sampled bytes.
    Never matches across encodings: a FLAC and the MP3 made from it are
    different bytes and are not reported."""
    roots = as_roots(root)
    if not quiet:
        print(f"Hashing audio under: {', '.join(roots)}")

    paths = [
        os.path.join(dirpath, f)
        for _src_root, dirpath, _dirs, files in iter_audio_dirs(roots)
        for f in sorted(files)
        if is_audio(f)
    ]
    pbar = _make_pbar(len(paths), "Hashing audio", quiet)
    sigs = list(map_concurrent(_fingerprint_file, paths, pbar=pbar).values())
    pbar.close()
    sigs.sort(key=lambda s: s.path)

    readable = [s for s in sigs if not s.error]
    errors = [s for s in sigs if s.error]

    by_full: dict[str, list[_FileSig]] = defaultdict(list)
    for s in readable:
        by_full[s.full].append(s)
    exact = [g for g in by_full.values() if len(g) > 1]
    exact_fulls = {s.full for g in exact for s in g}

    by_stream: dict[str, list[_FileSig]] = defaultdict(list)
    by_sample: dict[tuple[str, str], list[_FileSig]] = defaultdict(list)
    for s in readable:
        if s.full in exact_fulls:
            continue
        if s.stream:
            by_stream[s.stream].append(s)
        else:
            by_sample[(s.head, s.tail)].append(s)
    streams = [g for g in by_stream.values() if len(g) > 1]
    samples = [g for g in by_sample.values() if len(g) > 1]

    out_path = os.path.abspath(output or DEFAULT_AUDIO_DUPES_OUTPUT)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("AUDIO DUPLICATE REPORT\n")
        f.write(f"Root: {', '.join(roots)}\n")
        f.write(
            f"Files: {len(readable)} readable, {len(errors)} unreadable    "
            f"Samples: first/last {_fmt_size(DUPES_SAMPLE_BYTES)}\n"
        )
        f.write("=" * 70 + "\n\n")

        _sig_section(f, "EXACT DUPLICATES", exact, roots)
        _sig_section(f, "AUDIO-STREAM MATCHES", streams, roots)
        _sig_section(f, "SAMPLED CONTENT MATCHES", samples, roots)

        if errors:
            f.write(f"[UNREADABLE FILES]    ({len(errors)})\n\n")
            for s in errors:
                f.write(f"       {relpath_under(s.path, roots)}    ({s.error})\n")
            f.write("\n")

    if not quiet:
        print(f"\nHashed {len(readable)} files ({len(errors)} unreadable).")
        print(f"  Exact duplicates:      {len(exact)} group(s)")
        print(f"  Audio-stream matches:  {len(streams)} group(s)")
        print(f"  Sampled matches:       {len(samples)} group(s)")
        print(f"Results written to: {out_path}")
    return 0


# =====================================
# Mode: Library health score
# =====================================

# Deduction weights per album out of 100: tags 40, ReplayGain 30, art 20,
# bitrate 10. The buckets reuse the facts the other audits report, so the
# score is an aggregation, not a new lens.
_HEALTH_TAG_WEIGHT = 4  # per missing core field (title/artist/track/genre)
_HEALTH_TAG_CAP = 40
_HEALTH_RG_WEIGHT = 30  # proportional to track-gain coverage
_HEALTH_ART_SMALL_COVER = 8  # folder cover below the resolution floor
_HEALTH_ART_EMBEDDED_ONLY = 12  # art only inside the audio files
_HEALTH_ART_NONE = 20
_HEALTH_BITRATE_WEIGHT = 2  # per file below the floor
_HEALTH_BITRATE_CAP = 10


def _health_grade(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    if score >= 50:
        return "C"
    return "D"


def _album_health(
    bundles: dict[str, TagBundle],
    rg: dict[str, ReplayGainStatus],
    folder_cover: bool,
    cover_res: tuple[int, int] | None,
    embedded: bool,
    min_kbps: int,
    min_res: int,
) -> tuple[int, list[str]]:
    """Score one album directory out of 100 and list exactly what cost it
    points. `bundles` maps path -> TagBundle, `rg` path -> ReplayGainStatus;
    `cover_res` is the folder cover's (width, height) when known."""
    notes: list[str] = []
    penalty = 0

    n_missing = 0
    tag_bits: list[str] = []
    for path, t in sorted(bundles.items()):
        missing = [
            name
            for name, val in (
                ("title", t.title),
                ("artist", t.artist),
                ("tracknumber", t.trackno),
                ("genre", t.genre),
            )
            if not val
        ]
        if missing:
            n_missing += len(missing)
            tag_bits.append(f"{os.path.basename(path)}: {', '.join(missing)}")
    if n_missing:
        pen = min(_HEALTH_TAG_CAP, _HEALTH_TAG_WEIGHT * n_missing)
        penalty += pen
        notes.append(f"tags -{pen} (" + "; ".join(tag_bits) + ")")

    n = len(bundles)
    if n:
        n_gain = sum(1 for s in rg.values() if s.has_track_gain)
        if n_gain < n:
            pen = round(_HEALTH_RG_WEIGHT * (n - n_gain) / n)
            penalty += pen
            notes.append(f"replaygain -{pen} ({n_gain}/{n} tracks tagged)")

    if not folder_cover:
        if embedded:
            pen = _HEALTH_ART_EMBEDDED_ONLY
            notes.append(f"art -{pen} (embedded only; no folder cover)")
        else:
            pen = _HEALTH_ART_NONE
            notes.append(f"art -{pen} (no art found)")
        penalty += pen
    elif cover_res is not None and min(cover_res) < min_res:
        pen = _HEALTH_ART_SMALL_COVER
        penalty += pen
        notes.append(
            f"art -{pen} (folder cover {cover_res[0]}x{cover_res[1]} < {min_res}px)"
        )

    low_bits: list[str] = []
    for path, t in sorted(bundles.items()):
        if t.bitrate_kbps and t.bitrate_kbps < min_kbps:
            low_bits.append(f"{os.path.basename(path)} {t.bitrate_kbps}kbps")
    if low_bits:
        pen = min(_HEALTH_BITRATE_CAP, _HEALTH_BITRATE_WEIGHT * len(low_bits))
        penalty += pen
        notes.append(f"bitrate -{pen} (" + "; ".join(low_bits) + f" < {min_kbps}kbps)")

    return max(0, 100 - penalty), notes


def run_health_score(
    root: str | list[str],
    output: str,
    *,
    min_kbps: int,
    min_res: int,
    verbose: bool = False,
    quiet: bool = False,
) -> int:
    """Aggregate the audit lenses into a per-album health score out of 100:
    tag completeness (--auditTags), ReplayGain coverage (--auditReplayGain),
    art presence and resolution (--missingArt / --auditArtQuality), and the
    bitrate floor (--auditBitrate). One read-only pass; no decode scans (the
    integrity walks remain their own modes). Albums scoring below 100 are
    listed with their point-by-point deductions; --verbose also lists the
    perfect ones."""
    if not HAVE_MUTAGEN_BASE:
        print("ERROR: mutagen is required for the health score.", file=sys.stderr)
        return 2

    roots = as_roots(root)
    if not quiet:
        print(f"Scoring library health under: {', '.join(roots)}")

    total = count_audio_files(roots)
    pbar = _make_pbar(total * 2, "Scoring albums", quiet)

    # (src_root, dirpath, score, notes, n_files)
    albums: list[tuple[str, str, int, list[str], int]] = []
    for src_root, dirpath, _subdirs, files in iter_audio_dirs(roots):
        audio_files = sorted(f for f in files if is_audio(f))
        if not audio_files:
            continue
        paths = [os.path.join(dirpath, f) for f in audio_files]
        bundles = read_tags_concurrent(paths, pbar=pbar)
        rg = map_concurrent(read_replaygain, paths, pbar=pbar)

        cover = _find_cover_file(dirpath)
        cover_res = None
        if cover:
            try:
                with open(cover, "rb") as fh:
                    cover_res = _get_image_size(fh.read())
            except OSError:
                cover_res = None
        embedded = _has_embedded_art(dirpath) if not cover else False

        score, notes = _album_health(
            bundles, rg, cover is not None, cover_res, embedded, min_kbps, min_res
        )
        albums.append((src_root, dirpath, score, notes, len(paths)))

    pbar.close()

    grades: Counter = Counter(_health_grade(s) for _, _, s, _, _ in albums)
    mean = round(sum(s for _, _, s, _, _ in albums) / len(albums), 1) if albums else 0.0

    out_path = os.path.abspath(output or DEFAULT_HEALTH_SCORE_OUTPUT)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("LIBRARY HEALTH REPORT\n")
        f.write(f"Root: {', '.join(roots)}\n")
        f.write(f"Albums: {len(albums)}    Audio files: {total}\n")
        f.write(
            f"Grades: A {grades['A']}  B {grades['B']}  C {grades['C']}  "
            f"D {grades['D']}    Mean: {mean}\n"
        )
        f.write("=" * 60 + "\n\n")

        ranked = sorted(albums, key=lambda a: (a[2], a[1]))
        flagged = [a for a in ranked if a[2] < 100]
        full = [a for a in ranked if a[2] == 100]

        f.write(f"[ALBUMS WITH DEDUCTIONS]    ({len(flagged)} album(s))\n\n")
        for _src_root, dirpath, score, notes, n_files in flagged:
            rel = relpath_under(dirpath, roots)
            f.write(f"  {score:>3} {_health_grade(score)}  {rel}/  ({n_files} files)\n")
            for note in notes:
                f.write(f"       {note}\n")
            f.write("\n")

        if verbose:
            f.write(f"[FULL-SCORE ALBUMS]    ({len(full)} album(s))\n\n")
            for _src_root, dirpath, score, _notes, n_files in full:
                rel = relpath_under(dirpath, roots)
                f.write(
                    f"  {score:>3} {_health_grade(score)}  {rel}/  ({n_files} files)\n"
                )
            f.write("\n")
        else:
            f.write(f"[FULL-SCORE ALBUMS]    {len(full)} (list with --verbose)\n\n")

    if not quiet:
        print(f"\nScored {len(albums)} albums ({total} files).")
        print(
            f"  Grades: A {grades['A']}  B {grades['B']}  "
            f"C {grades['C']}  D {grades['D']}   Mean: {mean}"
        )
        print(f"  Deductions: {len(flagged)} album(s) below full score")
        print(f"Results written to: {out_path}")
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
