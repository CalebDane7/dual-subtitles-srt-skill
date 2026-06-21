#!/usr/bin/env python3
"""Build, validate, and proof combined dual-language SRT subtitles."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path


TIMESTAMP_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)
TAG_RE = re.compile(r"<[^>]+>|\{\\[^}]+\}")
AD_RE = re.compile(
    r"(?i)(www\.|https?://|\.com\b|subtitle|subscene|opensubtitles|"
    r"translated by|subtitles by|penerjemah|diterjemahkan oleh|resync|"
    r"sync(?:hronize)? by|visit us|instagram|facebook|idfl|sebuah-dongeng|"
    r"iklan|streaming|casino|\bbet\b|member of|created by|movie2shared|"
    r"ganool\.com|alih bahasa:)"
)
DEFAULT_SHIFT_SUFFIXES = [".en.srt", ".id.srt", ".dual.srt", ".dual.default.srt", ".srt"]


@dataclass
class Cue:
    index: int
    start_ms: int
    end_ms: int
    text: str


def parse_time(parts: tuple[str, str, str, str]) -> int:
    h, m, s, ms = map(int, parts)
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def fmt_time(ms: int) -> str:
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def fmt_ffmpeg_time(ms: int) -> str:
    h, rem = divmod(max(0, int(ms)), 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{milli:03d}"


def read_text_lenient(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Could not decode {path}")


def clean_text(text: str) -> str:
    text = TAG_RE.sub("", text)
    text = text.replace("&nbsp;", " ").replace("\ufeff", "")
    return " ".join(text.split())


def sidecar_path(movie: Path, suffix: str) -> Path:
    return movie.with_name(movie.name[: -len(movie.suffix)] + suffix)


def read_srt(path: Path) -> list[Cue]:
    decoded = read_text_lenient(path)
    normalized = decoded.replace("\r\n", "\n").replace("\r", "\n").strip()
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", normalized):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        ts_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if ts_index is None:
            continue
        match = TIMESTAMP_RE.search(lines[ts_index])
        if not match:
            continue
        text = clean_text(" ".join(lines[ts_index + 1 :]))
        if not text:
            continue
        cues.append(
            Cue(
                index=len(cues) + 1,
                start_ms=parse_time(match.groups()[:4]),
                end_ms=parse_time(match.groups()[4:]),
                text=text,
            )
        )
    return cues


def wrap_lines(text: str, width: int) -> list[str]:
    text = clean_text(text)
    lines = textwrap.wrap(text, width=width, break_long_words=False, break_on_hyphens=False)
    return lines or [text]


def split_oversized_english(cues: list[Cue], width: int) -> list[Cue]:
    split: list[Cue] = []
    for cue in cues:
        wrapped = wrap_lines(cue.text, width)
        if len(wrapped) <= 2:
            split.append(cue)
            continue

        words = cue.text.split()
        chunks: list[list[str]] = []
        current: list[str] = []
        for word in words:
            proposed = current + [word]
            if current and len(wrap_lines(" ".join(proposed), width)) > 2:
                chunks.append(current)
                current = [word]
            else:
                current = proposed
        if current:
            chunks.append(current)

        duration = max(1, cue.end_ms - cue.start_ms)
        cursor = cue.start_ms
        for i, chunk in enumerate(chunks):
            end = cue.end_ms if i == len(chunks) - 1 else cue.start_ms + round(duration * (i + 1) / len(chunks))
            end = max(cursor + 500, min(end, cue.end_ms))
            split.append(Cue(len(split) + 1, cursor, end, " ".join(chunk)))
            cursor = end

    for i, cue in enumerate(split, 1):
        cue.index = i
    return split


def chunk_for_two_lines(text: str, width: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    for word in clean_text(text).split():
        proposed = current + [word]
        if current and len(wrap_lines(" ".join(proposed), width)) > 2:
            chunks.append(" ".join(current))
            current = [word]
        else:
            current = proposed
    if current:
        chunks.append(" ".join(current))
    return chunks or [clean_text(text)]


def split_text_evenly(text: str, parts: int) -> list[str]:
    words = clean_text(text).split()
    if parts <= 1 or not words:
        return [clean_text(text)]
    chunks: list[str] = []
    for i in range(parts):
        start = round(i * len(words) / parts)
        end = round((i + 1) * len(words) / parts)
        chunks.append(" ".join(words[start:end]).strip())
    return [chunk for chunk in chunks if chunk] or [clean_text(text)]


def pad_chunks(chunks: list[str], parts: int, fallback: str) -> list[str]:
    # WHY: callers index chunks[0..parts-1] in lockstep for both languages. A short
    # side can yield fewer non-empty pieces than `parts`; pad by repeating the last
    # piece so every sub-cue stays bilingual (never blank) and indexing never raises.
    chunks = [c for c in chunks if c] or [clean_text(fallback)]
    while len(chunks) < parts:
        chunks.append(chunks[-1])
    return chunks[:parts]


def clamp_overlaps(cues: list[Cue]) -> list[Cue]:
    # WHY: a few source subtitles have cues whose display time overlaps (or is even out
    # of order with) the next cue; validate() rejects any overlap. Sort by start, then
    # trim each cue to end before the next one begins so the dual track never shows two
    # cues at once. id_texts is keyed by cue.index, so reordering the list here does not
    # break the English/Indonesian pairing.
    cues.sort(key=lambda c: (c.start_ms, c.end_ms))
    for a, b in zip(cues, cues[1:]):
        if a.end_ms > b.start_ms:
            a.end_ms = max(a.start_ms + 1, b.start_ms - 1)
    return cues


def split_bilingual_overflows(
    cues: list[Cue],
    id_texts: dict[int, str],
    english_width: int,
    indonesian_width: int,
) -> tuple[list[Cue], dict[int, str]]:
    fixed: list[Cue] = []
    fixed_id: dict[int, str] = {}

    for cue in cues:
        en_chunks = chunk_for_two_lines(cue.text, english_width)
        id_chunks = chunk_for_two_lines(id_texts[cue.index], indonesian_width)
        parts = max(len(en_chunks), len(id_chunks))
        if parts == 1:
            new_cue = Cue(len(fixed) + 1, cue.start_ms, cue.end_ms, cue.text)
            fixed.append(new_cue)
            fixed_id[new_cue.index] = id_texts[cue.index]
            continue

        if len(en_chunks) != parts:
            en_chunks = split_text_evenly(cue.text, parts)
        if len(id_chunks) != parts:
            id_chunks = split_text_evenly(id_texts[cue.index], parts)
        # WHY: split_text_evenly drops empty pieces, so a one-word side paired with a
        # long line can return fewer than `parts` chunks and crash chunks[i] below.
        en_chunks = pad_chunks(en_chunks, parts, cue.text)
        id_chunks = pad_chunks(id_chunks, parts, id_texts[cue.index])

        duration = max(1, cue.end_ms - cue.start_ms)
        cursor = cue.start_ms
        for i in range(parts):
            end = cue.end_ms if i == parts - 1 else cue.start_ms + round(duration * (i + 1) / parts)
            end = max(cursor + 500, min(end, cue.end_ms))
            new_cue = Cue(len(fixed) + 1, cursor, end, en_chunks[i])
            fixed.append(new_cue)
            fixed_id[new_cue.index] = id_chunks[i]
            cursor = end

    return fixed, fixed_id


def write_srt(path: Path, cues: list[Cue], texts: dict[int, str], width: int) -> None:
    lines: list[str] = []
    for cue in cues:
        lines.append(str(cue.index))
        lines.append(f"{fmt_time(cue.start_ms)} --> {fmt_time(cue.end_ms)}")
        lines.extend(wrap_lines(texts[cue.index], width))
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_dual(path: Path, cues: list[Cue], id_texts: dict[int, str], en_width: int, id_width: int) -> None:
    lines: list[str] = []
    for cue in cues:
        en_lines = wrap_lines(cue.text, en_width)
        id_lines = wrap_lines(id_texts[cue.index], id_width)
        if len(en_lines) > 2 or len(id_lines) > 2:
            raise ValueError(f"Cue {cue.index} exceeds two lines per language")
        lines.append(str(cue.index))
        lines.append(f"{fmt_time(cue.start_ms)} --> {fmt_time(cue.end_ms)}")
        lines.extend(en_lines)
        lines.extend(id_lines)
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def backup(paths: list[Path], label: str) -> Path | None:
    existing = [path for path in paths if path.exists()]
    if not existing:
        return None
    root = existing[0].parent / f"{existing[0].name}.backups" / (time.strftime("%Y%m%dT%H%M%SZ") + f"-{label}")
    root.mkdir(parents=True, exist_ok=True)
    for path in existing:
        shutil.copy2(path, root / f"{path.name}.before-{label}")
    return root


def translate_with_gemini(cues: list[Cue], cache_path: Path, model: str, chunk_size: int) -> dict[int, str]:
    try:
        from google import genai
        from google.genai import types
    except Exception as exc:
        raise RuntimeError("Install google-genai and set GEMINI_API_KEY for --translate gemini") from exc

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")

    cached = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    translated = {int(k): str(v) for k, v in cached.items()}
    missing = [cue for cue in cues if cue.index not in translated]
    if not missing:
        return translated

    client = genai.Client(api_key=api_key)
    models = [item.strip() for item in model.split(",") if item.strip()] or [model]
    schema = {
        "type": "object",
        "properties": {
            "translations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"index": {"type": "integer"}, "id": {"type": "string"}},
                    "required": ["index", "id"],
                },
            }
        },
        "required": ["translations"],
    }

    for offset in range(0, len(missing), chunk_size):
        chunk = missing[offset : offset + chunk_size]
        payload = [{"index": cue.index, "en": cue.text} for cue in chunk]
        prompt = (
            "Translate these English movie subtitle cues into natural Indonesian. "
            "Preserve complete meaning, names, numbers, quoted code words, title cards, "
            "military terms, and Terminator/Skynet terms. Keep each cue concise but complete. "
            "Return JSON only with translations[].index and translations[].id. Do not add timestamps or markdown.\n\n"
            + json.dumps({"cues": payload}, ensure_ascii=False)
        )
        last_exc: Exception | None = None
        result: dict[int, str] | None = None
        for model_name in models:
            for attempt in range(1, 3):
                try:
                    # WHY: Gemini free-tier quotas can be per model. Accepting a
                    # comma-separated model pool lets a long movie batch keep moving
                    # without dropping the cache-backed, structured JSON translation
                    # path or falling back to unsafe hand-written subtitle text.
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            temperature=0.1,
                            responseMimeType="application/json",
                            responseSchema=schema,
                        ),
                    )
                    data = json.loads(response.text)
                    result = {int(item["index"]): clean_text(str(item["id"])) for item in data["translations"]}
                    print(f"translated chunk with Gemini model {model_name}", flush=True)
                    break
                except Exception as exc:
                    last_exc = exc
                    text = str(exc)
                    quota_like = "RESOURCE_EXHAUSTED" in text or "429" in text or "quota" in text.lower()
                    if quota_like:
                        print(f"Gemini model {model_name} quota/error; trying next model", flush=True)
                        break
                    if attempt == 2:
                        print(f"Gemini model {model_name} failed; trying next model", flush=True)
                        break
                    time.sleep(2 * attempt)
            if result is not None:
                break
        if result is None:
            assert last_exc is not None
            raise last_exc
        expected = {cue.index for cue in chunk}
        if set(result) != expected:
            raise ValueError(f"Translation index mismatch: expected {len(expected)} got {len(result)}")
        translated.update(result)
        cache_path.write_text(json.dumps(translated, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"translated {len(translated)}/{len(cues)} cues", flush=True)

    return translated


def parse_json_object(text: str) -> dict[str, object]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def translate_with_gemini_cli(cues: list[Cue], cache_path: Path, model: str, chunk_size: int) -> dict[int, str]:
    cached = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    translated = {int(k): str(v) for k, v in cached.items()}
    missing = [cue for cue in cues if cue.index not in translated]
    if not missing:
        return translated

    if not shutil.which("gemini"):
        raise RuntimeError("gemini CLI is not available for --translate gemini-cli")

    for offset in range(0, len(missing), chunk_size):
        chunk = missing[offset : offset + chunk_size]
        payload = [{"index": cue.index, "en": cue.text} for cue in chunk]
        prompt = (
            "Translate these English movie subtitle cues into natural Indonesian. "
            "Preserve complete meaning, names, numbers, quoted code words, title cards, "
            "military terms, and proper names. Keep each cue concise but complete. "
            "Return JSON only with translations[].index and translations[].id. "
            "Do not add timestamps, markdown, comments, or extra keys.\n\n"
            + json.dumps({"cues": payload}, ensure_ascii=False)
        )
        cmd = ["gemini", "-p", prompt, "--output-format", "text"]
        if model and model.lower() not in {"default", "gemini-cli-default"}:
            cmd[1:1] = ["-m", model]

        for attempt in range(1, 4):
            try:
                # WHY: this workstation often has Gemini CLI OAuth available but no
                # exported GEMINI_API_KEY. Keep this headless CLI path cache-backed so
                # large subtitle batches can still be rebuilt without exposing secrets
                # or bypassing the normal four-line validation/backup writer below.
                proc = subprocess.run(
                    cmd,
                    check=True,
                    text=True,
                    capture_output=True,
                    timeout=300,
                )
                data = parse_json_object(proc.stdout)
                result = {int(item["index"]): clean_text(str(item["id"])) for item in data["translations"]}
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(5 * attempt)
        expected = {cue.index for cue in chunk}
        if set(result) != expected:
            raise ValueError(f"Translation index mismatch: expected {len(expected)} got {len(result)}")
        translated.update(result)
        cache_path.write_text(json.dumps(translated, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"translated {len(translated)}/{len(cues)} cues via gemini CLI", flush=True)

    return translated


def translate_with_mantis_antigravity(cues: list[Cue], cache_path: Path, model: str, chunk_size: int) -> dict[int, str]:
    cached = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    translated = {int(k): str(v) for k, v in cached.items()}
    missing = [cue for cue in cues if cue.index not in translated]
    if not missing:
        return translated

    if not shutil.which("mantis"):
        raise RuntimeError("mantis CLI is not available for --translate mantis-antigravity")

    for offset in range(0, len(missing), chunk_size):
        chunk = missing[offset : offset + chunk_size]
        payload = [{"index": cue.index, "en": cue.text} for cue in chunk]
        prompt = (
            "Translate these English movie subtitle cues into natural Indonesian. "
            "Preserve complete meaning, names, numbers, quoted code words, title cards, "
            "military terms, and proper names. Keep each cue concise but complete. "
            "Return JSON only with translations[].index and translations[].id. "
            "Do not add timestamps, commentary, markdown explanation, or extra keys.\n\n"
            + json.dumps({"cues": payload}, ensure_ascii=False)
        )
        cmd = ["mantis", "antigravity", "--print-timeout", "5m", "--print", prompt]

        for attempt in range(1, 4):
            try:
                # WHY: Gemini CLI OAuth can be unavailable or capped while this
                # workstation still has Mantis Antigravity configured with the
                # user's preferred Gemini lane. Keep this path cache-backed and
                # parse-strict so subtitle batches can resume without losing the
                # protected four-line validator/sidecar writer below.
                proc = subprocess.run(
                    cmd,
                    check=True,
                    text=True,
                    capture_output=True,
                    timeout=360,
                )
                data = parse_json_object(proc.stdout)
                result = {int(item["index"]): clean_text(str(item["id"])) for item in data["translations"]}
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(8 * attempt)
        expected = {cue.index for cue in chunk}
        if set(result) != expected:
            raise ValueError(f"Translation index mismatch: expected {len(expected)} got {len(result)}")
        translated.update(result)
        cache_path.write_text(json.dumps(translated, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"translated {len(translated)}/{len(cues)} cues via Mantis Antigravity", flush=True)

    return translated


def split_by_weights(text: str, weights: list[int]) -> list[str]:
    words = clean_text(text).split()
    if not words or len(weights) <= 1:
        return [clean_text(text)]
    total = sum(max(1, weight) for weight in weights)
    chunks: list[str] = []
    cursor = 0
    for i, weight in enumerate(weights):
        if i == len(weights) - 1:
            end = len(words)
        else:
            consumed = sum(max(1, w) for w in weights[: i + 1])
            end = round(len(words) * consumed / total)
            end = max(cursor + 1, min(end, len(words) - (len(weights) - i - 1)))
        chunks.append(" ".join(words[cursor:end]).strip())
        cursor = end
    return chunks


def align_indonesian(en_cues: list[Cue], id_source: Path, shift_ms: int) -> tuple[dict[int, str], dict[str, object]]:
    raw_id = read_srt(id_source)
    id_cues = [cue for cue in raw_id if not AD_RE.search(cue.text)]
    assigned: dict[int, list[str]] = {cue.index: [] for cue in en_cues}
    consumed = 0

    # WHY: A human Indonesian SRT often uses different cue boundaries. Distribute
    # each Indonesian cue across the English cues it overlaps so the final timing
    # still follows the English base.
    for id_cue in id_cues:
        shifted_start = id_cue.start_ms + shift_ms
        shifted_end = id_cue.end_ms + shift_ms
        id_duration = max(1, shifted_end - shifted_start)
        overlaps: list[tuple[Cue, int]] = []
        for en_cue in en_cues:
            overlap = max(0, min(en_cue.end_ms, shifted_end) - max(en_cue.start_ms, shifted_start))
            if overlap > 0:
                overlaps.append((en_cue, overlap))
        if not overlaps:
            continue
        # WHY: A tiny boundary-touch overlap (an ID cue ending right as the next EN
        # cue starts) used to leak a word of the Indonesian line into the wrong EN
        # cue, so the Indonesian no longer matched the English stacked above it.
        # Keep only overlaps that are a real share of the ID cue (>=30%) or >=250ms;
        # if none qualify, attach the whole ID cue to its single best-overlap EN cue.
        # This removes cross-cue word bleed while still splitting genuine multi-cue spans.
        strong = [(c, o) for c, o in overlaps if o >= 0.30 * id_duration or o >= 250]
        overlaps = strong or [max(overlaps, key=lambda co: co[1])]
        consumed += 1
        for (en_cue, _), part in zip(overlaps, split_by_weights(id_cue.text, [o for _, o in overlaps])):
            if part:
                assigned[en_cue.index].append(part)

    missing = [cue for cue in en_cues if not assigned[cue.index]]
    for en_cue in missing:
        nearest = min(
            id_cues,
            key=lambda cue: abs(((cue.start_ms + shift_ms + cue.end_ms + shift_ms) // 2) - ((en_cue.start_ms + en_cue.end_ms) // 2)),
        )
        assigned[en_cue.index].append(nearest.text)

    return (
        {index: clean_text(" ".join(parts)) for index, parts in assigned.items()},
        {
            "indonesian_source": str(id_source),
            "indonesian_shift_ms": shift_ms,
            "indonesian_source_cues": len(id_cues),
            "indonesian_cues_consumed_by_overlap": consumed,
            "missing_indonesian_before_nearest_fallback": len(missing),
            "fallback_english_indexes": [cue.index for cue in missing[:50]],
        },
    )


def validate_dual(path: Path) -> dict[str, object]:
    cues = []
    raw = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n").strip()
    for block in re.split(r"\n\s*\n", raw):
        lines = [line.rstrip() for line in block.splitlines() if line.strip()]
        ts_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if ts_index is None:
            continue
        match = TIMESTAMP_RE.search(lines[ts_index])
        if not match:
            continue
        cues.append((parse_time(match.groups()[:4]), parse_time(match.groups()[4:]), lines[ts_index + 1 :]))
    if not cues:
        raise ValueError(f"No cues parsed from {path}")

    overlaps = sum(1 for a, b in zip(cues, cues[1:]) if a[1] > b[0])
    max_lines = max(len(body) for _, _, body in cues)
    too_many = [i + 1 for i, (_, _, body) in enumerate(cues) if len(body) > 4]
    single_language = [i + 1 for i, (_, _, body) in enumerate(cues) if len(body) < 2]
    return {
        "cue_count": len(cues),
        "first_start": fmt_time(cues[0][0]),
        "last_end": fmt_time(cues[-1][1]),
        "max_visual_lines_per_cue": max_lines,
        "overlap_count": overlaps,
        "too_many_line_cues": too_many[:50],
        "single_language_or_blank_cues": single_language[:50],
        "ok": overlaps == 0 and not too_many and not single_language and max_lines <= 4,
    }


def validation_report_for_movie(movie: Path, dual: Path) -> dict[str, object]:
    report = validate_dual(dual)
    for suffix in (".dual.default.srt", ".srt"):
        candidate = sidecar_path(movie, suffix)
        if candidate.exists():
            report[f"{suffix}_byte_match"] = sha256(dual) == sha256(candidate)
    return report


def shift_label(shift_ms: int) -> str:
    direction = "plus" if shift_ms >= 0 else "minus"
    return f"{direction}{abs(shift_ms)}ms-timing-shift"


def shift_srt_text(text: str, shift_ms: int) -> tuple[str, dict[str, object]]:
    starts: list[int] = []
    ends: list[int] = []

    def replace(match: re.Match[str]) -> str:
        start = parse_time(match.groups()[:4])
        end = parse_time(match.groups()[4:])
        shifted_start = max(0, start + shift_ms)
        shifted_end = max(shifted_start + 1, end + shift_ms)
        starts.append(shifted_start)
        ends.append(shifted_end)
        return f"{fmt_time(shifted_start)} --> {fmt_time(shifted_end)}"

    shifted, count = TIMESTAMP_RE.subn(replace, text)
    return shifted, {
        "cue_count": count,
        "first_start": fmt_time(starts[0]) if starts else None,
        "last_end": fmt_time(ends[-1]) if ends else None,
    }


def existing_unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def ffprobe_json(movie: Path) -> dict[str, object]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-hide_banner",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(movie),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(proc.stdout)


def float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def stream_tags_with_sync_hints(stream: dict[str, object]) -> dict[str, object]:
    tags = stream.get("tags")
    if not isinstance(tags, dict):
        return {}
    return {
        str(key): value
        for key, value in tags.items()
        if re.search(r"(?i)(delay|sync|offset)", str(key)) or re.search(r"(?i)(delay|sync|offset)", str(value))
    }


def stream_duration(stream: dict[str, object]) -> object:
    tags = stream.get("tags")
    return stream.get("duration") or (tags.get("DURATION") if isinstance(tags, dict) else None)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command_build(args: argparse.Namespace) -> int:
    movie = args.movie.resolve()
    if not movie.exists():
        raise FileNotFoundError(movie)

    en_cues = split_oversized_english(read_srt(args.english_source), args.english_width)
    if args.translate == "gemini":
        id_texts = translate_with_gemini(en_cues, sidecar_path(movie, ".id.translation-cache.json"), args.model, args.chunk_size)
        alignment_report: dict[str, object] = {"translation_model": args.model}
    elif args.translate == "gemini-cli":
        id_texts = translate_with_gemini_cli(
            en_cues,
            sidecar_path(movie, ".id.translation-cache.json"),
            args.model,
            args.chunk_size,
        )
        alignment_report = {"translation_model": args.model, "translation_transport": "gemini-cli"}
    elif args.translate == "mantis-antigravity":
        id_texts = translate_with_mantis_antigravity(
            en_cues,
            sidecar_path(movie, ".id.translation-cache.json"),
            args.model,
            args.chunk_size,
        )
        alignment_report = {
            "translation_model": args.model,
            "translation_transport": "mantis-antigravity",
        }
    elif args.indonesian_source:
        id_texts, alignment_report = align_indonesian(en_cues, args.indonesian_source, args.shift_ms)
    else:
        raise ValueError("Use --translate gemini or provide --indonesian-source")

    en_cues, id_texts = split_bilingual_overflows(en_cues, id_texts, args.english_width, args.indonesian_width)
    en_cues = clamp_overlaps(en_cues)

    en_out = sidecar_path(movie, ".en.srt")
    id_out = sidecar_path(movie, ".id.srt")
    dual_out = sidecar_path(movie, ".dual.srt")
    default_out = sidecar_path(movie, ".dual.default.srt")
    plain_out = sidecar_path(movie, ".srt")
    report_out = sidecar_path(movie, ".dual.verify.json")

    overwrite_paths = [en_out, id_out, dual_out, report_out]
    if args.make_default:
        overwrite_paths.append(default_out)
    if args.make_plain:
        overwrite_paths.append(plain_out)
    backup_dir = backup(overwrite_paths, args.label)

    write_srt(en_out, en_cues, {cue.index: cue.text for cue in en_cues}, args.english_width)
    write_srt(id_out, en_cues, id_texts, args.indonesian_width)
    write_dual(dual_out, en_cues, id_texts, args.english_width, args.indonesian_width)
    if args.make_default:
        shutil.copy2(dual_out, default_out)
    if args.make_plain:
        shutil.copy2(dual_out, plain_out)

    report = validate_dual(dual_out)
    report.update(alignment_report)
    report.update(
        {
            "movie": str(movie),
            "english_source": str(args.english_source),
            "english_output": str(en_out),
            "indonesian_output": str(id_out),
            "dual_output": str(dual_out),
            "default_output": str(default_out) if args.make_default else None,
            "plain_output": str(plain_out) if args.make_plain else None,
            "backup_dir": str(backup_dir) if backup_dir else None,
            "layout_rule": "single SRT cue: up to 2 English lines followed by up to 2 Indonesian lines",
        }
    )
    report_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


def command_validate(args: argparse.Namespace) -> int:
    movie = args.movie.resolve()
    dual = args.srt or sidecar_path(movie, ".dual.srt")
    report = validation_report_for_movie(movie, dual)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


def command_probe_av(args: argparse.Namespace) -> int:
    movie = args.movie.resolve()
    if not movie.exists():
        raise FileNotFoundError(movie)
    data = ffprobe_json(movie)
    streams = data.get("streams", [])
    if not isinstance(streams, list):
        streams = []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)

    video_start_s = float_or_none(video.get("start_time")) if isinstance(video, dict) else None
    audio_start_s = float_or_none(audio.get("start_time")) if isinstance(audio, dict) else None
    delta_ms = None
    if video_start_s is not None and audio_start_s is not None:
        delta_ms = round((audio_start_s - video_start_s) * 1000)

    stream_summaries = []
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        stream_summaries.append(
            {
                "index": stream.get("index"),
                "codec_type": stream.get("codec_type"),
                "codec_name": stream.get("codec_name"),
                "start_time": stream.get("start_time"),
                "duration": stream_duration(stream),
                "sync_hint_tags": stream_tags_with_sync_hints(stream),
            }
        )

    # WHY: Subtitle timing complaints can actually be A/V mux problems. Report
    # container start-time evidence first so agents do not blindly shift SRTs
    # when the media stream timing itself is suspect.
    result = {
        "movie": str(movie),
        "format_duration": data.get("format", {}).get("duration") if isinstance(data.get("format"), dict) else None,
        "primary_video_stream_index": video.get("index") if isinstance(video, dict) else None,
        "primary_audio_stream_index": audio.get("index") if isinstance(audio, dict) else None,
        "video_start_ms": round(video_start_s * 1000) if video_start_s is not None else None,
        "audio_start_ms": round(audio_start_s * 1000) if audio_start_s is not None else None,
        "audio_minus_video_ms": delta_ms,
        "audio_video_stream_start_warning_threshold_ms": args.warn_ms,
        "stream_start_delta_is_large": abs(delta_ms) > args.warn_ms if delta_ms is not None else None,
        "interpretation": "negative audio_minus_video_ms means audio packets start before video; positive means audio starts after video",
        "limit": "ffprobe stream starts and tags can catch mux offsets, but they do not prove human lip sync by themselves",
        "streams": stream_summaries,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def command_shift(args: argparse.Namespace) -> int:
    movie = args.movie.resolve()
    if not movie.exists():
        raise FileNotFoundError(movie)
    suffixes = args.suffixes or DEFAULT_SHIFT_SUFFIXES
    candidate_paths = [sidecar_path(movie, suffix) for suffix in suffixes]
    candidate_paths.extend(args.srt or [])
    srt_paths = existing_unique_paths(candidate_paths)
    if not srt_paths:
        raise FileNotFoundError("No existing SRT files matched the requested movie/suffixes")

    prepared: list[tuple[Path, str, dict[str, object]]] = []
    for path in srt_paths:
        shifted, stats = shift_srt_text(read_text_lenient(path), args.shift_ms)
        if not stats["cue_count"]:
            raise ValueError(f"No timestamp cues found in {path}")
        prepared.append((path, shifted, stats))

    label = args.label or shift_label(args.shift_ms)
    report_path = sidecar_path(movie, ".dual.verify.json")
    backup_paths = srt_paths + ([report_path] if report_path.exists() else [])
    backup_dir = backup(backup_paths, label)

    for path, shifted, _ in prepared:
        path.write_text(shifted, encoding="utf-8")

    dual = sidecar_path(movie, ".dual.srt")
    validation = validation_report_for_movie(movie, dual) if dual.exists() and not args.no_validate else None
    timing_entry = {
        "applied_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "shift_ms": args.shift_ms,
        "meaning": "positive values delay subtitles; negative values make subtitles earlier",
        "label": label,
        "backup_dir": str(backup_dir) if backup_dir else None,
        "shifted_files": [
            {
                "path": str(path),
                **stats,
            }
            for path, _, stats in prepared
        ],
    }

    # WHY: Timing repairs are easy to lose track of when multiple sidecars exist.
    # Record the exact shift beside the validation report so the movie folder
    # itself explains why every active SRT moved together.
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            report = {"previous_verify_json_unparseable": str(report_path)}
        adjustments = report.setdefault("timing_adjustments", [])
        if isinstance(adjustments, list):
            adjustments.append(timing_entry)
        else:
            report["timing_adjustments"] = [timing_entry]
        if validation is not None:
            report["post_shift_validation"] = validation
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {
        "movie": str(movie),
        "shift_ms": args.shift_ms,
        "meaning": "positive values delay subtitles; negative values make subtitles earlier",
        "backup_dir": str(backup_dir) if backup_dir else None,
        "shifted_files": timing_entry["shifted_files"],
        "validation": validation,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if validation is None or validation.get("ok") else 2


def command_proof(args: argparse.Namespace) -> int:
    movie = args.movie.resolve()
    srt = args.srt or sidecar_path(movie, ".srt")
    if not srt.exists():
        srt = sidecar_path(movie, ".dual.srt")
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    raw = srt.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n").strip()
    candidates: list[tuple[int, int, int]] = []
    for block in re.split(r"\n\s*\n", raw):
        lines = [line.rstrip() for line in block.splitlines() if line.strip()]
        ts_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if ts_index is None:
            continue
        match = TIMESTAMP_RE.search(lines[ts_index])
        if not match:
            continue
        body_count = len(lines[ts_index + 1 :])
        start = parse_time(match.groups()[:4])
        end = parse_time(match.groups()[4:])
        candidates.append((body_count, start, end))
    candidates.sort(key=lambda item: (-item[0], item[1]))

    rendered = []
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_srt = Path(tmpdir) / "proof.srt"
        shutil.copy2(srt, tmp_srt)
        for i, (line_count, start, end) in enumerate(candidates[: args.count], 1):
            at = start + max(100, min(700, (end - start) // 2))
            out = out_dir / f"{movie.stem}_proof_{i}_{fmt_ffmpeg_time(at).replace(':', '-')}.png"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(movie),
                    "-ss",
                    fmt_ffmpeg_time(at),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"subtitles={tmp_srt}",
                    str(out),
                ],
                check=True,
            )
            rendered.append({"path": str(out), "time": fmt_ffmpeg_time(at), "line_count": line_count})
    print(json.dumps({"rendered": rendered}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build")
    build.add_argument("--movie", required=True, type=Path)
    build.add_argument("--english-source", required=True, type=Path)
    build.add_argument("--indonesian-source", type=Path)
    build.add_argument("--shift-ms", type=int, default=0)
    build.add_argument("--translate", choices=["gemini", "gemini-cli", "mantis-antigravity"])
    build.add_argument("--model", default="gemini-3.1-flash-lite")
    build.add_argument("--chunk-size", type=int, default=250)
    build.add_argument("--english-width", type=int, default=42)
    build.add_argument("--indonesian-width", type=int, default=40)
    build.add_argument("--label", default="dual-subtitle-rebuild")
    build.add_argument("--make-default", action="store_true")
    build.add_argument("--make-plain", action="store_true")
    build.set_defaults(func=command_build)

    validate = sub.add_parser("validate")
    validate.add_argument("--movie", required=True, type=Path)
    validate.add_argument("--srt", type=Path)
    validate.set_defaults(func=command_validate)

    probe_av = sub.add_parser("probe-av")
    probe_av.add_argument("--movie", required=True, type=Path)
    probe_av.add_argument("--warn-ms", type=int, default=250)
    probe_av.set_defaults(func=command_probe_av)

    shift = sub.add_parser("shift")
    shift.add_argument("--movie", required=True, type=Path)
    shift.add_argument("--shift-ms", required=True, type=int)
    shift.add_argument(
        "--suffix",
        dest="suffixes",
        action="append",
        help="Sidecar suffix to shift. Repeatable. Defaults to .en.srt, .id.srt, .dual.srt, .dual.default.srt, and .srt.",
    )
    shift.add_argument("--srt", action="append", type=Path, help="Additional explicit SRT path to shift. Repeatable.")
    shift.add_argument("--label", help="Backup label. Defaults to plus/minusNms-timing-shift.")
    shift.add_argument("--no-validate", action="store_true", help="Skip post-shift validation of the movie .dual.srt.")
    shift.set_defaults(func=command_shift)

    proof = sub.add_parser("proof")
    proof.add_argument("--movie", required=True, type=Path)
    proof.add_argument("--srt", type=Path)
    proof.add_argument("--out-dir", required=True, type=Path)
    proof.add_argument("--count", type=int, default=3)
    proof.set_defaults(func=command_proof)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
