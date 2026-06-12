# Dual Subtitle Quality Checklist

Run this checklist before claiming a dual subtitle job is complete.

## Source Checks

- Confirm the target video path and duration with `ffprobe`.
- Check for embedded subtitles with `ffprobe -show_streams`.
- Inspect existing sidecars and back them up before replacing.
- Prefer release-matched English timing.
- Reject downloaded subtitle sources that start or end with uploader ads, wrong-language text, or subtitle-site credits.
- Reject ASR sources with giant cues, very long cue durations, or obvious drift.

## Build Checks

- Use one combined SRT cue per timestamp.
- Put English above Indonesian in the same cue.
- Keep each language to at most two wrapped lines.
- Split long cues instead of allowing five or more visual lines.
- Preserve names, numbers, quoted code phrases, military terms, and title-card meaning in translations.
- If exact-basename `.srt` should auto-load, make it byte-identical to `.dual.srt`.

## Validation Checks

- Parse the final `.dual.srt`.
- Assert zero overlapping cues.
- Assert max visual lines per cue is `4`.
- Assert zero blank cues.
- Assert zero single-language cues unless explicitly accepted.
- Confirm `.dual.default.srt` byte-matches `.dual.srt`.
- Confirm exact-basename `.srt` byte-matches `.dual.srt` when auto-loading is desired.

## Proof Checks

- Render proof frames with ffmpeg from the actual movie file and active SRT.
- Include at least one four-line cue when available.
- Visually inspect the proof frame for vertical subtitle collision.
- Do not treat parser validation alone as visual proof.
