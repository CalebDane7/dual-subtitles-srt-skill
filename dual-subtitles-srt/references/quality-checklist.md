# Dual Subtitle Quality Checklist

Run every applicable check before claiming a movie's dual subtitles are
complete. English to Indonesian is the default pair; the same gates apply to
every source and target language.

## Movie and source

- Confirm the exact movie path, release, duration, and stream list with
  `ffprobe`.
- Record embedded, forced, and default subtitle streams.
- Inventory and back up existing sidecars, caches, and verification reports
  before replacement.
- Use a clean source-language transcript matched to this exact movie release.
- If the source came from ASR, check it against real dialogue before translating.
- Record that builds normalize cue text and do not preserve styling tags or
  intentional source line breaks.
- Reject malformed blocks, missing cue indexes, non-positive durations,
  non-monotonic starts, uploader ads, subtitle-production credits, wrong
  language, giant cues, or obvious drift.
- Confirm spoken content is not missing before the first cue or after the last
  cue.

## Timing

- Run `scripts/dual_srt.py probe-av` before a build or shift.
- Treat `audio_minus_video_ms` and sync tags as mux evidence, not dialogue-sync
  proof.
- Compare actual spoken words with source cues near the beginning, middle, and
  end.
- When subtitles are early or late, measure the signed offset in all three
  regions before changing files.
- Use a positive shift to delay early subtitles and a negative shift to advance
  late subtitles.
- Do not apply a blanket shift when the offset changes across the movie.
- Do not reuse one movie's offset for another release or movie.
- Preserve every source timestamp during build. Reject overlapping source cues
  instead of trimming or redistributing them.
- Apply a measured whole-file shift only to every active sidecar together and
  record the shift in the verification report.
- Never invent proportional cue times to make text fit.

## Translation

- Direct-translate the trusted source cue list with Gemini by default.
- Include neighboring source dialogue for every initial translation cue,
  including cues at provider chunk boundaries.
- Confirm the requested source and target language tags are accepted by the
  tool, provider, and renderer.
- Preserve names, numbers, quoted phrases, songs, signs, and title-card meaning.
- Reject blank/null results, cue/index labels, timestamp fragments, numeric
  placeholders, copied ordinary source dialogue, and implausibly short output.
- Bind cache reuse to exact source text and timing, language pair, transport,
  requested model selector or pool, prompt version, and validator version.
- Allow fallback-pool expansion only when it retains every prior selector, and
  record selector history instead of claiming an exact serving model.
- Require the automatic semantic pass to review every cue with two cues of
  source-and-target context before installation.
- Confirm its cache is bound to the complete current draft, language pair,
  model selector, exact rendered review prompt, prompt and validator versions,
  and target line width. A prompt-only change must invalidate the cache. Never
  trust a standalone `reviewed: true` flag or partial coverage.
- Review every remaining validator anomaly, identical source/target line,
  proper-name or number-heavy cue, and unusually long or short translation.
- Review stratified semantic samples from the beginning, middle, and end.
- Do not claim translation meaning was proven by SRT parsing or rendered frames.
- If using a matched target SRT, require one unambiguous source overlap per
  target cue. Reject weak or multi-source mappings instead of dividing words
  proportionally.

## Four-line build

- Use one combined SRT cue per source timestamp.
- Put source-language text above target-language text.
- Keep each language to at most two displayed lines.
- Confirm the combined cue has at most four displayed lines.
- Check no-space and wide-character scripts by display width, not ASCII
  character count.
- If a cue is too dense, shorten the translation without losing meaning or
  speech-align the source split. Do not extend its end time or repeat text.
- Build in staging and validate the complete bundle before installing it.
- Confirm a handled copy/final-validation failure restores every active
  sidecar byte that the install attempted to change.
- Confirm a final verification-report failure also restores every changed
  sidecar and the previous report.
- Treat the translation and semantic-review caches as separately written
  resumable work state; they may contain validated completed chunks after a
  failed provider/build run.
- Record that a forced kill or power loss between per-file replacements is not a
  crash-atomic multi-file transaction and may require backup recovery.

## Bundle validation

- Source, target, and dual cue counts match exactly.
- Every cue index and start/end timestamp matches across all three files.
- Every combined cue is exactly the source cue followed by the target cue.
- Zero overlapping cues.
- Zero malformed, blank, or unintended single-language cues.
- Zero cues above the two-lines-per-language/four-lines-total limit.
- Zero placeholder or suspicious target cues.
- `.dual.default.srt` byte-matches `.dual.srt` when auto-loading is enabled.
- Exact-basename `.srt` byte-matches `.dual.srt` when auto-loading is enabled.
- The installed bundle passes validation again after the staging copy.

## Real proof

- Render frames from the actual movie and active combined SRT.
- Include a real four-line cue when one exists.
- Sample proof times across the movie; do not render the same cue repeatedly.
- Confirm every subtitled proof frame differs from its unsubtitled baseline.
- Visually inspect language order, wrapping, readability, and vertical
  collision.
- If embedded/internal subtitles exist, select only the external combined track
  in the player or record the exact unresolved stacking risk.
- Remember that an SRT cannot disable another subtitle track or remove burned-in
  text.
- If the request concerns Jellyfin, Plex, VLC, mpv, or a TV app, prove selection
  and playback in that player before claiming player-specific success.

## Completion receipt

- Record movie, language pair, source transcript, and translation transport.
- Record backup location and exact files written.
- Record cue count, zero/failure counts, alias hashes, and verification report.
- Record audio timing samples and any measured shift.
- Record semantic translation samples and corrections.
- Record semantic-review version, full cue coverage, draft-bound cache path,
  and fail-closed result.
- Record rendered frames and player proof.
- State every remaining limitation plainly.
