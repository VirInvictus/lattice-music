import os
import re
from typing import NamedTuple

from lattice.utils import _looks_numeric, normalize_rating


class TagBundle(NamedTuple):
    """All metadata we care about, extracted once per file."""

    title: str | None = None
    artist: str | None = None
    trackno: int | None = None
    album: str | None = None
    genre: str | None = None
    rating: float | None = None
    duration_s: float | None = None
    bitrate_kbps: int | None = None
    year: int | None = None


class ReplayGainStatus(NamedTuple):
    """Whether a file carries track- and album-level ReplayGain gain tags."""

    has_track_gain: bool = False
    has_album_gain: bool = False


# Opus stores gain as R128_*_GAIN (EBU R128, integer Q7.8) instead of the
# replaygain_*_gain text tags MP3/FLAC use; both count as ReplayGain here, so an
# R128-tagged Opus file is not mistaken for untagged. Matched on the key's
# suffix so format prefixes (ID3 "TXXX:", iTunes "----:com.apple.iTunes:") fold
# in without per-format branches.
_RG_TRACK_SUFFIXES = ("replaygain_track_gain", "r128_track_gain")
_RG_ALBUM_SUFFIXES = ("replaygain_album_gain", "r128_album_gain")


# The canonical POPM star bytes (WMP convention). foobar2000, Winamp, MusicBee,
# and rerate.py all write these exact bytes regardless of the frame's email, so
# they map to whole stars for any email; only non-canonical bytes fall through
# to normalize_rating's magnitude guess (which would call byte 196 "3.84 stars"
# and make a `rating >= 4` playlist miss every 4-star MP3).
_POPM_BYTE_MAP = {1: 1.0, 64: 2.0, 128: 3.0, 196: 4.0, 255: 5.0}


def _popm_stars(byte: int) -> float | None:
    rating = _POPM_BYTE_MAP.get(byte)
    return rating if rating is not None else normalize_rating(byte)


# Rating tag names in preference order. A file can carry several rating-ish
# keys at once (Picard and foobar exports add "album rating"; some players add
# "love rating" or a "_custom_rating"), and taking whichever the container
# yielded first made the answer depend on the process's hash seed: mutagen's
# VCommentDict.keys() is built from a set, so a Vorbis/FLAC/Opus file's key
# order is randomized per run. 41 files in a 9.6k library read a different
# rating on each scan that way, which quietly changed `rating >= 4` playlists
# and the stats histogram between runs. An album-level rating is never the
# track's rating, so it ranks last rather than being trusted on a coin flip.
_RATING_PREFERRED = ("rating", "fmps_rating", "score", "stars", "rate")


def _rating_rank(key: str) -> tuple[int, str]:
    """Sort key for rating candidates: the standard names first (in
    _RATING_PREFERRED order), then anything else, then album-level keys. Ties
    break alphabetically, so the pick never depends on iteration order."""
    if key in _RATING_PREFERRED:
        return (0, f"{_RATING_PREFERRED.index(key):02d}")
    if "album" in key:
        return (2, key)
    return (1, key)


def _best_rating(candidates) -> float | None:
    """Decode the best rating from (lowercased key, value) pairs. Non-numeric
    values (a "love rating" of "L") are skipped, as before; the difference is
    that the surviving candidates are ranked instead of first-wins."""
    for key, val in sorted(candidates, key=lambda c: _rating_rank(c[0])):
        if _looks_numeric(val):
            return _tag_rating(key, val)
    return None


def _unwrap(val):
    """First element of a mutagen multi-value list, or the value itself."""
    return val[0] if isinstance(val, list) and val else val


def _tag_rating(key: str, val) -> float | None:
    """Decode one rating-ish tag value, scale-aware. FMPS_* tags store 0.0-1.0
    floats by spec, which normalize_rating's magnitude heuristic would read as
    a sub-one star count, so they get an explicit x5 scale."""
    if "fmps_" in key:
        try:
            f = float(str(val))
        except ValueError, TypeError:
            return None
        if 0.0 <= f <= 1.0:
            return f * 5.0
    return normalize_rating(val)


def _rg_flags(keys) -> tuple[bool, bool]:
    kl = [str(k).lower() for k in keys]
    has_track = any(k.endswith(s) for k in kl for s in _RG_TRACK_SUFFIXES)
    has_album = any(k.endswith(s) for k in kl for s in _RG_ALBUM_SUFFIXES)
    return has_track, has_album


def read_replaygain(file_path: str) -> ReplayGainStatus:
    """Report ReplayGain coverage for a file in a single open. An unreadable or
    untagged file reports no gain, so it surfaces as missing in the audit."""
    if not HAVE_MUTAGEN_BASE:
        return ReplayGainStatus()
    try:
        audio = MutagenFile(file_path)
        tags = getattr(audio, "tags", None) if audio is not None else None
        if not tags:
            return ReplayGainStatus()
        has_track, has_album = _rg_flags(tags.keys())
        return ReplayGainStatus(has_track, has_album)
    except Exception:
        return ReplayGainStatus()


def read_replaygain_values(file_path: str) -> tuple[float | None, float | None]:
    """(track_gain_db, album_gain_db), the value reader behind
    --verifyReplayGain. Key-aware about the two write conventions: standard
    replaygain_* keys parse as float dB, while Opus R128 keys are Q7.8
    integers (/256, bounded by ~0.004 dB of quantization). Unreadable or
    untagged files report (None, None), which the verifier buckets as
    ungauged."""
    track, album, _convention = read_replaygain_values_with_convention(file_path)
    return (track, album)


def read_replaygain_values_with_convention(
    file_path: str,
) -> tuple[float | None, float | None, str | None]:
    """(track_gain_db, album_gain_db, convention) with the write convention
    the stored keys imply: ``"r128"`` when the gains live in Opus R128 keys
    (Q7.8 integers, referenced to the R128 -23 LUFS baseline), ``"rg"``
    when they live in standard replaygain_* dB keys (referenced to the
    write target), and None when the file carries no readable gains. The
    convention is the reference the stored number was computed against --
    verifying an R128 gain against anything other than -23 LUFS reports a
    constant offset that is the convention, not a miswrite."""
    if not HAVE_MUTAGEN_BASE:
        return (None, None, None)
    try:
        audio = MutagenFile(file_path)
    except Exception:
        return (None, None, None)
    tags = getattr(audio, "tags", None) if audio is not None else None
    if not tags:
        return (None, None, None)
    try:
        items = list(tags.items())
    except Exception:
        return (None, None, None)

    def gain_of(key: str, val) -> float | None:
        if key.endswith("r128_track_gain") or key.endswith("r128_album_gain"):
            s = _unwrap(val)
            s = getattr(s, "text", s)
            s = _unwrap(s)
            if isinstance(s, bytes):
                s = s.decode("utf-8", "replace")
            try:
                return int(str(s).strip()) / 256.0
            except ValueError:
                return None
        s = _unwrap(val)
        text = getattr(s, "text", s)
        text = _unwrap(text)
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        raw = str(text).strip().removesuffix("dB").strip()
        try:
            return float(raw)
        except ValueError:
            return None

    track = album = None
    track_conv = album_conv = None
    for k, v in items:
        kl = str(k).lower()
        if kl.endswith("replaygain_track_gain") or kl.endswith("r128_track_gain"):
            track = gain_of(kl, v)
            track_conv = "r128" if kl.endswith("r128_track_gain") else "rg"
        elif kl.endswith("replaygain_album_gain") or kl.endswith("r128_album_gain"):
            album = gain_of(kl, v)
            album_conv = "r128" if kl.endswith("r128_album_gain") else "rg"
    convention = track_conv or album_conv
    return (track, album, convention)


# --- Mutagen imports ---
# This module centralizes mutagen imports for the package; `Picture` and
# `MUTAGEN_MP3` are unused here but re-exported for modes/artwork.py,
# modes/integrity.py, and scripts/slipcover.py.
HAVE_MUTAGEN_BASE = False
try:
    from mutagen import File as MutagenFile
    from mutagen.asf import ASF
    from mutagen.id3 import ID3 as MUTAGEN_ID3  # noqa: F401  (re-export)
    from mutagen.flac import FLAC, Picture  # noqa: F401  (re-export)
    from mutagen.mp4 import MP4
    from mutagen.oggvorbis import OggVorbis

    try:
        from mutagen.oggopus import OggOpus
    except ImportError:

        class OggOpus:  # type: ignore[no-redef]
            pass

    HAVE_MUTAGEN_BASE = True
except ImportError:
    pass

try:
    from mutagen.mp3 import MP3 as MUTAGEN_MP3  # noqa: F401  (re-export)

    HAVE_MUTAGEN_MP3 = True
except ImportError:
    HAVE_MUTAGEN_MP3 = False


def _first_text(val) -> str | None:
    if val is None:
        return None
    if isinstance(val, (list, tuple)):
        val = val[0] if val else None

    # Handle Mutagen ID3 frames which store strings in a .text list
    if hasattr(val, "text") and isinstance(val.text, list) and val.text:
        # Join multiple values with a slash instead of mutagen's default null byte
        val = "/".join(str(v) for v in val.text)

    try:
        if hasattr(val, "value"):
            val = val.value
    except Exception:
        pass

    if val is not None:
        # Strip string and explicitly replace any remaining null bytes
        s = str(val).replace("\x00", "/").strip()
        return s if s else None
    return None


def _parse_track_number(val) -> int | None:
    if val is None:
        return None
    if isinstance(val, list) and val and isinstance(val[0], tuple):
        try:
            num = int(val[0][0])
            return num if num > 0 else None
        except ValueError, IndexError, TypeError:
            return None
    s = _first_text(val)
    if not s:
        return None
    s = s.split("/")[0]
    try:
        n = int(s)
        return n if n > 0 else None
    except ValueError:
        return None


# Year-bearing tag keys, matched on the key's lowercase suffix like the
# ReplayGain flags: ID3 frames (TDRC/TYER), Vorbis date keys (date, and the
# originaldate/releasedate variants), MP4's ©day atom, ASF WM/Year.
_YEAR_KEY_SUFFIXES = ("tdrc", "tyer", "date", "year", "\xa9day")


def _parse_year(val) -> int | None:
    """First plausible year in a date-ish value ('1985', '1985-03-01',
    '1985-03-01T12:00:00'). Anything else parses as no year rather than
    guessing; plausible means a 1200-2100 integer."""
    s = _first_text(val)
    if not s:
        return None
    s = s.strip()
    m = re.match(r"(\d{4})(?:\D|$)", s)
    if not m:
        return None
    y = int(m.group(1))
    return y if 1200 <= y <= 2100 else None


def _year_from_tags(tags) -> int | None:
    """The file's year from the first year-bearing key that yields one.
    MusicBrainz keys are skipped: their '...Date' descs carry TXXX frames
    whose values are MBIDs, and a hex string can contain four digits."""
    if not hasattr(tags, "items"):
        return None
    candidates: list[tuple[str, object]] = []
    try:
        for k, v in tags.items():
            kl = str(k).lower()
            if "musicbrainz" in kl:
                continue
            if kl.endswith(_YEAR_KEY_SUFFIXES):
                candidates.append((kl, v))
    except Exception:
        return None
    for _kl, v in sorted(candidates, key=lambda c: c[0]):
        y = _parse_year(v)
        if y is not None:
            return y
    return None


def get_all_tags(file_path: str) -> TagBundle:
    """Extract all metadata in a single file open."""
    if not HAVE_MUTAGEN_BASE:
        return TagBundle()

    title = artist = album = genre = None
    trackno: int | None = None
    rating: float | None = None
    # Bound before the try: the year pass after it runs even when MutagenFile
    # raised on an unreadable file.
    tags = None
    duration_s: float | None = None
    bitrate_kbps: int | None = None

    try:
        audio = MutagenFile(file_path)
        if not audio:
            return TagBundle()

        # Extract duration and bitrate from audio.info
        info = getattr(audio, "info", None)
        if info:
            length = getattr(info, "length", 0.0) or 0.0
            if length > 0:
                duration_s = round(length, 3)
            br = getattr(info, "bitrate", 0) or 0
            if br > 0:
                bitrate_kbps = int(br / 1000)

        ext = os.path.splitext(file_path)[1].lower()
        tags = getattr(audio, "tags", {}) or {}

        if ext == ".mp3":
            # ID3 tags — accessible via audio.tags from MutagenFile
            if not tags:
                return TagBundle(
                    title,
                    artist,
                    trackno,
                    album,
                    genre,
                    rating,
                    duration_s,
                    bitrate_kbps,
                )

            if hasattr(tags, "get"):
                # Pass the frame itself, not frame.text: _first_text's list
                # branch would take text[0] and silently drop the rest of a
                # multi-valued frame before its "/" join could run.
                tit2 = tags.get("TIT2")
                if tit2:
                    title = _first_text(tit2)
                tpe1 = tags.get("TPE1")
                tpe2 = tags.get("TPE2")
                if tpe2:
                    artist = _first_text(tpe2)
                elif tpe1:
                    artist = _first_text(tpe1)
                trck = tags.get("TRCK")
                if trck:
                    trackno = _parse_track_number(trck.text)
                talb = tags.get("TALB")
                if talb:
                    album = _first_text(talb)

            if hasattr(tags, "getall"):
                tcon = tags.getall("TCON")
                if tcon:
                    genre = _first_text(tcon[0])

                # Rating: POPM (prefer WMP, then any) / TXXX
                for popm in tags.getall("POPM"):
                    if getattr(popm, "email", "") == "Windows Media Player 9 Series":
                        rating = _popm_stars(popm.rating)
                        break
                if rating is None:
                    for popm in tags.getall("POPM"):
                        if popm.rating > 0:
                            rating = _popm_stars(popm.rating)
                            break
                if rating is None:
                    txxx_candidates = []
                    for txxx in tags.getall("TXXX"):
                        desc = (txxx.desc or "").lower()
                        if "rating" in desc or desc in ("rate", "score", "stars"):
                            txxx_candidates.append(
                                (desc, txxx.text[0] if txxx.text else None)
                            )
                    rating = _best_rating(txxx_candidates)

        elif isinstance(audio, MP4):
            title = _first_text(tags.get("\xa9nam"))
            artist = _first_text(tags.get("aART")) or _first_text(tags.get("\xa9ART"))
            trackno = _parse_track_number(tags.get("trkn"))
            album = _first_text(tags.get("\xa9alb"))
            for k in ("\xa9gen", "gnre"):
                v = tags.get(k)
                if v:
                    genre = _first_text(v)
                    break
            mp4_candidates = []
            for k, v in tags.items():
                kl = k.lower() if isinstance(k, str) else str(k).lower()
                if "rate" in kl or "rating" in kl:
                    mp4_candidates.append((kl, _unwrap(v)))
            rating = _best_rating(mp4_candidates)

        elif isinstance(audio, (FLAC, OggVorbis, OggOpus)):
            keys = {k.lower(): k for k in tags.keys()}
            if "title" in keys:
                title = _first_text(tags[keys["title"]])
            if "albumartist" in keys:
                artist = _first_text(tags[keys["albumartist"]])
            elif "artist" in keys:
                artist = _first_text(tags[keys["artist"]])
            if "tracknumber" in keys:
                trackno = _parse_track_number(tags[keys["tracknumber"]])
            if "album" in keys:
                album = _first_text(tags[keys["album"]])
            if "genre" in keys:
                genre = _first_text(tags[keys["genre"]])
            vorbis_candidates = []
            for key, val in tags.items():
                kl = key.lower()
                if "rating" in kl or "score" in kl or "stars" in kl:
                    vorbis_candidates.append((kl, _unwrap(val)))
            rating = _best_rating(vorbis_candidates)

        elif isinstance(audio, ASF):
            name_map = {k_name.lower(): k_name for k_name in tags.keys()}
            if key_name := name_map.get("title"):
                title = _first_text(tags.get(key_name))
            if key_name := name_map.get("wm/albumartist") or name_map.get("author"):
                artist = _first_text(tags.get(key_name))
            if key_name := name_map.get("wm/tracknumber") or name_map.get(
                "tracknumber"
            ):
                trackno = _parse_track_number(tags.get(key_name))
            if key_name := name_map.get("wm/albumtitle"):
                album = _first_text(tags.get(key_name))
            if key_name := name_map.get("wm/genre"):
                genre = _first_text(tags.get(key_name))
            asf_candidates = []
            for key, val in tags.items():
                kl = key.lower()
                if "rating" in kl:
                    asf_candidates.append((kl, _unwrap(val)))
            rating = _best_rating(asf_candidates)

        # Fallback: generic tag iteration for album/genre if still missing
        if album is None or genre is None:
            for k, v in tags.items():
                kl = str(k).lower()
                if album is None and kl == "album":
                    album = _first_text(v)
                if genre is None and kl in ("genre", "wm/genre"):
                    genre = _first_text(v)
            if album is None:
                getall_fn = getattr(tags, "getall", None)
                if getall_fn:
                    talb = getall_fn("TALB")
                    if talb:
                        album = _first_text(talb[0])

    except Exception:
        pass

    # The year read is a late generic pass (unlike the per-container fields):
    # year keys share no frame/atom machinery, only naming, so suffix matching
    # over the raw tag dict covers every container in one place.
    year = _year_from_tags(tags) if tags else None

    return TagBundle(
        title, artist, trackno, album, genre, rating, duration_s, bitrate_kbps, year
    )
