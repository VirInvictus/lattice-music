import os
from collections import Counter, defaultdict

from lattice import utils
from lattice.config import DEFAULT_LAYOUT
from lattice.utils import (
    _make_pbar,
    as_roots,
    count_audio_files,
    is_audio,
    iter_audio_dirs,
    parse_layout,
    read_tags_concurrent,
)

# =====================================
# Mode: Library statistics
# =====================================


_RATING_LABELS = (
    "★★★★★ (5)",
    "★★★★☆ (4)",
    "★★★☆☆ (3)",
    "★★☆☆☆ (2)",
    "★☆☆☆☆ (1)",
)
_UNRATED = "unrated"


def _rating_label(rating: float | None) -> str:
    """Bucket a 0–5 rating into its star label; None is "unrated". A 0 rating
    falls into the 1-star bucket, matching the original tally."""
    if rating is None:
        return _UNRATED
    stars = max(1, min(5, int(rating)))
    return _RATING_LABELS[5 - stars]


def _empty_rating_tally() -> dict[str, int]:
    return {label: 0 for label in (*_RATING_LABELS, _UNRATED)}


def _format_size(size_bytes: int) -> str:
    """Format byte count into human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024**2:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024**3:
        return f"{size_bytes / (1024**2):.1f} MB"
    else:
        return f"{size_bytes / (1024**3):.2f} GB"


def run_stats(
    root: str | list[str],
    output: str | None,
    *,
    layout: str = DEFAULT_LAYOUT,
    quiet: bool = False,
) -> str:
    """Generate a library-wide statistics report (combined across all roots).

    `layout` is the path pattern used to recover the artist/album directory
    component when grouping, so the artist count is correct on a genre-first
    tree ({genre}/{artist}/{album}) as well as the default {artist}/{album}."""
    roots = as_roots(root)

    total_files = count_audio_files(roots)
    if total_files == 0:
        if not quiet and not utils.IN_TUI:
            print(f"No audio files found under: {', '.join(roots)}")
        return ""

    if not quiet and not utils.IN_TUI:
        print(f"Scanning {total_files} files under: {', '.join(roots)}")

    pbar = _make_pbar(total_files, "Gathering stats", quiet)

    # Accumulators
    format_counts: Counter = Counter()
    format_sizes: Counter = Counter()
    genre_counts: Counter = Counter()
    artist_counts: Counter = Counter()
    rating_counts: dict[str, int] = _empty_rating_tally()
    genre_ratings: dict[str, dict[str, int]] = defaultdict(_empty_rating_tally)
    total_size = 0
    total_duration = 0.0
    album_dirs: set = set()
    artist_dirs: set = set()
    bitrates: list[int] = []
    fully_tagged = 0  # has title + artist + track + genre

    # (filepath, owning root) for every audio file, walked in deterministic
    # order; tags are then read concurrently and the accumulation below stays
    # exactly as before (Counters are order-independent).
    entries: list[tuple[str, str]] = [
        (os.path.join(dirpath, f), src_root)
        for src_root, dirpath, _dirs, files in iter_audio_dirs(roots)
        for f in sorted(files)
        if is_audio(f)
    ]
    tags = read_tags_concurrent([e[0] for e in entries], pbar=pbar)
    pbar.close()

    for filepath, src_root in entries:
        ext = os.path.splitext(filepath)[1].lower()
        format_counts[ext] += 1

        try:
            fsize = os.path.getsize(filepath)
            total_size += fsize
            format_sizes[ext] += fsize
        except OSError:
            pass

        t = tags[filepath]

        # Artist/album tracking from directory structure, via the configured
        # layout so the artist component is correct on a genre-first tree too.
        # Album dirs are counted by their full relative path (unique per album
        # folder on any layout); artist comes from the layout's {artist} slot.
        rel_file = os.path.relpath(filepath, src_root)
        parsed = parse_layout(rel_file, layout)
        artist_dir = parsed.get("artist")
        if artist_dir:
            artist_dirs.add(artist_dir)
        # A directory counts as an album when the layout's {album} slot is
        # filled — depth-counting undercounted flat layouts and counted loose
        # files' artist dirs as albums on a genre-first tree.
        if parsed.get("album"):
            album_dirs.add(os.path.relpath(os.path.dirname(filepath), src_root))

        # Artist from tags (prefer tag, fall back to the layout's directory).
        artist_name = t.artist or artist_dir
        if artist_name:
            artist_counts[artist_name] += 1

        if t.genre:
            genre_counts[t.genre] += 1

        label = _rating_label(t.rating)
        rating_counts[label] += 1
        if t.genre:
            genre_ratings[t.genre][label] += 1

        # Duration and bitrate come from TagBundle
        if t.duration_s:
            total_duration += t.duration_s
        if t.bitrate_kbps:
            bitrates.append(t.bitrate_kbps)

        # Fully tagged check
        has_all = all([t.title, t.artist, t.trackno is not None, t.genre])
        if has_all:
            fully_tagged += 1

    # Build report
    lines: list[str] = []
    lines.append("LIBRARY STATISTICS")
    lines.append(f"Root: {', '.join(roots)}")
    lines.append("=" * 60)
    lines.append("")

    # Overview
    lines.append("OVERVIEW")
    lines.append("-" * 40)
    lines.append(f"  Total files:    {total_files}")
    lines.append(f"  Total size:     {_format_size(total_size)}")
    if total_duration > 0:
        hours = int(total_duration // 3600)
        mins = int((total_duration % 3600) // 60)
        lines.append(f"  Total duration: {hours}h {mins}m")
    lines.append(f"  Artists:        {len(artist_dirs)}")
    lines.append(f"  Albums:         {len(album_dirs)}")
    pct_tagged = (fully_tagged / total_files * 100) if total_files else 0
    lines.append(f"  Fully tagged:   {fully_tagged}/{total_files} ({pct_tagged:.0f}%)")
    lines.append("")

    # Format breakdown
    lines.append("FORMAT BREAKDOWN")
    lines.append("-" * 40)
    for ext, count in format_counts.most_common():
        pct = count / total_files * 100
        size_str = _format_size(format_sizes[ext])
        lines.append(f"  {ext:<8} {count:>6} files  ({pct:>5.1f}%)  {size_str:>10}")
    lines.append("")

    # Bitrate summary
    if bitrates:
        lines.append("BITRATE")
        lines.append("-" * 40)
        avg_br = sum(bitrates) / len(bitrates)
        min_br = min(bitrates)
        max_br = max(bitrates)
        lines.append(f"  Average: {avg_br:.0f} kbps")
        lines.append(f"  Range:   {min_br}–{max_br} kbps")
        # Flag low-quality files
        low_quality = sum(1 for b in bitrates if b < 192)
        if low_quality:
            lines.append(f"  Below 192 kbps: {low_quality} files")
        lines.append("")

    # Rating distribution
    rated = total_files - rating_counts["unrated"]
    lines.append(f"RATINGS ({rated} rated, {rating_counts['unrated']} unrated)")
    lines.append("-" * 40)
    for label in _RATING_LABELS:
        count = rating_counts[label]
        if count > 0:
            bar_len = min(30, int(count / max(1, total_files) * 150))
            bar = "█" * bar_len
            lines.append(f"  {label}  {count:>5}  {bar}")
    lines.append("")

    # Genre distribution (top 15)
    if genre_counts:
        lines.append(f"GENRES (top 15 of {len(genre_counts)})")
        lines.append("-" * 40)
        for genre, count in genre_counts.most_common(15):
            pct = count / total_files * 100
            lines.append(f"  {genre:<30} {count:>5}  ({pct:.1f}%)")
        lines.append("")

    # Rating distribution per genre
    if genre_ratings:
        lines.append("RATING DISTRIBUTION PER GENRE")
        lines.append("-" * 40)
        for genre, _ in genre_counts.most_common(15):
            lines.append(f"  {genre}:")
            for label in (*_RATING_LABELS, _UNRATED):
                count = genre_ratings[genre][label]
                if count > 0:
                    lines.append(f"    {label}  {count:>5}")
        lines.append("")

    # Top artists (top 15)
    if artist_counts:
        lines.append(f"TOP ARTISTS (by track count, top 15 of {len(artist_counts)})")
        lines.append("-" * 40)
        for artist, count in artist_counts.most_common(15):
            lines.append(f"  {artist:<35} {count:>5} tracks")
        lines.append("")

    report = "\n".join(lines) + "\n"

    # Write to file if output specified, otherwise stdout
    if output:
        out_path = os.path.abspath(output)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as out_file:
            out_file.write(report)
        if not quiet and not utils.IN_TUI:
            print(f"\nStatistics written to: {out_path}")
    else:
        if not quiet and not utils.IN_TUI:
            print()
            print(report)

    return report
