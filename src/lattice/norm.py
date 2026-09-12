"""Pure name and tag normalization rules, shared by every write-capable mode.

Promoted from scripts/cleaner.py's rules engine (v1.5.0) so the passes in
lattice.modes.clean, the compat shim, and any future mode consume one copy.
Zero I/O: nothing here touches the filesystem or a tag block; every function
maps strings to strings (or answers a predicate about a name).
"""

import re
import unicodedata

QUOTE_DASH_FOLD = {
    "‘": "'",  # left single quote
    "’": "'",  # right single quote (curly apostrophe)
    "ʼ": "'",  # modifier letter apostrophe
    "“": '"',  # left double quote
    "”": '"',  # right double quote
    "‐": "-",  # hyphen
    "‑": "-",  # non-breaking hyphen
    "‒": "-",  # figure dash
    "–": "-",  # en dash
    "—": "-",  # em dash
    "―": "-",  # horizontal bar
}

# Narrower fold for *display* renaming (canonical_render): only characters that
# are genuinely wrong AND whose ASCII form is legal on every filesystem the
# library may live on. The target is frequently NTFS/exFAT (shared with
# Windows), which forbids a trailing "." and the literal " character. So this
# deliberately leaves alone:
#   - en/em dashes (correct punctuation, e.g. ranges like "85–92")
#   - the ellipsis glyph … ("..." would end a name in dots -> NTFS rejects it)
#   - curly double quotes (straight " is forbidden on Windows)
# Those glyphs are all valid in a path component, so keeping them is safe.
RENDER_FOLD = {
    "‐": "-",  # U+2010 hyphen
    "‑": "-",  # U+2011 non-breaking hyphen
    "‒": "-",  # U+2012 figure dash
    "―": "-",  # U+2015 horizontal bar
    "‘": "'",  # left single quote
    "’": "'",  # right single quote (curly apostrophe)
    "ʼ": "'",  # modifier letter apostrophe
}

# Path-component characters forbidden on Windows/NTFS/exFAT, plus the trailing
# "." / " " rule. A normalized name that would be illegal there is skipped.
_ILLEGAL_NAME_CHARS = set('<>:"/\\|?*')


def is_legal_name(name: str) -> bool:
    return bool(name) and not (_ILLEGAL_NAME_CHARS & set(name)) and name[-1] not in ". "


def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    for k, v in QUOTE_DASH_FOLD.items():
        s = s.replace(k, v)
    # Fold apostrophe-present vs absent ("Director's Cut" == "Directors Cut") and
    # collapse whitespace so removing a quote can't leave a stray double space.
    s = s.replace("'", "").replace('"', "")
    return " ".join(s.split()).lower()


def canonical_render(s: str) -> str:
    """Normalized *display* rendering of a folder name via RENDER_FOLD: broken
    hyphens and curly single-quotes/apostrophes go to ASCII, and whitespace is
    collapsed. Case, en/em dashes, curly double quotes, the ellipsis glyph, and
    prime marks are preserved (all valid path characters), and no NFKC is
    applied. Deliberately narrower than normalize_name, which folds aggressively
    (NFKC, en/em dashes, apostrophe stripping, lowercasing) for *duplicate
    matching*."""
    for k, v in RENDER_FOLD.items():
        s = s.replace(k, v)
    return " ".join(s.split())


# CP1252 bytes read back as Latin-1 land on these C1 code points; together with
# the genuine Unicode curly punctuation they fold so a tag like
# "Bonnie \x93Prince\x94 Billy" or "Tim O\x92Brien" comes out clean.
#
# Deliberately narrow, matching canonical_render's philosophy: only the genuinely
# wrong glyphs go to ASCII. En/em dashes and the ellipsis are CORRECT typography
# (e.g. the range "85–92") and are preserved; a CP1252-mojibake dash/ellipsis is
# repaired to the real glyph, not flattened to a hyphen. Tags are not path
# components, so curly double quotes do fold to straight " (legal in a tag).
_TAG_FOLD = {
    # CP1252 C1 bytes (read back as Latin-1) -> intended glyph.
    0x91: "'",
    0x92: "'",
    0x93: '"',
    0x94: '"',
    0x96: "–",  # en dash (kept as a real en dash, not a hyphen)
    0x97: "—",  # em dash
    0x85: "…",  # ellipsis
    # Curly quotes/apostrophes -> straight ASCII.
    0x2018: "'",
    0x2019: "'",
    0x02BC: "'",  # modifier letter apostrophe
    0x201C: '"',
    0x201D: '"',
    # Genuinely broken hyphens -> ASCII hyphen. En/em dashes + ellipsis preserved.
    0x2010: "-",
    0x2011: "-",
    0x2012: "-",
    0x2015: "-",
}
# The marker needs a real token boundary on its left: a bare \s* is zero-width,
# which let the "ft" ending "Left"/"Swift"/"Croft" match and corrupt clean tags.
_FEAT_RE = re.compile(r"(?<!\w)(?:feat\.?|ft\.?|featuring)\s+(.*)$", re.IGNORECASE)


def tag_fold(s: str) -> str:
    """Fold CP1252 mojibake, curly quotes, and broken hyphens in a tag value to
    their clean form, collapsing whitespace. En/em dashes, ellipsis, and primes
    are preserved (correct typography), matching canonical_render's narrow fold;
    unlike it, curly double quotes go to straight " since tags are not paths."""
    return " ".join(s.translate(_TAG_FOLD).split())


def tag_dedupe(s: str) -> str:
    """Removes 'A / A', 'A / A feat. B', or identical duplicate tag values
    that can occur when multi-value tags are flattened or double-written."""
    for sep in (" / ", " // ", " \\\\ ", " ; "):
        if sep in s:
            parts = [p.strip() for p in s.split(sep) if p.strip()]
            if not parts:
                continue

            # 1. Exact case-insensitive deduplication
            unique = []
            seen = set()
            for p in parts:
                lower = p.lower()
                if lower not in seen:
                    seen.add(lower)
                    unique.append(p)

            if len(unique) == 1:
                return unique[0]

            # 2. Substring deduplication (keep the longest)
            unique.sort(key=len, reverse=True)
            survivors = []
            for p in unique:
                if not any(p.lower() in surv.lower() for surv in survivors):
                    survivors.append(p)

            # Join the remaining unique parts back together
            return sep.join(survivors)
    return s


def _base_artist(raw: str) -> str:
    folded = tag_fold(raw)
    m = _FEAT_RE.search(folded)
    if m is None:
        return folded
    # "Bonnie 'Prince' Billy feat. Tim O'Brien" -> "Bonnie 'Prince' Billy"
    return folded[: m.start()].strip()


def canon_track_artist(raw: str, canonical: str) -> str:
    """Track-level artist for a file under a folder whose artist is `canonical`.
    Collapses the band name to `canonical` but keeps a trailing guest credit
    ("... feat. X"), folding the guest's punctuation."""
    folded = tag_fold(raw)
    m = _FEAT_RE.search(folded)
    if m is None:
        return canonical
    guest = m.group(1).strip()
    # A "(feat. X)" credit leaves its closing paren on the guest; drop it.
    if guest.endswith(")") and guest.count(")") > guest.count("("):
        guest = guest[:-1].rstrip()
    return f"{canonical} feat. {guest}"
