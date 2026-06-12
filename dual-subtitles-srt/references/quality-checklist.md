# Dual Subtitle Quality Checklist

Run this checklist before claiming a dual subtitle job is complete.

## Source Checks

- Confirm the target video path and duration with `ffprobe`.
- Check for embedded subtitles with `ffprobe -show_streams`.
- Inspect existing sidecars and back them up before replacing.
- Prefer release-matched English timing.
- Reject downloaded subtitle sources that start or end with uploader ads, wrong-language text, or subtitle-site credits.
- Reject ASR sources with giant cues, very long cue durations, or obvious drift.

## Timing Checks

- Before shifting SRT timing, run `scripts/dual_srt.py probe-av` and record `audio_minus_video_ms` plus any sync/delay/offset tags.
- Treat ffprobe A/V starts as mux evidence only. If the user reports visible lip-sync problems, create short comparison clips or otherwise inspect playback before remuxing or shifting subtitles.
- When the user says subtitles are early or late, sample real movie audio against cues from the beginning, middle, and end before shifting files.
- Use measured offsets consistently: positive offset means the subtitle is early and should be delayed; negative offset means it is late and should be moved earlier.
- Shift only the movie whose timing was proven wrong. Do not reuse one movie's offset on the whole library.
- Shift `.en.srt`, `.id.srt`, `.dual.srt`, `.dual.default.srt`, and exact-basename `.srt` together with `scripts/dual_srt.py shift`.
- Re-run validation after every timing shift and render at least one proof frame from the shifted active `.srt`.

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
