# Dual Subtitles SRT Skill

[![GitHub stars](https://img.shields.io/github/stars/CalebDane7/dual-subtitles-srt-skill?style=social)](https://github.com/CalebDane7/dual-subtitles-srt-skill/stargazers)
[![Public repo](https://img.shields.io/badge/repo-public-brightgreen)](https://github.com/CalebDane7/dual-subtitles-srt-skill)
[![Skill](https://img.shields.io/badge/Codex%20%2F%20Claude-skill-blue)](dual-subtitles-srt/SKILL.md)

Build, repair, validate, and prove bilingual movie subtitles in one readable SRT track.

This repository contains a portable agent skill for English plus Indonesian dual subtitles. It is designed for Codex, Claude Code, and other agents that can load skill folders. The main use case is simple: take a movie, an English subtitle source, and either an Indonesian subtitle source or a translation path, then produce a single combined SRT that works cleanly in players like Jellyfin, Plex, VLC, mpv, and most TV apps.

If this saves you time, star the repo so other people can find it:

https://github.com/CalebDane7/dual-subtitles-srt-skill

## The problem this fixes

Watching with two subtitle tracks sounds easy until the player renders both tracks in the same bottom subtitle band.

Typical failure modes:

- English and Indonesian overlap on top of each other.
- One language loads higher than expected and covers faces or action.
- The TV app chooses the wrong `.srt` sidecar.
- A translated subtitle has different cue boundaries than the English file, so the two languages drift apart.
- Long subtitle lines turn into five, six, or seven visual lines.
- A downloaded subtitle source has uploader ads, wrong-language text, or timing drift.
- One movie needs a timing shift, but only one sidecar gets shifted, so English and Indonesian no longer match.
- The media file itself has an audio/video start offset, but the subtitle file gets blamed.
- An agent says the job is done because it wrote a file, even though no validation or visual proof was run.

This skill fixes those problems by producing one combined subtitle cue per timestamp:

```text
English line 1
English line 2, if needed
Indonesian line 1
Indonesian line 2, if needed
```

That layout gives the player one subtitle track to render. English stays above Indonesian. Each cue is capped at four visual lines. The exact-basename `.srt` can be made byte-identical to the validated dual subtitle so the movie auto-loads the right captions.

## What the skill does

The skill gives an agent a complete workflow for bilingual SRT work:

- Find the real movie file and its sidecars.
- Inspect embedded subtitle streams with `ffprobe`.
- Choose a safe English timing base.
- Build Indonesian text from a matched Indonesian SRT or by translating English cues.
- Align Indonesian text to English cue timing.
- Split long bilingual cues so no cue exceeds four visual lines.
- Clamp source cue overlaps so the final subtitle track validates cleanly.
- Write `.en.srt`, `.id.srt`, `.dual.srt`, `.dual.default.srt`, and usually exact-basename `.srt`.
- Keep `.dual.srt`, `.dual.default.srt`, and plain `.srt` byte-identical when auto-loading is desired.
- Validate zero cue overlaps, zero blank cues, zero unintended single-language cues, and max four visual lines per cue.
- Probe audio/video stream starts before timing repairs.
- Shift every active subtitle sidecar together when a measured timing fix is needed.
- Render proof frames from the real movie so the agent can inspect the actual subtitle layout.

The skill is intentionally script-backed. The rules live in `SKILL.md`, but the fragile work happens in `scripts/dual_srt.py` so agents do not rewrite subtitle parsing, wrapping, timing, backup, and validation logic from scratch each time.

## Who should use it

Use this skill if you:

- Watch movies with English and Indonesian subtitles.
- Maintain a local Jellyfin, Plex, VLC, or TV-app movie library.
- Want dual-language captions without vertical overlap.
- Need agents to repair subtitle timing safely.
- Want a repeatable subtitle workflow with backups and validation.
- Need proof frames instead of "trust me" subtitle claims.
- Are building a Codex or Claude workflow for movie subtitle cleanup.

It is especially useful for large movie folders where the work needs to be batched, resumed, and proven without corrupting existing sidecars.

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
    └── scripts/
        └── dual_srt.py
```

Important files:

- `dual-subtitles-srt/SKILL.md` is the agent skill entrypoint.
- `dual-subtitles-srt/scripts/dual_srt.py` builds, validates, probes, shifts, and proofs subtitles.
- `dual-subtitles-srt/references/quality-checklist.md` defines the completion bar.
- `dual-subtitles-srt/agents/openai.yaml` provides UI metadata for skill lists.

## Install for Codex

Clone the repo:

```bash
git clone https://github.com/CalebDane7/dual-subtitles-srt-skill.git
cd dual-subtitles-srt-skill
```

Copy the skill folder into your Codex skills directory:

```bash
mkdir -p ~/.codex/skills
cp -R dual-subtitles-srt ~/.codex/skills/
```

Or symlink it if you want local edits to update immediately:

```bash
mkdir -p ~/.codex/skills
ln -sfn "$PWD/dual-subtitles-srt" ~/.codex/skills/dual-subtitles-srt
```

Then ask Codex:

```text
Use $dual-subtitles-srt to create English and Indonesian dual subtitles for /path/to/movie.mkv.
```

## Install for Claude Code

Claude-style skill folders use the same core layout. Clone the repo, then copy or symlink the skill folder into `~/.claude/skills`:

```bash
git clone https://github.com/CalebDane7/dual-subtitles-srt-skill.git
cd dual-subtitles-srt-skill
mkdir -p ~/.claude/skills
cp -R dual-subtitles-srt ~/.claude/skills/
```

Symlink option:

```bash
mkdir -p ~/.claude/skills
ln -sfn "$PWD/dual-subtitles-srt" ~/.claude/skills/dual-subtitles-srt
```

Then prompt Claude Code with the skill name and the movie path:

```text
Use the dual-subtitles-srt skill to build a four-line English plus Indonesian SRT for this movie. Validate it and render proof frames.
```

## Requirements

Minimum:

- Python 3.10 or newer.
- `ffmpeg` and `ffprobe` for media probing and proof frames.

Optional translation paths:

- `GEMINI_API_KEY` plus `google-genai` for direct Gemini API translation.
- Gemini CLI for OAuth-based local translation.
- Mantis Antigravity for a configured Gemini lane.

Install the optional Python package for direct Gemini API translation:

```bash
python3 -m pip install google-genai
```

Check that ffmpeg is available:

```bash
ffmpeg -version
ffprobe -version
```

## Quick start

Build from a matched English SRT and Indonesian SRT:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py build \
  --movie "/movies/Example.Movie.2024.1080p.mkv" \
  --english-source "/movies/Example.Movie.2024.1080p.en.srt" \
  --indonesian-source "/movies/Example.Movie.2024.1080p.id.srt" \
  --make-default \
  --make-plain
```

That writes:

```text
Example.Movie.2024.1080p.en.srt
Example.Movie.2024.1080p.id.srt
Example.Movie.2024.1080p.dual.srt
Example.Movie.2024.1080p.dual.default.srt
Example.Movie.2024.1080p.srt
Example.Movie.2024.1080p.dual.verify.json
```

Validate the output:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py validate \
  --movie "/movies/Example.Movie.2024.1080p.mkv"
```

Render proof frames:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py proof \
  --movie "/movies/Example.Movie.2024.1080p.mkv" \
  --count 3 \
  --out-dir "/tmp/dual-subtitle-proof"
```

## Translation modes

The best timing source is usually a clean English SRT that matches the exact movie release. Indonesian can come from a matched Indonesian SRT or from translation.

Direct Gemini API:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py build \
  --movie "/movies/Example.Movie.2024.1080p.mkv" \
  --english-source "/movies/Example.Movie.2024.1080p.en.srt" \
  --translate gemini \
  --model gemini-3.1-flash-lite,gemini-2.5-flash-lite \
  --make-default \
  --make-plain
```

Gemini CLI OAuth:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py build \
  --movie "/movies/Example.Movie.2024.1080p.mkv" \
  --english-source "/movies/Example.Movie.2024.1080p.en.srt" \
  --translate gemini-cli \
  --model default \
  --make-default \
  --make-plain
```

Mantis Antigravity:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py build \
  --movie "/movies/Example.Movie.2024.1080p.mkv" \
  --english-source "/movies/Example.Movie.2024.1080p.en.srt" \
  --translate mantis-antigravity \
  --model auto \
  --make-default \
  --make-plain
```

Translation output is cached beside the movie as `.id.translation-cache.json`, so interrupted batches can resume without paying for the same cue translations again.
The cache is treated as untrusted on every rebuild: if a translated cue is just a cue number, timestamp fragment, or numeric placeholder while the English cue has words, the script discards that cached value and retranslates it.

## Timing repair

Do not blindly shift subtitles. First check whether the media file has an audio/video stream-start offset:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py probe-av \
  --movie "/movies/Example.Movie.2024.1080p.mkv"
```

If measured subtitle timing is wrong, shift all active subtitle sidecars together:

```bash
python3 dual-subtitles-srt/scripts/dual_srt.py shift \
  --movie "/movies/Example.Movie.2024.1080p.mkv" \
  --shift-ms 670
```

Use a positive value when subtitles appear too early and need to be delayed. Use a negative value when subtitles appear too late and need to move earlier.

The shift command backs up existing subtitle files, shifts the active sidecars together, updates verification metadata when present, and validates the final dual SRT.

## Validation rules

The validator checks:

- No cue overlaps.
- No cue has more than four visual lines.
- No blank cues.
- No unintended single-language cues.
- No translated/lower-language cue is just a cue number or numeric placeholder when the English cue has words.
- `.dual.default.srt` byte-matches `.dual.srt`.
- Exact-basename `.srt` byte-matches `.dual.srt` when auto-loading is enabled.

Example validator output:

```json
{
  "cue_count": 1200,
  "max_visual_lines_per_cue": 4,
  "overlap_count": 0,
  "too_many_line_cues": [],
  "single_language_or_blank_cues": [],
  "numeric_translation_line_cues": [],
  "translation_sidecar_issue_count": 0,
  "ok": true,
  ".dual.default.srt_byte_match": true,
  ".srt_byte_match": true
}
```

Validation is necessary, but it is not the finish line. The skill also asks agents to render proof frames from the real movie so a person can inspect readability.

## How agents should use it

A good agent run should follow this pattern:

1. Find the actual movie file.
2. List existing sidecars.
3. Back up existing subtitles before replacement.
4. Choose a clean English timing base.
5. Translate or align Indonesian text.
6. Build `.en.srt`, `.id.srt`, `.dual.srt`, `.dual.default.srt`, and plain `.srt`.
7. Validate the final dual SRT.
8. Confirm byte-identical auto-load files.
9. Render proof frames from the real video.
10. Report what changed and what remains unproven.

Prompt example:

```text
Use $dual-subtitles-srt on /movies/Upgrade.2018.1080p.mkv.
Create one English plus Indonesian four-line dual SRT.
Use English above Indonesian.
Make the plain .srt auto-load copy.
Validate it and render proof frames.
Do not overwrite working sidecars without backups.
```

## Safety rules

- Do not overwrite good subtitles without backups.
- Do not use two separate active subtitle tracks for final proof.
- Do not call the job complete without validation.
- Do not call the job visually proven without proof frames.
- Do not apply one movie's timing shift to a whole library.
- Do not treat mux-level stream timestamps as full lip-sync proof.
- Do not copy partial translation output into a movie folder after a provider failure.
- Do not accept translated subtitle lines that only echo cue numbers, even if timing and four-line layout validation pass.

For long batches, build in a temp folder first, validate, then copy the finished sidecars into the movie folder.

## Why this is useful for movie libraries

Large personal libraries get messy fast. One movie has embedded English subtitles. Another has an external English file. Another has Indonesian subtitles from a different release. Another has a plain `.srt` that auto-loads but is not the one you meant to use. A TV app may hide subtitle-track details and simply load whatever sidecar name it recognizes.

This skill makes the final state explicit:

- One active dual-language track.
- English and Indonesian in one cue.
- Four visual lines maximum.
- Matching default and plain auto-load copies.
- A JSON verification report.
- Proof frames when the job matters.

That is much easier to audit than a folder full of unrelated `.srt`, `.en.srt`, `.id.srt`, and partially translated files.

## Keywords

Codex skill, Claude skill, Claude Code skill, bilingual subtitles, dual subtitles, dual SRT, English Indonesian subtitles, Indonesian subtitles, SRT repair, subtitle sync, subtitle timing shift, subtitle validation, ffmpeg subtitles, Jellyfin subtitles, Plex subtitles, VLC subtitles, movie subtitles, Gemini translation, Mantis Antigravity, AI agent skill.

## Contributing

Issues and pull requests are welcome. Useful contributions include:

- Better subtitle-source filtering.
- More proof-frame strategies.
- More translation transports.
- Better batch tooling for large movie folders.
- Edge cases for overlapping cues or unusual encodings.

When changing the script, run at least:

```bash
python3 -m py_compile dual-subtitles-srt/scripts/dual_srt.py
python3 dual-subtitles-srt/scripts/dual_srt.py build --help
python3 dual-subtitles-srt/scripts/dual_srt.py validate --help
python3 dual-subtitles-srt/scripts/dual_srt.py proof --help
```

For behavior changes, also run a small synthetic build and validate that `.dual.srt`, `.dual.default.srt`, and plain `.srt` match when auto-loading is enabled.
