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
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


TIMESTAMP_RE = re.compile(
    r"(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})"
)
TAG_RE = re.compile(r"<[^>]+>|\{\\[^}]+\}")
AD_RE = re.compile(
    r"(?i)(www\.|https?://|\.com\b|subtitle|subscene|opensubtitles|"
    r"translated by|subtitles by|penerjemah|diterjemahkan oleh|resync|"
    r"sync(?:hronize)? by|visit us|instagram|facebook|idfl|sebuah-dongeng|"
    r"iklan|streaming|casino|member of|created by|movie2shared|"
    r"ganool\.com|alih bahasa:)"
)
SOURCE_AD_RE = re.compile(
    r"(?i)(www\.|https?://|\.com\b|subscene|opensubtitles|visit us|"
    r"instagram|facebook|idfl|sebuah-dongeng|iklan|streaming|casino|"
    r"member of|created by|movie2shared|ganool\.com|alih bahasa:)"
)
INDEX_PLACEHOLDER_RE = re.compile(
    r"(?i)^\s*(?:cue|caption|subtitle|translation|line|item|index|"
    r"terjemahan|teks|baris)\s*[:#-]?\s*\d+\s*[.!]?\s*$"
)
NULL_PLACEHOLDER_RE = re.compile(r"(?i)^\s*(?:null|none|nil|undefined|n/?a)\s*[.!]?\s*$")
SOURCE_CREDIT_RE = re.compile(
    r"(?i)\b(?:subtitles?|subtitled|translated|synced|synchronized|timed)\s+by\b"
)
CJK_NUMERAL_CHARS = frozenset(
    "零〇一二三四五六七八九十百千万萬亿億兆两兩壹贰貳叁參肆伍陆陸柒捌玖拾佰仟"
)
TRANSLATION_CACHE_VERSION = 5
TRANSLATION_PROMPT_VERSION = "dual-translation-v5"
SEMANTIC_REVIEW_VERSION = "dual-semantic-review-v1"
VALIDATOR_VERSION = "dual-bundle-v5"
DEFAULT_GEMINI_MODEL_POOL = "gemini-pro-latest,gemini-flash-latest,gemini-flash-lite-latest"
LANGUAGE_TAG_RE = re.compile(
    r"^(?:x(?:-[A-Za-z0-9]{1,8})+|[A-Za-z]{2,8}(?:-[A-Za-z0-9]{2,8})*)$",
    re.IGNORECASE,
)


@dataclass
class Cue:
    index: int
    start_ms: int
    end_ms: int
    text: str


def parse_time(parts: tuple[str, str, str, str]) -> int:
    h, m, s, ms = map(int, parts)
    if m > 59 or s > 59 or ms > 999:
        raise ValueError(
            f"Invalid SRT timestamp component: hours={h} minutes={m} seconds={s} milliseconds={ms}"
        )
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


def proof_frame_command(movie: Path, srt: Path | None, output: Path, at_ms: int) -> list[str]:
    # WHY: input-side seeking avoids decoding the entire movie for every proof
    # frame. -copyts keeps the seeked video's original timeline so libass still
    # selects the SRT cue at at_ms instead of starting subtitles from 00:00.
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-copyts",
        "-ss",
        fmt_ffmpeg_time(at_ms),
        "-i",
        str(movie),
        "-an",
        "-sn",
        "-frames:v",
        "1",
    ]
    if srt is not None:
        command.extend(["-vf", f"subtitles={srt}"])
    command.append(str(output))
    return command


def proof_sample_time(start_ms: int, end_ms: int) -> int:
    if end_ms <= start_ms:
        raise ValueError("Proof cue end must be after its start")
    duration = end_ms - start_ms
    # WHY: even a one-millisecond SRT cue must be sampled inside [start, end).
    # A fixed 100 ms offset can render after short cues and falsely prove a frame
    # containing no external subtitle.
    return start_ms + min(duration // 2, duration - 1)


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


def has_alpha(text: str) -> bool:
    return any(char.isalpha() for char in clean_text(text))


def alpha_tokens(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", clean_text(text), flags=re.UNICODE)


def normalize_language_tag(tag: str) -> str:
    value = tag.strip()
    if not LANGUAGE_TAG_RE.fullmatch(value):
        raise ValueError(
            f"Invalid language tag {tag!r}; use a common tag such as en, id, fr, ja, ar, zh-Hant, or x-private"
        )
    return value.lower()


def looks_numeric_placeholder(text: str) -> bool:
    cleaned = clean_text(text)
    if cleaned and any(char in CJK_NUMERAL_CHARS for char in cleaned):
        # WHY: common Han numerals are alphabetic Unicode characters, so a
        # generic numeric-category check misses number-only outputs such as 十二.
        if all(
            char.isspace()
            or char in CJK_NUMERAL_CHARS
            or unicodedata.category(char)[0] in {"P", "S", "Z"}
            for char in cleaned
        ):
            return True
    has_numeric = any(unicodedata.category(char).startswith("N") for char in cleaned)
    if not cleaned or not has_numeric or has_alpha(cleaned):
        return False
    # WHY: \d plus ASCII punctuation misses Arabic and other Unicode numeric
    # punctuation. A placeholder may contain only numbers, separators, symbols,
    # and whitespace, regardless of script.
    return all(
        char.isspace()
        or unicodedata.category(char).startswith("N")
        or unicodedata.category(char)[0] in {"N", "P", "S", "Z"}
        for char in cleaned
    )


def looks_like_copied_dialogue(source_text: str, translated_text: str) -> bool:
    source_cleaned = clean_text(source_text)
    translated_cleaned = clean_text(translated_text)
    if translated_cleaned.casefold() != source_cleaned.casefold():
        return False
    words = alpha_tokens(source_cleaned)
    letters = sum(char.isalpha() for char in source_cleaned)
    # WHY: copied-dialogue detection must work for Cyrillic, Arabic, CJK, and
    # scripts without spaces while still allowing short names, codes, and titles.
    return len(words) >= 5 or letters >= 30


def looks_suspiciously_short_translation(source_text: str, translated_text: str) -> bool:
    source_words = alpha_tokens(source_text)
    target_words = alpha_tokens(translated_text)
    target_cleaned = clean_text(translated_text)
    target_letters = sum(char.isalpha() for char in target_cleaned)
    # Letter-other scripts include Chinese, Japanese, Thai, Arabic, and many
    # other scripts where a short no-space translation can carry full meaning.
    # Leave those to semantic review instead of applying a Latin word-count
    # heuristic as a hard cross-language failure.
    if any(unicodedata.category(char) == "Lo" for char in target_cleaned if char.isalpha()):
        return False
    return len(source_words) >= 8 and len(target_words) <= 1 and target_letters <= 8


def semantic_review_original_source(source_text: str) -> str:
    try:
        record = json.loads(source_text)
    except (json.JSONDecodeError, TypeError):
        return source_text
    if not isinstance(record, dict):
        return source_text
    required = {
        "source",
        "current_translation",
        "context_before",
        "context_after",
    }
    if not required.issubset(record):
        return source_text
    original = record.get("source")
    current = record.get("current_translation")
    before = record.get("context_before")
    after = record.get("context_after")
    if (
        not isinstance(original, str)
        or not isinstance(current, str)
        or not isinstance(before, list)
        or not isinstance(after, list)
    ):
        return source_text
    return clean_text(original)


def translated_text_issue(cue_index: int, source_text: str, translated_text: str) -> str | None:
    cleaned = clean_text(translated_text)
    validation_source = semantic_review_original_source(source_text)
    if not cleaned:
        return "blank"
    if "-->" in cleaned or TIMESTAMP_RE.search(cleaned):
        return "timestamp_or_srt_metadata"
    if INDEX_PLACEHOLDER_RE.fullmatch(cleaned):
        return "labeled_cue_index_placeholder"
    if NULL_PLACEHOLDER_RE.fullmatch(cleaned) and not NULL_PLACEHOLDER_RE.fullmatch(
        clean_text(validation_source)
    ):
        return "null_placeholder"
    if looks_numeric_placeholder(cleaned) and not looks_numeric_placeholder(validation_source):
        digits = re.sub(r"\D", "", cleaned)
        if digits == str(cue_index):
            return "matches_cue_index"
        return "numeric_placeholder"
    if has_alpha(validation_source) and looks_like_copied_dialogue(validation_source, cleaned):
        return "copied_source_text"
    if looks_suspiciously_short_translation(validation_source, cleaned):
        return "suspiciously_short_translation"
    return None


def translation_quality_issues(cues: list[Cue], translated_texts: dict[int, str]) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    for cue in cues:
        if cue.index not in translated_texts:
            issues.append({"index": cue.index, "reason": "missing", "source": cue.text, "translation": ""})
            continue
        raw_translation = translated_texts[cue.index]
        translation = clean_text(raw_translation) if isinstance(raw_translation, str) else ""
        issue = translated_text_issue(cue.index, cue.text, translation)
        if issue:
            issues.append({"index": cue.index, "reason": issue, "source": cue.text, "translation": translation})
    return issues


def source_hygiene_issues(cues: list[Cue]) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    for cue in cues:
        text = clean_text(cue.text)
        if SOURCE_CREDIT_RE.search(text):
            reason = "subtitle_production_credit"
        elif SOURCE_AD_RE.search(text):
            reason = "advertisement_or_external_link"
        else:
            continue
        issues.append({"index": cue.index, "reason": reason, "text": text})
    return issues


def source_credit_issues(cues: list[Cue]) -> list[dict[str, object]]:
    """Backward-compatible alias for the broader source hygiene guard."""
    return source_hygiene_issues(cues)


def assert_valid_translated_texts(cues: list[Cue], translated_texts: dict[int, str], context: str) -> None:
    issues = translation_quality_issues(cues, translated_texts)
    if issues:
        preview = json.dumps(issues[:20], ensure_ascii=False)
        raise ValueError(f"{context} contains invalid translated subtitle text: {preview}")


def translated_item_text(item: dict[str, object]) -> str:
    # WHY: two-letter language fields such as "id" can be interpreted as an
    # identifier and filled with cue numbers. Prefer generic "translation" for
    # every target language, while still accepting newer Indonesian-specific and
    # old cache-compatible provider responses.
    value = item.get("translation", item.get("indonesian", item.get("id", "")))
    if not isinstance(value, str):
        return ""
    return clean_text(value)


def parse_provider_translations(
    data: object,
    expected_indexes: set[int],
) -> dict[int, str]:
    if not isinstance(data, dict):
        raise ValueError("Translation response must be a JSON object")
    items = data.get("translations")
    if not isinstance(items, list):
        raise ValueError("Translation response must contain a translations array")
    if len(items) != len(expected_indexes):
        raise ValueError(
            "Translation item count mismatch: "
            f"expected {len(expected_indexes)} got {len(items)}"
        )

    translated: dict[int, str] = {}
    for position, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Translation item {position} must be a JSON object")
        raw_index = item.get("index")
        # WHY: converting indexes and then building a dictionary used to let
        # duplicate provider entries overwrite one another silently. Validate
        # the raw structured response before accepting any cue text.
        if type(raw_index) is not int:
            raise ValueError(f"Translation item {position} index must be an integer")
        if raw_index in translated:
            raise ValueError(f"Duplicate translation index: {raw_index}")
        translated[raw_index] = translated_item_text(item)

    actual_indexes = set(translated)
    if actual_indexes != expected_indexes:
        missing = sorted(expected_indexes - actual_indexes)
        unexpected = sorted(actual_indexes - expected_indexes)
        raise ValueError(
            "Translation index mismatch: "
            f"missing={missing[:20]} unexpected={unexpected[:20]}"
        )
    return translated


def cue_source_hash(cues: list[Cue]) -> str:
    payload = [
        {
            "index": cue.index,
            "start_ms": cue.start_ms,
            "end_ms": cue.end_ms,
            "text": clean_text(cue.text),
        }
        for cue in cues
    ]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def translation_cache_context(
    source_language: str = "en",
    target_language: str = "id",
    transport: str = "unspecified",
    model: str = "unspecified",
    prompt_sha256: str = "unspecified",
) -> dict[str, str]:
    return {
        "source_language": normalize_language_tag(source_language),
        "target_language": normalize_language_tag(target_language),
        "transport": transport.strip() or "unspecified",
        # WHY: aliases such as *-latest and provider auto-routing are selectors,
        # not immutable proof of the exact model that served every request.
        "model_selector": model.strip() or "unspecified",
        "prompt_version": TRANSLATION_PROMPT_VERSION,
        # WHY: version labels are human-maintained and can be missed during a
        # prompt-only correction. Bind resumable output to the exact rendered
        # prompt contract so changed review rules always force provider review.
        "prompt_sha256": prompt_sha256.strip() or "unspecified",
        "validator_version": VALIDATOR_VERSION,
    }


def read_translation_cache(
    cache_path: Path,
    cues: list[Cue],
    source_language: str = "en",
    target_language: str = "id",
    transport: str = "unspecified",
    model: str = "unspecified",
    prompt_sha256: str = "unspecified",
) -> dict[int, str]:
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    except json.JSONDecodeError:
        print(f"discarded corrupt translated subtitle cache from {cache_path}", flush=True)
        return {}
    expected_hash = cue_source_hash(cues)
    expected_context = translation_cache_context(
        source_language,
        target_language,
        transport,
        model,
        prompt_sha256,
    )
    if isinstance(cached, dict) and isinstance(cached.get("translations"), dict):
        if cached.get("source_sha256") != expected_hash:
            # WHY: timing/source changes can keep the same cue indexes while the
            # text moved. Discard stale caches so future builds cannot pair a new
            # English cue list with old Indonesian lines.
            print(f"discarded stale translated subtitle cache from {cache_path}: source hash mismatch", flush=True)
            return {}
        version = cached.get("version")
        if version != TRANSLATION_CACHE_VERSION:
            print(
                f"discarded translated subtitle cache from {cache_path}: "
                f"cache version {version!r} != {TRANSLATION_CACHE_VERSION}",
                flush=True,
            )
            return {}
        context_mismatch = any(
            cached.get(key) != value
            for key, value in expected_context.items()
            if key != "model_selector"
        )
        cached_selectors = {
            item.strip()
            for item in str(cached.get("model_selector", "")).split(",")
            if item.strip()
        }
        requested_selectors = {
            item.strip()
            for item in expected_context["model_selector"].split(",")
            if item.strip()
        }
        # WHY: a rate-limited model may require expanding the requested fallback
        # pool. Reuse already validated chunks only when every cached selector is
        # still present; switching to an unrelated or narrower selector remains
        # a cache miss.
        selector_compatible = bool(cached_selectors) and cached_selectors.issubset(
            requested_selectors
        )
        if context_mismatch or not selector_compatible:
            print(
                f"discarded translated subtitle cache from {cache_path}: language/provider context mismatch",
                flush=True,
            )
            return {}
        cached_translations = cached["translations"]
    elif cached:
        print(f"discarded legacy translated subtitle cache from {cache_path}: missing source hash", flush=True)
        return {}
    else:
        cached_translations = {}

    cues_by_index = {cue.index: cue for cue in cues}
    translated: dict[int, str] = {}
    dropped: list[dict[str, object]] = []

    for key, value in cached_translations.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        cue = cues_by_index.get(index)
        if cue is None:
            continue
        cleaned = clean_text(value) if isinstance(value, str) else ""
        issue = translated_text_issue(index, cue.text, cleaned)
        if issue:
            dropped.append({"index": index, "reason": issue, "source": cue.text, "translation": cleaned})
            continue
        translated[index] = cleaned

    if dropped:
        # WHY: provider/cache output has previously returned cue numbers as the
        # translated language. Treat cache as untrusted so bad values are
        # retranslated instead of being written into fresh dual subtitle sidecars.
        print(
            f"discarded {len(dropped)} invalid cached translated subtitles from {cache_path}: "
            + json.dumps(dropped[:10], ensure_ascii=False),
            flush=True,
        )
    return translated


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_copy(source: Path, destination: Path) -> None:
    atomic_write_bytes(destination, source.read_bytes())


def write_translation_cache(
    cache_path: Path,
    cues: list[Cue],
    translated: dict[int, str],
    source_language: str = "en",
    target_language: str = "id",
    transport: str = "unspecified",
    model: str = "unspecified",
    prompt_sha256: str = "unspecified",
) -> None:
    selector = model.strip() or "unspecified"
    selector_history = {selector}
    if cache_path.exists():
        try:
            previous = json.loads(cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}
        if (
            isinstance(previous, dict)
            and previous.get("version") == TRANSLATION_CACHE_VERSION
            and previous.get("source_sha256") == cue_source_hash(cues)
        ):
            previous_selector = previous.get("model_selector")
            if isinstance(previous_selector, str) and previous_selector.strip():
                selector_history.add(previous_selector.strip())
            previous_history = previous.get("model_selector_history")
            if isinstance(previous_history, list):
                selector_history.update(
                    item.strip()
                    for item in previous_history
                    if isinstance(item, str) and item.strip()
                )
    payload = {
        "version": TRANSLATION_CACHE_VERSION,
        "source_sha256": cue_source_hash(cues),
        **translation_cache_context(
            source_language,
            target_language,
            transport,
            model,
            prompt_sha256,
        ),
        "model_selector_history": sorted(selector_history),
        "translations": {str(index): text for index, text in sorted(translated.items())},
    }
    atomic_write_text(
        cache_path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def sidecar_path(movie: Path, suffix: str) -> Path:
    stem = movie.name[: -len(movie.suffix)] if movie.suffix else movie.name
    if not stem or stem in {".", ".."}:
        raise ValueError(f"Cannot derive a safe sidecar name from {movie}")
    return movie.with_name(stem + suffix)


def parse_srt_blocks(path: Path) -> list[tuple[int, int, int, list[str]]]:
    decoded = read_text_lenient(path)
    normalized = decoded.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError(f"Empty SRT source: {path}")

    parsed: list[tuple[int, int, int, list[str]]] = []
    previous_start = -1
    for block_number, block in enumerate(re.split(r"\n\s*\n", normalized), 1):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3:
            raise ValueError(f"Malformed SRT block {block_number} in {path}: expected index, timestamp, and text")
        if not lines[0].isdigit():
            raise ValueError(f"Malformed SRT block {block_number} in {path}: cue index is not an integer")
        source_index = int(lines[0])
        if source_index != block_number:
            raise ValueError(
                f"Malformed SRT block {block_number} in {path}: cue index {source_index} is not sequential"
            )
        match = TIMESTAMP_RE.fullmatch(lines[1])
        if not match:
            raise ValueError(f"Malformed SRT block {block_number} in {path}: invalid timestamp line {lines[1]!r}")
        start_ms = parse_time(match.groups()[:4])
        end_ms = parse_time(match.groups()[4:])
        if end_ms <= start_ms:
            raise ValueError(
                f"Malformed SRT block {block_number} in {path}: end time must be after start time"
            )
        if start_ms < previous_start:
            raise ValueError(
                f"Malformed SRT block {block_number} in {path}: cue starts are not monotonic"
            )
        body = [clean_text(line) for line in lines[2:] if clean_text(line)]
        if not body:
            raise ValueError(f"Malformed SRT block {block_number} in {path}: empty subtitle text")
        parsed.append((source_index, start_ms, end_ms, body))
        previous_start = start_ms
    return parsed


def read_srt(path: Path) -> list[Cue]:
    return [
        Cue(index=index, start_ms=start_ms, end_ms=end_ms, text=clean_text(" ".join(body)))
        for index, start_ms, end_ms, body in parse_srt_blocks(path)
    ]


def character_display_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def display_width(text: str) -> int:
    return sum(character_display_width(char) for char in text)


def split_token_by_display_width(token: str, width: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_width = 0
    for char in token:
        char_width = character_display_width(char)
        if current and current_width + char_width > width:
            chunks.append("".join(current))
            current = [char]
            current_width = char_width
        else:
            current.append(char)
            current_width += char_width
    if current:
        chunks.append("".join(current))
    return chunks or [token]


def wrap_lines(text: str, width: int) -> list[str]:
    text = clean_text(text)
    if width < 1:
        raise ValueError("Subtitle wrap width must be positive")
    tokens: list[str] = []
    for word in text.split():
        tokens.extend(split_token_by_display_width(word, width))
    if not tokens and text:
        tokens = split_token_by_display_width(text, width)

    lines: list[str] = []
    current = ""
    for token in tokens:
        candidate = token if not current else f"{current} {token}"
        if current and display_width(candidate) > width:
            lines.append(current)
            current = token
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [text]


def language_line_limit_issues(
    cues: list[Cue],
    texts: dict[int, str],
    width: int,
) -> list[dict[str, object]]:
    return [
        {
            "index": cue.index,
            "line_count": len(wrap_lines(texts[cue.index], width)),
            "text": clean_text(texts[cue.index]),
        }
        for cue in cues
        if len(wrap_lines(texts[cue.index], width)) > 2
    ]


def assert_two_line_language_limit(
    cues: list[Cue],
    texts: dict[int, str],
    width: int,
    language_label: str,
) -> None:
    oversized = language_line_limit_issues(cues, texts, width)
    if oversized:
        raise ValueError(
            f"{language_label} cues exceed the two-line bilingual layout. "
            "Do not invent proportional timestamps; shorten/retranslate the text "
            "or provide speech-aligned source cue splits: "
            + json.dumps(oversized[:20], ensure_ascii=False)
        )


def overlap_report(cues: list[Cue]) -> list[dict[str, object]]:
    ordered = sorted(cues, key=lambda c: (c.start_ms, c.end_ms))
    overlaps: list[dict[str, object]] = []
    for a, b in zip(ordered, ordered[1:]):
        if a.end_ms > b.start_ms:
            overlaps.append(
                {
                    "cue_index": a.index,
                    "next_cue_index": b.index,
                    "cue_end": fmt_time(a.end_ms),
                    "next_start": fmt_time(b.start_ms),
                    "overlap_ms": a.end_ms - b.start_ms,
                }
            )
    return overlaps


def write_srt(path: Path, cues: list[Cue], texts: dict[int, str], width: int) -> None:
    lines: list[str] = []
    for cue in cues:
        lines.append(str(cue.index))
        lines.append(f"{fmt_time(cue.start_ms)} --> {fmt_time(cue.end_ms)}")
        lines.extend(wrap_lines(texts[cue.index], width))
        lines.append("")
    atomic_write_text(path, "\n".join(lines))


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
    atomic_write_text(path, "\n".join(lines))


def backup(paths: list[Path], label: str) -> Path | None:
    existing = [path for path in paths if path.exists()]
    if not existing:
        return None
    safe_label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip(".-") or "backup"
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    root = (
        existing[0].parent
        / f"{existing[0].name}.backups"
        / f"{timestamp}-{time.time_ns() % 1_000_000_000:09d}-{safe_label}"
    )
    root.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, object]] = []
    for path in existing:
        path_key = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:10]
        destination = root / f"{path.name}.{path_key}.before-{safe_label}"
        shutil.copy2(path, destination)
        original_hash = sha256(path)
        backup_hash = sha256(destination)
        if original_hash != backup_hash:
            raise IOError(f"Backup verification failed for {path}")
        manifest.append(
            {
                "original_path": str(path),
                "backup_path": str(destination),
                "sha256": original_hash,
                "size": path.stat().st_size,
            }
        )
    atomic_write_text(
        root / "manifest.json",
        json.dumps({"files": manifest}, ensure_ascii=False, indent=2) + "\n",
    )
    return root


def translation_prompt(
    cues: list[Cue],
    source_language: str,
    target_language: str,
    additional_instruction: str | None = None,
    current_translations: dict[int, str] | None = None,
    neighboring_sources: dict[int, dict[str, str]] | None = None,
) -> str:
    payload = [
        {
            "index": cue.index,
            "source": cue.text,
            **(
                neighboring_sources.get(cue.index, {})
                if neighboring_sources
                else {}
            ),
            **(
                {"current_translation": current_translations[cue.index]}
                if current_translations and cue.index in current_translations
                else {}
            ),
        }
        for cue in cues
    ]
    instruction = additional_instruction.strip() if additional_instruction else ""
    if current_translations:
        task = (
            f"Review each current_translation against its {source_language} source "
            f"and return a natural {target_language} movie subtitle, correcting it "
            "only when needed while preserving the complete meaning."
        )
    else:
        task = (
            f"Translate these movie subtitle cues from language tag {source_language} "
            f"to natural {target_language}."
        )
    return (
        (f"Critical constraints: {instruction}\n\n" if instruction else "")
        + task
        + " Preserve complete meaning, speaker turns, names, "
        "numbers, quoted code words, title cards, movie-specific terms, and technical "
        "language. A source transcript can contain dialogue in a third language; "
        "translate that dialogue into the requested target language too instead of "
        "copying it unchanged. Treat previous_source and next_source as context only; "
        "never copy their dialogue into the current cue. Preserve sentence continuation, "
        "capitalization, punctuation, idioms, wordplay, and speaker register across cue "
        "boundaries. Do not repeat a conjunction or phrase already supplied by an adjacent "
        "cue. Keep each cue concise but complete. Return JSON only with "
        "translations[].index and translations[].translation. The translation field "
        "must contain translated subtitle text in the target language, never a cue "
        "number, label, timestamp, source-language paraphrase, comment, or markdown. "
        "Do not omit dialogue.\n\n"
        + json.dumps({"source_language": source_language, "target_language": target_language, "cues": payload}, ensure_ascii=False)
    )


def neighboring_source_context(cues: list[Cue]) -> dict[int, dict[str, str]]:
    context: dict[int, dict[str, str]] = {}
    for position, cue in enumerate(cues):
        item: dict[str, str] = {}
        if position:
            item["previous_source"] = cues[position - 1].text
        if position + 1 < len(cues):
            item["next_source"] = cues[position + 1].text
        context[cue.index] = item
    return context


def translation_prompt_sha256(
    cues: list[Cue],
    source_language: str,
    target_language: str,
    additional_instruction: str | None = None,
    current_translations: dict[int, str] | None = None,
    include_neighboring_sources: bool = True,
) -> str:
    neighboring_sources = (
        neighboring_source_context(cues) if include_neighboring_sources else None
    )
    # Render one complete deterministic job prompt. Actual provider requests can
    # be chunked, but every static instruction and every job input appears here,
    # so a prompt-only edit or review-rule change invalidates the whole cache.
    rendered = translation_prompt(
        cues,
        source_language,
        target_language,
        additional_instruction,
        current_translations,
        neighboring_sources,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def translation_retry_prompt(
    base_prompt: str,
    issues: list[dict[str, object]],
) -> str:
    return (
        base_prompt
        + "\n\nYour previous response failed deterministic subtitle validation. "
        "Return the complete JSON response again, correcting every listed cue. "
        "Do not copy foreign-language dialogue unchanged, return cue numbers, or "
        "collapse a full sentence into a token fragment. Validation failures:\n"
        + json.dumps(issues[:50], ensure_ascii=False)
    )


def require_gemini_stop_finish(response: object) -> None:
    candidates = getattr(response, "candidates", None)
    if not isinstance(candidates, (list, tuple)) or not candidates:
        raise ValueError("Gemini response has no candidate finish reason")
    for candidate in candidates:
        reason = getattr(candidate, "finish_reason", None)
        name = getattr(reason, "name", None)
        normalized = str(name or reason or "").rsplit(".", 1)[-1].upper()
        if normalized != "STOP":
            raise ValueError(
                f"Gemini response finish reason must be STOP, got {normalized or 'missing'}"
            )


def translate_with_gemini(
    cues: list[Cue],
    cache_path: Path,
    model: str,
    chunk_size: int,
    source_language: str = "en",
    target_language: str = "id",
    additional_instruction: str | None = None,
    current_translations: dict[int, str] | None = None,
    include_neighboring_sources: bool = True,
) -> dict[int, str]:
    try:
        from google import genai
        from google.genai import types
    except Exception as exc:
        raise RuntimeError("Install google-genai and set GEMINI_API_KEY for --translate gemini") from exc

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")

    prompt_sha256 = translation_prompt_sha256(
        cues,
        source_language,
        target_language,
        additional_instruction,
        current_translations,
        include_neighboring_sources,
    )
    translated = read_translation_cache(
        cache_path,
        cues,
        source_language,
        target_language,
        "gemini-api",
        model,
        prompt_sha256,
    )
    missing = [cue for cue in cues if cue.index not in translated]
    if not missing:
        return translated

    source_context = neighboring_source_context(cues) if include_neighboring_sources else None
    client = genai.Client(api_key=api_key)
    models = [item.strip() for item in model.split(",") if item.strip()] or [model]
    schema = {
        "type": "object",
        "properties": {
            "translations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"index": {"type": "integer"}, "translation": {"type": "string"}},
                    "required": ["index", "translation"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["translations"],
        "additionalProperties": False,
    }

    for offset in range(0, len(missing), chunk_size):
        chunk = missing[offset : offset + chunk_size]
        chunk_schema = json.loads(json.dumps(schema))
        chunk_schema["properties"]["translations"]["minItems"] = len(chunk)
        chunk_schema["properties"]["translations"]["maxItems"] = len(chunk)
        prompt = translation_prompt(
            chunk,
            source_language,
            target_language,
            additional_instruction,
            current_translations,
            source_context,
        )
        request_prompt = prompt
        expected = {cue.index for cue in chunk}
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
                        contents=request_prompt,
                        config=types.GenerateContentConfig(
                            temperature=0.1,
                            responseMimeType="application/json",
                            responseSchema=chunk_schema,
                        ),
                    )
                    require_gemini_stop_finish(response)
                    data = json.loads(response.text)
                    candidate = parse_provider_translations(data, expected)
                    issues = translation_quality_issues(chunk, candidate)
                    if issues:
                        request_prompt = translation_retry_prompt(prompt, issues)
                        raise ValueError(
                            f"Gemini translation chunk starting at cue {chunk[0].index} "
                            "contains invalid translated subtitle text: "
                            + json.dumps(issues[:20], ensure_ascii=False)
                        )
                    result = candidate
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
        translated.update(result)
        write_translation_cache(
            cache_path,
            cues,
            translated,
            source_language,
            target_language,
            "gemini-api",
            model,
            prompt_sha256,
        )
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


def translate_with_gemini_cli(
    cues: list[Cue],
    cache_path: Path,
    model: str,
    chunk_size: int,
    source_language: str = "en",
    target_language: str = "id",
    additional_instruction: str | None = None,
    current_translations: dict[int, str] | None = None,
    include_neighboring_sources: bool = True,
) -> dict[int, str]:
    prompt_sha256 = translation_prompt_sha256(
        cues,
        source_language,
        target_language,
        additional_instruction,
        current_translations,
        include_neighboring_sources,
    )
    translated = read_translation_cache(
        cache_path,
        cues,
        source_language,
        target_language,
        "gemini-cli",
        model,
        prompt_sha256,
    )
    missing = [cue for cue in cues if cue.index not in translated]
    if not missing:
        return translated

    if not shutil.which("gemini"):
        raise RuntimeError("gemini CLI is not available for --translate gemini-cli")

    source_context = neighboring_source_context(cues) if include_neighboring_sources else None
    for offset in range(0, len(missing), chunk_size):
        chunk = missing[offset : offset + chunk_size]
        prompt = translation_prompt(
            chunk,
            source_language,
            target_language,
            additional_instruction,
            current_translations,
            source_context,
        )
        request_prompt = prompt

        for attempt in range(1, 4):
            try:
                cmd = ["gemini", "-p", request_prompt, "--output-format", "text"]
                if model and model.lower() not in {"default", "gemini-cli-default"}:
                    cmd[1:1] = ["-m", model]
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
                expected = {cue.index for cue in chunk}
                candidate = parse_provider_translations(data, expected)
                issues = translation_quality_issues(chunk, candidate)
                if issues:
                    request_prompt = translation_retry_prompt(prompt, issues)
                    raise ValueError(
                        f"Gemini CLI translation chunk starting at cue {chunk[0].index} "
                        "contains invalid translated subtitle text: "
                        + json.dumps(issues[:20], ensure_ascii=False)
                    )
                result = candidate
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(5 * attempt)
        translated.update(result)
        write_translation_cache(
            cache_path,
            cues,
            translated,
            source_language,
            target_language,
            "gemini-cli",
            model,
            prompt_sha256,
        )
        print(f"translated {len(translated)}/{len(cues)} cues via gemini CLI", flush=True)

    return translated


def translate_with_mantis_antigravity(
    cues: list[Cue],
    cache_path: Path,
    model: str,
    chunk_size: int,
    source_language: str = "en",
    target_language: str = "id",
    additional_instruction: str | None = None,
    current_translations: dict[int, str] | None = None,
    include_neighboring_sources: bool = True,
) -> dict[int, str]:
    prompt_sha256 = translation_prompt_sha256(
        cues,
        source_language,
        target_language,
        additional_instruction,
        current_translations,
        include_neighboring_sources,
    )
    translated = read_translation_cache(
        cache_path,
        cues,
        source_language,
        target_language,
        "mantis-antigravity",
        model,
        prompt_sha256,
    )
    missing = [cue for cue in cues if cue.index not in translated]
    if not missing:
        return translated

    if not shutil.which("mantis"):
        raise RuntimeError("mantis CLI is not available for --translate mantis-antigravity")

    source_context = neighboring_source_context(cues) if include_neighboring_sources else None
    for offset in range(0, len(missing), chunk_size):
        chunk = missing[offset : offset + chunk_size]
        prompt = translation_prompt(
            chunk,
            source_language,
            target_language,
            additional_instruction,
            current_translations,
            source_context,
        )
        request_prompt = prompt

        for attempt in range(1, 4):
            try:
                cmd = ["mantis", "antigravity"]
                if model and model.lower() != "auto":
                    cmd.extend(["--model", model])
                cmd.extend(["--print-timeout", "5m", "--print", request_prompt])
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
                expected = {cue.index for cue in chunk}
                candidate = parse_provider_translations(data, expected)
                issues = translation_quality_issues(chunk, candidate)
                if issues:
                    request_prompt = translation_retry_prompt(prompt, issues)
                    raise ValueError(
                        f"Mantis Antigravity translation chunk starting at cue {chunk[0].index} "
                        "contains invalid translated subtitle text: "
                        + json.dumps(issues[:20], ensure_ascii=False)
                    )
                result = candidate
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(8 * attempt)
        translated.update(result)
        write_translation_cache(
            cache_path,
            cues,
            translated,
            source_language,
            target_language,
            "mantis-antigravity",
            model,
            prompt_sha256,
        )
        print(f"translated {len(translated)}/{len(cues)} cues via Mantis Antigravity", flush=True)

    return translated


def semantic_review_inputs(
    cues: list[Cue],
    target_texts: dict[int, str],
    context_radius: int = 2,
    target_width: int = 40,
) -> list[Cue]:
    assert_valid_translated_texts(cues, target_texts, "Semantic-review draft")
    reviewed_inputs: list[Cue] = []
    for position, cue in enumerate(cues):
        before = cues[max(0, position - context_radius) : position]
        after = cues[position + 1 : position + context_radius + 1]
        record = {
            "review_version": SEMANTIC_REVIEW_VERSION,
            "target_width": target_width,
            "source": cue.text,
            "current_translation": target_texts[cue.index],
            "context_before": [
                {
                    "source": item.text,
                    "translation": target_texts[item.index],
                }
                for item in before
            ],
            "context_after": [
                {
                    "source": item.text,
                    "translation": target_texts[item.index],
                }
                for item in after
            ],
        }
        # WHY: the semantic-review cache source hash must change whenever any
        # draft or neighboring context changes. Encoding the complete review
        # record as the synthetic source binds resumable reviewed chunks to the
        # exact first-pass translation instead of trusting a stale "reviewed"
        # boolean.
        reviewed_inputs.append(
            Cue(
                cue.index,
                cue.start_ms,
                cue.end_ms,
                json.dumps(record, ensure_ascii=False, separators=(",", ":")),
            )
        )
    return reviewed_inputs


def semantic_review_translations(
    cues: list[Cue],
    target_texts: dict[int, str],
    cache_path: Path,
    transport: str,
    model: str,
    chunk_size: int,
    source_language: str,
    target_language: str,
    target_width: int,
) -> dict[int, str]:
    translators: dict[str, Callable[..., dict[int, str]]] = {
        "gemini": translate_with_gemini,
        "gemini-cli": translate_with_gemini_cli,
        "mantis-antigravity": translate_with_mantis_antigravity,
    }
    review_cues = semantic_review_inputs(
        cues,
        target_texts,
        target_width=target_width,
    )
    preferred_total_width = max(target_width, target_width * 2 - 6)
    instruction = (
        f"This is the mandatory {SEMANTIC_REVIEW_VERSION} second pass before any "
        "subtitle is installed. Each source field is a JSON review record. Translate "
        "only record.source; record.current_translation is the draft to approve or "
        "correct, while context_before and context_after are read-only continuity "
        "context and must never be copied into the current cue. Return a complete "
        f"natural {target_language} movie subtitle for every cue. Correct semantic "
        "omissions, literal calques, duplicated connectors, broken cross-cue grammar, "
        "idioms, wordplay, speaker register, capitalization, and punctuation. Preserve "
        "names, numbers, technical terms, title cards, and speaker intent. Do not "
        "rubber-stamp an awkward draft and do not explain edits. If the current "
        "translation is already correct and natural, return it unchanged rather than "
        "paraphrasing it for preference. "
        f"Every result must wrap into at most two lines of {target_width} Unicode "
        f"display columns; prefer no more than {preferred_total_width} total columns."
    )
    translator = translators[transport]
    reviewed = translator(
        review_cues,
        cache_path,
        model,
        chunk_size,
        source_language,
        target_language,
        instruction,
        target_texts,
        False,
    )
    assert_valid_translated_texts(
        cues,
        reviewed,
        "Mandatory semantic-reviewed subtitle text",
    )
    assert_two_line_language_limit(
        cues,
        reviewed,
        target_width,
        target_language,
    )
    return reviewed


def retranslate_overlong_cues(
    cues: list[Cue],
    target_texts: dict[int, str],
    cache_path: Path,
    transport: str,
    model: str,
    chunk_size: int,
    source_language: str,
    target_language: str,
    target_width: int,
    temporary_parent: Path,
) -> list[int]:
    translators: dict[str, Callable[..., dict[int, str]]] = {
        "gemini": translate_with_gemini,
        "gemini-cli": translate_with_gemini_cli,
        "mantis-antigravity": translate_with_mantis_antigravity,
    }
    translator = translators[transport]
    cache_transport = {
        "gemini": "gemini-api",
        "gemini-cli": "gemini-cli",
        "mantis-antigravity": "mantis-antigravity",
    }[transport]
    cues_by_index = {cue.index: cue for cue in cues}
    repaired: set[int] = set()
    first_pass_prompt_sha256 = translation_prompt_sha256(
        cues,
        source_language,
        target_language,
    )

    for repair_round in range(1, 3):
        issues = language_line_limit_issues(cues, target_texts, target_width)
        if not issues:
            assert_valid_translated_texts(
                cues,
                target_texts,
                "Layout-repaired translated subtitle text",
            )
            write_translation_cache(
                cache_path,
                cues,
                target_texts,
                source_language,
                target_language,
                cache_transport,
                model,
                first_pass_prompt_sha256,
            )
            return sorted(repaired)
        indexes = [int(issue["index"]) for issue in issues]
        repair_cues = [cues_by_index[index] for index in indexes]
        current_drafts = {index: target_texts[index] for index in indexes}
        repaired.update(indexes)
        preferred_total_width = max(target_width, target_width * 2 - 6)
        instruction = (
            f"For every cue, rewrite the supplied current_translation so word "
            f"wrapping produces at most two lines of {target_width} Unicode display "
            f"columns. Prefer no more than {preferred_total_width} total display "
            "columns. Do not return the unchanged overlong draft. Preserve meaning, "
            "names, numbers, and speaker intent. Return plain cue text without "
            "manual line-break markup."
        )
        with tempfile.TemporaryDirectory(
            prefix=f".dual-layout-repair-{repair_round}-",
            dir=str(temporary_parent),
        ) as temporary_dir:
            repair_cache = Path(temporary_dir) / "translation-cache.json"
            replacements = translator(
                repair_cues,
                repair_cache,
                model,
                max(1, min(chunk_size, len(repair_cues))),
                source_language,
                target_language,
                instruction,
                current_drafts,
            )
        target_texts.update(replacements)

    if not language_line_limit_issues(cues, target_texts, target_width):
        assert_valid_translated_texts(
            cues,
            target_texts,
            "Layout-repaired translated subtitle text",
        )
        write_translation_cache(
            cache_path,
            cues,
            target_texts,
            source_language,
            target_language,
            cache_transport,
            model,
            first_pass_prompt_sha256,
        )
    return sorted(repaired)


def align_target(
    source_cues: list[Cue],
    target_source: Path,
    shift_ms: int,
    source_language: str = "en",
    target_language: str = "id",
) -> tuple[dict[int, str], dict[str, object]]:
    raw_target = read_srt(target_source)
    target_cues = [cue for cue in raw_target if not AD_RE.search(cue.text)]
    if not target_cues:
        raise ValueError(f"Target subtitle source has no usable cues after filtering: {target_source}")
    assigned: dict[int, list[str]] = {cue.index: [] for cue in source_cues}
    consumed = 0
    unconsumed: list[dict[str, object]] = []

    # WHY: dividing target words proportionally across source cues can corrupt
    # sentence meaning while still passing structural validation. Accept only
    # an unambiguous target-to-source overlap and preserve the target cue text
    # whole; otherwise require cue-by-cue translation from the trusted source.
    for target_cue in target_cues:
        shifted_start = target_cue.start_ms + shift_ms
        shifted_end = target_cue.end_ms + shift_ms
        target_duration = max(1, shifted_end - shifted_start)
        overlaps: list[tuple[Cue, int]] = []
        for source_cue in source_cues:
            overlap = max(0, min(source_cue.end_ms, shifted_end) - max(source_cue.start_ms, shifted_start))
            if overlap > 0:
                overlaps.append((source_cue, overlap))
        if not overlaps:
            unconsumed.append(
                {
                    "index": target_cue.index,
                    "reason": "no_source_overlap",
                    "start": fmt_time(shifted_start),
                    "end": fmt_time(shifted_end),
                    "text": target_cue.text,
                }
            )
            continue
        # A fixed millisecond threshold accepts a tiny fraction of long cues.
        # Require at least 30% of the target cue instead.
        strong = [(cue, overlap) for cue, overlap in overlaps if overlap >= 0.30 * target_duration]
        if not strong:
            unconsumed.append(
                {
                    "index": target_cue.index,
                    "reason": "insufficient_source_overlap",
                    "maximum_overlap_ms": max(overlap for _, overlap in overlaps),
                    "target_duration_ms": target_duration,
                    "start": fmt_time(shifted_start),
                    "end": fmt_time(shifted_end),
                    "text": target_cue.text,
                }
            )
            continue
        if len(strong) != 1:
            unconsumed.append(
                {
                    "index": target_cue.index,
                    "reason": "ambiguous_multiple_source_overlaps",
                    "source_cue_indexes": [cue.index for cue, _ in strong],
                    "overlap_ms": [overlap for _, overlap in strong],
                    "target_duration_ms": target_duration,
                    "start": fmt_time(shifted_start),
                    "end": fmt_time(shifted_end),
                    "text": target_cue.text,
                }
            )
            continue
        source_cue, _ = strong[0]
        consumed += 1
        assigned[source_cue.index].append(clean_text(target_cue.text))

    if unconsumed:
        raise ValueError(
            "Target subtitle has cues without meaningful source overlap; "
            "refusing to drop or nearest-match target dialogue: "
            + json.dumps(unconsumed[:20], ensure_ascii=False)
        )

    missing = [cue for cue in source_cues if not assigned[cue.index]]
    if missing:
        # WHY: nearest-cue fallback can silently pair unrelated dialogue from a
        # different release. Missing overlap is a release/timing failure.
        raise ValueError(
            "Target subtitle does not cover every source cue; refusing nearest-dialogue fallback: "
            + json.dumps(
                [
                    {
                        "index": cue.index,
                        "start": fmt_time(cue.start_ms),
                        "end": fmt_time(cue.end_ms),
                        "text": cue.text,
                    }
                    for cue in missing[:20]
                ],
                ensure_ascii=False,
            )
        )

    return (
        {index: clean_text(" ".join(parts)) for index, parts in assigned.items()},
        {
            "target_source": str(target_source),
            "source_language": normalize_language_tag(source_language),
            "target_language": normalize_language_tag(target_language),
            "target_shift_ms": shift_ms,
            "target_source_cues": len(target_cues),
            "target_cues_consumed_by_overlap": consumed,
            "unconsumed_target_cues": 0,
            "missing_target_cues": 0,
        },
    )


def align_indonesian(en_cues: list[Cue], id_source: Path, shift_ms: int) -> tuple[dict[int, str], dict[str, object]]:
    """Backward-compatible English-to-Indonesian wrapper."""
    return align_target(en_cues, id_source, shift_ms, "en", "id")


def validate_dual(path: Path) -> dict[str, object]:
    blocks = parse_srt_blocks(path)
    cues = [(start_ms, end_ms, body) for _, start_ms, end_ms, body in blocks]

    overlaps = sum(1 for a, b in zip(cues, cues[1:]) if a[1] > b[0])
    max_lines = max(len(body) for _, _, body in cues)
    too_many = [i + 1 for i, (_, _, body) in enumerate(cues) if len(body) > 4]
    single_language = [i + 1 for i, (_, _, body) in enumerate(cues) if len(body) < 2]
    numeric_translation: list[int] = []
    for i, (_, _, body) in enumerate(cues):
        if len(body) < 2:
            continue
        # WHY: a three-line bilingual block can mean EN1+ID2 or EN2+ID1.
        # Flag a numeric lower-language value only when every valid 2+2 layout
        # interpretation makes it a translation, while preserving genuinely
        # numeric English dialogue such as a character counting aloud.
        split_points = [1] if len(body) == 2 else ([1, 2] if len(body) == 3 else [2])
        invalid_for_every_split = all(
            looks_numeric_placeholder(" ".join(body[split:]))
            and not looks_numeric_placeholder(" ".join(body[:split]))
            for split in split_points
        )
        if invalid_for_every_split:
            numeric_translation.append(i + 1)
    return {
        "cue_count": len(cues),
        "first_start": fmt_time(cues[0][0]),
        "last_end": fmt_time(cues[-1][1]),
        "max_visual_lines_per_cue": max_lines,
        "overlap_count": overlaps,
        "too_many_line_cues": too_many[:50],
        "single_language_or_blank_cues": single_language[:50],
        "numeric_translation_line_count": len(numeric_translation),
        "numeric_translation_line_cues": numeric_translation[:50],
        "numeric_indonesian_line_count": len(numeric_translation),
        "numeric_indonesian_line_cues": numeric_translation[:50],
        "validation_scope": "combined_srt_structure_only",
        "bundle_language_and_composition_verified": False,
        "ok": overlaps == 0 and not too_many and not single_language and not numeric_translation and max_lines <= 4,
    }


def language_sidecar_suffix(language_tag: str) -> str:
    return f".{normalize_language_tag(language_tag)}.srt"


def validate_bundle(
    dual: Path,
    source: Path,
    target: Path,
    default: Path | None = None,
    plain: Path | None = None,
    require_aliases: bool = True,
) -> dict[str, object]:
    report = validate_dual(dual)
    source_blocks = parse_srt_blocks(source)
    target_blocks = parse_srt_blocks(target)
    dual_blocks = parse_srt_blocks(dual)

    count_match = len(source_blocks) == len(target_blocks) == len(dual_blocks)
    timing_mismatches: list[int] = []
    composition_mismatches: list[int] = []
    source_too_many_lines: list[int] = []
    target_too_many_lines: list[int] = []

    for position, triplet in enumerate(zip(source_blocks, target_blocks, dual_blocks), 1):
        source_block, target_block, dual_block = triplet
        _, source_start, source_end, source_body = source_block
        _, target_start, target_end, target_body = target_block
        _, dual_start, dual_end, dual_body = dual_block
        if (source_start, source_end) != (target_start, target_end) or (
            source_start,
            source_end,
        ) != (dual_start, dual_end):
            timing_mismatches.append(position)
        if dual_body != source_body + target_body:
            composition_mismatches.append(position)
        if len(source_body) > 2:
            source_too_many_lines.append(position)
        if len(target_body) > 2:
            target_too_many_lines.append(position)

    source_cues = read_srt(source)
    target_cues = read_srt(target)
    target_texts = {cue.index: cue.text for cue in target_cues}
    translation_issues = translation_quality_issues(source_cues, target_texts)
    source_issues = source_hygiene_issues(source_cues)

    alias_failures: list[str] = []
    aliases = [(".dual.default.srt", default), (".srt", plain)]
    for label, candidate in aliases:
        if candidate is None or not candidate.exists():
            report[f"{label}_byte_match"] = False
            if require_aliases:
                alias_failures.append(label)
            continue
        byte_match = sha256(dual) == sha256(candidate)
        report[f"{label}_byte_match"] = byte_match
        if not byte_match:
            alias_failures.append(label)

    report.update(
        {
            "source_target_dual_cue_count_match": count_match,
            "source_cue_count": len(source_blocks),
            "target_cue_count": len(target_blocks),
            "timing_mismatch_count": len(timing_mismatches),
            "timing_mismatch_cues": timing_mismatches[:50],
            "composition_mismatch_count": len(composition_mismatches),
            "composition_mismatch_cues": composition_mismatches[:50],
            "source_more_than_two_lines_count": len(source_too_many_lines),
            "source_more_than_two_lines_cues": source_too_many_lines[:50],
            "target_more_than_two_lines_count": len(target_too_many_lines),
            "target_more_than_two_lines_cues": target_too_many_lines[:50],
            "translation_sidecar_issue_count": len(translation_issues),
            "translation_sidecar_issues": translation_issues[:50],
            "source_hygiene_issue_count": len(source_issues),
            "source_hygiene_issues": source_issues[:50],
            "autoload_aliases_required": require_aliases,
            "autoload_byte_match_failures": alias_failures,
        }
    )
    report["ok"] = bool(report["ok"]) and all(
        (
            count_match,
            not timing_mismatches,
            not composition_mismatches,
            not source_too_many_lines,
            not target_too_many_lines,
            not translation_issues,
            not source_issues,
            not alias_failures,
        )
    )
    report["bundle_language_and_composition_verified"] = bool(report["ok"])
    return report


def validation_report_for_movie(
    movie: Path,
    dual: Path,
    source_language: str = "en",
    target_language: str = "id",
    sidecar_base: Path | None = None,
    require_aliases: bool = True,
) -> dict[str, object]:
    base = sidecar_base or movie
    source_path = sidecar_path(base, language_sidecar_suffix(source_language))
    target_path = sidecar_path(base, language_sidecar_suffix(target_language))
    default_path = sidecar_path(base, ".dual.default.srt")
    plain_path = sidecar_path(base, ".srt")
    missing = [
        str(path)
        for path in (source_path, target_path, dual)
        if not path.exists()
    ]
    if missing:
        return {
            "ok": False,
            "bundle_language_and_composition_verified": False,
            "missing_required_sidecars": missing,
        }

    report = validate_bundle(
        dual,
        source_path,
        target_path,
        default_path,
        plain_path,
        require_aliases=require_aliases,
    )
    report["source_language"] = normalize_language_tag(source_language)
    report["target_language"] = normalize_language_tag(target_language)
    embedded_subtitles = subtitle_stream_summaries(movie)
    report["embedded_subtitle_stream_count"] = len(embedded_subtitles)
    report["embedded_subtitle_streams"] = embedded_subtitles
    report["embedded_subtitle_stack_warning"] = (
        "External SRT is one combined track. Disable embedded/internal subtitle tracks in the player, "
        "or remux/remove forced embedded tracks, to prevent visual stacking."
        if embedded_subtitles
        else None
    )
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


def subtitle_stream_summaries(movie: Path) -> list[dict[str, object]]:
    data = ffprobe_json(movie)
    streams = data.get("streams", [])
    if not isinstance(streams, list):
        return []
    summaries: list[dict[str, object]] = []
    for stream in streams:
        if not isinstance(stream, dict) or stream.get("codec_type") != "subtitle":
            continue
        tags = stream.get("tags")
        tags = tags if isinstance(tags, dict) else {}
        summaries.append(
            {
                "index": stream.get("index"),
                "codec_name": stream.get("codec_name"),
                "language": tags.get("language"),
                "title": tags.get("title"),
                "start_time": stream.get("start_time"),
                "duration": stream_duration(stream),
                "disposition": stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {},
            }
        )
    return summaries


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    # WHY: movie proof inputs can be many gigabytes. Incremental hashing keeps
    # memory bounded instead of loading the complete media file into RAM.
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def sidecar_output_paths(base_movie: Path, source_language: str, target_language: str) -> dict[str, Path]:
    return {
        "source": sidecar_path(base_movie, language_sidecar_suffix(source_language)),
        "target": sidecar_path(base_movie, language_sidecar_suffix(target_language)),
        "cache": sidecar_path(
            base_movie,
            f".{normalize_language_tag(target_language)}.translation-cache.json",
        ),
        "semantic_cache": sidecar_path(
            base_movie,
            f".{normalize_language_tag(target_language)}.semantic-review-cache.json",
        ),
        "dual": sidecar_path(base_movie, ".dual.srt"),
        "default": sidecar_path(base_movie, ".dual.default.srt"),
        "plain": sidecar_path(base_movie, ".srt"),
        "report": sidecar_path(base_movie, ".dual.verify.json"),
    }


def install_staged_files(
    staged_to_destination: list[tuple[Path, Path]],
    validate_installed: Callable[[], dict[str, object]],
    remove_destinations: list[Path] | None = None,
    tracked_destinations: list[Path] | None = None,
    finalize_installed: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    removals = remove_destinations or []
    tracked = tracked_destinations or []
    destinations = [destination for _, destination in staged_to_destination] + removals + tracked
    if len(set(destinations)) != len(destinations):
        raise ValueError("Install destinations and removals must be unique")
    originals = {
        destination: destination.read_bytes() if destination.exists() else None
        for destination in destinations
    }
    try:
        for staged, destination in staged_to_destination:
            atomic_copy(staged, destination)
        for destination in removals:
            destination.unlink(missing_ok=True)
        validation = validate_installed()
        if not bool(validation.get("ok")):
            raise ValueError(
                "Installed subtitle bundle failed validation: "
                + json.dumps(validation, ensure_ascii=False)
            )
        if finalize_installed is not None:
            # WHY: the verify report is part of a handled install transaction.
            # If its final write fails, restore every active sidecar and the old
            # report instead of leaving new subtitles with a failed receipt.
            finalize_installed(validation)
        return validation
    except Exception as install_error:
        # WHY: a build must never leave a mixed or partial active subtitle
        # bundle after a handled copy/validation failure. Attempt every restore
        # even when one restore itself fails, then report the incomplete
        # rollback instead of silently abandoning later destinations.
        restore_errors: list[str] = []
        for destination, original in originals.items():
            try:
                if original is None:
                    destination.unlink(missing_ok=True)
                else:
                    atomic_write_bytes(destination, original)
            except Exception as restore_error:
                restore_errors.append(f"{destination}: {restore_error}")
        if restore_errors:
            raise RuntimeError(
                "Subtitle install failed and rollback was incomplete: "
                + json.dumps(restore_errors, ensure_ascii=False)
            ) from install_error
        raise


def command_build(args: argparse.Namespace) -> int:
    movie = absolute_without_resolving(args.movie)
    if not movie.exists() or not movie.is_file():
        raise FileNotFoundError(movie)
    source_language = normalize_language_tag(args.source_language)
    target_language = normalize_language_tag(args.target_language)
    if source_language == target_language:
        raise ValueError("Source and target language tags must be different")

    source_srt = absolute_without_resolving(args.source_srt)
    target_srt = absolute_without_resolving(args.target_srt) if args.target_srt else None
    raw_source_cues = read_srt(source_srt)
    source_issues = source_hygiene_issues(raw_source_cues)
    if source_issues:
        raise ValueError(
            "Source subtitle contains production credits, advertising, or external links; "
            f"clean the source before building: {json.dumps(source_issues[:20], ensure_ascii=False)}"
        )
    source_texts = {cue.index: cue.text for cue in raw_source_cues}
    assert_two_line_language_limit(
        raw_source_cues,
        source_texts,
        args.source_width,
        source_language,
    )
    source_overlaps = overlap_report(raw_source_cues)
    if source_overlaps:
        # WHY: trimming overlap boundaries is not audio alignment and previously
        # corrupted matched target text. Keep the trusted source timestamps exact
        # and require a release-matched, non-overlapping source instead.
        raise ValueError(
            "Source subtitle timing has overlapping cues; refusing to mutate or "
            "redistribute dialogue. Supply a release-matched non-overlapping source. "
            f"First overlaps: {json.dumps(source_overlaps[:10], ensure_ascii=False)}"
        )

    # Probe before any cache or active subtitle write. Missing/corrupt media must
    # not be discovered after live sidecars have already changed.
    embedded_subtitles = subtitle_stream_summaries(movie)

    output_dir = absolute_without_resolving(args.output_dir) if args.output_dir else movie.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    output_base = output_dir / movie.name
    outputs = sidecar_output_paths(output_base, source_language, target_language)
    if output_dir == movie.parent and any(path == movie for path in outputs.values()):
        raise ValueError(f"Movie path collides with a generated sidecar path: {movie}")
    overwrite_paths = [
        outputs["source"],
        outputs["target"],
        outputs["cache"],
        outputs["semantic_cache"],
        outputs["dual"],
        outputs["default"],
        outputs["plain"],
        outputs["report"],
    ]
    backup_dir = backup(overwrite_paths, args.label)

    if args.translate == "gemini":
        model = args.model or DEFAULT_GEMINI_MODEL_POOL
        target_texts = translate_with_gemini(
            raw_source_cues,
            outputs["cache"],
            model,
            args.chunk_size,
            source_language,
            target_language,
        )
        alignment_report: dict[str, object] = {
            "translation_model_selector": model,
            "translation_transport": "gemini-api",
        }
    elif args.translate == "gemini-cli":
        model = args.model or "default"
        target_texts = translate_with_gemini_cli(
            raw_source_cues,
            outputs["cache"],
            model,
            args.chunk_size,
            source_language,
            target_language,
        )
        alignment_report = {
            "translation_model_selector": model,
            "translation_transport": "gemini-cli",
        }
    elif args.translate == "mantis-antigravity":
        model = args.model or "auto"
        target_texts = translate_with_mantis_antigravity(
            raw_source_cues,
            outputs["cache"],
            model,
            args.chunk_size,
            source_language,
            target_language,
        )
        alignment_report = {
            "translation_model_selector": model,
            "translation_transport": "mantis-antigravity",
        }
    elif target_srt:
        target_texts, alignment_report = align_target(
            raw_source_cues,
            target_srt,
            args.shift_ms,
            source_language,
            target_language,
        )
    else:
        raise ValueError("Use --translate or provide --target-srt/--indonesian-source")

    assert_valid_translated_texts(raw_source_cues, target_texts, "Translated subtitle text before writing")
    if args.translate and language_line_limit_issues(
        raw_source_cues,
        target_texts,
        args.target_width,
    ):
        layout_retranslated = retranslate_overlong_cues(
            raw_source_cues,
            target_texts,
            outputs["cache"],
            args.translate,
            model,
            args.chunk_size,
            source_language,
            target_language,
            args.target_width,
            output_dir,
        )
        alignment_report["layout_retranslated_cue_count"] = len(layout_retranslated)
        alignment_report["layout_retranslated_cues"] = layout_retranslated
    assert_two_line_language_limit(
        raw_source_cues,
        target_texts,
        args.target_width,
        target_language,
    )
    if args.translate:
        target_texts = semantic_review_translations(
            raw_source_cues,
            target_texts,
            outputs["semantic_cache"],
            args.translate,
            model,
            args.chunk_size,
            source_language,
            target_language,
            args.target_width,
        )
        alignment_report.update(
            {
                "semantic_review_required": True,
                "semantic_review_complete": True,
                "semantic_review_version": SEMANTIC_REVIEW_VERSION,
                "semantic_reviewed_cue_count": len(target_texts),
                "semantic_review_target_width": args.target_width,
                "semantic_review_cache": str(outputs["semantic_cache"]),
            }
        )
    else:
        alignment_report.update(
            {
                "semantic_review_required": False,
                "semantic_review_complete": False,
            }
        )
    assert_two_line_language_limit(
        raw_source_cues,
        target_texts,
        args.target_width,
        target_language,
    )
    source_cues = raw_source_cues
    assert_valid_translated_texts(source_cues, target_texts, "Translated subtitle text before staging")

    with tempfile.TemporaryDirectory(
        prefix=f".{movie.stem}.dual-build-",
        dir=str(output_dir),
    ) as temporary_dir:
        staged_base = Path(temporary_dir) / movie.name
        staged = sidecar_output_paths(staged_base, source_language, target_language)
        write_srt(staged["source"], source_cues, {cue.index: cue.text for cue in source_cues}, args.source_width)
        write_srt(staged["target"], source_cues, target_texts, args.target_width)
        write_dual(staged["dual"], source_cues, target_texts, args.source_width, args.target_width)
        if args.make_default:
            atomic_copy(staged["dual"], staged["default"])
        if args.make_plain:
            atomic_copy(staged["dual"], staged["plain"])

        report = validate_bundle(
            staged["dual"],
            staged["source"],
            staged["target"],
            staged["default"] if args.make_default else None,
            staged["plain"] if args.make_plain else None,
            require_aliases=args.make_default and args.make_plain,
        )
        report.update(alignment_report)
        report.update(
            {
                "movie": str(movie),
                "output_directory": str(output_dir),
                "source_language": source_language,
                "target_language": target_language,
                "source_subtitle": str(source_srt),
                "source_output": str(outputs["source"]),
                "target_output": str(outputs["target"]),
                "translation_cache": str(outputs["cache"]) if args.translate else None,
                "semantic_review_cache": (
                    str(outputs["semantic_cache"]) if args.translate else None
                ),
                "translation_cache_is_resumable_work_state": bool(args.translate),
                "dual_output": str(outputs["dual"]),
                "default_output": str(outputs["default"]) if args.make_default else None,
                "plain_output": str(outputs["plain"]) if args.make_plain else None,
                "backup_dir": str(backup_dir) if backup_dir else None,
                "layout_rule": (
                    f"single SRT cue: up to 2 {source_language} lines followed by "
                    f"up to 2 {target_language} lines"
                ),
                "source_timing_modified": False,
                "timing_rule": (
                    "source cue timestamps preserved exactly; overlapping sources "
                    "and proportional text-based splits are rejected"
                ),
                "embedded_subtitle_streams": embedded_subtitles,
                "embedded_subtitle_stream_count": len(embedded_subtitles),
                "embedded_subtitle_stack_warning": (
                    "External SRT is one combined track. Disable embedded/internal subtitle tracks in the player; "
                    "SRT cannot disable forced, secondary, or burned-in subtitles."
                    if embedded_subtitles
                    else None
                ),
                "overlap_repairs": [],
                "validator_version": VALIDATOR_VERSION,
                "output_sha256": {
                    key: sha256(staged[key])
                    for key in ("source", "target", "dual")
                },
            }
        )
        if args.make_default:
            report["output_sha256"]["default"] = sha256(staged["default"])
        if args.make_plain:
            report["output_sha256"]["plain"] = sha256(staged["plain"])
        if not report["ok"]:
            raise ValueError("Staged subtitle bundle failed validation: " + json.dumps(report, ensure_ascii=False))

        install_pairs = [
            (staged["source"], outputs["source"]),
            (staged["target"], outputs["target"]),
            (staged["dual"], outputs["dual"]),
        ]
        if args.make_default:
            install_pairs.append((staged["default"], outputs["default"]))
        if args.make_plain:
            install_pairs.append((staged["plain"], outputs["plain"]))
        disabled_aliases = [
            alias
            for enabled, alias in (
                (args.make_default, outputs["default"]),
                (args.make_plain, outputs["plain"]),
            )
            if not enabled
        ]

        def finalize_build_report(final_validation: dict[str, object]) -> None:
            report["final_installed_validation"] = final_validation
            report["removed_disabled_aliases"] = [str(path) for path in disabled_aliases]
            atomic_write_text(
                outputs["report"],
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            )

        install_staged_files(
            install_pairs,
            lambda: validate_bundle(
                outputs["dual"],
                outputs["source"],
                outputs["target"],
                outputs["default"] if args.make_default else None,
                outputs["plain"] if args.make_plain else None,
                require_aliases=args.make_default and args.make_plain,
            ),
            remove_destinations=disabled_aliases,
            tracked_destinations=[outputs["report"]],
            finalize_installed=finalize_build_report,
        )

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


def command_validate(args: argparse.Namespace) -> int:
    movie = absolute_without_resolving(args.movie)
    source_language = normalize_language_tag(args.source_language)
    target_language = normalize_language_tag(args.target_language)
    sidecar_base = (
        absolute_without_resolving(args.sidecar_dir) / movie.name
        if args.sidecar_dir
        else movie
    )
    dual = absolute_without_resolving(args.srt) if args.srt else sidecar_path(sidecar_base, ".dual.srt")
    report = validation_report_for_movie(
        movie,
        dual,
        source_language,
        target_language,
        sidecar_base=sidecar_base,
        require_aliases=not args.no_require_aliases,
    )
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
    movie = absolute_without_resolving(args.movie)
    if not movie.exists():
        raise FileNotFoundError(movie)
    source_language = normalize_language_tag(args.source_language)
    target_language = normalize_language_tag(args.target_language)
    suffixes = args.suffixes or [
        language_sidecar_suffix(source_language),
        language_sidecar_suffix(target_language),
        ".dual.srt",
        ".dual.default.srt",
        ".srt",
    ]
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

    dual = sidecar_path(movie, ".dual.srt")
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

    if report_path.exists():
        try:
            loaded_report = json.loads(report_path.read_text(encoding="utf-8"))
            report = loaded_report if isinstance(loaded_report, dict) else {}
        except json.JSONDecodeError:
            report = {"previous_verify_json_unparseable": str(report_path)}
    else:
        report = {"movie": str(movie)}

    validation: dict[str, object] | None = None
    with tempfile.TemporaryDirectory(
        prefix=f".{movie.stem}.dual-shift-",
        dir=str(movie.parent),
    ) as temporary_dir:
        install_pairs: list[tuple[Path, Path]] = []
        for index, (path, shifted, _) in enumerate(prepared, 1):
            staged_path = Path(temporary_dir) / f"{index:03d}-{path.name}"
            atomic_write_text(staged_path, shifted)
            install_pairs.append((staged_path, path))

        def validate_shifted_bundle() -> dict[str, object]:
            nonlocal validation
            if dual.exists() and not args.no_validate:
                validation = validation_report_for_movie(
                    movie,
                    dual,
                    source_language,
                    target_language,
                    require_aliases=not args.no_require_aliases,
                )
                return validation
            validation = None
            return {"ok": True, "validation_skipped": True}

        def finalize_shift_report(installed_validation: dict[str, object]) -> None:
            # WHY: timing repairs and their receipt are one handled transaction.
            # A report-write failure must restore all shifted sidecars too.
            adjustments = report.setdefault("timing_adjustments", [])
            if isinstance(adjustments, list):
                adjustments.append(timing_entry)
            else:
                report["timing_adjustments"] = [timing_entry]
            if validation is not None:
                report["post_shift_validation"] = validation
            else:
                report["post_shift_validation"] = installed_validation
            atomic_write_text(
                report_path,
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            )

        install_staged_files(
            install_pairs,
            validate_shifted_bundle,
            tracked_destinations=[report_path],
            finalize_installed=finalize_shift_report,
        )

    # Timing repairs are easy to lose track of when multiple sidecars exist.
    # The transaction above records the exact shift beside the active bundle.

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


def select_proof_srt(movie: Path, explicit_srt: Path | None = None) -> Path:
    if explicit_srt is not None:
        selected = absolute_without_resolving(explicit_srt)
        if not selected.exists() or not selected.is_file():
            raise FileNotFoundError(selected)
        return selected

    dual = sidecar_path(movie, ".dual.srt")
    if not dual.exists() or not dual.is_file():
        raise FileNotFoundError(
            f"Validated combined sidecar is required for proof: {dual}. "
            "Use --srt only when intentionally proving another file."
        )
    mismatched_aliases = [
        str(alias)
        for alias in (sidecar_path(movie, ".dual.default.srt"), sidecar_path(movie, ".srt"))
        if alias.exists() and sha256(alias) != sha256(dual)
    ]
    if mismatched_aliases:
        raise ValueError(
            "Refusing ambiguous proof because active aliases do not byte-match "
            f"{dual}: {json.dumps(mismatched_aliases, ensure_ascii=False)}"
        )
    return dual


def select_proof_candidates(
    candidates: list[tuple[int, int, int]],
    count: int,
    require_four_line: bool,
) -> list[tuple[int, int, int]]:
    ordered = sorted(candidates, key=lambda item: item[1])
    if not ordered:
        raise ValueError("No subtitle cues available for proof")
    movie_midpoint = (ordered[0][1] + ordered[-1][1]) // 2
    four_line_candidates = [candidate for candidate in ordered if candidate[0] == 4]
    four_line = (
        min(four_line_candidates, key=lambda candidate: abs(candidate[1] - movie_midpoint))
        if four_line_candidates
        else None
    )
    if require_four_line and four_line is None:
        raise ValueError("No four-line cue exists for --require-four-line proof")
    sample_count = min(count, len(ordered))
    selected = [
        ordered[round(index * (len(ordered) - 1) / max(1, sample_count - 1))]
        for index in range(sample_count)
    ]
    if four_line is not None and four_line not in selected:
        # Keep the first and last movie regions when three or more frames are
        # requested; put the most representative layout case in the middle slot.
        selected[sample_count // 2 if sample_count >= 3 else 0] = four_line
    return selected


def command_proof(args: argparse.Namespace) -> int:
    movie = absolute_without_resolving(args.movie)
    if not movie.exists() or not movie.is_file():
        raise FileNotFoundError(movie)
    if args.count < 1:
        raise ValueError("--count must be at least 1")
    srt = select_proof_srt(movie, args.srt)
    out_dir = absolute_without_resolving(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    candidates = [
        (len(body), start_ms, end_ms)
        for _, start_ms, end_ms, body in parse_srt_blocks(srt)
    ]
    try:
        selected = select_proof_candidates(
            candidates,
            args.count,
            args.require_four_line,
        )
    except ValueError as exc:
        raise ValueError(f"{exc}: {srt}") from exc
    sample_count = len(selected)

    rendered = []
    run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{time.time_ns() % 1_000_000_000:09d}"
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_srt = Path(tmpdir) / "proof.srt"
        shutil.copy2(srt, tmp_srt)
        for i, (line_count, start, end) in enumerate(selected, 1):
            at = proof_sample_time(start, end)
            out = out_dir / (
                f"{movie.stem}_{srt.stem}_{run_id}_proof_{i}_"
                f"{fmt_ffmpeg_time(at).replace(':', '-')}.png"
            )
            baseline = Path(tmpdir) / f"baseline-{i}.png"
            subprocess.run(
                proof_frame_command(movie, None, baseline, at),
                check=True,
                timeout=120,
            )
            subprocess.run(
                proof_frame_command(movie, tmp_srt, out, at),
                check=True,
                timeout=120,
            )
            baseline_hash = sha256(baseline)
            rendered_hash = sha256(out)
            if baseline_hash == rendered_hash:
                raise RuntimeError(
                    f"Proof frame {i} is pixel-identical to the unsubtitled baseline; "
                    "the selected external subtitle was not visibly rendered"
                )
            rendered.append(
                {
                    "path": str(out),
                    "sha256": rendered_hash,
                    "baseline_sha256": baseline_hash,
                    "subtitle_pixels_differ_from_baseline": True,
                    "time": fmt_ffmpeg_time(at),
                    "line_count": line_count,
                }
            )
    if len(rendered) != sample_count:
        raise RuntimeError(f"Rendered {len(rendered)} proof frames; expected {sample_count}")
    receipt = {
        "movie": str(movie),
        "movie_sha256": sha256(movie),
        "srt": str(srt),
        "srt_sha256": sha256(srt),
        "rendered": rendered,
        "visual_inspection_required": True,
        "visually_inspected": False,
        "translation_meaning_limit": (
            "Rendered frames prove layout only; they do not prove that the "
            "target-language text preserves the source meaning."
        ),
        "audio_alignment_limit": (
            "Rendered frames do not compare subtitle boundaries with spoken "
            "audio; beginning/middle/end dialogue checks remain required."
        ),
        "player_selection_limit": (
            "These frames prove the external SRT layout only. They do not prove a "
            "Jellyfin/Plex/TV player disabled embedded, forced, secondary, or burned-in subtitles."
        ),
    }
    if args.receipt:
        atomic_write_text(absolute_without_resolving(args.receipt), json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build")
    build.add_argument("--movie", required=True, type=Path)
    build.add_argument(
        "--source-srt",
        "--english-source",
        dest="source_srt",
        required=True,
        type=Path,
        help="Release-matched source-language SRT. --english-source remains a compatible alias.",
    )
    build.add_argument(
        "--target-srt",
        "--indonesian-source",
        dest="target_srt",
        type=Path,
        help="Optional matched target-language SRT. --indonesian-source remains a compatible alias.",
    )
    build.add_argument("--source-language", default="en", help="Source-language tag (default: en)")
    build.add_argument("--target-language", default="id", help="Target-language tag (default: id)")
    build.add_argument("--shift-ms", type=int, default=0)
    build.add_argument("--translate", choices=["gemini", "gemini-cli", "mantis-antigravity"])
    build.add_argument(
        "--model",
        help=(
            "Translation model selector or comma-separated Gemini API selector pool. Defaults to "
            "current Pro, Flash, and Flash Lite aliases for the API; the signed-in "
            "default for Gemini CLI; and auto for Mantis Antigravity."
        ),
    )
    build.add_argument("--chunk-size", type=int, default=250)
    build.add_argument(
        "--source-width",
        "--english-width",
        dest="source_width",
        type=int,
        default=42,
        help="Maximum display columns per source-language line",
    )
    build.add_argument(
        "--target-width",
        "--indonesian-width",
        dest="target_width",
        type=int,
        default=40,
        help="Maximum display columns per target-language line",
    )
    build.add_argument(
        "--output-dir",
        type=Path,
        help="Write the staged/final sidecar bundle here instead of beside the movie",
    )
    build.add_argument("--label", default="dual-subtitle-rebuild")
    build.add_argument(
        "--make-default",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write byte-identical .dual.default.srt (default: enabled)",
    )
    build.add_argument(
        "--make-plain",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write byte-identical exact-basename .srt (default: enabled)",
    )
    build.set_defaults(func=command_build)

    validate = sub.add_parser("validate")
    validate.add_argument("--movie", required=True, type=Path)
    validate.add_argument("--srt", type=Path)
    validate.add_argument("--sidecar-dir", type=Path, help="Directory containing the bundle to validate")
    validate.add_argument("--source-language", default="en")
    validate.add_argument("--target-language", default="id")
    validate.add_argument("--no-require-aliases", action="store_true")
    validate.set_defaults(func=command_validate)

    probe_av = sub.add_parser("probe-av")
    probe_av.add_argument("--movie", required=True, type=Path)
    probe_av.add_argument("--warn-ms", type=int, default=250)
    probe_av.set_defaults(func=command_probe_av)

    shift = sub.add_parser("shift")
    shift.add_argument("--movie", required=True, type=Path)
    shift.add_argument("--shift-ms", required=True, type=int)
    shift.add_argument("--source-language", default="en")
    shift.add_argument("--target-language", default="id")
    shift.add_argument(
        "--suffix",
        dest="suffixes",
        action="append",
        help="Sidecar suffix to shift. Repeatable. Defaults to the selected source/target tags plus dual/default/plain.",
    )
    shift.add_argument("--srt", action="append", type=Path, help="Additional explicit SRT path to shift. Repeatable.")
    shift.add_argument("--label", help="Backup label. Defaults to plus/minusNms-timing-shift.")
    shift.add_argument("--no-validate", action="store_true", help="Skip post-shift validation of the movie .dual.srt.")
    shift.add_argument("--no-require-aliases", action="store_true")
    shift.set_defaults(func=command_shift)

    proof = sub.add_parser("proof")
    proof.add_argument("--movie", required=True, type=Path)
    proof.add_argument("--srt", type=Path)
    proof.add_argument("--out-dir", required=True, type=Path)
    proof.add_argument("--count", type=int, default=3)
    proof.add_argument("--require-four-line", action="store_true")
    proof.add_argument("--receipt", type=Path, help="Optional JSON receipt path")
    proof.set_defaults(func=command_proof)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
