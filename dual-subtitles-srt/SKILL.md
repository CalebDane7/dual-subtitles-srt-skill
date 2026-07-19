---
name: dual-subtitles-srt
description: Build, repair, translate, verify, and render proof frames for synchronized dual-language movie SRT subtitles in any two supported languages. Use whenever the user says "use the subtitle skill," asks for bilingual or dual subtitles, wants an existing source subtitle translated with Gemini, reports missing or numeric-only translations, needs a maximum-four-line combined SRT, or needs subtitle timing checked against a movie. English to Indonesian is the default pair when no languages are named.
---

# Dual Subtitles SRT

## Purpose

Create one synchronized dual-language SRT track:

```text
source-language line 1
source-language line 2, if needed
target-language line 1
target-language line 2, if needed
```

Use one combined track because two independently active subtitle tracks often
share the same screen area and overlap. The language pair may use any common
language tag accepted by this tool and supported by the translation provider
and subtitle renderer. Default to English (`en`) above Indonesian (`id`) when
the user names no pair.

This skill starts from a trusted source-language transcript: an existing
release-matched sidecar, a text subtitle stream extracted from the movie, or an
SRT generated separately with speech recognition. The bundled script does not
perform speech recognition. It also normalizes cue text, so styling tags and
intentional source line breaks are not preserved.

## Default Contract

A bare request to "use the subtitle skill" authorizes this complete workflow.
Do not make the user repeat these requirements:

1. Locate the exact movie and its existing or embedded source-language
   transcript. Use the transcript matched to that movie release as the timing
   authority.
2. Back up every active subtitle, cache, and verification file before replacing
   it.
3. Probe the media, then compare real dialogue with source cues near the
   beginning, middle, and end. Inspect before the first cue and after the last.
   Do not shift timing without a measured, stable, signed offset.
4. Translate each source cue directly into the requested target language with
   Gemini by default. Give every cue its neighboring source dialogue, including
   across provider chunk boundaries. Preserve the source cue's timestamps
   exactly.
5. Run the mandatory second Gemini pass across every translated cue before
   installation. Give it two cues of source-and-target context on each side;
   require it to correct meaning, natural phrasing, continuity, idioms,
   wordplay, punctuation, and speaker register. A partial or invalid review
   fails closed.
6. Build one combined SRT with at most two source lines followed by at most two
   target lines. Never add proportional or guessed timestamps simply to force
   text into four lines.
7. Reject blank or null translations, cue labels such as `Cue 42`, timestamp
   fragments, numeric placeholders, implausibly short translations, and copied
   ordinary source dialogue before publishing.
8. Stage the source, target, dual, alias, and verification files; validate exact
   cue/timing/composition parity; replace each file atomically; and restore the
   pre-build bytes after a handled copy or validation failure. Validate the
   installed files again.
9. Render frames from the real movie, include a four-line cue when one exists,
   and visually inspect the layout.
10. Confirm the automatic review receipt covers every cue, then inspect any
    remaining anomaly plus samples from the beginning, middle, and end.
    Structural validation and model review reduce risk but cannot mathematically
    prove meaning.
11. If the movie has embedded or forced subtitles, prove the player is using
    only the combined external track. An SRT cannot disable another track or
    remove text burned into the video.

Do not claim completion until the quality checklist passes.

## Non-Negotiable Invariants

- Source and target cue counts match the combined cue count.
- Every source, target, and combined cue has the same index and timestamps.
- The combined cue is exactly the source text followed by the target text.
- Each language uses no more than two displayed lines; the combined cue uses no
  more than four.
- Cue times remain monotonic, positive-duration, and non-overlapping.
- Source timing is preserved exactly during build. Overlapping source cues are
  rejected instead of trimmed or redistributed. A measured whole-file shift
  must move every active sidecar together.
- Exact-basename `.srt` and `.dual.default.srt` byte-match `.dual.srt` when
  auto-loading is intended.
- Cache entries belong to the exact source text/timing, language pair, requested
  model selector or pool, transport, rendered prompt hash, prompt version, and
  validator version. A pool may expand to survive quota failure, but never
  switch to an unrelated or narrower selector while reusing old chunks; retain
  selector history.
- A translated build cannot stage or install until a context-aware semantic
  review covers every cue. Its cache is bound to the complete first-pass draft,
  language pair, model selector, rendered review prompt, prompt and validator
  versions, and line-width contract; changing any draft, neighboring context,
  or review instruction invalidates the review.
- Malformed SRT blocks are errors. Never silently drop a cue.
- Provider failure must not publish partial active sidecars. The separately
  written translation cache may safely retain validated completed chunks so a
  later run can resume.

## Workflow

### 1. Inspect the movie and current sidecars

- Resolve the exact movie path; do not infer a similarly named release.
- List existing source, target, dual, default, plain, cache, and verify files.
- Run `ffprobe` or `probe-av` before mutation and record embedded subtitle
  streams.
- Preserve the exact original bytes and hashes in a timestamped backup.

### 2. Select the timing authority

Prefer, in order:

1. A clean same-release source-language SRT.
2. A compatible source-language text stream extracted from that movie.
3. A separately generated source-language ASR transcript that has been checked
   against the real audio.

Reject malformed cues, uploader ads, subtitle-production credits, wrong
language, giant unsynchronized cues, uncovered dialogue, or a different cut.
If no trusted transcript exists, create and verify one before using this skill's
build command.

### 3. Prove timing

Run `probe-av` first. Its stream starts and sync tags can expose mux offsets, but
they do not prove dialogue sync.

Compare actual spoken words with cue boundaries in at least these regions:

- beginning;
- middle;
- end;
- before the first cue; and
- after the last cue.

If a stable offset is measured:

- positive `--shift-ms` delays subtitles that appear early;
- negative `--shift-ms` advances subtitles that appear late.

If the offset changes over the movie, do not apply one blanket shift. Diagnose
release mismatch, frame-rate drift, A/V sync, or speech alignment instead.

### 4. Translate directly

Default to direct Gemini translation from the trusted source cue list. The
script supports:

- `gemini` for the Google Gen AI API;
- `gemini-cli` for a signed-in local Gemini CLI; and
- `mantis-antigravity` for a configured Antigravity lane.

Use a matched target-language SRT only as a fallback or when the user requests
it. Alignment must have real timestamp overlap; a nearby cue is not sufficient.
Each target cue must map unambiguously to one source cue. Weak or multi-cue
overlaps fail; the script never divides target words proportionally across
source cues.

Keep names, numbers, quoted phrases, signs, songs, and title cards meaningful.
The validator sends deterministic cue failures back to Gemini for a corrected
chunk. The build then runs a separate context-aware Gemini pass over every cue
and refuses to install partial, invalid, or overlong reviewed output. Inspect
flagged or unchanged lines and stratified samples as final evidence; correct,
rebuild, and revalidate if that evidence still exposes a meaning defect.

### 5. Fit the four-line layout without inventing timing

The script wraps by Unicode display width, including no-space CJK text. If
the translated language cannot fit within two lines at the requested width, the
selected Gemini route gets two bounded attempts to rewrite only those cues more
concisely without losing meaning. Each repair prompt includes the failed draft
and a measurable display-width budget. If either language still exceeds two
lines, the build fails before active sidecars are installed.

Resolve an over-dense cue by:

- making the translation concise without losing meaning;
- using a better release-matched source with natural cue boundaries; or
- speech-aligning and splitting the source at measured word boundaries.

Never split timing proportionally by character or word count and present it as
audio-aligned.

### 6. Stage, validate, and install

The build command validates the movie and source before changing active
sidecars. It then creates a same-filesystem staging bundle, verifies it, replaces
each destination with `os.replace`, writes the final report inside the same
handled rollback transaction, and restores all prior bytes after a handled copy,
installed-validation, or report-write failure. The resumable translation and
semantic-review caches are written separately and may advance even when the
final build fails. The review cache is keyed to the complete draft, so a stale
successful review cannot approve changed text.

This is not a crash-atomic multi-file filesystem transaction. A forced process
kill or power loss between per-file replacements can require recovery from the
timestamped backup.

For extra review before installation, use `--output-dir` to build into a separate
directory. Validate and inspect that bundle before rebuilding beside the movie.

### 7. Produce proof from the real output

- Run complete source/target/dual bundle validation.
- Render representative real-video frames with `proof`.
- Require a four-line frame when a four-line cue exists, while still sampling
  across the movie.
- Require each rendered frame to differ from its unsubtitled baseline.
- Inspect readability, wrapping, ordering, and screen collision.
- Confirm the intended external combined subtitle is selected in the actual
  player when player behavior is part of the request.
- Record that rendered frames prove layout only; they do not prove translation
  meaning, audio alignment, or player selection by themselves.

## Quick Start

Resolve `SKILL_DIR` to the directory containing this `SKILL.md`; never assume
the caller's current directory is the repository root. Every command below uses
that installed-skill path.

Generic source and target languages with Gemini:

```bash
python3 "$SKILL_DIR/scripts/dual_srt.py" build \
  --movie "/movies/Example.mkv" \
  --source-srt "/movies/Example.fr.srt" \
  --source-language fr \
  --target-language ja \
  --translate gemini
```

Default English to Indonesian:

```bash
python3 "$SKILL_DIR/scripts/dual_srt.py" build \
  --movie "/movies/Example.mkv" \
  --source-srt "/movies/Example.en.srt" \
  --translate gemini
```

The compatible aliases `--english-source` and `--indonesian-source` remain
available for older English-Indonesian commands.

Build from two matched SRT sources:

```bash
python3 "$SKILL_DIR/scripts/dual_srt.py" build \
  --movie "/movies/Example.mkv" \
  --source-srt "/movies/Example.es.srt" \
  --target-srt "/movies/Example.pt-BR.srt" \
  --source-language es \
  --target-language pt-BR
```

Validate the installed bundle:

```bash
python3 "$SKILL_DIR/scripts/dual_srt.py" validate \
  --movie "/movies/Example.mkv" \
  --source-language fr \
  --target-language ja
```

Probe A/V stream starts:

```bash
python3 "$SKILL_DIR/scripts/dual_srt.py" probe-av \
  --movie "/movies/Example.mkv"
```

Shift every active sidecar in the selected language pair after measured proof:

```bash
python3 "$SKILL_DIR/scripts/dual_srt.py" shift \
  --movie "/movies/Example.mkv" \
  --source-language fr \
  --target-language ja \
  --shift-ms 670
```

Render proof frames and require a four-line example:

```bash
python3 "$SKILL_DIR/scripts/dual_srt.py" proof \
  --movie "/movies/Example.mkv" \
  --out-dir "/tmp/dual-subtitle-proof" \
  --count 3 \
  --require-four-line
```

## Completion Report

Before responding, use `references/quality-checklist.md`. Report:

- the movie and language pair;
- the timing source and how dialogue sync was sampled;
- files written and backup location;
- cue counts and all validation failures, including zero counts;
- semantic translation checks;
- rendered/player proof completed; and
- any limitation that remains unproven.
