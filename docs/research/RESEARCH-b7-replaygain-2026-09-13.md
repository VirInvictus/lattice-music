# RESEARCH: B.7 ReplayGain verification (2026-09-13)

Read-only research for landscape box B.7: "ReplayGain verification:
stored gain vs a fresh rsgain/ffmpeg ebur128 measurement, read-only. M."
Every number below was measured live on this machine against synthetic
audio in /tmp (sine 997 Hz, 10 s, FLAC + Opus + MP3); the real music
library was not touched. The lane brief gated B.7 on "an rsgain/ebur128
harness"; this research resolves that gate: no special harness is
needed, for reasons in section 3.

## 1. What exists today

- `lattice.tags.read_replaygain` (tags.py:108) returns
  `ReplayGainStatus(has_track_gain, has_album_gain)`: booleans only.
  The audit mode (`--auditReplayGain`, audit.py) reports coverage, never
  values. Verification needs the stored VALUES, so tags.py grows a
  value-returning reader (section 5).
- `scripts/replaygain.py` already contains the value-reading half:
  `read_gain_strings` (line 167) returns the stored track/album gain
  across every convention lattice supports (MP3 TXXX frame text,
  Vorbis/Opus plain strings, MP4FreeForm bytes decoded, R128 integers
  as their raw string form). B.7 promotes this into `lattice.tags` as
  the public value reader (the script then imports it back, like the
  cleaner/apestrip promotion but one function).
- `scripts/replaygain.py` shells to `rsgain` and documents its contract
  (easy vs custom, `--target-lufs` switching to custom mode, the
  -14-vs-18 target caveat at lines 32-35 that B.7 inherits: stored tags
  cannot say which target they were written for).

## 2. Tool probes (live, this machine; rsgain 3.6 / libebur128 1.2.6)

**The verification engine already exists inside rsgain, read-only.**
`rsgain custom` tagmode `s` ("Scan files but don't write ReplayGain
tags") is the DEFAULT, and `-O` prints tab-delimited scan data:

    $ rsgain custom -a -O -s s 01_sine.flac
    Filename  Loudness (LUFS)  Gain (dB)  Peak     Peak (dB)  Peak Type  Clipping Adjustment?
    01_sine.flac  -27.09       9.09       0.062500  -24.08    Sample     N
    Album        -27.09       9.09       0.062500  -24.08    Sample     N

Verified during the probe: the scanned file's sha256 is identical
before and after (`-s s` truly writes nothing), `-a` adds the album
row (aggregate analysis, so ALBUM verification needs zero extra
machinery), and the identity that powers the whole mode holds exactly:

    stored gain == target - measured loudness      (-18 - (-27.09) = 9.09)

because the fresh scan uses the same libebur128 that computed the
stored tags. ffmpeg's ebur128 filter agrees on the same file
(`I: -27.1 LUFS` vs rsgain's -27.09; both engines land within 0.01 dB
on a steady sine), so the ffmpeg route is a viable fallback, but it is
per-invocation-per-album (concat filter for album loudness), prints to
stderr, and is a DIFFERENT BS.1770 implementation than the writer.
Recommendation: rsgain is the engine (it is the writer; engine
consistency is the whole value of verification), required on PATH
exactly like scripts/replaygain.py. Do not build the ffmpeg leg.

**Write conventions, read back through lattice** (written to the
synthetic files with `-s i`, read with read_gain_strings):

| Format | Stored form | lattice-music reads |
|---|---|---|
| MP3 (TXXX) | `'9.09 dB'` | `9.09` after float parse |
| FLAC/Ogg (Vorbis) | `'9.09 dB'` | same |
| Opus (`-o r`, R128) | `'2327'` (Q7.8) | `2327 / 256 = 9.0898 dB` |

R128 quantization is 1/256 dB (0.0039), so Opus deltas versus a 2-decimal
expectation are bounded by ~0.002 dB: noise. Note `-o d` (rsgain's opus
default, which scripts/replaygain.py uses) writes standard
replaygain_* strings on Opus instead, so the verifier must accept both
conventions per format; read_gain_strings already does.

## 3. The harness gate, resolved

The lane gated B.7 on "an rsgain/ebur128 harness" for testing. House
convention: subprocess modes are not exercised end to end in the suite
(the integrity modes' `classify_decode` brain is unit-tested instead).
B.7 has the same shape, so the same split applies and no audio-generating
harness is needed:

- **Unit-tested in the suite (subprocess mocked, house style):** the
  `-O` TSV parser (fixture strings incl. album rows and the Clipping
  column), the value reader (promoted `read_gain_strings` + R128 /256),
  the delta/bucket math, and the report contract via a mocked runner.
  None of this needs real audio.
- **On-metal proof (done once, here):** this research's probe IS the
  harness validation: generate sine, measure, confirm identity and
  byte-identical no-write. The procedure is four commands, recorded in
  section 2, repeatable any time. First real-library run is Brandon's
  (read-only, but his call when).

## 4. Mode design (for commissioning)

Flag: `--verifyReplayGain` (family-consistent alternatives: see the
decision prompts). Output `replaygain_verify.txt`
(DEFAULT_REPLAYGAIN_VERIFY_OUTPUT). Read-only; requires rsgain.

    per album (find_album_dirs, same as --auditReplayGain):
      run: rsgain custom -a -O -q -s s <files...>
      parse TSV: per-file loudness + album row
      per file: expected = target - loudness
                delta   = stored_track - expected   (UNTAGGED if none)
      album: same against the album row's loudness
      bucket per file/album: OK (|delta| <= tol) / OFF / UNGAUGED*
    * clip protection: a file whose stored gain was clip-adjusted at
      write time legitimately differs from target - loudness; rsgain's
      Clipping Adjustment column flags those rows; report them
      separately, never as OFF.

Knobs: `--target-lufs N` (default -18, matching the writer's caveat
that targets cannot be read back from tags: a -14-targeted library
verifies clean only when verified at -14), `--tolerance` (default
0.5 dB: engine-identical re-measurement is deterministic and lands
within ~0.01 dB, so 0.5 separates "wrong/mismatched tag" from jitter
while forgiving cross-version libebur128 drift), `--verbose` to list OK
albums. Report sections follow the audit family: header block, then
[GAIN OFF BY > tol], [UNTAGGED], [CLIP-ADJUSTED], [OK (n, list with
--verbose)], stdout summary with counts.

Tests: TSV parser units, value-reader units (string + R128 + MP4 bytes),
bucket math boundaries (tolerance edge, clip column, missing tags),
mocked-runner run test with a canned TSV, CLI/TUI wiring pins. ~15
tests, suite 560 to ~575.

Effort: M as originally rated (one mode function + promoted reader +
wiring; the analysis engine is rsgain's, not ours). The only tag-layer
change is additive (new value reader; nothing existing moves).

## 5. Open calls (asked as prompts with this report; all three were
answered live on 2026-09-13, decisions recorded in the repo: push =
landed, engine = rsgain -O, flag = --verifyReplayGain)

1. Push this research commit now vs hold for the next batch.
2. Engine: rsgain -O (recommended) vs ffmpeg ebur128.
3. Flag name: --verifyReplayGain (recommended) vs alternatives.
