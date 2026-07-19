# Dual Subtitles for Any Movie and Any Two Languages

[![GitHub stars](https://img.shields.io/github/stars/CalebDane7/dual-subtitles-srt-skill?style=social)](https://github.com/CalebDane7/dual-subtitles-srt-skill/stargazers)
[![Codex and Claude skill](https://img.shields.io/badge/agent_skill-Codex_%26_Claude-blue)](dual-subtitles-srt/SKILL.md)
[![Tests](https://img.shields.io/badge/tests-unittest-brightgreen)](dual-subtitles-srt/tests/test_dual_srt.py)

Build one synchronized movie SRT with the source language above its translation:
up to two lines per language and four lines total.

This dual-language subtitle workflow works with any movie that already has a
trusted, release-matched source transcript and can be read by ffmpeg. It can
translate each cue with Gemini, context-review every translation before
installation, keep the source timing, reject broken output, and verify the
source, target, and combined sidecars.

One build command performs both internal language passes: the first creates the
translation, and the second reviews every cue with surrounding dialogue for
meaning, natural phrasing, sentence continuity, idioms, wordplay, and speaker
tone. An incomplete or invalid review cannot publish active movie subtitles.

Choose any two common language tags accepted by the tool and supported by your
translation route and target player. English to Indonesian is the tested
default, not a hard-coded limit.

For a movie-transcription pipeline in any two supported languages, this skill
handles the complete dual-language subtitle stage. It does not transcribe audio.
Builds also normalize cue text, so styling tags and intentional source line
breaks are not preserved.

## Quick start

Translate a release-matched French SRT directly into Japanese:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py build \
  --movie "/movies/Example.mkv" \
  --source-srt "/movies/Example.fr.srt" \
  --source-language fr \
  --target-language ja \
  --translate gemini
```

The combined cue stays on the source cue's exact timestamps:

```text
source line 1
source line 2, if needed
target line 1
target line 2, if needed
```

The command writes a pair-specific, validated bundle:

```text
Example.fr.srt
Example.ja.srt
Example.ja.translation-cache.json
Example.ja.semantic-review-cache.json
Example.dual.srt
Example.dual.default.srt
Example.srt
Example.dual.verify.json
```

Validate it:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py validate \
  --movie "/movies/Example.mkv" \
  --source-language fr \
  --target-language ja
```

English to Indonesian remains the no-extra-flags default:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py build \
  --movie "/movies/Example.mkv" \
  --source-srt "/movies/Example.en.srt" \
  --translate gemini
```

## What it protects

Movie subtitle jobs often fail in ways that look valid at first glance: a
target-language file contains only cue numbers, malformed SRT blocks disappear
silently, a stale cache belongs to another language, or the build overwrites
working files before the final check.

The script treats those as release-blocking failures:

- Strict SRT parsing rejects malformed, missing, non-sequential, or
  non-positive-duration cues instead of dropping them.
- Source, target, and combined cue counts and timestamps must match exactly.
- Every combined cue must equal its source cue followed by its target cue.
- Blank/null translations, `Cue 12`-style labels, timestamp fragments, numeric
  placeholders, implausibly short translations, and copied ordinary source
  dialogue are rejected.
- A rejected Gemini chunk is retried with the exact cue indexes and failure
  reasons, including foreign-language dialogue that was copied unchanged.
- Every first-pass cue receives neighboring source dialogue, including at
  translation chunk boundaries.
- In translation mode, before installation, a mandatory second Gemini pass
  reviews every cue with two cues of source-and-translation context on each
  side. It checks for and can correct semantic omissions, literal phrasing,
  duplicated connectors, cross-cue grammar, idioms, wordplay, punctuation,
  and speaker register.
- The semantic-review cache is bound to the complete first-pass draft, its
  context, and the exact rendered review prompt. A changed draft or prompt
  cannot reuse a stale review, and partial review coverage cannot publish
  sidecars.
- Genuine numeric dialogue is still allowed when the source cue is genuinely
  numeric.
- Translation caches are bound to exact source text and timing, language pair,
  requested model selector or pool, transport, rendered prompt hash, prompt
  version, and validator version. A selector pool may be expanded after a quota
  failure without discarding validated chunks; selector history is retained.
- Unicode display width is used for wrapping, including wide and no-space CJK
  text.
- A cue that cannot fit two lines per language fails for correction. In
  translation mode, the script first asks the selected Gemini route for a
  constrained, meaning-preserving rewrite using the failed draft and an exact
  display-width budget. It never invents proportional timing to squeeze text
  in.
- Source, target, dual, and alias files are built in staging and validated. The
  sidecars and final verification report are installed in one handled rollback
  transaction and restored to their original bytes after a handled copy,
  installed-validation, or final-report failure.
- The translation and semantic-review caches are separate resumable work state.
  They are written atomically per update and may retain validated completed
  chunks after a later provider or build failure.
- `.dual.default.srt` and exact-basename `.srt` are byte-identical to the
  validated `.dual.srt` by default.

The result is one subtitle track, so the two languages do not compete for the
same subtitle area as separate external tracks.

## Start from a trusted transcript

The build command requires a source-language SRT. That source can be:

1. an existing sidecar matched to the exact movie release;
2. a text subtitle stream extracted from the movie; or
3. a separately generated speech-recognition transcript that has been checked
   against the real audio.

This repository does not bundle automatic speech recognition. If the movie has
no transcript, create and verify one first. A clean transcript from a different
cut is not a safe timing source.

The compatible flags `--english-source` and `--indonesian-source` remain
available for existing English-Indonesian workflows. New commands should use
the generic `--source-srt`, `--target-srt`, `--source-language`, and
`--target-language` flags.

## Translation modes

Direct Gemini translation is the default workflow because it maps every target
cue to the trusted source cue's index and timestamps.

### Google Gen AI API

Install the optional package and export your API key through your normal secret
management:

```bash
python3 -m pip install google-genai
```

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py build \
  --movie "/movies/Example.mkv" \
  --source-srt "/movies/Example.en.srt" \
  --source-language en \
  --target-language id \
  --translate gemini
```

When `--model` is omitted, the API path tries
`gemini-pro-latest,gemini-flash-latest,gemini-flash-lite-latest` in that order.
The `*-latest` aliases can move to newer releases; pass a pinned supported model
when reproducible output matters.

Receipts and caches label these values as requested model selectors, not as
proof of one immutable model serving every cue.

### Gemini CLI

Use the signed-in local CLI without placing an API key in the command:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py build \
  --movie "/movies/Example.mkv" \
  --source-srt "/movies/Example.en.srt" \
  --translate gemini-cli
```

### Mantis Antigravity

Use an already configured Antigravity lane:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py build \
  --movie "/movies/Example.mkv" \
  --source-srt "/movies/Example.en.srt" \
  --translate mantis-antigravity
```

### Matched source and target SRT files

Translation from the source cue list is preferred. A close release-matched
target SRT is also supported:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py build \
  --movie "/movies/Example.mkv" \
  --source-srt "/movies/Example.es.srt" \
  --target-srt "/movies/Example.pt-BR.srt" \
  --source-language es \
  --target-language pt-BR
```

Each target cue must overlap one source cue unambiguously. The aligner does not
pair unrelated dialogue merely because it is nearby, and it never divides words
proportionally across source cues. Ambiguous or weak overlaps fail so you can
use direct cue-by-cue translation or a better release-matched target SRT.

## Timing that follows the movie

The script preserves the trusted source SRT's timestamps exactly. Overlapping
source cues are rejected instead of being trimmed or redistributed. It does not
pretend that container metadata alone proves that spoken words and subtitles
match.

Check audio/video stream starts before timing work:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py probe-av \
  --movie "/movies/Example.mkv"
```

Then compare real dialogue with source cues near the beginning, middle, and end.
Also inspect before the first cue and after the last cue for missing dialogue.

If the same signed offset is measured across the movie, shift the entire active
language pair together:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py shift \
  --movie "/movies/Example.mkv" \
  --source-language fr \
  --target-language ja \
  --shift-ms 670
```

A positive value delays subtitles that appear early. A negative value advances
subtitles that appear late. If the error grows or changes through the movie,
investigate release mismatch, frame-rate drift, or media A/V sync instead of
applying one blanket shift.

## Proof from the real movie

Render representative frames using the active combined SRT:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py proof \
  --movie "/movies/Example.mkv" \
  --out-dir "/tmp/dual-subtitle-proof" \
  --count 3 \
  --require-four-line
```

The proof command samples across the movie, forces one real four-line cue into
the sample when requested, and compares every rendered frame with an
unsubtitled baseline to confirm that external subtitle pixels appeared. It can
also write a JSON receipt with frame and input hashes.

Rendered frames prove layout, not translation meaning or audio sync. The agent
workflow therefore also requires semantic translation samples and real audio
checks before it claims a complete movie.

## Install as an agent skill

Clone the repository:

```bash
git clone https://github.com/CalebDane7/dual-subtitles-srt-skill.git
cd dual-subtitles-srt-skill
```

### Codex

The current personal skill discovery path is `~/.agents/skills`:

```bash
mkdir -p ~/.agents/skills
ln -sfn "$PWD/dual-subtitles-srt" \
  ~/.agents/skills/dual-subtitles-srt
```

Then ask:

```text
Use $dual-subtitles-srt for this movie. Make English and Indonesian
subtitles in one synchronized four-line track, translate directly with Gemini,
validate the complete movie, and render proof frames.
```

If you name different languages, the same default contract applies.

### Claude Code

```bash
mkdir -p ~/.claude/skills
ln -sfn "$PWD/dual-subtitles-srt" \
  ~/.claude/skills/dual-subtitles-srt
```

Then ask Claude Code to use the `dual-subtitles-srt` skill and provide the movie
path plus the two languages.

## Requirements

- Python 3.10 or newer.
- `ffmpeg` and `ffprobe` for media inspection and rendered proof.
- One translation route when no matched target SRT is supplied:
  `google-genai`, Gemini CLI, or Mantis Antigravity.

## Validation guarantees

`validate` checks the complete installed bundle by default:

- strict SRT structure;
- positive cue durations and monotonic timing;
- zero cue overlaps;
- at most two lines per language and four lines total;
- exact source/target/dual cue count and timing parity;
- exact source-plus-target composition;
- zero suspicious translated cues;
- byte-identical default and plain auto-load aliases; and
- embedded subtitle stream reporting.

`--srt` can select an alternate combined-SRT path, but `validate` still requires
the matching source and target sidecars so it can prove timing and composition.

## Output files

For `fr` to `ja`, the normal sidecars are:

| File | Purpose |
| --- | --- |
| `Movie.fr.srt` | Normalized source-language cues |
| `Movie.ja.srt` | Target-language cues on the same timestamps |
| `Movie.dual.srt` | Combined source-above-target track |
| `Movie.dual.default.srt` | Byte-identical default alias |
| `Movie.srt` | Byte-identical exact-basename auto-load alias |
| `Movie.ja.translation-cache.json` | Translation mode only: source/language/transport/model-selector/prompt-bound resumable cache |
| `Movie.ja.semantic-review-cache.json` | Translation mode only: draft-and-prompt-bound resumable full-cue semantic-review cache |
| `Movie.dual.verify.json` | Build and validation receipt |

Existing files are copied into a timestamped backup directory before
replacement. Use `--output-dir` when you want a separately reviewable bundle
without writing beside the movie.

## Limits stated plainly

- "Any two languages" means common language tags accepted by this tool and
  languages supported by the selected translation route and player's renderer.
- The script does not perform speech recognition.
- Builds normalize text and do not preserve styling tags or intentional source
  line breaks.
- Automated checks catch structural corruption and common bogus translation
  output; they cannot prove every sentence's meaning.
- `probe-av` detects mux-level stream-start evidence, not human lip sync.
- Rendered ffmpeg frames prove layout, not which track a particular TV app will
  select.
- An external SRT cannot disable embedded, forced, secondary, or burned-in
  subtitles. Disable extra tracks in the player or remux when required.
- Per-file replacement and handled-error rollback are not a crash-atomic
  multi-file transaction. A forced kill or power loss can require recovery from
  the timestamped backup.
- Right-to-left and complex-script rendering depends on the installed libass,
  fonts, shaping stack, and player. Test the actual target player.

## Tests

Run the regression suite:

```bash
python3 -B -m unittest discover -s dual-subtitles-srt/tests -v
python3 -m py_compile dual-subtitles-srt/scripts/dual_srt.py
```

The suite covers numeric-placeholder rejection, cache isolation, strict SRT
parsing, four-line wrapping, Unicode/CJK display width, exact bundle
composition, generic language pairs, and handled-failure rollback.

## Repository layout

```text
dual-subtitles-srt-skill/
├── README.md
└── dual-subtitles-srt/
    ├── SKILL.md
    ├── agents/
    │   └── openai.yaml
    ├── references/
    │   └── quality-checklist.md
    ├── scripts/
    │   └── dual_srt.py
    └── tests/
        └── test_dual_srt.py
```

- [`SKILL.md`](dual-subtitles-srt/SKILL.md) defines the agent's complete default
  workflow.
- [`dual_srt.py`](dual-subtitles-srt/scripts/dual_srt.py) builds, validates,
  probes, shifts, and renders proof frames for SRT bundles.
- [`quality-checklist.md`](dual-subtitles-srt/references/quality-checklist.md)
  defines the completion gate.

## Contributing

Issues and pull requests are welcome, especially for additional scripts,
renderers, language pairs, translation transports, and release-matched timing
edge cases.

Behavior changes should include an old-failure regression test and the adjacent
test suite. Do not weaken the no-placeholder, exact-timing, four-line, backup,
or handled-install-failure rollback guarantees.
