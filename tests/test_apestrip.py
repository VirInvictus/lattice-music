import contextlib
import io
import os
import shutil
import stat
import struct
import sys
import tempfile
import unittest
from pathlib import Path

# apestrip's brain lives in the lattice package now (promoted out of scripts/,
# which launches it as a shim). It writes tags, so it is exercised against a
# copy of the committed MP3 fixture, seeded with an APEv2 tag. The fixture's
# ID3 has TIT2/TPE1/TALB/TCON(Rock)/TRCK and no date, which lets us cover
# redundant fields, sole-source migration, and the genre/rating policy in one
# place.
from lattice.modes import apestrip
from mutagen.apev2 import (
    BINARY,
    EXTERNAL,
    TEXT,
    APENoHeaderError,
    APEv2,
    APEValue,
)
from mutagen.id3 import COMM, ID3
from mutagen.mp3 import MP3

FIXTURES = Path(__file__).parent / "fixtures" / "library"
MP3_SRC = FIXTURES / "Cursive" / "Domestica" / "01 - The Casualty.mp3"

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9"


def _build_malformed_ape(items, binary_keys=()):
    """Hand-build an APE tag whose footer has the IS_HEADER bit wrongly set,
    followed by junk bytes — the structure mutagen refuses to parse but
    _parse_raw_ape can recover (matches the real-world Lil Wayne rips)."""
    body = b""
    for k, v in items.items():
        kind = 1 if k in binary_keys else 0
        body += struct.pack("<II", len(v), kind << 1) + k.encode("latin1") + b"\x00" + v
    size = len(body) + 32
    header = (
        b"APETAGEX"
        + struct.pack("<IIII", 2000, size, len(items), 0xA0000000)
        + b"\x00" * 8
    )
    footer = (
        b"APETAGEX"
        + struct.pack("<IIII", 2000, size, len(items), 0x80000000)
        + b"\x00" * 8
    )
    junk = b"\xde\xad\xbe\xef\xba\x4c\xa3\xad\x73\x9e\x09\x00"
    return header + body + footer + junk + b"TAG" + b"\x00" * 125


def _write_malformed(path):
    base = Path(MP3_SRC).read_bytes()
    blob = _build_malformed_ape(
        {"Title": b"The Casualty", "GENRE": b"Rap", "YEAR": b"2011"}
    )
    Path(path).write_bytes(base + blob)


class ClassifyTests(unittest.TestCase):
    def test_core_text_field(self):
        action, payload = apestrip.classify_ape_field("Year")
        self.assertEqual(action, "frame")
        self.assertEqual(payload.__name__, "TDRC")

    def test_sort_order(self):
        action, payload = apestrip.classify_ape_field("Albumartistsortorder")
        self.assertEqual(action, "frame")
        self.assertEqual(payload.__name__, "TSO2")

    def test_genre_is_skip(self):
        self.assertEqual(apestrip.classify_ape_field("Genre"), ("genre", None))

    def test_rating_is_report(self):
        self.assertEqual(apestrip.classify_ape_field("Rating"), ("rating", None))

    def test_cover_and_lyrics(self):
        self.assertEqual(apestrip.classify_ape_field("Cover Art (Front)")[0], "cover")
        self.assertEqual(apestrip.classify_ape_field("Unsynced lyrics")[0], "lyrics")

    def test_comment(self):
        self.assertEqual(apestrip.classify_ape_field("Comment"), ("comment", None))

    def test_unknown_passthrough(self):
        action, payload = apestrip.classify_ape_field("Barcode")
        self.assertEqual(action, "txxx")
        self.assertEqual(payload, "Barcode")


class ApeStripTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "track.mp3")
        shutil.copy(MP3_SRC, self.path)
        ape = APEv2()
        ape["Year"] = APEValue("1986", TEXT)  # sole-source -> migrate
        ape["Title"] = APEValue("The Casualty", TEXT)  # redundant with ID3
        ape["Genre"] = APEValue("Trash Metal", TEXT)  # stray -> never migrate
        ape["Comment"] = APEValue("from the rip", TEXT)  # sole-source -> COMM
        ape["Barcode"] = APEValue("075992413114", TEXT)  # unknown -> TXXX
        ape["Rating"] = APEValue("4", TEXT)  # report only
        ape["Unsynced lyrics"] = APEValue("la la la", TEXT)  # -> USLT
        ape["Cover Art (Front)"] = APEValue(b"cover.jpg\x00" + JPEG, BINARY)  # -> APIC
        ape.save(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self):
        return apestrip.process_file(self.path, dry_run=False, keep_metadata=True)

    def test_ape_tag_removed(self):
        r = self._run()
        self.assertTrue(r.stripped)
        self.assertIsNone(r.error)
        with self.assertRaises(APENoHeaderError):
            APEv2(self.path)

    def test_genre_not_migrated(self):
        self._run()
        id3 = ID3(self.path)
        self.assertEqual(id3["TCON"].text, ["Rock"])  # APE "Trash Metal" ignored

    def test_sole_source_year_preserved(self):
        self._run()
        id3 = ID3(self.path)
        frame = id3.get("TDRC") or id3.get("TYER")
        self.assertIsNotNone(frame)
        self.assertIn("1986", str(frame))

    def test_redundant_title_not_duplicated(self):
        self._run()
        id3 = ID3(self.path)
        self.assertEqual(id3["TIT2"].text, ["The Casualty"])

    def test_unknown_field_to_txxx(self):
        self._run()
        id3 = ID3(self.path)
        self.assertIn("TXXX:Barcode", id3)
        self.assertEqual(id3["TXXX:Barcode"].text, ["075992413114"])

    def test_comment_to_comm(self):
        self._run()
        id3 = ID3(self.path)
        comms = [id3[k] for k in id3 if k.startswith("COMM")]
        self.assertTrue(any("from the rip" in str(c.text[0]) for c in comms))

    def test_cover_to_apic(self):
        self._run()
        id3 = ID3(self.path)
        apics = [id3[k] for k in id3 if k.startswith("APIC")]
        self.assertEqual(len(apics), 1)
        self.assertEqual(apics[0].data, JPEG)
        self.assertEqual(apics[0].mime, "image/jpeg")

    def test_lyrics_to_uslt(self):
        self._run()
        id3 = ID3(self.path)
        uslts = [id3[k] for k in id3 if k.startswith("USLT")]
        self.assertTrue(any("la la la" in str(u.text) for u in uslts))

    def test_rating_reported_not_written(self):
        r = self._run()
        self.assertEqual(r.ratings, ["4"])
        id3 = ID3(self.path)
        self.assertNotIn("TXXX:Rating", id3)
        # No new POPM beyond the fixture's existing one.
        self.assertEqual(len([k for k in id3 if k.startswith("POPM")]), 1)

    def test_idempotent(self):
        self._run()
        second = apestrip.process_file(self.path, dry_run=False, keep_metadata=True)
        self.assertFalse(second.had_ape)
        self.assertFalse(second.stripped)

    def test_dry_run_writes_nothing(self):
        r = apestrip.process_file(self.path, dry_run=True, keep_metadata=True)
        self.assertTrue(r.had_ape)
        self.assertTrue(any(k == "Year" for k, _ in r.migrated))
        # APE tag and original ID3 genre are untouched.
        self.assertIsNotNone(APEv2(self.path))
        self.assertEqual(ID3(self.path)["TCON"].text, ["Rock"])


class StripOnlyDefaultTests(unittest.TestCase):
    """Default behavior (no --keep-metadata): delete the APE block, leave ID3
    byte-for-byte. Nothing is migrated; genre/rating are still reported."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "track.mp3")
        shutil.copy(MP3_SRC, self.path)
        ape = APEv2()
        ape["Year"] = APEValue("1986", TEXT)  # sole-source, but NOT migrated
        ape["Genre"] = APEValue("Trash Metal", TEXT)  # stray, reported
        ape["Barcode"] = APEValue("075992413114", TEXT)  # unknown, NOT migrated
        ape["Rating"] = APEValue("4", TEXT)  # reported
        ape["Cover Art (Front)"] = APEValue(b"cover.jpg\x00" + JPEG, BINARY)
        ape.save(self.path)
        self._id3_before = ID3(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self):
        return apestrip.process_file(self.path, dry_run=False)

    def test_ape_tag_removed(self):
        r = self._run()
        self.assertTrue(r.stripped)
        self.assertIsNone(r.error)
        with self.assertRaises(APENoHeaderError):
            APEv2(self.path)

    def test_nothing_migrated(self):
        r = self._run()
        self.assertEqual(r.migrated, [])
        id3 = ID3(self.path)
        self.assertNotIn("TXXX:Barcode", id3)  # unknown field dropped, not kept
        self.assertEqual(id3["TCON"].text, ["Rock"])  # genre untouched
        self.assertFalse([k for k in id3 if k.startswith("APIC")])  # cover dropped

    def test_id3_untouched(self):
        # A pure strip rewrites no ID3 frames: every frame present before is
        # present after, unchanged, and no new frame appears.
        self._run()
        id3 = ID3(self.path)
        self.assertEqual(set(id3.keys()), set(self._id3_before.keys()))

    def test_genre_and_rating_still_reported(self):
        r = self._run()
        self.assertTrue(r.genre_warning is False)  # ID3 has a genre, so no warning
        self.assertEqual(r.ratings, ["4"])

    def test_dry_run_lists_no_migrations(self):
        r = apestrip.process_file(self.path, dry_run=True)
        self.assertTrue(r.had_ape)
        self.assertEqual(r.migrated, [])
        self.assertEqual(r.ratings, ["4"])
        self.assertIsNotNone(APEv2(self.path))  # untouched


class PlanGuardTests(unittest.TestCase):
    def test_file_without_ape_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "clean.mp3")
            shutil.copy(MP3_SRC, p)
            r = apestrip.process_file(p, dry_run=False)
            self.assertFalse(r.had_ape)
            self.assertFalse(r.stripped)

    def test_raw_signature_detection(self):
        with tempfile.TemporaryDirectory() as d:
            clean = os.path.join(d, "clean.mp3")
            shutil.copy(MP3_SRC, clean)
            self.assertFalse(apestrip._has_raw_ape_signature(clean))
            tagged = os.path.join(d, "tagged.mp3")
            shutil.copy(MP3_SRC, tagged)
            ape = APEv2()
            ape["Genre"] = APEValue("Rap", TEXT)
            ape.save(tagged)
            self.assertTrue(apestrip._has_raw_ape_signature(tagged))

    def test_malformed_ape_is_reported_not_skipped(self):
        # An APETAGEX signature that mutagen cannot parse must be reported with
        # an error and left untouched, never silently treated as "no APE tag".
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "track.mp3")
            shutil.copy(MP3_SRC, p)
            ape = APEv2()
            ape["Genre"] = APEValue("Rap", TEXT)
            ape.save(p)
            data = bytearray(Path(p).read_bytes())
            fidx = data.rfind(b"APETAGEX")
            struct.pack_into("<I", data, fidx + 12, 8)  # bogus tag size
            Path(p).write_bytes(data)
            with self.assertRaises(Exception):
                APEv2(p)  # confirm mutagen now chokes on it
            r = apestrip.process_file(p, dry_run=False)
            self.assertTrue(r.had_ape)
            self.assertFalse(r.stripped)
            self.assertIsNotNone(r.error)

    def test_genre_warning_when_id3_has_no_genre(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "track.mp3")
            shutil.copy(MP3_SRC, p)
            id3 = ID3(p)
            id3.delall("TCON")
            id3.save(p)
            ape = APEv2()
            ape["Genre"] = APEValue("Trash Metal", TEXT)
            ape.save(p)
            r = apestrip.process_file(p, dry_run=True)
            self.assertTrue(r.genre_warning)


def _id3v1_block(title: str, artist: str) -> bytes:
    return (
        b"TAG"
        + title.encode().ljust(30, b"\x00")
        + artist.encode().ljust(30, b"\x00")
        + b"\x00" * 30  # album
        + b"1998"  # year
        + b"\x00" * 30  # comment
        + b"\x11"  # genre 17 (Rock)
    )


class V1OnlyTests(unittest.TestCase):
    """M1: --keep-metadata on an MP3 whose only tag is ID3v1 must preserve the
    v1 values. Without seeing v1, every APE field looks sole-source and
    save(v1=2) rebuilds ID3v1 from the sparse v2 frames, blanking the old
    title/artist. (mutagen 1.46+ reads v1 into ID3() itself; _id3_for seeds it
    by hand for older mutagen. This pins the behavior either way.)"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "track.mp3")
        shutil.copy(MP3_SRC, self.path)
        ID3(self.path).delete(self.path)  # drop the fixture's ID3v2
        ape = APEv2()
        ape["Composer"] = APEValue("Tim Kasher", TEXT)  # sole-source (v1 has none)
        ape["Title"] = APEValue("APE Title", TEXT)  # redundant with the v1 title
        ape.save(self.path)
        # Hand-append the ID3v1 so the layout is audio|APE|ID3v1 (mutagen's
        # APEv2.save would otherwise clobber a pre-existing v1).
        with open(self.path, "ab") as fh:
            fh.write(_id3v1_block("The Casualty", "Cursive"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_id3v1_values_survive_keep_metadata(self):
        r = apestrip.process_file(self.path, dry_run=False, keep_metadata=True)
        self.assertTrue(r.stripped)
        self.assertIsNone(r.error)
        id3 = ID3(self.path)
        self.assertEqual(id3["TIT2"].text, ["The Casualty"])  # v1 title kept
        self.assertEqual(id3["TCOM"].text, ["Tim Kasher"])  # sole-source migrated
        tail = Path(self.path).read_bytes()[-128:]
        self.assertEqual(tail[:3], b"TAG")
        self.assertIn(b"The Casualty", tail)  # regenerated v1 is faithful

    def test_v1_values_count_as_redundant(self):
        r = apestrip.process_file(self.path, dry_run=True, keep_metadata=True)
        self.assertIn("Title", r.redundant)


class MultiValueTests(unittest.TestCase):
    """M2: multi-value APE text items must migrate as separate ID3 text values
    (mutagen NUL-joins them; an embedded NUL truncates a v2.3 UTF-16 frame)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "track.mp3")
        shutil.copy(MP3_SRC, self.path)
        ape = APEv2()
        ape["Composer"] = APEValue("Tim Kasher\x00Matt Maginn", TEXT)
        ape["Barcode"] = APEValue("111\x00222", TEXT)
        ape.save(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_both_values_survive_in_native_frame_and_txxx(self):
        r = apestrip.process_file(self.path, dry_run=False, keep_metadata=True)
        self.assertTrue(r.stripped)
        id3 = ID3(self.path)
        # The v2.3 save renders multi-value text frames slash-joined; the
        # guarantee is that every value survives and no embedded NUL truncates
        # the string (players showed only the first value before).
        tcom = "/".join(id3["TCOM"].text)
        self.assertIn("Tim Kasher", tcom)
        self.assertIn("Matt Maginn", tcom)
        self.assertNotIn("\x00", tcom)
        barcode = "/".join(id3["TXXX:Barcode"].text)
        self.assertIn("111", barcode)
        self.assertIn("222", barcode)
        self.assertNotIn("\x00", barcode)

    def test_label_renders_values_readably(self):
        r = apestrip.process_file(self.path, dry_run=True, keep_metadata=True)
        labels = dict(r.migrated)
        self.assertIn("Tim Kasher / Matt Maginn", labels["Composer"])


class UnmigratableFieldTests(unittest.TestCase):
    """A1/A2/A4: binary items, non-embedded cover references, and covers in an
    unrecognized image format have no honest ID3 home; they are reported as
    skipped and dropped with the tag, never written as junk frames."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "track.mp3")
        shutil.copy(MP3_SRC, self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def _strip(self):
        return apestrip.process_file(self.path, dry_run=False, keep_metadata=True)

    def test_binary_item_skipped_not_empty_txxx(self):
        ape = APEv2()
        ape["Fingerprint"] = APEValue(b"\x01\x02\x03\x04", BINARY)
        ape.save(self.path)
        r = self._strip()
        self.assertTrue(r.stripped)
        self.assertIn(("Fingerprint", "binary value (not migrated)"), r.skipped)
        self.assertNotIn("TXXX:Fingerprint", ID3(self.path))

    def test_external_cover_reference_skipped(self):
        ape = APEv2()
        ape["Cover Art (Front)"] = APEValue("http://example.com/cover.jpg", EXTERNAL)
        ape.save(self.path)
        r = self._strip()
        self.assertTrue(r.stripped)
        self.assertEqual(
            r.skipped,
            [("Cover Art (Front)", "not embedded image data (not migrated)")],
        )
        self.assertFalse([k for k in ID3(self.path) if k.startswith("APIC")])

    def test_unrecognized_image_format_skipped(self):
        ape = APEv2()
        ape["Cover Art (Front)"] = APEValue(b"cover.bmp\x00BMnotarealbitmap", BINARY)
        ape.save(self.path)
        r = self._strip()
        self.assertTrue(r.stripped)
        self.assertIn(
            ("Cover Art (Front)", "unrecognized image format (not migrated)"),
            r.skipped,
        )
        self.assertFalse([k for k in ID3(self.path) if k.startswith("APIC")])

    def test_strip_only_reports_no_skips(self):
        ape = APEv2()
        ape["Fingerprint"] = APEValue(b"\x01\x02", BINARY)
        ape.save(self.path)
        r = apestrip.process_file(self.path, dry_run=False)
        self.assertTrue(r.stripped)
        self.assertEqual(r.skipped, [])


class CommRedundancyTests(unittest.TestCase):
    """A3: a described COMM frame (e.g. COMM:iTunNORM:eng) must not make a real
    APE Comment read as redundant; only an unqualified comment counts."""

    def test_itunnorm_does_not_block_comment_migration(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "track.mp3")
            shutil.copy(MP3_SRC, p)
            id3 = ID3(p)
            id3.add(COMM(encoding=3, lang="eng", desc="iTunNORM", text=[" 00000123"]))
            id3.save(p)
            ape = APEv2()
            ape["Comment"] = APEValue("from the rip", TEXT)
            ape.save(p)
            r = apestrip.process_file(p, dry_run=False, keep_metadata=True)
            self.assertTrue(r.stripped)
            self.assertNotIn("Comment", r.redundant)
            plain = [fr for fr in ID3(p).getall("COMM") if not fr.desc]
            self.assertTrue(any("from the rip" in str(fr.text[0]) for fr in plain))

    def test_plain_comment_still_counts_as_redundant(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "track.mp3")
            shutil.copy(MP3_SRC, p)
            id3 = ID3(p)
            id3.add(COMM(encoding=3, lang="eng", desc="", text=["already here"]))
            id3.save(p)
            ape = APEv2()
            ape["Comment"] = APEValue("from the rip", TEXT)
            ape.save(p)
            r = apestrip.process_file(p, dry_run=False, keep_metadata=True)
            self.assertIn("Comment", r.redundant)
            comms = ID3(p).getall("COMM")
            self.assertFalse(any("from the rip" in str(fr.text) for fr in comms))


class RepairMalformedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "track.mp3")
        _write_malformed(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_mutagen_cannot_parse_but_raw_parse_can(self):
        with self.assertRaises(APENoHeaderError):
            APEv2(self.path)
        data = Path(self.path).read_bytes()
        parsed = apestrip._parse_raw_ape(data)
        self.assertIsNotNone(parsed)
        _, _, items = parsed
        self.assertIn("YEAR", items)
        self.assertIn("GENRE", items)

    def test_repair_off_by_default_reports(self):
        r = apestrip.process_file(self.path, dry_run=False, repair=False)
        self.assertTrue(r.had_ape)
        self.assertFalse(r.repaired)
        self.assertIn("repair-malformed", r.error)

    def test_repair_excises_and_migrates_sole_source(self):
        r = apestrip.process_file(
            self.path, dry_run=False, repair=True, keep_metadata=True
        )
        self.assertTrue(r.repaired)
        self.assertTrue(r.stripped)
        self.assertIsNone(r.error)
        self.assertFalse(apestrip._has_raw_ape_signature(self.path))
        with self.assertRaises(APENoHeaderError):
            APEv2(self.path)
        id3 = ID3(self.path)
        # sole-source YEAR migrated; stray GENRE never migrated (ID3 stays Rock)
        self.assertIn("2011", str(id3.get("TDRC") or id3.get("TYER")))
        self.assertEqual(id3["TCON"].text, ["Rock"])
        # audio still decodes
        self.assertGreater(getattr(MP3(self.path).info, "length", 0), 0)

    def test_repair_default_excises_without_migrating(self):
        # Without --keep-metadata the malformed block is still excised, but the
        # sole-source YEAR is dropped rather than copied into ID3.
        r = apestrip.process_file(self.path, dry_run=False, repair=True)
        self.assertTrue(r.repaired)
        self.assertTrue(r.stripped)
        self.assertEqual(r.migrated, [])
        self.assertFalse(apestrip._has_raw_ape_signature(self.path))
        id3 = ID3(self.path)
        self.assertIsNone(id3.get("TDRC") or id3.get("TYER"))
        self.assertEqual(id3["TCON"].text, ["Rock"])
        self.assertGreater(getattr(MP3(self.path).info, "length", 0), 0)

    def test_repair_dry_run_writes_nothing(self):
        before = Path(self.path).read_bytes()
        r = apestrip.process_file(
            self.path, dry_run=True, repair=True, keep_metadata=True
        )
        self.assertTrue(r.malformed)
        self.assertTrue(any(k == "YEAR" for k, _ in r.migrated))
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_parse_raw_ape_rejects_inconsistent_structure(self):
        # Corrupt the header size so the footer no longer sits at header+size:
        # the parser must refuse rather than excise a guessed region.
        data = bytearray(Path(self.path).read_bytes())
        hidx = data.find(b"APETAGEX")
        struct.pack_into("<I", data, hidx + 12, 999999)
        self.assertIsNone(apestrip._parse_raw_ape(bytes(data)))

    def test_parse_raw_ape_bounds_item_length(self):
        # A9: an item whose declared value length overruns the footer must not
        # slurp footer/audio bytes into a value; the malformed item is dropped.
        body = struct.pack("<II", 4, 0) + b"YEAR\x00" + b"2011"
        body += struct.pack("<II", 999999, 0) + b"BAD\x00" + b"xx"
        size = len(body) + 32
        header = (
            b"APETAGEX" + struct.pack("<IIII", 2000, size, 2, 0xA0000000) + b"\x00" * 8
        )
        footer = (
            b"APETAGEX" + struct.pack("<IIII", 2000, size, 2, 0x80000000) + b"\x00" * 8
        )
        data = Path(MP3_SRC).read_bytes() + header + body + footer
        parsed = apestrip._parse_raw_ape(data)
        self.assertIsNotNone(parsed)
        _, _, items = parsed
        self.assertIn("YEAR", items)
        self.assertNotIn("BAD", items)

    def test_repair_preserves_file_permissions(self):
        # M3: mkstemp creates the temp file 0600; os.replace must not install
        # that verbatim on a shared-mount library.
        os.chmod(self.path, 0o644)
        r = apestrip.process_file(self.path, dry_run=False, repair=True)
        self.assertTrue(r.repaired)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o644)

    def test_repair_in_readonly_dir_reports_error_not_raises(self):
        # M4: an un-creatable temp file must become a per-file error result,
        # not an exception that kills a library-wide run mid-write-pass.
        os.chmod(self._tmp.name, 0o555)
        try:
            r = apestrip.process_file(self.path, dry_run=False, repair=True)
        finally:
            os.chmod(self._tmp.name, 0o755)
        self.assertTrue(r.had_ape)
        self.assertIsNotNone(r.error)


def _ape_blob(items):
    """A well-formed raw APE block (header + items + footer), no trailers."""
    body = b""
    for k, v in items.items():
        body += struct.pack("<II", len(v), 0) + k.encode("latin1") + b"\x00" + v
    size = len(body) + 32
    header = (
        b"APETAGEX"
        + struct.pack("<IIII", 2000, size, len(items), 0xA0000000)
        + b"\x00" * 8
    )
    footer = (
        b"APETAGEX"
        + struct.pack("<IIII", 2000, size, len(items), 0x80000000)
        + b"\x00" * 8
    )
    return header + body + footer


class RepairTrailerTests(unittest.TestCase):
    """M5: only recognized trailing structures (Lyrics3v2, a strictly terminal
    ID3v1) may follow the APE block; anything else refuses the repair. The
    repair machinery is exercised directly (repair_file); whether mutagen
    happens to choke on a given shape is its business, not this contract's."""

    ID3V1 = b"TAG" + b"\x00" * 125
    LYRICS3 = b"LYRICSBEGIN" + b"LYR200Some lyric text here" + b"000037LYRICS200"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "track.mp3")

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, tail: bytes) -> None:
        base = Path(MP3_SRC).read_bytes()
        Path(self.path).write_bytes(base + _ape_blob({"YEAR": b"2011"}) + tail)

    def test_lyrics3_block_survives_repair(self):
        self._write(self.LYRICS3 + self.ID3V1)
        r = apestrip.repair_file(self.path, dry_run=False)
        self.assertTrue(r.repaired, r.error)
        data = Path(self.path).read_bytes()
        self.assertNotIn(b"APETAGEX", data)
        self.assertIn(b"LYRICSBEGIN", data)  # Lyrics3 preserved byte for byte
        self.assertIn(b"LYRICS200", data)
        self.assertEqual(data[-128:][:3], b"TAG")

    def test_id3v1_with_trailing_padding_is_refused(self):
        # The old end-anchoring excised the (no longer terminal) ID3v1 itself.
        self._write(self.ID3V1 + b"\x00\x00\x00")
        before = Path(self.path).read_bytes()
        r = apestrip.repair_file(self.path, dry_run=False)
        self.assertFalse(r.repaired)
        self.assertIsNotNone(r.error)
        self.assertEqual(Path(self.path).read_bytes(), before)  # untouched

    def test_junk_hiding_a_lyrics3_block_is_refused(self):
        self._write(b"\xde\xad" + self.LYRICS3 + self.ID3V1)
        before = Path(self.path).read_bytes()
        r = apestrip.repair_file(self.path, dry_run=False)
        self.assertFalse(r.repaired)
        self.assertEqual(Path(self.path).read_bytes(), before)

    def test_strictly_terminal_ape_block_repairs(self):
        self._write(b"")
        r = apestrip.repair_file(self.path, dry_run=False)
        self.assertTrue(r.repaired, r.error)
        self.assertNotIn(b"APETAGEX", Path(self.path).read_bytes())


class MainTests(unittest.TestCase):
    """Coverage for the execution loop (previously untested): honest per-file
    log lines (M6) and a nonzero exit when any file errored."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _run_main(self, argv):
        old = sys.argv
        sys.argv = ["apestrip.py", *argv]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                rc = apestrip.main()
        finally:
            sys.argv = old
        return rc, buf.getvalue()

    def _mp3_with_ape(self, name="track.mp3"):
        p = os.path.join(self.root, name)
        shutil.copy(MP3_SRC, p)
        ape = APEv2()
        ape["Genre"] = APEValue("Rap", TEXT)
        ape.save(p)
        return p

    def test_happy_path_strips_and_exits_zero(self):
        self._mp3_with_ape()
        rc, out = self._run_main([self.root, "--yes"])
        self.assertEqual(rc, 0)
        self.assertIn("stripped APEv2 tag", out)
        self.assertTrue(os.path.exists(os.path.join(self.root, "apestrip.log")))

    def test_vanished_tag_logs_skip_not_strip(self):
        # M6: a file whose APE tag disappears between the planning and write
        # passes must not get a false "stripped APEv2 tag" audit line.
        self._mp3_with_ape()
        real = apestrip.process_file

        def fake(path, dry_run, repair=False, keep_metadata=False):
            if dry_run:
                return real(path, True, repair, keep_metadata)
            return apestrip.FileResult(path, had_ape=False)

        apestrip.process_file = fake
        try:
            rc, out = self._run_main([self.root, "--yes"])
        finally:
            apestrip.process_file = real
        self.assertEqual(rc, 0)
        self.assertIn("no APEv2 tag at write time (skipped)", out)
        self.assertNotIn("stripped APEv2 tag\n", out.replace("\r", ""))

    def test_per_file_error_exits_nonzero(self):
        p = self._mp3_with_ape()
        data = bytearray(Path(p).read_bytes())
        fidx = data.rfind(b"APETAGEX")
        struct.pack_into("<I", data, fidx + 12, 8)  # bogus tag size
        Path(p).write_bytes(data)
        rc, out = self._run_main([self.root, "--yes"])  # no --repair-malformed
        self.assertEqual(rc, 1)
        self.assertIn("1 error(s)", out)


class IterMp3sTests(unittest.TestCase):
    """The walk prunes hidden directories, matching rerate.py and
    replaygain.py. apestrip is the destructive one, so a `.testing/` working
    copy of an album must not be rewritten behind the user's back."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        for rel in (
            "Zed/Album/02.mp3",
            "Zed/Album/01.mp3",
            "Abe/Album/01.mp3",
            ".testing/Copy/01.mp3",
            "Abe/.stash/01.mp3",
            "Abe/Album/cover.jpg",
        ):
            p = Path(self.root) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"")

    def tearDown(self):
        self._tmp.cleanup()

    def test_hidden_dirs_pruned(self):
        found = {os.path.relpath(p, self.root) for p in apestrip._iter_mp3s(self.root)}
        self.assertEqual(
            found,
            {
                os.path.join("Zed", "Album", "02.mp3"),
                os.path.join("Zed", "Album", "01.mp3"),
                os.path.join("Abe", "Album", "01.mp3"),
            },
        )

    def test_walk_order_is_sorted(self):
        found = list(apestrip._iter_mp3s(self.root))
        self.assertEqual(found, sorted(found))


if __name__ == "__main__":
    unittest.main()
