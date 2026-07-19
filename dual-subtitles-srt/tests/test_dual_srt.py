import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import dual_srt


class TranslationSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cues = [
            dual_srt.Cue(1, 1_000, 2_500, "Please take notes."),
            dual_srt.Cue(2, 2_700, 4_200, "There are no wounds on the body."),
            dual_srt.Cue(3, 4_400, 5_900, "The contents of the safe are intact."),
        ]

    def test_bandidas_style_numeric_provider_output_is_rejected(self) -> None:
        translated = {cue.index: str(cue.index) for cue in self.cues}

        with self.assertRaisesRegex(ValueError, "matches_cue_index"):
            dual_srt.assert_valid_translated_texts(
                self.cues,
                translated,
                "Gemini translation chunk",
            )

    def test_bandidas_style_numeric_cache_is_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / "movie.id.translation-cache.json"
            cache.write_text(
                json.dumps(
                    {
                        "version": dual_srt.TRANSLATION_CACHE_VERSION,
                        "source_sha256": dual_srt.cue_source_hash(self.cues),
                        "translations": {
                            str(cue.index): str(cue.index) for cue in self.cues
                        },
                    }
                ),
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                loaded = dual_srt.read_translation_cache(cache, self.cues)

        self.assertEqual({}, loaded)

    def test_translation_field_wins_over_identifier_like_id_field(self) -> None:
        item = {
            "index": 1,
            "id": 1,
            "translation": "Silakan mencatat.",
        }

        self.assertEqual(
            "Silakan mencatat.",
            dual_srt.translated_item_text(item),
        )

    def test_numeric_lower_language_line_makes_dual_validation_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            srt = Path(tmpdir) / "movie.dual.srt"
            srt.write_text(
                "1\n"
                "00:00:01,000 --> 00:00:02,500\n"
                "Please take notes.\n"
                "1\n\n"
                "2\n"
                "00:00:02,700 --> 00:00:04,200\n"
                "♪♪\n"
                "2\n",
                encoding="utf-8",
            )

            report = dual_srt.validate_dual(srt)

        self.assertFalse(report["ok"])
        self.assertEqual(2, report["numeric_translation_line_count"])
        self.assertEqual([1, 2], report["numeric_translation_line_cues"])

    def test_numeric_wrapped_line_inside_real_translation_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            srt = Path(tmpdir) / "movie.dual.srt"
            srt.write_text(
                "1\n"
                "00:00:01,000 --> 00:00:02,500\n"
                "Yeah, an ancient, elderly, 1-5-0 fart.\n"
                "Ya, tahu kan, tua bangka kuno berumur\n"
                "1-5-0.\n",
                encoding="utf-8",
            )

            report = dual_srt.validate_dual(srt)

        self.assertTrue(report["ok"])
        self.assertEqual(0, report["numeric_translation_line_count"])

    def test_music_symbol_source_does_not_allow_numeric_translation(self) -> None:
        self.assertEqual(
            "matches_cue_index",
            dual_srt.translated_text_issue(12, "♪♪", "12"),
        )

    def test_labeled_cue_number_placeholder_is_rejected(self) -> None:
        self.assertEqual(
            "labeled_cue_index_placeholder",
            dual_srt.translated_text_issue(1, "Please take notes.", "Cue 1"),
        )

    def test_literal_null_placeholder_is_rejected(self) -> None:
        self.assertEqual(
            "null_placeholder",
            dual_srt.translated_text_issue(1, "Hello there.", "null"),
        )

    def test_unicode_numeral_placeholder_is_rejected(self) -> None:
        self.assertEqual(
            "numeric_placeholder",
            dual_srt.translated_text_issue(1, "Hello there.", "Ⅻ"),
        )

    def test_null_provider_translation_becomes_blank(self) -> None:
        self.assertEqual("", dual_srt.translated_item_text({"index": 1, "translation": None}))

    def test_semantically_implausible_one_word_translation_is_rejected(self) -> None:
        self.assertEqual(
            "suspiciously_short_translation",
            dual_srt.translated_text_issue(
                4,
                "Take the blue folder, lock every door, and call the police immediately.",
                "Ya.",
            ),
        )

    def test_copied_cyrillic_dialogue_is_rejected(self) -> None:
        source = "Это обычная длинная реплика, которую обязательно нужно перевести полностью."
        self.assertEqual(
            "copied_source_text",
            dual_srt.translated_text_issue(7, source, source),
        )

    def test_genuinely_numeric_dialogue_can_remain_numeric(self) -> None:
        self.assertIsNone(
            dual_srt.translated_text_issue(
                571,
                "247, 248, 24--",
                "247, 248, 24--",
            )
        )

    def test_translation_cache_is_bound_to_exact_source_text_and_timing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / "movie.id.translation-cache.json"
            dual_srt.write_translation_cache(
                cache,
                self.cues,
                {
                    1: "Silakan mencatat.",
                    2: "Tidak ada luka pada tubuh.",
                    3: "Isi brankas masih utuh.",
                },
            )
            changed = [
                dual_srt.Cue(1, 1_000, 2_600, "Please take notes."),
                *self.cues[1:],
            ]

            with contextlib.redirect_stdout(io.StringIO()):
                loaded = dual_srt.read_translation_cache(cache, changed)

        self.assertEqual({}, loaded)

    def test_translation_cache_is_bound_to_target_language_and_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / "movie.fr.translation-cache.json"
            translations = {
                1: "Veuillez prendre des notes.",
                2: "Le corps ne présente aucune blessure.",
                3: "Le contenu du coffre est intact.",
            }
            dual_srt.write_translation_cache(
                cache,
                self.cues,
                translations,
                "en",
                "fr",
                "gemini-api",
                "gemini-pro",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                wrong_language = dual_srt.read_translation_cache(
                    cache,
                    self.cues,
                    "en",
                    "de",
                    "gemini-api",
                    "gemini-pro",
                )
                wrong_model = dual_srt.read_translation_cache(
                    cache,
                    self.cues,
                    "en",
                    "fr",
                    "gemini-api",
                    "another-model",
                )

        self.assertEqual({}, wrong_language)
        self.assertEqual({}, wrong_model)

    def test_cache_can_resume_when_model_selector_pool_is_safely_expanded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / "movie.id.translation-cache.json"
            translations = {
                1: "Silakan mencatat.",
                2: "Tidak ada luka pada tubuh.",
                3: "Isi brankas masih utuh.",
            }
            dual_srt.write_translation_cache(
                cache,
                self.cues,
                translations,
                "en",
                "id",
                "gemini-api",
                "model-a",
            )

            resumed = dual_srt.read_translation_cache(
                cache,
                self.cues,
                "en",
                "id",
                "gemini-api",
                "model-a,model-b",
            )
            dual_srt.write_translation_cache(
                cache,
                self.cues,
                resumed,
                "en",
                "id",
                "gemini-api",
                "model-a,model-b",
            )
            payload = json.loads(cache.read_text(encoding="utf-8"))

        self.assertEqual(translations, resumed)
        self.assertEqual(
            ["model-a", "model-a,model-b"],
            payload["model_selector_history"],
        )

    def test_cache_cannot_resume_with_narrower_model_selector_pool(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / "movie.id.translation-cache.json"
            translations = {
                1: "Silakan mencatat.",
                2: "Tidak ada luka pada tubuh.",
                3: "Isi brankas masih utuh.",
            }
            dual_srt.write_translation_cache(
                cache,
                self.cues,
                translations,
                "en",
                "id",
                "gemini-api",
                "model-a,model-b",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                resumed = dual_srt.read_translation_cache(
                    cache,
                    self.cues,
                    "en",
                    "id",
                    "gemini-api",
                    "model-b",
                )

        self.assertEqual({}, resumed)

    def test_cache_cannot_resume_after_prompt_contract_changes(self) -> None:
        cue = [dual_srt.Cue(1, 1_000, 2_000, "Hello there.")]
        draft = {1: "Halo di sana."}
        provider_calls = 0

        class FakeModels:
            def generate_content(self, **kwargs: object) -> object:
                nonlocal provider_calls
                del kwargs
                provider_calls += 1
                return types.SimpleNamespace(
                    text=json.dumps(
                        {
                            "translations": [
                                {"index": 1, "translation": "Halo di sana."}
                            ]
                        }
                    ),
                    candidates=[types.SimpleNamespace(finish_reason="STOP")],
                )

        fake_genai = types.ModuleType("google.genai")
        fake_genai.Client = lambda api_key: types.SimpleNamespace(models=FakeModels())
        fake_genai.types = types.SimpleNamespace(
            GenerateContentConfig=lambda **kwargs: kwargs
        )
        fake_google = types.ModuleType("google")
        fake_google.genai = fake_genai

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / "semantic-review.json"
            with mock.patch.dict(
                sys.modules,
                {"google": fake_google, "google.genai": fake_genai},
            ), mock.patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "test-only"},
            ), contextlib.redirect_stdout(io.StringIO()):
                dual_srt.translate_with_gemini(
                    cue,
                    cache,
                    "test-model",
                    1,
                    "en",
                    "id",
                    "First semantic review contract.",
                    draft,
                    False,
                )
                dual_srt.translate_with_gemini(
                    cue,
                    cache,
                    "test-model",
                    1,
                    "en",
                    "id",
                    "Corrected semantic review contract.",
                    draft,
                    False,
                )
            payload = json.loads(cache.read_text(encoding="utf-8"))

        self.assertEqual(2, provider_calls)
        self.assertEqual(
            dual_srt.translation_prompt_sha256(
                cue,
                "en",
                "id",
                "Corrected semantic review contract.",
                draft,
                False,
            ),
            payload["prompt_sha256"],
        )

    def test_provider_indexes_are_strict_before_dictionary_construction(self) -> None:
        cases = [
            (
                "extra duplicate",
                [
                    {"index": 1, "translation": "Satu."},
                    {"index": 1, "translation": "Timpa."},
                    {"index": 2, "translation": "Dua."},
                ],
                "item count mismatch",
            ),
            (
                "same-length duplicate",
                [
                    {"index": 1, "translation": "Satu."},
                    {"index": 1, "translation": "Timpa."},
                ],
                "Duplicate translation index",
            ),
            (
                "string index",
                [
                    {"index": "1", "translation": "Satu."},
                    {"index": 2, "translation": "Dua."},
                ],
                "index must be an integer",
            ),
            (
                "wrong exact set",
                [
                    {"index": 1, "translation": "Satu."},
                    {"index": 3, "translation": "Tiga."},
                ],
                "index mismatch",
            ),
        ]

        for label, items, message in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, message):
                    dual_srt.parse_provider_translations(
                        {"translations": items},
                        {1, 2},
                    )

    def test_copied_ordinary_english_dialogue_is_rejected(self) -> None:
        source = "This is ordinary dialogue that needs a real translation."

        self.assertEqual(
            "copied_source_text",
            dual_srt.translated_text_issue(9, source, source),
        )

    def test_copied_seven_word_dialogue_is_rejected(self) -> None:
        source = "Everybody must leave this building right now."

        self.assertEqual(
            "copied_source_text",
            dual_srt.translated_text_issue(9, source, source),
        )

    def test_copied_five_word_dialogue_is_rejected(self) -> None:
        source = "Close the door right now."

        self.assertEqual(
            "copied_source_text",
            dual_srt.translated_text_issue(9, source, source),
        )

    def test_copied_song_lyric_is_not_exempt_from_translation(self) -> None:
        source = "♪ We will always stay together ♪"

        self.assertEqual(
            "copied_source_text",
            dual_srt.translated_text_issue(9, source, source),
        )

    def test_long_source_to_short_latin_word_is_rejected(self) -> None:
        self.assertEqual(
            "suspiciously_short_translation",
            dual_srt.translated_text_issue(
                9,
                "You must close every door before the guards arrive here.",
                "Mungkin",
            ),
        )

    def test_cjk_number_only_translation_is_rejected(self) -> None:
        self.assertEqual(
            "numeric_placeholder",
            dual_srt.translated_text_issue(9, "There are twelve guards.", "十二"),
        )

    def test_concise_cjk_translation_is_not_rejected_by_latin_word_heuristic(self) -> None:
        self.assertIsNone(
            dual_srt.translated_text_issue(
                4,
                "You need to get out of here very quickly.",
                "早く出ろ",
            )
        )

    def test_short_proper_name_can_remain_unchanged(self) -> None:
        self.assertIsNone(
            dual_srt.translated_text_issue(9, "Bullseye!", "Bullseye!")
        )

    def test_subtitle_production_credit_is_rejected_from_source(self) -> None:
        credits = dual_srt.source_credit_issues(
            [
                dual_srt.Cue(
                    1400,
                    5_529_107,
                    5_531_484,
                    "Subtitled by: Axium Digital, Inc.",
                )
            ]
        )

        self.assertEqual(
            "subtitle_production_credit",
            credits[0]["reason"],
        )

    def test_source_advertisement_or_link_is_rejected(self) -> None:
        issues = dual_srt.source_hygiene_issues(
            [dual_srt.Cue(1, 1_000, 2_000, "Visit www.example.com today.")]
        )

        self.assertEqual("advertisement_or_external_link", issues[0]["reason"])

    def test_ordinary_dialogue_using_bet_is_not_treated_as_an_ad(self) -> None:
        issues = dual_srt.source_hygiene_issues(
            [dual_srt.Cue(1, 1_000, 2_000, "I bet they're arguing over us.")]
        )

        self.assertEqual([], issues)

    def test_failed_gemini_validation_does_not_leak_invalid_candidate(self) -> None:
        class FakeModels:
            def __init__(self) -> None:
                self.calls = 0

            def generate_content(self, **kwargs: object) -> object:
                del kwargs
                self.calls += 1
                if self.calls == 1:
                    return types.SimpleNamespace(
                        text=json.dumps(
                            {"translations": [{"index": 1, "translation": "1"}]}
                        ),
                        candidates=[types.SimpleNamespace(finish_reason="STOP")],
                    )
                raise RuntimeError("provider failed after invalid response")

        fake_models = FakeModels()
        fake_genai = types.ModuleType("google.genai")
        fake_genai.Client = lambda api_key: types.SimpleNamespace(models=fake_models)
        fake_genai.types = types.SimpleNamespace(
            GenerateContentConfig=lambda **kwargs: kwargs
        )
        fake_google = types.ModuleType("google")
        fake_google.genai = fake_genai
        cue = [dual_srt.Cue(1, 1_000, 2_000, "Please take notes.")]

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / "cache.json"
            with mock.patch.dict(
                sys.modules,
                {"google": fake_google, "google.genai": fake_genai},
            ), mock.patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "test-only"},
            ), mock.patch.object(
                dual_srt.time,
                "sleep",
                return_value=None,
            ):
                with self.assertRaisesRegex(RuntimeError, "provider failed"):
                    dual_srt.translate_with_gemini(
                        cue,
                        cache,
                        "test-model",
                        1,
                    )

            self.assertFalse(cache.exists())

    def test_gemini_retry_receives_deterministic_failure_feedback(self) -> None:
        prompts: list[str] = []

        class FakeModels:
            def generate_content(self, **kwargs: object) -> object:
                prompts.append(str(kwargs["contents"]))
                translation = (
                    "-Hasta mañana, Papa. -Hasta mañana."
                    if len(prompts) == 1
                    else "-Sampai jumpa besok, Papa. -Sampai jumpa besok."
                )
                return types.SimpleNamespace(
                    text=json.dumps(
                        {"translations": [{"index": 1, "translation": translation}]}
                    ),
                    candidates=[types.SimpleNamespace(finish_reason="STOP")],
                )

        fake_genai = types.ModuleType("google.genai")
        fake_genai.Client = lambda api_key: types.SimpleNamespace(models=FakeModels())
        fake_genai.types = types.SimpleNamespace(
            GenerateContentConfig=lambda **kwargs: kwargs
        )
        fake_google = types.ModuleType("google")
        fake_google.genai = fake_genai
        cue = [
            dual_srt.Cue(
                1,
                1_000,
                2_000,
                "-Hasta mañana, Papa. -Hasta mañana.",
            )
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / "cache.json"
            with mock.patch.dict(
                sys.modules,
                {"google": fake_google, "google.genai": fake_genai},
            ), mock.patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "test-only"},
            ):
                translated = dual_srt.translate_with_gemini(
                    cue,
                    cache,
                    "test-model",
                    1,
                )

        self.assertEqual(
            {1: "-Sampai jumpa besok, Papa. -Sampai jumpa besok."},
            translated,
        )
        self.assertEqual(2, len(prompts))
        self.assertIn("copied_source_text", prompts[1])

    def test_gemini_non_stop_finish_reason_is_rejected(self) -> None:
        class FakeModels:
            def generate_content(self, **kwargs: object) -> object:
                del kwargs
                return types.SimpleNamespace(
                    text=json.dumps(
                        {"translations": [{"index": 1, "translation": "Halo di sana."}]}
                    ),
                    candidates=[types.SimpleNamespace(finish_reason="MAX_TOKENS")],
                )

        fake_genai = types.ModuleType("google.genai")
        fake_genai.Client = lambda api_key: types.SimpleNamespace(models=FakeModels())
        fake_genai.types = types.SimpleNamespace(
            GenerateContentConfig=lambda **kwargs: kwargs
        )
        fake_google = types.ModuleType("google")
        fake_google.genai = fake_genai
        cue = [dual_srt.Cue(1, 1_000, 2_000, "Hello there.")]

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(
                sys.modules,
                {"google": fake_google, "google.genai": fake_genai},
            ), mock.patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "test-only"},
            ), mock.patch.object(
                dual_srt.time,
                "sleep",
                return_value=None,
            ):
                with self.assertRaisesRegex(ValueError, "finish reason"):
                    dual_srt.translate_with_gemini(
                        cue,
                        Path(tmpdir) / "cache.json",
                        "test-model",
                        1,
                    )

    def test_mantis_receives_explicit_model_selector(self) -> None:
        cue = [dual_srt.Cue(1, 1_000, 2_000, "Hello there.")]
        response = types.SimpleNamespace(
            stdout=json.dumps(
                {"translations": [{"index": 1, "translation": "Halo di sana."}]}
            )
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / "cache.json"
            with mock.patch.object(
                dual_srt.shutil,
                "which",
                return_value="/usr/bin/mantis",
            ), mock.patch.object(
                dual_srt.subprocess,
                "run",
                return_value=response,
            ) as run:
                translated = dual_srt.translate_with_mantis_antigravity(
                    cue,
                    cache,
                    "gemini-3.1-pro-preview",
                    1,
                )

            command = run.call_args.args[0]
            self.assertEqual("gemini-3.1-pro-preview", command[command.index("--model") + 1])
            self.assertEqual({1: "Halo di sana."}, translated)
            cache_payload = json.loads(cache.read_text(encoding="utf-8"))
            self.assertEqual(
                "gemini-3.1-pro-preview",
                cache_payload["model_selector"],
            )
            self.assertNotIn("model", cache_payload)

    def test_overlong_translation_is_retranslated_with_layout_instruction(self) -> None:
        cues = [
            dual_srt.Cue(
                1,
                1_000,
                2_000,
                "Woody, is it as bad out there as everyone says?",
            )
        ]
        target_texts = {
            1: "Woody, apakah keadaan di luar sana seburuk yang dikatakan semua orang?"
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache = root / "cache.json"
            with mock.patch.object(
                dual_srt,
                "translate_with_gemini",
                return_value={1: "Woody, apa di luar separah kata mereka?"},
            ) as translate:
                repaired = dual_srt.retranslate_overlong_cues(
                    cues,
                    target_texts,
                    cache,
                    "gemini",
                    "test-model",
                    10,
                    "en",
                    "id",
                    20,
                    root,
                )

            instruction = translate.call_args.args[-2]
            current_drafts = translate.call_args.args[-1]
            cache_payload = json.loads(cache.read_text(encoding="utf-8"))

        self.assertEqual([1], repaired)
        self.assertIn("at most two lines", instruction)
        self.assertEqual(
            "Woody, apakah keadaan di luar sana seburuk yang dikatakan semua orang?",
            current_drafts[1],
        )
        self.assertLessEqual(
            len(dual_srt.wrap_lines(target_texts[1], 20)),
            2,
        )
        self.assertEqual(
            "Woody, apa di luar separah kata mereka?",
            cache_payload["translations"]["1"],
        )
        prompt = dual_srt.translation_prompt(
            cues,
            "en",
            "id",
            instruction,
            current_drafts,
        )
        self.assertIn('"current_translation"', prompt)
        self.assertLess(
            prompt.index("Critical constraints:"),
            prompt.index('"current_translation"'),
        )

    def test_translation_prompt_keeps_neighbors_at_chunk_boundaries(self) -> None:
        cues = [
            dual_srt.Cue(1, 1_000, 2_000, "The banks"),
            dual_srt.Cue(2, 2_000, 3_000, "control the cash flow,"),
            dual_srt.Cue(3, 3_000, 4_000, "and through it they control us."),
        ]
        prompt = dual_srt.translation_prompt(
            [cues[1]],
            "en",
            "id",
            neighboring_sources=dual_srt.neighboring_source_context(cues),
        )

        self.assertIn('"previous_source": "The banks"', prompt)
        self.assertIn('"next_source": "and through it they control us."', prompt)
        self.assertIn("Do not repeat a conjunction", prompt)

    def test_semantic_review_inputs_bind_draft_and_two_cue_context(self) -> None:
        cues = [
            dual_srt.Cue(index, index * 1_000, index * 1_000 + 900, f"Source {index}")
            for index in range(1, 6)
        ]
        target = {index: f"Target {index}" for index in range(1, 6)}
        review_inputs = dual_srt.semantic_review_inputs(cues, target)
        record = json.loads(review_inputs[2].text)

        changed = dict(target)
        changed[2] = "Changed adjacent target"
        changed_inputs = dual_srt.semantic_review_inputs(cues, changed)

        self.assertEqual("Source 3", record["source"])
        self.assertEqual("Target 3", record["current_translation"])
        self.assertEqual(2, len(record["context_before"]))
        self.assertEqual(2, len(record["context_after"]))
        self.assertNotEqual(
            dual_srt.cue_source_hash(review_inputs),
            dual_srt.cue_source_hash(changed_inputs),
        )

    def test_semantic_review_cache_validates_against_original_source_text(self) -> None:
        cues = [dual_srt.Cue(1, 1_000, 2_000, "Bravo.")]
        target = {1: "Bravo."}
        review_inputs = dual_srt.semantic_review_inputs(cues, target)

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / "semantic-review.json"
            dual_srt.write_translation_cache(
                cache,
                review_inputs,
                target,
                "en",
                "id",
                "gemini-api",
                "test-model",
            )
            loaded = dual_srt.read_translation_cache(
                cache,
                review_inputs,
                "en",
                "id",
                "gemini-api",
                "test-model",
            )

        self.assertEqual(target, loaded)

    def test_semantic_review_rejects_invalid_provider_output(self) -> None:
        cues = [dual_srt.Cue(1, 1_000, 2_000, "This is ordinary source dialogue.")]
        target = {1: "Ini dialog sumber biasa."}

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(
                dual_srt,
                "translate_with_gemini",
                return_value={1: "1"},
            ) as translate:
                with self.assertRaisesRegex(ValueError, "Mandatory semantic-reviewed"):
                    dual_srt.semantic_review_translations(
                        cues,
                        target,
                        Path(tmpdir) / "review.json",
                        "gemini",
                        "test-model",
                        10,
                        "en",
                        "id",
                        40,
                    )

        self.assertFalse(translate.call_args.args[-1])

    def test_semantic_review_rejects_overlong_result_before_staging(self) -> None:
        cues = [dual_srt.Cue(1, 1_000, 2_000, "A concise source sentence.")]
        target = {1: "Kalimat sumber yang ringkas."}
        overlong = " ".join(["terjemahan"] * 20)

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(
                dual_srt,
                "translate_with_gemini",
                return_value={1: overlong},
            ):
                with self.assertRaisesRegex(ValueError, "Do not invent proportional timestamps"):
                    dual_srt.semantic_review_translations(
                        cues,
                        target,
                        Path(tmpdir) / "review.json",
                        "gemini",
                        "test-model",
                        10,
                        "en",
                        "id",
                        20,
                    )

    def test_second_layout_round_receives_first_failed_rewrite_as_its_draft(self) -> None:
        cues = [
            dual_srt.Cue(
                648,
                1_000,
                5_421,
                "I was wondering if you could tell me if she had any issues with her father.",
            )
        ]
        original = (
            "Saya ingin tahu apakah Anda bisa memberi tahu saya jika dia punya "
            "masalah dengan ayahnya."
        )
        still_long = (
            "Bisakah Anda memberi tahu saya apakah dia pernah memiliki masalah "
            "yang cukup serius dengan ayahnya?"
        )
        concise = "Apa dia punya masalah dengan ayahnya?"
        target_texts = {648: original}
        drafts_seen: list[str] = []

        def rewrite(*args: object) -> dict[int, str]:
            drafts = args[-1]
            self.assertIsInstance(drafts, dict)
            drafts_seen.append(drafts[648])
            return {648: still_long if len(drafts_seen) == 1 else concise}

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache = root / "cache.json"
            with mock.patch.object(
                dual_srt,
                "translate_with_gemini",
                side_effect=rewrite,
            ):
                dual_srt.retranslate_overlong_cues(
                    cues,
                    target_texts,
                    cache,
                    "gemini",
                    "test-model",
                    10,
                    "en",
                    "id",
                    40,
                    root,
                )

        self.assertEqual([original, still_long], drafts_seen)
        self.assertEqual(concise, target_texts[648])

    def test_failed_layout_rewrites_do_not_publish_repair_cache(self) -> None:
        cues = [
            dual_srt.Cue(
                1364,
                1_000,
                5_379,
                "call of duty, and for the service he has given the people of Mexico.",
            )
        ]
        overlong = (
            "panggilan tugas, dan atas pengabdian yang telah ia berikan bagi "
            "seluruh rakyat Meksiko."
        )
        target_texts = {1364: overlong}

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache = root / "cache.json"
            with mock.patch.object(
                dual_srt,
                "translate_with_gemini",
                return_value={1364: overlong},
            ):
                dual_srt.retranslate_overlong_cues(
                    cues,
                    target_texts,
                    cache,
                    "gemini",
                    "test-model",
                    10,
                    "en",
                    "id",
                    40,
                    root,
                )

            self.assertFalse(cache.exists())
            with self.assertRaisesRegex(ValueError, "exceed the two-line"):
                dual_srt.assert_two_line_language_limit(
                    cues,
                    target_texts,
                    40,
                    "id",
                )


class TimingAndLayoutTests(unittest.TestCase):
    def test_sha256_streams_files_without_reading_all_bytes_at_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "large-fixture.bin"
            path.write_bytes(b"subtitle-proof" * 100_000)
            expected = hashlib.sha256(path.read_bytes()).hexdigest()

            with mock.patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("whole-file read is forbidden"),
            ):
                actual = dual_srt.sha256(path)

        self.assertEqual(expected, actual)

    def test_proof_frames_fast_seek_without_resetting_subtitle_timeline(self) -> None:
        command = dual_srt.proof_frame_command(
            Path("/movies/example.mkv"),
            Path("/tmp/proof.srt"),
            Path("/tmp/frame.png"),
            2_400_330,
        )

        self.assertLess(command.index("-ss"), command.index("-i"))
        self.assertLess(command.index("-copyts"), command.index("-i"))
        self.assertEqual("00:40:00.330", command[command.index("-ss") + 1])
        self.assertEqual("/movies/example.mkv", command[command.index("-i") + 1])

    def test_proof_baseline_command_has_no_subtitle_filter(self) -> None:
        command = dual_srt.proof_frame_command(
            Path("/movies/example.mkv"),
            None,
            Path("/tmp/baseline.png"),
            1_000,
        )

        self.assertNotIn("-vf", command)

    def test_one_millisecond_cue_is_sampled_inside_its_interval(self) -> None:
        at = dual_srt.proof_sample_time(1_000, 1_001)

        self.assertGreaterEqual(at, 1_000)
        self.assertLess(at, 1_001)

    def test_proportional_timing_helpers_are_not_exposed(self) -> None:
        self.assertFalse(hasattr(dual_srt, "subcue_ranges"))
        self.assertFalse(hasattr(dual_srt, "split_bilingual_overflows"))
        self.assertFalse(hasattr(dual_srt, "split_by_weights"))

    def test_four_line_proof_keeps_movie_start_and_end_samples(self) -> None:
        candidates = [
            (2, 1_000, 2_000),
            (4, 2_000, 3_000),
            (2, 3_000, 4_000),
            (2, 4_000, 5_000),
            (2, 5_000, 6_000),
        ]

        selected = dual_srt.select_proof_candidates(candidates, 3, True)

        self.assertEqual(1_000, selected[0][1])
        self.assertEqual(5_000, selected[-1][1])
        self.assertTrue(any(line_count == 4 for line_count, _, _ in selected))

    def test_four_line_proof_uses_layout_case_nearest_movie_midpoint(self) -> None:
        candidates = [
            (4, 1_000, 2_000),
            (2, 2_000, 3_000),
            (2, 3_000, 4_000),
            (4, 4_000, 5_000),
            (2, 5_000, 6_000),
            (2, 6_000, 7_000),
        ]

        selected = dual_srt.select_proof_candidates(candidates, 3, True)

        self.assertEqual([1_000, 4_000, 6_000], [candidate[1] for candidate in selected])

    def test_combined_output_stays_within_four_lines(self) -> None:
        cues = [
            dual_srt.Cue(
                1,
                1_000,
                4_000,
                "This English sentence wraps onto at most two subtitle lines.",
            )
        ]
        translations = {
            1: "Kalimat bahasa Indonesia ini juga paling banyak dua baris.",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            dual = Path(tmpdir) / "movie.dual.srt"
            dual_srt.write_dual(dual, cues, translations, 42, 40)
            report = dual_srt.validate_dual(dual)

        self.assertTrue(report["ok"])
        self.assertLessEqual(report["max_visual_lines_per_cue"], 4)

    def test_cjk_without_spaces_wraps_by_display_width(self) -> None:
        lines = dual_srt.wrap_lines("这是一个没有空格而且必须正确换行的中文字幕测试", 12)

        self.assertGreater(len(lines), 1)
        self.assertTrue(all(dual_srt.display_width(line) <= 12 for line in lines))

class StrictParsingAndBundleTests(unittest.TestCase):
    def write_srt(self, path: Path, blocks: list[tuple[int, str, str, list[str]]]) -> None:
        text = "\n\n".join(
            f"{index}\n{start} --> {end}\n" + "\n".join(body)
            for index, start, end, body in blocks
        )
        path.write_text(text + "\n", encoding="utf-8")

    def test_malformed_block_is_not_silently_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bad.srt"
            path.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nHello.\n\n"
                "2\n00:00:03,000 -> 00:00:04,000\nMissing dialogue.\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "invalid timestamp"):
                dual_srt.read_srt(path)

    def test_non_positive_duration_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bad.srt"
            self.write_srt(
                path,
                [(1, "00:00:02,000", "00:00:01,000", ["Backwards."])],
            )
            with self.assertRaisesRegex(ValueError, "end time must be after start"):
                dual_srt.validate_dual(path)

    def test_impossible_minute_and_second_components_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bad.srt"
            self.write_srt(
                path,
                [(1, "00:99:60,000", "00:99:61,000", ["Impossible time."])],
            )

            with self.assertRaisesRegex(ValueError, "Invalid SRT timestamp component"):
                dual_srt.read_srt(path)

    def test_one_hundred_hour_timestamp_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "long.srt"
            self.write_srt(
                path,
                [(1, "100:00:00,000", "100:00:01,000", ["Long recording."])],
            )

            cues = dual_srt.read_srt(path)

        self.assertEqual(360_000_000, cues[0].start_ms)

    def test_language_tags_accept_common_and_private_tags_but_reject_incomplete_singleton(self) -> None:
        self.assertEqual("zh-hant", dual_srt.normalize_language_tag("zh-Hant"))
        self.assertEqual("x-private", dual_srt.normalize_language_tag("x-private"))
        with self.assertRaisesRegex(ValueError, "Invalid language tag"):
            dual_srt.normalize_language_tag("en-x")

    def test_bundle_validation_proves_timing_and_composition(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "movie.fr.srt"
            target = root / "movie.ja.srt"
            dual = root / "movie.dual.srt"
            default = root / "movie.dual.default.srt"
            plain = root / "movie.srt"
            cues = [dual_srt.Cue(1, 1_000, 2_500, "Bonjour tout le monde.")]
            target_texts = {1: "皆さん、こんにちは。"}
            dual_srt.write_srt(source, cues, {1: cues[0].text}, 42)
            dual_srt.write_srt(target, cues, target_texts, 40)
            dual_srt.write_dual(dual, cues, target_texts, 42, 40)
            dual_srt.atomic_copy(dual, default)
            dual_srt.atomic_copy(dual, plain)

            report = dual_srt.validate_bundle(dual, source, target, default, plain)

        self.assertTrue(report["ok"])
        self.assertTrue(report["bundle_language_and_composition_verified"])
        self.assertEqual(0, report["timing_mismatch_count"])
        self.assertEqual(0, report["composition_mismatch_count"])

    def test_composition_mismatch_cannot_claim_language_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "movie.en.srt"
            target = root / "movie.id.srt"
            dual = root / "movie.dual.srt"
            default = root / "movie.dual.default.srt"
            plain = root / "movie.srt"
            cues = [dual_srt.Cue(1, 1_000, 2_000, "Good evening.")]
            target_texts = {1: "Selamat malam."}
            dual_srt.write_srt(source, cues, {1: cues[0].text}, 42)
            dual_srt.write_srt(target, cues, target_texts, 40)
            dual_srt.write_dual(dual, cues, {1: "Sampai jumpa."}, 42, 40)
            dual_srt.atomic_copy(dual, default)
            dual_srt.atomic_copy(dual, plain)

            report = dual_srt.validate_bundle(dual, source, target, default, plain)

        self.assertFalse(report["ok"])
        self.assertFalse(report["bundle_language_and_composition_verified"])
        self.assertEqual(1, report["composition_mismatch_count"])

    def test_source_advertisement_makes_bundle_validation_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "movie.en.srt"
            target = root / "movie.id.srt"
            dual = root / "movie.dual.srt"
            default = root / "movie.dual.default.srt"
            plain = root / "movie.srt"
            cues = [dual_srt.Cue(1, 1_000, 2_000, "Visit www.example.com today.")]
            target_texts = {1: "Kunjungi situs kami hari ini."}
            dual_srt.write_srt(source, cues, {1: cues[0].text}, 42)
            dual_srt.write_srt(target, cues, target_texts, 40)
            dual_srt.write_dual(dual, cues, target_texts, 42, 40)
            dual_srt.atomic_copy(dual, default)
            dual_srt.atomic_copy(dual, plain)

            report = dual_srt.validate_bundle(dual, source, target, default, plain)

        self.assertFalse(report["ok"])
        self.assertFalse(report["bundle_language_and_composition_verified"])
        self.assertEqual(1, report["source_hygiene_issue_count"])

    def test_extensionless_movie_keeps_its_name(self) -> None:
        self.assertEqual(
            Path("/movies/MOVIE.srt"),
            dual_srt.sidecar_path(Path("/movies/MOVIE"), ".srt"),
        )

    def test_target_alignment_rejects_one_millisecond_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "target.ja.srt"
            self.write_srt(
                target,
                [(1, "00:00:02,499", "00:00:03,500", ["離れて。"])],
            )
            source = [dual_srt.Cue(1, 1_000, 2_500, "Stay away.")]

            with self.assertRaisesRegex(ValueError, "without meaningful source overlap"):
                dual_srt.align_target(source, target, 0, "en", "ja")

    def test_target_alignment_rejects_250ms_of_ten_second_cue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "target.id.srt"
            self.write_srt(
                target,
                [(1, "00:00:00,000", "00:00:10,000", ["Target dialogue."])],
            )
            source = [dual_srt.Cue(1, 9_750, 11_000, "Source dialogue.")]

            with self.assertRaisesRegex(ValueError, "without meaningful source overlap"):
                dual_srt.align_target(source, target, 0, "en", "id")

    def test_target_alignment_rejects_ambiguous_multi_source_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "target.id.srt"
            self.write_srt(
                target,
                [(1, "00:00:01,000", "00:00:04,000", ["Target sentence stays whole."])],
            )
            source = [
                dual_srt.Cue(1, 1_000, 3_000, "First source cue."),
                dual_srt.Cue(2, 2_000, 4_000, "Second source cue."),
            ]

            with self.assertRaisesRegex(ValueError, "without meaningful source overlap"):
                dual_srt.align_target(source, target, 0, "en", "id")

    def test_proof_defaults_to_dual_and_rejects_stale_plain_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = Path(tmpdir) / "movie.mkv"
            movie.write_bytes(b"movie")
            dual = Path(tmpdir) / "movie.dual.srt"
            plain = Path(tmpdir) / "movie.srt"
            dual.write_text("dual", encoding="utf-8")
            plain.write_text("stale", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "active aliases do not byte-match"):
                dual_srt.select_proof_srt(movie)

            plain.write_text("dual", encoding="utf-8")
            self.assertEqual(dual, dual_srt.select_proof_srt(movie))


class TransactionAndGenericLanguageTests(unittest.TestCase):
    def write_single_cue(self, path: Path, text: str) -> None:
        path.write_text(
            f"1\n00:00:01,000 --> 00:00:02,500\n{text}\n",
            encoding="utf-8",
        )

    def test_mid_install_failure_restores_all_original_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            staged_a = root / "stage-a"
            staged_b = root / "stage-b"
            live_a = root / "live-a"
            live_b = root / "live-b"
            staged_a.write_bytes(b"new-a")
            staged_b.write_bytes(b"new-b")
            live_a.write_bytes(b"old-a")
            live_b.write_bytes(b"old-b")

            original_atomic_copy = dual_srt.atomic_copy
            calls = 0

            def fail_second_copy(source: Path, destination: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated install failure")
                original_atomic_copy(source, destination)

            with mock.patch.object(dual_srt, "atomic_copy", side_effect=fail_second_copy):
                with self.assertRaisesRegex(OSError, "simulated install failure"):
                    dual_srt.install_staged_files(
                        [(staged_a, live_a), (staged_b, live_b)],
                        lambda: {"ok": True},
                    )

            self.assertEqual(b"old-a", live_a.read_bytes())
            self.assertEqual(b"old-b", live_b.read_bytes())

    def test_rollback_attempts_every_destination_after_one_restore_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            staged_a = root / "stage-a"
            staged_b = root / "stage-b"
            live_a = root / "live-a"
            live_b = root / "live-b"
            staged_a.write_bytes(b"new-a")
            staged_b.write_bytes(b"new-b")
            live_a.write_bytes(b"old-a")
            live_b.write_bytes(b"old-b")
            original_atomic_write = dual_srt.atomic_write_bytes

            def fail_only_first_restore(destination: Path, data: bytes) -> None:
                if destination == live_a and data == b"old-a":
                    raise OSError("simulated restore failure")
                original_atomic_write(destination, data)

            with mock.patch.object(
                dual_srt,
                "atomic_write_bytes",
                side_effect=fail_only_first_restore,
            ):
                with self.assertRaisesRegex(RuntimeError, "rollback was incomplete"):
                    dual_srt.install_staged_files(
                        [(staged_a, live_a), (staged_b, live_b)],
                        lambda: {"ok": False},
                    )

            self.assertEqual(b"new-a", live_a.read_bytes())
            self.assertEqual(b"old-b", live_b.read_bytes())

    def test_finalizer_failure_restores_sidecars_and_tracked_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            staged = root / "staged.srt"
            live = root / "live.srt"
            report = root / "verify.json"
            staged.write_bytes(b"new-subtitle")
            live.write_bytes(b"old-subtitle")
            report.write_bytes(b"old-report")

            def fail_finalizer(validation: dict[str, object]) -> None:
                self.assertTrue(validation["ok"])
                dual_srt.atomic_write_bytes(report, b"new-report")
                raise OSError("simulated final report failure")

            with self.assertRaisesRegex(OSError, "simulated final report failure"):
                dual_srt.install_staged_files(
                    [(staged, live)],
                    lambda: {"ok": True},
                    tracked_destinations=[report],
                    finalize_installed=fail_finalizer,
                )

            self.assertEqual(b"old-subtitle", live.read_bytes())
            self.assertEqual(b"old-report", report.read_bytes())

    def test_generic_fr_to_ja_build_writes_pair_specific_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            movie = root / "Example.Movie.mkv"
            movie.write_bytes(b"fixture")
            source = root / "source.fr.srt"
            target = root / "target.ja.srt"
            output = root / "output"
            self.write_single_cue(source, "Bonjour tout le monde.")
            self.write_single_cue(target, "皆さん、こんにちは。")
            args = argparse.Namespace(
                movie=movie,
                source_srt=source,
                target_srt=target,
                source_language="fr",
                target_language="ja",
                shift_ms=0,
                translate=None,
                model="unused",
                chunk_size=10,
                source_width=42,
                target_width=40,
                output_dir=output,
                label="test",
                make_default=True,
                make_plain=True,
            )

            with mock.patch.object(dual_srt, "subtitle_stream_summaries", return_value=[]):
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = dual_srt.command_build(args)

                base = output / "Example.Movie"
                report = dual_srt.validation_report_for_movie(
                    movie,
                    base.with_name(base.name + ".dual.srt"),
                    "fr",
                    "ja",
                    sidecar_base=output / movie.name,
                )

        self.assertEqual(0, exit_code)
        self.assertTrue(report["ok"])

    def test_translated_build_semantic_reviews_before_installing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            movie = root / "Review.Movie.mkv"
            movie.write_bytes(b"fixture")
            source = root / "source.en.srt"
            output = root / "output"
            self.write_single_cue(source, "Natural source dialogue.")
            args = argparse.Namespace(
                movie=movie,
                source_srt=source,
                target_srt=None,
                source_language="en",
                target_language="id",
                shift_ms=0,
                translate="gemini",
                model="test-model",
                chunk_size=10,
                source_width=42,
                target_width=40,
                output_dir=output,
                label="test",
                make_default=True,
                make_plain=True,
            )

            with mock.patch.object(
                dual_srt,
                "subtitle_stream_summaries",
                return_value=[],
            ), mock.patch.object(
                dual_srt,
                "translate_with_gemini",
                side_effect=[
                    {1: "Draf target yang alami."},
                    {1: "Target akhir yang alami."},
                ],
            ) as translate:
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = dual_srt.command_build(args)

            report = json.loads(
                (output / "Review.Movie.dual.verify.json").read_text(encoding="utf-8")
            )
            target_text = (output / "Review.Movie.id.srt").read_text(encoding="utf-8")
            review_record = json.loads(translate.call_args_list[1].args[0][0].text)

        self.assertEqual(0, exit_code)
        self.assertEqual(2, translate.call_count)
        self.assertEqual("Natural source dialogue.", review_record["source"])
        self.assertEqual("Draf target yang alami.", review_record["current_translation"])
        self.assertIn("Target akhir yang alami.", target_text)
        self.assertNotIn("Draf target yang alami.", target_text)
        self.assertTrue(report["semantic_review_required"])
        self.assertTrue(report["semantic_review_complete"])
        self.assertEqual(dual_srt.SEMANTIC_REVIEW_VERSION, report["semantic_review_version"])

    def test_long_source_fails_before_live_sidecar_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            movie = root / "Long.Movie.mkv"
            movie.write_bytes(b"fixture")
            source = root / "source.en.srt"
            target = root / "target.id.srt"
            self.write_single_cue(source, " ".join(["dialogue"] * 40))
            self.write_single_cue(target, "Terjemahan.")
            live = root / "Long.Movie.srt"
            live.write_bytes(b"keep-me")
            args = argparse.Namespace(
                movie=movie,
                source_srt=source,
                target_srt=target,
                source_language="en",
                target_language="id",
                shift_ms=0,
                translate=None,
                model="unused",
                chunk_size=10,
                source_width=20,
                target_width=20,
                output_dir=None,
                label="test",
                make_default=True,
                make_plain=True,
            )

            with self.assertRaisesRegex(ValueError, "Do not invent proportional timestamps"):
                dual_srt.command_build(args)

            self.assertEqual(b"keep-me", live.read_bytes())

    def test_overlapping_source_is_rejected_without_automatic_repair_or_redistribution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            movie = root / "Overlap.Movie.mkv"
            movie.write_bytes(b"fixture")
            source = root / "source.en.srt"
            target = root / "target.id.srt"
            source.write_text(
                "1\n00:00:01,000 --> 00:00:03,000\nFirst source cue.\n\n"
                "2\n00:00:02,500 --> 00:00:04,000\nSecond source cue.\n",
                encoding="utf-8",
            )
            target.write_text(
                "1\n00:00:01,000 --> 00:00:03,000\nIsyarat sumber pertama.\n\n"
                "2\n00:00:02,500 --> 00:00:04,000\nIsyarat sumber kedua.\n",
                encoding="utf-8",
            )
            live = root / "Overlap.Movie.srt"
            live.write_bytes(b"keep-me")
            args = argparse.Namespace(
                movie=movie,
                source_srt=source,
                target_srt=target,
                source_language="en",
                target_language="id",
                shift_ms=0,
                translate=None,
                model=None,
                chunk_size=10,
                source_width=42,
                target_width=40,
                output_dir=None,
                label="test",
                make_default=True,
                make_plain=True,
            )

            with self.assertRaisesRegex(ValueError, "refusing to mutate or redistribute dialogue"):
                dual_srt.command_build(args)

            self.assertEqual(b"keep-me", live.read_bytes())

    def test_disabled_aliases_are_backed_up_and_removed_instead_of_left_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            movie = root / "No.Aliases.mkv"
            movie.write_bytes(b"fixture")
            source = root / "source.en.srt"
            target = root / "target.id.srt"
            self.write_single_cue(source, "Hello there.")
            self.write_single_cue(target, "Halo.")
            default = root / "No.Aliases.dual.default.srt"
            plain = root / "No.Aliases.srt"
            default.write_bytes(b"stale-default")
            plain.write_bytes(b"stale-plain")
            args = argparse.Namespace(
                movie=movie,
                source_srt=source,
                target_srt=target,
                source_language="en",
                target_language="id",
                shift_ms=0,
                translate=None,
                model=None,
                chunk_size=10,
                source_width=42,
                target_width=40,
                output_dir=None,
                label="test",
                make_default=False,
                make_plain=False,
            )

            with mock.patch.object(dual_srt, "subtitle_stream_summaries", return_value=[]):
                with contextlib.redirect_stdout(io.StringIO()):
                    exit_code = dual_srt.command_build(args)

            self.assertEqual(0, exit_code)
            self.assertFalse(default.exists())
            self.assertFalse(plain.exists())
            backup_manifests = list(
                root.glob("No.Aliases.dual.default.srt.backups/*/manifest.json")
            )
            self.assertEqual(1, len(backup_manifests))

    def test_shift_report_failure_restores_every_shifted_file_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            movie = root / "Shift.Movie.mkv"
            movie.write_bytes(b"fixture")
            source = root / "Shift.Movie.en.srt"
            self.write_single_cue(source, "Hello there.")
            report = root / "Shift.Movie.dual.verify.json"
            report.write_bytes(b'{"existing": true}\n')
            original_source = source.read_bytes()
            original_report = report.read_bytes()
            args = argparse.Namespace(
                movie=movie,
                shift_ms=500,
                source_language="en",
                target_language="id",
                suffixes=[".en.srt"],
                srt=[],
                label="test-shift",
                no_validate=True,
                no_require_aliases=True,
            )
            original_atomic_write_text = dual_srt.atomic_write_text

            def fail_report_write(path: Path, text: str) -> None:
                if path == report and "timing_adjustments" in text:
                    raise OSError("simulated shift report failure")
                original_atomic_write_text(path, text)

            with mock.patch.object(
                dual_srt,
                "atomic_write_text",
                side_effect=fail_report_write,
            ):
                with self.assertRaisesRegex(OSError, "simulated shift report failure"):
                    dual_srt.command_shift(args)

            self.assertEqual(original_source, source.read_bytes())
            self.assertEqual(original_report, report.read_bytes())

if __name__ == "__main__":
    unittest.main()
