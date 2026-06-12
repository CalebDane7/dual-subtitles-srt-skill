---
name: dual-subtitles-srt
description: Build, repair, verify, and proof SRT subtitle files, especially dual English plus Indonesian movie subtitles. Use when Codex needs to create bilingual .srt sidecars, align English and Indonesian subtitle sources, translate English cues into Indonesian, prevent two subtitle languages from vertically overlapping, validate cue timing and line wrapping, correct subtitles that are early or late, safely shift every active sidecar SRT together, replace auto-loaded exact-basename .srt files, or render proof frames with ffmpeg.
---

# Dual Subtitles SRT

## Purpose

Create one combined SRT timing track for bilingual subtitles. Do not use two separate active subtitle tracks for English and Indonesian, because many players render both tracks in the same bottom subtitle band and the text collides.

Default layout per cue:

```text
English line 1
English line 2, if needed
Indonesian line 1
Indonesian line 2, if needed
```

Never call the job complete until validation and rendered proof frames show that the dual subtitle block is readable.

## Workflow

1. Locate the real movie file and existing sidecars.
   - Use `find`, `rg --files`, `ffprobe`, and local library paths.
   - Check whether the video has embedded subtitle streams.
   - Back up existing `.srt`, `.en.srt`, `.id.srt`, `.dual.srt`, `.dual.default.srt`, caches, and verify JSON before replacement.

2. Choose the timing base.
   - Prefer a clean English SRT matched to the movie release.
   - If no good English source exists, transcribe or extract subtitles, then inspect cue duration and gaps.
   - Reject ASR or downloaded sources with giant cues, wrong language, ad/uploader lines, or major timing drift.

3. Check timing when the user reports early/late subtitles.
   - Compare real audio against several cues from the start, middle, and end before shifting.
   - Treat positive measured offset as "subtitle is early": delay the SRT by that many milliseconds.
   - Treat negative measured offset as "subtitle is late": move the SRT earlier by the absolute value.
   - Shift only the movies/files that measurement proves are wrong. Do not apply a global offset to unrelated movies just because one file is early.
   - Use the `shift` command so `.en.srt`, `.id.srt`, `.dual.srt`, `.dual.default.srt`, exact-basename `.srt`, and verify metadata stay together.

4. Choose Indonesian generation mode.
   - Prefer direct translation from the English cue list when an API or local translation model is available. This keeps both languages on identical timestamps and prevents source-spill from unrelated Indonesian cue boundaries.
   - Use a human Indonesian SRT only when it is a close release match. Score timing overlap, filter ad/uploader lines, and verify spot cues. Source alignment is a fallback, not a shortcut.

5. Build the output.
   - Use `scripts/dual_srt.py`.
   - Write `.en.srt`, `.id.srt`, `.dual.srt`, `.dual.default.srt`, and usually exact-basename `.srt`.
   - Keep `.dual.srt`, `.dual.default.srt`, and exact-basename `.srt` byte-identical when the combined dual subtitle should auto-load.

6. Validate strictly.
   - No cue overlaps.
   - No cue has more than four text lines.
   - No cue is blank or single-language unless the user explicitly asks to preserve single-language title cards.
   - The first and last cues cover the dialogue span expected for that subtitle source.
   - Render proof frames from real video frames, preferably actual four-line cues.

## Script Quick Start

Build from English SRT with Gemini translation:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py build \
  --movie "/path/movie.mp4" \
  --english-source "/path/movie.en.srt" \
  --translate gemini \
  --model gemini-3.1-flash-lite \
  --make-default \
  --make-plain
```

Build from English and Indonesian SRT sources:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py build \
  --movie "/path/movie.mp4" \
  --english-source "/path/movie.en.srt" \
  --indonesian-source "/path/movie.id.srt" \
  --shift-ms -650 \
  --make-default \
  --make-plain
```

Validate existing sidecars:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py validate \
  --movie "/path/movie.mp4"
```

Shift active sidecars after measured timing proof:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py shift \
  --movie "/path/movie.mp4" \
  --shift-ms 670
```

Use a positive `--shift-ms` when subtitles appear too early and must be delayed. Use a negative value when subtitles appear too late and must be moved earlier. The command backs up existing sidecars, shifts all default active SRT files for the movie, updates `.dual.verify.json` when present, and validates the final `.dual.srt`.

Render proof frames:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py proof \
  --movie "/path/movie.mp4" \
  --count 3 \
  --out-dir "/tmp/subtitle-proof"
```

## Quality Bar

Use `references/quality-checklist.md` before final response. The final response should state what was changed, which files were written, and what proof was run.

If translation APIs rate-limit or stall, do not overwrite working movie-folder files with partial output. Build in a temp folder first, validate there, then copy into the movie folder only after success.
