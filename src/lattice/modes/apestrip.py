"""Strip stray APEv2 tags from MP3 files, losslessly (the apestrip write mode).

Promoted from scripts/apestrip.py (v1.2.0), which now launches this module as
a compat shim. Some MP3s (notably torrent rips) carry an APEv2 tag *in
addition to* their ID3 tags. Players that read APEv2 on MP3 (foobar2000,
DeaDBeeF) merge the APE values over the ID3 ones, so a stray APE genre like
"Trash Metal" keeps reappearing no matter how many times you fix the ID3
genre — standard tag editors never touch the APEv2 block. retag.py removes
APEv2 only as a side effect of rewriting the genre; this is the general
stripper.

By default apestrip simply **deletes** the APEv2 block and leaves ID3 untouched.
The whole point is to drop the stray APE values (the genre being the usual
culprit), so absorbing them back into ID3 would defeat the tool — it is exactly
how a bad APE genre ends up baked into ID3.

Pass keep_metadata to opt in to migration: before deleting the APEv2 tag,
every APE field ID3 does not already carry is copied into the right ID3 frame.
The redundancy check is presence-level: a field ID3 already has, whatever its
value, is treated as authoritative and not overwritten. Binary items other than
an embedded front cover have no ID3 home and are reported, not migrated:

    Year/Date          -> TDRC        Title/Artist/Album    -> TIT2/TPE1/TALB
    Album Artist/Band  -> TPE2        Track/Disc            -> TRCK/TPOS
    Composer/Publisher -> TCOM/TPUB   Comment               -> COMM
    Cover Art (Front)  -> APIC        Unsynced lyrics       -> USLT
    sort orders        -> TSOP/TSO2/TSOT/TSOA
    anything else (MusicBrainz IDs, ISRC, barcode, ReplayGain, ...) -> TXXX:<key>

Two deliberate exceptions hold even under keep_metadata:
  * Genre is NEVER migrated. ID3 stays authoritative; the APE genre is exactly the
    value we distrust. (If a file has no ID3 genre at all, it is reported, not
    invented.)
  * Rating is NEVER written. APE and POPM use different rating scales, so an
    auto-conversion would corrupt star counts (see rerate.py). APE ratings are
    reported so you can apply them deliberately.

APE genre and ratings are always reported (even without keep_metadata) so you
can see what is being dropped.

Destructive: it rewrites tags in place. Dry-run is the default, both here and
in the `lattice --apestrip` mode; a real run prints the worklist and asks for
confirmation. MP3-only; other formats carry their own authoritative tags and
are skipped. Recursive over the given directory, so it handles one album or a
whole library. Idempotent: a file with no APEv2 tag is left untouched. Every
strip or migration is recorded in an append-only timestamped log (default
<root>/apestrip.log).

This is one of the package's two write modes (the other is
lattice.modes.clean). `main` keeps the companion script's historical
apply-by-default contract for scripts/apestrip.py.
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

from mutagen.apev2 import APENoHeaderError, APEv2
from mutagen.id3 import (
    APIC,
    COMM,
    ID3,
    TALB,
    TBPM,
    TCOM,
    TCOP,
    TDRC,
    TENC,
    TEXT,
    TIT1,
    TIT2,
    TKEY,
    TLAN,
    TPE1,
    TPE2,
    TPOS,
    TPUB,
    TRCK,
    TSO2,
    TSOA,
    TSOP,
    TSOT,
    TSRC,
    TXXX,
    USLT,
    Frame,
    ID3NoHeaderError,
    ParseID3v1,
)
from mutagen.mp3 import MP3
from vir_tui import core as ui

# APE key (lowercased) -> simple ID3 text frame class. Genre/Rating/Comment/cover/
# lyrics are handled out of band; every other *text* item is preserved via a
# TXXX:<key> passthrough. Binary items (other than the front cover) have no ID3
# representation and are reported, then dropped with the tag.
SIMPLE_FRAMES: dict[str, type[Frame]] = {
    "title": TIT2,
    "artist": TPE1,
    "album": TALB,
    "band": TPE2,
    "album artist": TPE2,
    "albumartist": TPE2,
    "track": TRCK,
    "tracknumber": TRCK,
    "disc": TPOS,
    "discnumber": TPOS,
    "year": TDRC,
    "date": TDRC,
    "composer": TCOM,
    "publisher": TPUB,
    "label": TPUB,
    "copyright": TCOP,
    "grouping": TIT1,
    "bpm": TBPM,
    "language": TLAN,
    "isrc": TSRC,
    "initial key": TKEY,
    "encoded by": TENC,
    "lyricist": TEXT,
    "artistsortorder": TSOP,
    "albumartistsortorder": TSO2,
    "titlesortorder": TSOT,
    "albumsortorder": TSOA,
}

_COVER_KEYS = {"cover art (front)", "cover art(front)", "coverart", "cover"}
_LYRICS_KEYS = {"unsynced lyrics", "unsynchronised lyrics", "unsyncedlyrics", "lyrics"}

AUDIO_EXT = ".mp3"


def classify_ape_field(key: str) -> tuple[str, type[Frame] | str | None]:
    """Map an APE key to an action + payload. The pure brain of the migration.

    Returns one of:
      ("genre", None)            -> never migrated (ID3 authoritative)
      ("rating", None)           -> never written, only reported
      ("comment", None)          -> COMM frame
      ("cover", None)            -> APIC front frame
      ("lyrics", None)           -> USLT frame
      ("frame", <ID3 class>)     -> a simple text frame
      ("txxx", "<desc>")         -> TXXX passthrough (lossless fallback)
    """
    k = key.strip().lower()
    if k == "genre":
        return ("genre", None)
    if k == "rating":
        return ("rating", None)
    if k == "comment":
        return ("comment", None)
    if k in _COVER_KEYS:
        return ("cover", None)
    if k in _LYRICS_KEYS:
        return ("lyrics", None)
    if k in SIMPLE_FRAMES:
        return ("frame", SIMPLE_FRAMES[k])
    return ("txxx", key.strip())


def _ape_values(value) -> list[str]:
    """Values of an APE text item as a list (mutagen renders multi-value items
    NUL-joined; a NUL embedded in a v2.3 UTF-16 frame truncates the string, so
    the values must stay separate). Empty list for binary values."""
    try:
        if getattr(value, "kind", 0) == 1:  # BINARY
            return []
        return str(value).split("\x00")
    except Exception:
        return []


def _ape_text(value) -> str:
    """Single readable string of an APE value, for labels/reports and USLT."""
    return " / ".join(_ape_values(value))


def _img_mime(data: bytes) -> str | None:
    """MIME type of embedded image data; None when the format is unrecognized
    (a cover that can't be labeled honestly is skipped, not mislabeled JPEG)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


def _frame_nonempty(frame) -> bool:
    text = getattr(frame, "text", None)
    if not text:
        return False
    return str(text[0]).strip() != ""


def id3_has_equivalent(
    id3: ID3, action: str, payload: type[Frame] | str | None
) -> bool:
    """True if ID3 already carries the datum, so the APE field is redundant."""
    if action in ("genre", "rating"):
        return True  # handled out of band; never migrated
    if action == "frame":
        fr = id3.get(cast("type[Frame]", payload).__name__)
        return fr is not None and _frame_nonempty(fr)
    if action == "comment":
        # Only an unqualified comment (empty desc) counts: a COMM:iTunNORM:eng
        # normalization blob is not "already has a comment".
        return any(_frame_nonempty(fr) for fr in id3.getall("COMM") if not fr.desc)
    if action == "cover":
        return any(k.startswith("APIC") for k in id3)
    if action == "lyrics":
        return any(k.startswith("USLT") for k in id3)
    if action == "txxx":
        desc = str(payload).lower()
        for k in id3:
            if k.startswith("TXXX:") and k.split(":", 1)[1].lower() == desc:
                if _frame_nonempty(id3[k]):
                    return True
        return False
    return False


def migration_label(action: str, payload: type[Frame] | str | None, value) -> str:
    """Human-readable description of a migration, for preview and logging."""
    if action == "frame":
        return f"{cast('type[Frame]', payload).__name__} = {_ape_text(value)!r}"
    if action == "comment":
        return f"COMM = {_ape_text(value)!r}"
    if action == "lyrics":
        return f"USLT ({len(_ape_text(value))} chars)"
    if action == "cover":
        img = _cover_bytes(value)
        return f"APIC front ({_img_mime(img)}, {len(img)} bytes)"
    if action == "txxx":
        return f"TXXX:{payload} = {_ape_text(value)!r}"
    return "?"


def _cover_bytes(value) -> bytes:
    raw = value.value if getattr(value, "kind", 0) == 1 else b""
    _, sep, img = raw.partition(b"\x00")
    return img if sep else raw


def apply_migration(
    id3: ID3, action: str, payload: type[Frame] | str | None, value
) -> str:
    """Write one APE field into the ID3 object. Returns its migration label."""
    if action == "frame":
        cls = cast("type[Frame]", payload)
        id3.setall(cls.__name__, [cls(encoding=3, text=_ape_values(value) or [""])])
    elif action == "comment":
        id3.add(COMM(encoding=3, lang="eng", desc="", text=_ape_values(value) or [""]))
    elif action == "lyrics":
        id3.add(USLT(encoding=3, lang="eng", desc="", text=_ape_text(value)))
    elif action == "cover":
        img = _cover_bytes(value)
        id3.add(APIC(encoding=3, mime=_img_mime(img), type=3, desc="", data=img))
    elif action == "txxx":
        id3.add(TXXX(encoding=3, desc=str(payload), text=_ape_values(value) or [""]))
    return migration_label(action, payload, value)


@dataclass
class FileResult:
    path: str
    had_ape: bool = False
    migrated: list[tuple[str, str]] = field(default_factory=list)
    redundant: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    ratings: list[str] = field(default_factory=list)
    genre_warning: bool = False
    stripped: bool = False
    repaired: bool = False
    malformed: bool = False
    error: str | None = None


def _id3_for(path: str) -> ID3:
    """ID3 object used to plan migrations. On older mutagen (<1.46) ID3()
    raises for a v1-only file, so the fresh ID3() is seeded from the trailing
    v1 block: otherwise every APE field looks sole-source, and the eventual
    save(v1=2) would rebuild ID3v1 from the sparse v2 frames, blanking
    whatever title/artist/album the old ID3v1 held. (mutagen 1.46+ loads v1
    into ID3() itself, in which case the except branch never fires.)"""
    try:
        return ID3(path)
    except ID3NoHeaderError:
        id3 = ID3()
        try:
            with open(path, "rb") as fh:
                fh.seek(-128, os.SEEK_END)
                tail = fh.read(128)
        except OSError:
            return id3
        if tail[:3] == b"TAG":
            for frame in (ParseID3v1(tail) or {}).values():
                id3.add(frame)
        return id3


def _id3_has_genre(id3: ID3) -> bool:
    fr = id3.get("TCON")
    return fr is not None and _frame_nonempty(fr)


def _has_raw_ape_signature(path: str, window: int = 262144) -> bool:
    """True if an APETAGEX signature is present near the end of the file.

    A cheap second opinion for when mutagen's APEv2 reader raises: a malformed
    tag still leaves the signature on disk, and we would rather flag it than
    pretend the file is clean.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(-min(size, window), os.SEEK_END)
            return b"APETAGEX" in fh.read()
    except OSError:
        return False


_APE_IS_HEADER = 0x80000000


class _RawAPEValue:
    """A stand-in for a mutagen APE value, built from a manually parsed item.

    Implements only the surface the migration path relies on (`kind`, `value`,
    `str()`), so items recovered from a malformed tag can flow through the same
    planning/migration code as mutagen-parsed ones.
    """

    def __init__(self, kind: int, data: bytes):
        self.kind = kind
        self.value = data

    def __str__(self) -> str:
        return self.value.decode("utf-8", "replace")


def _parse_raw_ape(data: bytes):
    """Parse a malformed APEv2 tag straight from bytes.

    Returns ``(header_start, excise_end, items)`` where excising
    ``data[header_start:excise_end]`` removes the whole APE block (and any junk
    between its footer and a strictly terminal ID3v1), or ``None`` if no
    structurally consistent tag is found. The consistency check (footer exactly
    where the header's size field points) is what makes the byte surgery safe:
    it proves ``header_start`` is a real tag boundary, not a chance signature
    in the audio. Recognized trailing structures after the APE block (a
    Lyrics3v2 block, a terminal ID3v1) are preserved; unrecognized trailing
    bytes in any other shape (e.g. an ID3v1 followed by padding, which the old
    end-anchoring would have mis-cut) make the parse refuse rather than guess.
    """
    h = data.find(b"APETAGEX")
    if h == -1 or len(data) < h + 32:
        return None
    _ver, size, count, flags = struct.unpack("<IIII", data[h + 8 : h + 24])
    if not (flags & _APE_IS_HEADER):
        return None  # first signature must be the header
    footer = data.rfind(b"APETAGEX")
    if footer != h + size or footer <= h:
        return None  # footer not where the header claims; refuse to touch
    n = len(data)
    ape_end = footer + 32
    if ape_end > n:
        return None  # truncated footer
    if ape_end == n:
        excise_end = ape_end  # APE block is strictly terminal
    elif data[ape_end : ape_end + 11] == b"LYRICSBEGIN":
        # A Lyrics3v2 block is a real structure, not junk; it (and a terminal
        # ID3v1 after it) is preserved byte for byte.
        lyr = data.find(b"LYRICS200", ape_end)
        if lyr == -1:
            return None
        after = lyr + 9
        if after != n and not (n - after == 128 and data[after : after + 3] == b"TAG"):
            return None  # unrecognized bytes after the lyrics block
        excise_end = ape_end
    elif n - ape_end == 128 and data[ape_end : ape_end + 3] == b"TAG":
        excise_end = ape_end  # clean terminal ID3v1, preserved
    elif ape_end < n - 128 and data[n - 128 : n - 125] == b"TAG":
        # Junk between the APE block and a strictly terminal ID3v1 (the
        # downloader-damage shape that motivated --repair-malformed): the junk
        # is excised with the tag, the ID3v1 kept. A Lyrics3 block hiding in
        # that region is a real structure, not junk; refuse rather than cut it.
        if b"LYRICSBEGIN" in data[ape_end : n - 128]:
            return None
        excise_end = n - 128
    else:
        return None  # unrecognized trailing data; refuse rather than guess
    items: dict[str, _RawAPEValue] = {}
    p = h + 32  # items begin after the 32-byte header
    for _ in range(count):
        if p + 8 > footer:
            break
        vlen, vflags = struct.unpack("<II", data[p : p + 8])
        p += 8
        ks = data.find(b"\x00", p)
        if ks == -1 or ks >= footer:
            break
        key = data[p:ks].decode("latin1")
        p = ks + 1
        kind = (vflags >> 1) & 3
        if p + vlen > footer:
            break  # value length overruns the footer: malformed item, stop here
        items[key] = _RawAPEValue(1 if kind == 1 else 0, data[p : p + vlen])
        p += vlen
    return h, excise_end, items


def repair_file(path: str, dry_run: bool, keep_metadata: bool = False) -> FileResult:
    """Repair a malformed APEv2 tag mutagen cannot parse by excising it.

    Same contract as the normal path: with keep_metadata, sole-source fields are
    migrated into ID3 first (genre is never migrated, ratings are reported);
    without it the block is simply excised. The APE region is then cut out of the
    file bytes directly; the result is written to a temp file, verified (decodes,
    no APE signature survives), and atomically swapped in.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        return FileResult(path, had_ape=True, error=str(e))
    parsed = _parse_raw_ape(data)
    if parsed is None:
        return FileResult(
            path,
            had_ape=True,
            error="malformed APEv2 tag could not be safely parsed for repair "
            "(inconsistent structure or unrecognized trailing data)",
        )
    header_start, excise_end, items = parsed

    try:
        id3 = _id3_for(path)
    except Exception as e:
        return FileResult(path, had_ape=True, error=str(e))

    migrations, redundant, ratings, genre_warning, skipped = plan_file(
        id3, items, keep_metadata
    )
    result = FileResult(
        path,
        had_ape=True,
        redundant=redundant,
        skipped=skipped,
        ratings=ratings,
        genre_warning=genre_warning,
        malformed=True,
    )

    if dry_run:
        result.migrated = [
            (key, migration_label(action, payload, value))
            for key, action, payload, value in migrations
        ]
        return result

    for key, action, payload, value in migrations:
        result.migrated.append((key, apply_migration(id3, action, payload, value)))

    # Everything before header_start (ID3v2 + audio) and recognized trailers
    # (a Lyrics3v2 block, the terminal ID3v1) are preserved byte for byte; the
    # cut covers only the APE block plus junk before a terminal ID3v1.
    new = data[:header_start] + data[excise_end:]
    tmp_dir = os.path.dirname(path) or "."
    try:
        fd, tmp = tempfile.mkstemp(dir=tmp_dir, suffix=".apestrip.tmp")
    except OSError as e:
        result.error = f"repair failed: {e}"
        return result
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(new)
            out.flush()
            os.fsync(out.fileno())
        if migrations:
            id3.save(tmp, v2_version=3, v1=2)
            # The save reopened and rewrote the temp file after the fsync
            # above; sync again so os.replace can't install a half-flushed tag.
            with open(tmp, "r+b") as sf:
                os.fsync(sf.fileno())
        with open(tmp, "rb") as vf:
            if b"APETAGEX" in vf.read():
                raise ValueError("APE signature survived repair")
        if getattr(MP3(tmp).info, "sample_rate", 0) <= 0:
            raise ValueError("repaired file does not decode")
        shutil.copymode(path, tmp)  # mkstemp created it 0600
        os.replace(tmp, path)
        result.repaired = True
        result.stripped = True
    except Exception as e:
        if os.path.exists(tmp):
            os.unlink(tmp)
        result.error = f"repair failed: {e}"
    return result


def _handle_malformed(
    path: str, dry_run: bool, repair: bool, keep_metadata: bool = False
) -> FileResult:
    if repair:
        return repair_file(path, dry_run, keep_metadata)
    return FileResult(
        path,
        had_ape=True,
        error="malformed APEv2 tag (mutagen cannot parse); "
        "not stripped (use --repair-malformed)",
    )


def plan_file(
    id3: ID3, ape: APEv2 | dict[str, _RawAPEValue], keep_metadata: bool = False
):
    """Decide, for one file's parsed tags, what to migrate / drop / report.

    Pure: inspects but does not mutate. Returns (migrations, redundant, ratings,
    genre_warning, skipped), where migrations is a list of (key, action,
    payload, value) and skipped is a list of (key, reason) for fields that have
    no honest ID3 representation (binary blobs, external cover references,
    unrecognized image data): those are reported and dropped with the tag,
    never written as junk frames.

    Without keep_metadata the default is a pure strip: nothing is migrated, so
    migrations, redundant, and skipped come back empty. Genre and rating are
    still scanned so they can be reported (the values being dropped).
    """
    migrations = []
    redundant: list[str] = []
    ratings: list[str] = []
    skipped: list[tuple[str, str]] = []
    has_ape_genre = False
    for key in ape:
        value = ape[key]
        action, payload = classify_ape_field(key)
        if action == "genre":
            has_ape_genre = True
            continue
        if action == "rating":
            ratings.append(_ape_text(value))
            continue
        if not keep_metadata:
            continue  # strip-only: drop every other field with the tag
        if action == "cover":
            img = _cover_bytes(value)
            if getattr(value, "kind", 0) != 1 or not img:
                skipped.append((key, "not embedded image data (not migrated)"))
                continue
            if _img_mime(img) is None:
                skipped.append((key, "unrecognized image format (not migrated)"))
                continue
        elif getattr(value, "kind", 0) == 1:
            skipped.append((key, "binary value (not migrated)"))
            continue
        if id3_has_equivalent(id3, action, payload):
            redundant.append(key)
            continue
        migrations.append((key, action, payload, value))
    genre_warning = has_ape_genre and not _id3_has_genre(id3)
    return migrations, redundant, ratings, genre_warning, skipped


def process_file(
    path: str, dry_run: bool, repair: bool = False, keep_metadata: bool = False
) -> FileResult:
    """Plan and (unless dry_run) execute the strip for one MP3."""
    try:
        ape = APEv2(path)
    except APENoHeaderError:
        # mutagen sees no APE tag, but a raw APETAGEX signature can still be
        # present if the tag is malformed (e.g. a footer with the IS_HEADER bit
        # wrongly set). Repair it if asked, otherwise report it rather than
        # silently leaving it behind.
        if _has_raw_ape_signature(path):
            return _handle_malformed(path, dry_run, repair, keep_metadata)
        return FileResult(path, had_ape=False)
    except Exception:  # malformed tag mutagen chokes on
        if _has_raw_ape_signature(path):
            return _handle_malformed(path, dry_run, repair, keep_metadata)
        return FileResult(path, had_ape=True, error="unreadable APEv2 tag")

    try:
        id3 = _id3_for(path)
    except Exception as e:
        return FileResult(path, had_ape=True, error=str(e))

    migrations, redundant, ratings, genre_warning, skipped = plan_file(
        id3, ape, keep_metadata
    )
    result = FileResult(
        path,
        had_ape=True,
        redundant=redundant,
        skipped=skipped,
        ratings=ratings,
        genre_warning=genre_warning,
    )

    if dry_run:
        result.migrated = [
            (key, migration_label(action, payload, value))
            for key, action, payload, value in migrations
        ]
        return result

    try:
        for key, action, payload, value in migrations:
            label = apply_migration(id3, action, payload, value)
            result.migrated.append((key, label))
        # Delete the APE tag first (retag.py ordering). Re-save ID3 only when we
        # actually migrated something; a pure strip leaves ID3 byte-for-byte.
        APEv2(path).delete()
        if migrations:
            id3.save(path, v2_version=3, v1=2)
        try:
            APEv2(path)
            result.error = "APEv2 tag still present after delete"
        except APENoHeaderError:
            result.stripped = True
    except Exception as e:
        result.error = str(e)
    return result


def _iter_mp3s(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune hidden dirs (.testing/ album copies etc.), like rerate.py and
        # replaygain.py. This is the destructive one, so scanning a hidden
        # working copy was the worst place for the inconsistency to live.
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            if fn.lower().endswith(AUDIO_EXT):
                yield os.path.join(dirpath, fn)


def run_apestrip(
    root,
    *,
    dry_run: bool = True,
    keep_metadata: bool = False,
    repair_malformed: bool = False,
    log_path=None,
    assume_yes: bool = False,
    quiet: bool = False,
    _title: str = "lattice apestrip - APEv2 Tag Stripper",
) -> int:
    """Run the APEv2 strip over one directory tree.

    The package write mode behind `lattice --apestrip`: dry-run by default,
    apply only on an explicit opt-in (dry_run=False, the CLI's --apply). A real
    run keeps the script's two-phase shape: a dry planning pass builds the
    worklist, the worklist is printed, and (unless assume_yes or stdin is not a
    TTY) a confirmation prompt guards the write pass — a non-interactive run
    proceeds, so cron and pipelines keep working. Every strip or migration is
    recorded to an append-only timestamped log (default <root>/apestrip.log).
    Returns a process exit code: 1 when the root is unusable, the log is
    unopenable, or any file errored, else 0.
    """
    root = str(root)
    if not os.path.isdir(root):
        print(f"[!] Directory not found: {root}", file=sys.stderr)
        return 1

    if log_path is None:
        log_path = os.path.join(root, "apestrip.log")

    if not quiet:
        ui.print_header(f"{_title}{' [DRY RUN]' if dry_run else ''}")
        print(f"Target: {root}")
        print(f"Migrate metadata: {'Yes' if keep_metadata else 'No'}\n")

    log_fh = None
    if not dry_run:
        try:
            log_fh = open(log_path, "a", encoding="utf-8")
        except OSError as e:
            # rerate.py/replaygain.py report this and exit 1; an unwritable log
            # path used to be a bare traceback here.
            print(ui.error(f"cannot open log file {log_path}: {e}"), file=sys.stderr)
            return 1

    def log(msg: str) -> None:
        ui.tqdm.write(msg)
        if log_fh is not None:
            ts = datetime.now().isoformat(timespec="seconds")
            log_fh.write(f"[{ts}] {ui.color(msg, '')}\n")  # write uncolored to log

    try:
        # Planning pass: build the worklist without writing.
        worklist: list[FileResult] = []
        # Pre-scan for progress bar
        mp3_files = list(
            ui.tqdm(_iter_mp3s(root), desc=ui.info("Scanning directories"), leave=False)
        )

        for path in ui.tqdm(mp3_files, desc=ui.info("Analyzing tags")):
            r = process_file(
                path,
                dry_run=True,
                repair=repair_malformed,
                keep_metadata=keep_metadata,
            )
            if r.had_ape:
                worklist.append(r)

        if not worklist:
            print(ui.success("No MP3s with an APEv2 tag found. Nothing to do."))
            return 0

        head = "[DRY RUN] " if dry_run else ""
        print(f"{head}APEv2 tags found in {len(worklist)} file(s) under {root}\n")
        rating_files = 0
        warn_files = 0
        for r in worklist:
            rel = os.path.relpath(r.path, root)
            if r.error:
                print(f"  [!] {rel}: {r.error}")
                continue
            print(f"  {rel}{'  [repair malformed]' if r.malformed else ''}")
            for key, label in r.migrated:
                print(f"      migrate {key!r} -> {label}")
            if not r.migrated:
                if keep_metadata:
                    print("      (APE fully redundant with ID3; strip only)")
                else:
                    print("      (strip only; --keep-metadata to migrate into ID3)")
            for key, reason in r.skipped:
                print(f"      [skip] APE {key!r}: {reason}")
            for val in r.ratings:
                rating_files += 1
                print(f"      [rating] APE Rating={val!r} (reported, not written)")
            if r.genre_warning:
                warn_files += 1
                print("      [warn] APE genre present but ID3 has no genre; left blank")

        total_migrations = sum(len(r.migrated) for r in worklist)
        total_skipped = sum(len(r.skipped) for r in worklist)
        skip_note = (
            f" {total_skipped} unmigratable field(s) skipped," if total_skipped else ""
        )
        print(
            f"\n{head}{len(worklist)} file(s), {total_migrations} field migration(s),"
            f"{skip_note} "
            f"{rating_files} rating(s) reported, {warn_files} genre warning(s)."
        )

        if dry_run:
            print("\nDry run: no files modified.")
            return 0

        # Confirmation, replaygain.py style. assume_yes stands in for the
        # script's --yes (the TUI passes it after its own ask_yn confirm).
        if not assume_yes and sys.stdin.isatty():
            try:
                ans = input("\nStrip these APEv2 tags? [y/N] ").strip().lower()
            except EOFError:
                ans = ""
            if ans not in ("y", "yes"):
                log("Aborted by user; no files modified.")
                return 0

        log(f"Stripping APEv2 from {len(worklist)} file(s) under {root}")
        stripped = 0
        repaired = 0
        errors = 0
        for r0 in worklist:
            r = process_file(
                r0.path,
                dry_run=False,
                repair=repair_malformed,
                keep_metadata=keep_metadata,
            )
            rel = os.path.relpath(r.path, root)
            if r.error:
                errors += 1
                log(f"  [!] {rel}: {r.error}")
                continue
            if r.stripped:
                stripped += 1
            if r.repaired:
                repaired += 1
            for key, label in r.migrated:
                log(f"  {rel}: migrated {key!r} -> {label}")
            for key, reason in r.skipped:
                log(f"  {rel}: [skip] APE {key!r}: {reason}")
            for val in r.ratings:
                log(f"  {rel}: [rating] APE Rating={val!r} (not written)")
            if r.genre_warning:
                log(f"  {rel}: [warn] no ID3 genre after strip")
            if r.repaired:
                log(f"  {rel}: repaired (excised malformed APEv2 tag)")
            elif r.stripped:
                log(f"  {rel}: stripped APEv2 tag")
            else:
                # e.g. the tag vanished between the planning and write passes;
                # the audit log must not claim a strip that never happened.
                log(f"  {rel}: no APEv2 tag at write time (skipped)")
        log(
            f"-> stripped {stripped} file(s) "
            f"({repaired} via malformed-tag repair); {errors} error(s)."
        )
        if errors:
            return 1
    finally:
        if log_fh is not None:
            log_fh.close()

    return 0


def main(argv: list[str] | None = None) -> int:
    """Companion-script CLI for the APEv2 strip, preserving scripts/apestrip.py's
    historical contract: apply by default, `--dry-run` to opt out. The package
    mode (`lattice --apestrip`) goes through run_apestrip instead, which
    dry-runs by default."""
    parser = argparse.ArgumentParser(
        description="Losslessly strip stray APEv2 tags from MP3 files."
    )
    parser.add_argument("directory", help="Directory to walk (album or library root)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview migrations and strips; write nothing",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt (auto-skipped when stdin is not a TTY)",
    )
    parser.add_argument(
        "--keep-metadata",
        action="store_true",
        help="Before stripping, migrate APE fields not already in ID3 into the "
        "matching ID3 frame (genre is never migrated, ratings never written). "
        "Off by default: the default is a pure strip that leaves ID3 untouched.",
    )
    parser.add_argument(
        "--repair-malformed",
        action="store_true",
        help="Also repair malformed APE tags mutagen cannot parse, by excising "
        "the tag bytes directly (verified + atomic). Off by default; without it "
        "such files are only reported.",
    )
    parser.add_argument(
        "--log",
        dest="log_path",
        default=None,
        help="Append a timestamped record to this file "
        "(default: <directory>/apestrip.log)",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 1.3.0")
    args = parser.parse_args(argv)

    return run_apestrip(
        args.directory,
        dry_run=args.dry_run,
        keep_metadata=args.keep_metadata,
        repair_malformed=args.repair_malformed,
        log_path=args.log_path,
        assume_yes=args.yes,
        _title="apestrip.py - APEv2 Tag Stripper",
    )
