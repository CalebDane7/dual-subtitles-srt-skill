# Dual Subtitles SRT Skill

This repository contains a Codex skill for creating and verifying bilingual SRT subtitles, especially English plus Indonesian movie subtitles.

## Purpose

The purpose of this skill is to stop the common dual-subtitle problem where two separate subtitle tracks render on top of each other. Instead of loading an English SRT and an Indonesian SRT as separate tracks, the skill creates one combined SRT file where each cue contains English first and Indonesian underneath it.

That makes the subtitle layout predictable:

```text
English line 1
English line 2, if needed
Indonesian line 1
Indonesian line 2, if needed
```

The skill also forces verification. It checks cue overlaps, line count, blank or single-language cues, exact-basename `.srt` auto-load copies, and rendered ffmpeg proof frames. The goal is not just to generate an SRT file, but to prove that the subtitles are synced, complete, and readable.

## What It Is For

- Creating dual English and Indonesian SRT sidecars.
- Rebuilding broken dual subtitles that overlap vertically.
- Translating English subtitle cues into Indonesian while keeping the same timing.
- Aligning a matched Indonesian SRT to an English timing base when translation is not available.
- Validating final `.srt` files before calling the job complete.
- Rendering proof frames so the user can see the subtitle layout.

## Install

Copy the skill folder into your Codex skills directory:

```bash
cp -r dual-subtitles-srt ~/.codex/skills/
```

Then ask Codex with a prompt like:

```text
Use $dual-subtitles-srt to create synced English and Indonesian dual subtitles for this movie.
```

## Included Files

- `dual-subtitles-srt/SKILL.md` - The Codex skill instructions.
- `dual-subtitles-srt/scripts/dual_srt.py` - Reusable build, validate, and proof script.
- `dual-subtitles-srt/references/quality-checklist.md` - Completion checklist.
- `dual-subtitles-srt/agents/openai.yaml` - UI metadata for the skill.

## Requirements

- Python 3.10 or newer.
- `ffmpeg` and `ffprobe` for media inspection and proof frames.
- Optional: `GEMINI_API_KEY` and `google-genai` for direct translation mode.

## Notes

For best results, build in a temporary folder first when using network translation APIs. Only copy outputs into the movie folder after the build and validation pass.
