import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import lattice.modes.integrity as integrity_mod
from lattice.modes.integrity import (
    TIER_CORRUPT,
    TIER_METADATA,
    TIER_OK,
    TIER_SUSPECT,
    _find_files_by_ext_path,
    _progress_file,
    classify_decode,
    run_flac_mode,
    run_mp3_mode,
)

# Real stderr signatures captured from ffmpeg / libFLAC during the audit.
_CLAVISH = (
    "Error submitting packet to decoder: Invalid data found when processing input\n"
    "[mp3float] Header missing\n"
    "Error submitting packet to decoder: Invalid data found when processing input"
)
_BOM_NOISE = (
    "Incorrect BOM value: 0x3500\n"
    "Error reading frame artists, skipped\n"
    "Incorrect BOM value: 0x3500\n"
    "Error reading frame PERFORMER_SORT_ORDER, skipped"
)
_FLAC_TRAILING = (
    "*** Got error code 0:FLAC__STREAM_DECODER_ERROR_STATUS_LOST_SYNC "
    "after processing 2527850 samples"
)
_FLAC_TRUNCATED = (
    "*** Got error code 0:FLAC__STREAM_DECODER_ERROR_STATUS_LOST_SYNC "
    "after processing 1589248 samples"
)
_FLAC_TOTAL = 2527850


def _tier(*args, **kwargs):
    return classify_decode(*args, **kwargs)[0]


class ClassifyDecodeTests(unittest.TestCase):
    def test_clean_is_ok(self):
        self.assertEqual(_tier(0, ""), TIER_OK)

    def test_pure_tag_noise_is_metadata(self):
        self.assertEqual(_tier(0, _BOM_NOISE), TIER_METADATA)

    def test_lone_header_missing_is_metadata(self):
        self.assertEqual(_tier(0, "[mp3float] Header missing"), TIER_METADATA)

    def test_lone_backstep_is_metadata(self):
        self.assertEqual(_tier(0, "[mp3float] invalid new backstep -1"), TIER_METADATA)

    def test_completed_decode_with_faults_is_suspect(self):
        # rc == 0: decoded to the end despite 'Invalid data' lines (it plays).
        self.assertEqual(_tier(0, _CLAVISH), TIER_SUSPECT)

    def test_single_decode_fault_is_suspect(self):
        self.assertEqual(
            _tier(0, "Error submitting packet to decoder: Invalid data found"),
            TIER_SUSPECT,
        )

    def test_unknown_line_is_suspect_not_hidden(self):
        self.assertEqual(_tier(0, "some unrecognized decoder whining"), TIER_SUSPECT)

    def test_mixed_metadata_and_decode_is_suspect(self):
        self.assertEqual(
            _tier(
                0, "Incorrect BOM value: 0x10\nError submitting packet: Invalid data"
            ),
            TIER_SUSPECT,
        )

    def test_cannot_open_is_corrupt(self):
        self.assertEqual(
            _tier(1, "Error opening input: Invalid data found when processing input"),
            TIER_CORRUPT,
        )

    def test_nonzero_exit_no_stderr_is_corrupt(self):
        self.assertEqual(_tier(1, ""), TIER_CORRUPT)

    def test_flac_trailing_is_suspect(self):
        self.assertEqual(_tier(1, _FLAC_TRAILING, _FLAC_TOTAL), TIER_SUSPECT)

    def test_flac_truncated_is_corrupt(self):
        self.assertEqual(_tier(1, _FLAC_TRUNCATED, _FLAC_TOTAL), TIER_CORRUPT)

    def test_flac_lostsync_without_declared_count_is_suspect(self):
        # Can't prove truncation without the declared total, so do not escalate.
        self.assertEqual(_tier(1, _FLAC_TRAILING), TIER_SUSPECT)

    def test_reason_is_returned(self):
        tier, reason = classify_decode(1, _FLAC_TRUNCATED, _FLAC_TOTAL)
        self.assertEqual(tier, TIER_CORRUPT)
        self.assertIn("1589248", reason)


class Mp3HeaderInfoTests(unittest.TestCase):
    def test_vbr_mode_is_the_member_name(self):
        # __class__.__name__ rendered every bitrate mode as "BitrateMode";
        # the row must carry the actual member ("CBR"/"VBR"/"ABR").
        from pathlib import Path

        from lattice.modes.integrity import _mutagen_header_info

        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "library"
            / "Cursive"
            / "Domestica"
            / "01 - The Casualty.mp3"
        )
        meta = _mutagen_header_info(fixture)
        self.assertEqual(meta.get("vbr_mode"), "CBR")


class FindFilesByExtTests(unittest.TestCase):
    """The integrity file walk must agree with utils.iter_audio_dirs, which
    every other mode uses: hidden directories are pruned, and the order is
    stable so two scans of one library produce comparable reports."""

    def _tree(self, td: str) -> None:
        for rel in (
            "Zed/Album/02.mp3",
            "Zed/Album/01.mp3",
            "Abe/Album/01.mp3",
            ".testing/Copy/01.mp3",
            "Abe/.stash/01.mp3",
        ):
            p = Path(td) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"")

    def test_hidden_dirs_pruned(self):
        with tempfile.TemporaryDirectory() as td:
            self._tree(td)
            found = _find_files_by_ext_path([td], ".mp3")
            rels = {str(p.relative_to(td)) for p in found}
            self.assertEqual(
                rels,
                {
                    os.path.join("Zed", "Album", "02.mp3"),
                    os.path.join("Zed", "Album", "01.mp3"),
                    os.path.join("Abe", "Album", "01.mp3"),
                },
            )

    def test_results_are_sorted(self):
        with tempfile.TemporaryDirectory() as td:
            self._tree(td)
            found = [str(p) for p in _find_files_by_ext_path([td], ".mp3")]
            self.assertEqual(found, sorted(found))

    def test_explicit_file_root_still_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "track.mp3"
            f.write_bytes(b"")
            self.assertEqual(_find_files_by_ext_path([str(f)], ".mp3"), [f])


class DecodeReportOrderTests(unittest.TestCase):
    """Rows come back in as_completed order, so the report sections sort by
    path; without that, two runs over an unchanged library produced
    differently-ordered reports that could not be diffed."""

    def test_ok_section_is_path_sorted(self):
        names = ["Abe", "Mid", "Zed"]
        # Sleep longest for the alphabetically-first directory, so with a pool
        # the futures complete in exactly reverse-sorted order. Without the
        # sort in _section the report comes out Zed/Mid/Abe and this fails;
        # relying on scheduling luck would have made the guard meaningless.
        delay = {n: (len(names) - i) * 0.02 for i, n in enumerate(names)}
        real = integrity_mod._scan_one_file

        def slow(path, ffmpeg_path, *, enrich=False):
            time.sleep(delay.get(Path(path).parent.name, 0.0))
            row = real(path, ffmpeg_path, enrich=enrich)
            row["tier"] = TIER_OK
            row["reason"] = "decode check skipped (test forces OK)"
            return row

        with tempfile.TemporaryDirectory() as td:
            for name in names:
                p = Path(td) / name / "01.mp3"
                p.parent.mkdir(parents=True)
                p.write_bytes(b"")
            out = Path(td) / "report.txt"
            # A decoder is never invoked: _find_ffmpeg fakes one (the scan
            # refuses without it) and the wrapped _scan_one_file returns OK
            # rows directly, which --no-only-errors then lists.
            with (
                mock.patch.object(
                    integrity_mod, "_find_ffmpeg", return_value="/fake/ffmpeg"
                ),
                mock.patch.object(integrity_mod, "_scan_one_file", slow),
            ):
                rc = run_mp3_mode(
                    [td],
                    str(out),
                    len(names),
                    None,
                    only_errors=False,
                    verbose=False,
                    quiet=True,
                )
            self.assertEqual(rc, 0)
            listed = [
                ln.strip()
                for ln in out.read_text(encoding="utf-8").splitlines()
                if ln.startswith("  ") and ln.strip().endswith("01.mp3")
            ]
            self.assertEqual(listed, sorted(listed))
            self.assertEqual(len(listed), 3)


class ProgressPersistenceTests(unittest.TestCase):
    """--resume (FLAC/MP3 scoped): every scan writes verdicts through to
    <output>.progress.json as it goes, an interrupted run leaves the state
    behind, a resumed run reuses recorded verdicts and scans only the
    remainder, and a completed scan deletes the state."""

    def _tree(self, td: str, ext: str, names=("A", "B", "C")) -> list[Path]:
        files = []
        for name in names:
            p = Path(td) / name / f"01{ext}"
            p.parent.mkdir(parents=True)
            p.write_bytes(b"")
            files.append(p)
        return files

    def test_flac_interrupt_leaves_state_and_resume_finishes(self):
        with tempfile.TemporaryDirectory() as td:
            files = self._tree(td, ".flac")
            out = Path(td) / "flac_errors.txt"
            pfile = Path(str(out) + ".progress.json")
            calls: list[str] = []
            armed = [True]  # run one raises for B; the resumed run never does

            def verdict(path, *, use_flac, ffmpeg_path):
                calls.append(path)
                if armed[0] and Path(path).parent.name == "B":
                    raise KeyboardInterrupt
                return ("flac", TIER_OK, "clean")

            with (
                mock.patch.object(integrity_mod, "has_tool", return_value=True),
                mock.patch.object(integrity_mod, "_flac_verdict", verdict),
            ):
                rc1 = run_flac_mode([td], str(out), 1, "flac", quiet=True)
            self.assertEqual(rc1, 130)
            self.assertTrue(pfile.exists())
            # The pool may have started files beyond the recorded verdicts
            # before the interrupt landed; the state holds what was recorded.
            cached = set(json.loads(pfile.read_text(encoding="utf-8"))["results"])
            self.assertIn(str(files[0]), cached)  # A, first in the 1-worker queue

            armed[0] = False
            before = len(calls)
            with (
                mock.patch.object(integrity_mod, "has_tool", return_value=True),
                mock.patch.object(integrity_mod, "_flac_verdict", verdict),
            ):
                rc2 = run_flac_mode([td], str(out), 1, "flac", resume=True, quiet=True)
            self.assertEqual(rc2, 0)
            # The rerun scans exactly the files the interrupted run never recorded.
            self.assertEqual(set(calls[before:]), {str(p) for p in files} - cached)
            # The full report covers every file; completion cleared the state.
            self.assertIn("Scanned: 3", out.read_text(encoding="utf-8"))
            self.assertFalse(pfile.exists())

    def test_flac_resume_discards_state_when_the_tool_changes(self):
        # A libFLAC verdict is not comparable to one from ffmpeg's stricter
        # decoder; the state must be ignored rather than trusted.
        with tempfile.TemporaryDirectory() as td:
            files = self._tree(td, ".flac")
            out = Path(td) / "flac_errors.txt"
            pfile = Path(str(out) + ".progress.json")
            calls: list[str] = []
            armed = [True]

            def verdict(path, *, use_flac, ffmpeg_path):
                calls.append(path)
                if armed[0] and Path(path).parent.name == "B":
                    raise KeyboardInterrupt
                return ("flac", TIER_OK, "clean")

            with (
                mock.patch.object(integrity_mod, "has_tool", return_value=True),
                mock.patch.object(integrity_mod, "_flac_verdict", verdict),
            ):
                run_flac_mode([td], str(out), 1, "flac", quiet=True)
            self.assertTrue(pfile.exists())

            armed[0] = False
            before = len(calls)
            with (
                mock.patch.object(integrity_mod, "has_tool", return_value=True),
                # The tool-change flip needs an actual ffmpeg to resolve: on
                # a runner without one, use_flac would stay True and the
                # state would match, hiding the discard this test pins.
                mock.patch.object(
                    integrity_mod, "_find_ffmpeg", return_value="/fake/ffmpeg"
                ),
                mock.patch.object(integrity_mod, "_flac_verdict", verdict),
            ):
                # prefer=ffmpeg flips use_flac, invalidating every cached verdict.
                run_flac_mode([td], str(out), 1, "ffmpeg", resume=True, quiet=True)
            # Every file was re-scanned despite the state file existing.
            self.assertEqual(set(calls[before:]), {str(p) for p in files})

    def test_mp3_resume_reuses_verdicts_including_corrupt_rows(self):
        with tempfile.TemporaryDirectory() as td:
            files = self._tree(td, ".mp3")
            out = Path(td) / "mp3_results.txt"
            pfile = _progress_file(str(out))
            calls: list[str] = []
            armed = [True]
            real = integrity_mod._scan_one_file

            def scan(path, ffmpeg_path, *, enrich=False):
                calls.append(str(path))
                if armed[0] and Path(path).parent.name == "B":
                    raise KeyboardInterrupt
                row = real(path, ffmpeg_path, enrich=enrich)
                if Path(path).parent.name == "C":
                    row["tier"] = TIER_CORRUPT
                    row["reason"] = "decode failed (test fixture)"
                return row

            with (
                # A decoder must resolve for the scan to run at all (the
                # missing-decoder refusal fires first these days); the decode
                # itself is stubbed so the test never needs a real ffmpeg.
                mock.patch.object(
                    integrity_mod, "_find_ffmpeg", return_value="/fake/ffmpeg"
                ),
                mock.patch.object(
                    integrity_mod, "_ffmpeg_decode_check", return_value=(0, "")
                ),
                mock.patch.object(integrity_mod, "_scan_one_file", scan),
            ):
                rc1 = run_mp3_mode(
                    [td], str(out), 1, None, only_errors=True, verbose=False, quiet=True
                )
            self.assertEqual(rc1, 130)
            cached = set(json.loads(pfile.read_text(encoding="utf-8"))["results"])

            armed[0] = False
            before = len(calls)
            with (
                # Same decoder stub as run 1: the resume scan_id carries the
                # resolved ffmpeg path, so the two runs must resolve it the
                # same way or the recorded state is discarded as foreign.
                mock.patch.object(
                    integrity_mod, "_find_ffmpeg", return_value="/fake/ffmpeg"
                ),
                mock.patch.object(
                    integrity_mod, "_ffmpeg_decode_check", return_value=(0, "")
                ),
                mock.patch.object(integrity_mod, "_scan_one_file", scan),
            ):
                rc2 = run_mp3_mode(
                    [td],
                    str(out),
                    1,
                    None,
                    only_errors=True,
                    verbose=False,
                    quiet=True,
                    resume=True,
                )
            self.assertEqual(rc2, 1)  # C's cached CORRUPT keeps the exit code
            second_run = set(calls[before:])
            self.assertEqual(second_run, {str(p) for p in files} - cached)
            report = out.read_text(encoding="utf-8")
            self.assertIn("Scanned: 3", report)
            self.assertIn("decode failed (test fixture)", report)  # cached row
            self.assertFalse(pfile.exists())

    def test_progress_load_rejects_foreign_or_corrupt_state(self):
        with tempfile.TemporaryDirectory() as td:
            pfile = Path(td) / "r.txt.progress.json"
            scan_id = {"kind": "flac", "use_flac": True}
            self.assertIsNone(integrity_mod._load_progress(pfile, scan_id))
            pfile.write_text("not json", encoding="utf-8")
            self.assertIsNone(integrity_mod._load_progress(pfile, scan_id))
            integrity_mod._save_progress(pfile, scan_id, {"/x": {"tier": TIER_OK}})
            self.assertEqual(
                integrity_mod._load_progress(pfile, scan_id),
                {"/x": {"tier": TIER_OK}},
            )
            self.assertIsNone(
                integrity_mod._load_progress(pfile, {"kind": "flac", "use_flac": False})
            )


class FlacToolHonestyTests(unittest.TestCase):
    """--prefer ffmpeg that cannot be honored warns instead of silently
    switching decoders, and an explicit --ffmpeg path is actually used (it
    never reached the FLAC mode before)."""

    def _tree(self, td: str, names=("A", "B")) -> list[Path]:
        files = []
        for name in names:
            p = Path(td) / name / "01.flac"
            p.parent.mkdir(parents=True)
            p.write_bytes(b"")
            files.append(p)
        return files

    def test_unhonorable_prefer_ffmpeg_warns_and_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            self._tree(td)
            out = Path(td) / "flac_errors.txt"
            calls: list[bool] = []

            def verdict(path, *, use_flac, ffmpeg_path):
                calls.append(use_flac)
                return ("flac", TIER_OK, "clean")

            err = io.StringIO()
            with (
                mock.patch.object(
                    integrity_mod, "has_tool", side_effect=lambda t: t == "flac"
                ),
                mock.patch.object(integrity_mod, "_find_ffmpeg", return_value=None),
                mock.patch.object(integrity_mod, "_flac_verdict", verdict),
                contextlib.redirect_stderr(err),
            ):
                rc = run_flac_mode([td], str(out), 1, "ffmpeg", quiet=False)
            self.assertEqual(rc, 0)
            # The scan ran on libFLAC, and the stderr page says so.
            self.assertTrue(calls and all(calls))
            self.assertIn("falling back to flac", err.getvalue())

    def test_explicit_ffmpeg_path_is_honored(self):
        with tempfile.TemporaryDirectory() as td:
            self._tree(td)
            fake_ffmpeg = Path(td) / "bin" / "ffmpeg"
            fake_ffmpeg.parent.mkdir()
            fake_ffmpeg.write_bytes(b"")
            out = Path(td) / "flac_errors.txt"
            calls: list[tuple[bool, str]] = []

            def verdict(path, *, use_flac, ffmpeg_path):
                calls.append((use_flac, ffmpeg_path))
                return ("ffmpeg", TIER_OK, "clean")

            with (
                mock.patch.object(integrity_mod, "has_tool", return_value=False),
                mock.patch.object(integrity_mod, "_flac_verdict", verdict),
            ):
                rc = run_flac_mode(
                    [td], str(out), 1, "ffmpeg", ffmpeg=str(fake_ffmpeg), quiet=True
                )
            self.assertEqual(rc, 0)
            self.assertTrue(calls)
            self.assertFalse(calls[0][0])  # ffmpeg, not flac
            self.assertEqual(calls[0][1], str(fake_ffmpeg))

    def test_resume_survives_a_damaged_cached_record(self):
        # The state file is best-effort by contract: a record truncated by the
        # interrupt (tier key gone) must degrade to OK, not KeyError the run.
        with tempfile.TemporaryDirectory() as td:
            files = self._tree(td, names=("A",))
            out = Path(td) / "flac_errors.txt"
            pfile = Path(str(out) + ".progress.json")
            scan_id = {"kind": "flac", "use_flac": True}
            pfile.parent.mkdir(parents=True, exist_ok=True)
            integrity_mod._save_progress(
                pfile, scan_id, {str(files[0]): {"tool": "flac"}}
            )

            def verdict(path, *, use_flac, ffmpeg_path):
                raise AssertionError("the damaged record must not be rescanned")

            with (
                mock.patch.object(integrity_mod, "has_tool", return_value=True),
                mock.patch.object(integrity_mod, "_flac_verdict", verdict),
            ):
                rc = run_flac_mode([td], str(out), 1, "flac", resume=True, quiet=True)
            self.assertEqual(rc, 0)
            report = out.read_text(encoding="utf-8")
            self.assertIn("Scanned: 1", report)
            self.assertFalse(pfile.exists())


class MissingDecoderRefusalTests(unittest.TestCase):
    """A decode scan with no decoder available must refuse (exit 2), not
    grade every file OK with 'decode check skipped' and exit 0: a scan that
    verified nothing reporting success is the worst possible report."""

    def _tree(self, td: str, ext: str) -> Path:
        p = Path(td) / "Album" / f"01{ext}"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"")
        return p

    def test_mp3_without_ffmpeg_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            self._tree(td, ".mp3")
            out = Path(td) / "mp3_errors.txt"
            scanned: list[str] = []

            def scan(path, ffmpeg_path, *, enrich=False):
                scanned.append(str(path))
                return {"path": str(path), "tier": TIER_OK, "reason": ""}

            with (
                mock.patch.object(integrity_mod, "_find_ffmpeg", return_value=None),
                mock.patch.object(integrity_mod, "_scan_one_file", scan),
            ):
                rc = run_mp3_mode(
                    [td], str(out), 1, None, only_errors=True, verbose=False, quiet=True
                )
            self.assertEqual(rc, 2)
            self.assertEqual(scanned, [])
            self.assertFalse(out.exists())

    def test_opus_without_ffmpeg_still_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            self._tree(td, ".opus")
            out = Path(td) / "opus_errors.txt"
            with mock.patch.object(integrity_mod, "_find_ffmpeg", return_value=None):
                rc = integrity_mod.run_opus_mode(
                    [td],
                    str(out),
                    1,
                    None,
                    only_errors=True,
                    verbose=False,
                    quiet=True,
                )
            self.assertEqual(rc, 2)
            self.assertFalse(out.exists())


class WavWmaModeTests(unittest.TestCase):
    """The WAV and WMA decode scans had zero coverage (P3-5): they are
    thin _run_decode_scan wrappers, so the tiers are covered upstream;
    what needs pinning here is the refusal (no decoder = exit 2, never a
    successful-looking empty scan) and each wrapper's own plumbing (the
    ffmpeg format name, the report default, the title)."""

    def _tree(self, td: str, ext: str) -> Path:
        p = Path(td) / "Album" / f"01{ext}"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"")
        return p

    def test_wav_without_ffmpeg_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            self._tree(td, ".wav")
            out = Path(td) / "wav_errors.txt"
            scanned: list[str] = []

            def scan(path, ffmpeg_path, *, enrich=False):
                scanned.append(str(path))
                return {"path": str(path), "tier": TIER_OK, "reason": ""}

            with (
                mock.patch.object(integrity_mod, "_find_ffmpeg", return_value=None),
                mock.patch.object(integrity_mod, "_scan_one_file", scan),
            ):
                rc = integrity_mod.run_wav_mode(
                    [td], str(out), 1, None, only_errors=True, verbose=False, quiet=True
                )
            self.assertEqual(rc, 2)
            self.assertEqual(scanned, [])
            self.assertFalse(out.exists())

    def test_wma_without_ffmpeg_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            self._tree(td, ".wma")
            out = Path(td) / "wma_errors.txt"
            with mock.patch.object(integrity_mod, "_find_ffmpeg", return_value=None):
                rc = integrity_mod.run_wma_mode(
                    [td], str(out), 1, None, only_errors=True, verbose=False, quiet=True
                )
            self.assertEqual(rc, 2)
            self.assertFalse(out.exists())

    def test_wav_scan_writes_report_and_flags_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            self._tree(td, ".wav")
            out = Path(td) / "wav_errors.txt"

            def scan(path, ffmpeg_path, *, enrich=False):
                return {
                    "path": str(path),
                    "tier": TIER_CORRUPT,
                    "reason": "decoder exit code 1",
                }

            with (
                mock.patch.object(
                    integrity_mod, "_find_ffmpeg", return_value="/usr/bin/ffmpeg"
                ),
                mock.patch.object(integrity_mod, "_scan_one_file", scan),
            ):
                rc = integrity_mod.run_wav_mode(
                    [td], str(out), 1, None, only_errors=True, verbose=False, quiet=True
                )
            self.assertEqual(rc, 1)
            body = out.read_text()
            self.assertIn("WAV INTEGRITY REPORT", body)
            self.assertIn("01.wav", body)

    def test_wma_scan_writes_report_and_flags_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            self._tree(td, ".wma")
            out = Path(td) / "wma_errors.txt"

            def scan(path, ffmpeg_path, *, enrich=False):
                return {
                    "path": str(path),
                    "tier": TIER_CORRUPT,
                    "reason": "decoder exit code 1",
                }

            with (
                mock.patch.object(
                    integrity_mod, "_find_ffmpeg", return_value="/usr/bin/ffmpeg"
                ),
                mock.patch.object(integrity_mod, "_scan_one_file", scan),
            ):
                rc = integrity_mod.run_wma_mode(
                    [td], str(out), 1, None, only_errors=True, verbose=False, quiet=True
                )
            self.assertEqual(rc, 1)
            body = out.read_text()
            self.assertIn("WMA INTEGRITY REPORT", body)
            self.assertIn("01.wma", body)

    def test_wav_scan_all_clean_is_exit_zero(self):
        with tempfile.TemporaryDirectory() as td:
            self._tree(td, ".wav")
            out = Path(td) / "wav_errors.txt"

            def scan(path, ffmpeg_path, *, enrich=False):
                return {"path": str(path), "tier": TIER_OK, "reason": ""}

            with (
                mock.patch.object(
                    integrity_mod, "_find_ffmpeg", return_value="/usr/bin/ffmpeg"
                ),
                mock.patch.object(integrity_mod, "_scan_one_file", scan),
            ):
                rc = integrity_mod.run_wav_mode(
                    [td], str(out), 1, None, only_errors=True, verbose=False, quiet=True
                )
            # The report always writes (the summary is the record); the
            # exit code is what a gate keys on.
            self.assertEqual(rc, 0)
            self.assertIn("Corrupt: 0", out.read_text())


if __name__ == "__main__":
    unittest.main()
