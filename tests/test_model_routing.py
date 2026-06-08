from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from job_bot.ai_client import AIClient
from job_bot.config import Settings, load_settings
from job_bot.filter_ai import FilterAI
from job_bot.models import LinkCandidate
from job_bot.telegram_config import load_telegram_settings


def _fake_response(payload: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps(payload),
                )
            )
        ]
    )


class ModelEnvLoadingTests(unittest.TestCase):
    def test_load_settings_reads_split_model_env_vars(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cwd = Path(temp_dir)
            (cwd / "sites.txt").write_text("https://example.com/jobs\n", encoding="utf-8")
            env_file = cwd / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "OPENAI_API_KEY=test-key",
                        "OPENAI_MODEL=gpt-5.4-mini",
                        "OPENAI_MODEL_WEBSITE_GUARD=gpt-5.4-nano",
                        "OPENAI_MODEL_LINK_RANKING=gpt-5.4-nano",
                        "OPENAI_MODEL_KEYWORD_EXPANSION=gpt-5.4-nano",
                        "OPENAI_MODEL_EXTRACTION=gpt-5.4-mini",
                        "OPENAI_MODEL_POST_AGE=gpt-5.4-mini",
                        "OPENAI_MODEL_FILTER_MATCH=gpt-5.4-mini",
                        f"SCRAPE_SITES_FILE={cwd / 'sites.txt'}",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                settings = load_settings(cwd)

        self.assertEqual(settings.openai_model, "gpt-5.4-mini")
        self.assertEqual(settings.openai_model_website_guard, "gpt-5.4-nano")
        self.assertEqual(settings.openai_model_link_ranking, "gpt-5.4-nano")
        self.assertEqual(settings.openai_model_keyword_expansion, "gpt-5.4-nano")
        self.assertEqual(settings.openai_model_extraction, "gpt-5.4-mini")
        self.assertEqual(settings.openai_model_post_age, "gpt-5.4-mini")
        self.assertEqual(settings.openai_model_filter_match, "gpt-5.4-mini")

    def test_load_telegram_settings_reads_split_model_env_vars(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cwd = Path(temp_dir)
            env_file = cwd / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "TELEGRAM_BOT_TOKEN=test-token",
                        "OPENAI_API_KEY=test-key",
                        "OPENAI_MODEL=gpt-5.4-mini",
                        "OPENAI_MODEL_WEBSITE_GUARD=gpt-5.4-nano",
                        "OPENAI_MODEL_KEYWORD_EXPANSION=gpt-5.4-nano",
                        "OPENAI_MODEL_FILTER_MATCH=gpt-5.4-mini",
                        "NO_POSTS_REPORT_MINUTES=180",
                        f"STATE_DB_PATH={cwd / 'job_bot_state.db'}",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                settings = load_telegram_settings(cwd)

        self.assertEqual(settings.openai_model, "gpt-5.4-mini")
        self.assertEqual(settings.openai_model_website_guard, "gpt-5.4-nano")
        self.assertEqual(settings.openai_model_keyword_expansion, "gpt-5.4-nano")
        self.assertEqual(settings.openai_model_filter_match, "gpt-5.4-mini")
        self.assertEqual(settings.no_posts_report_minutes, 180)
        self.assertEqual(settings.state_db_path, cwd / "job_bot_state.db")

    def test_load_telegram_settings_reads_subscription_admin_chat_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cwd = Path(temp_dir)
            env_file = cwd / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "TELEGRAM_BOT_TOKEN=test-token",
                        "SUBSCRIPTION_ADMIN_CHAT_IDS=100000001,100000002",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                settings = load_telegram_settings(cwd)

        self.assertEqual(settings.subscription_admin_chat_ids, (100000001, 100000002))


class AIClientModelRoutingTests(unittest.TestCase):
    @staticmethod
    def _settings() -> Settings:
        return Settings(
            openai_api_key="test-key",
            openai_model="gpt-5.4-mini",
            websites=["https://example.com/jobs"],
            agent_count=1,
            cycle_seconds=60,
            max_cards_per_cycle=3,
            seen_streak_stop=3,
            max_candidates_per_site=10,
            max_seed_pages_per_site=2,
            state_db_path=Path("state/test.db"),
            output_file_path=Path("output/test.txt"),
            headless_browser=True,
            log_level="INFO",
            request_timeout_ms=1000,
            verify_ssl=True,
            telegram_bot_token="",
            telegram_chat_id="",
            telegram_subs_db_path=Path("state/subs.db"),
            telegram_user_log_dir=Path("state/user_logs"),
            websites_file_path=Path("sites.txt"),
            neglect_post_if_filters_miss=False,
            enable_human_review_queue=True,
            human_review_confidence_threshold=0.62,
            openai_model_link_ranking="gpt-5.4-nano",
            openai_model_extraction="gpt-5.4-mini",
            openai_model_post_age="gpt-5.4-mini",
            openai_model_keyword_expansion="gpt-5.4-nano",
            openai_model_filter_match="gpt-5.4-mini",
            openai_model_website_guard="gpt-5.4-nano",
        )

    def test_ai_client_routes_calls_to_expected_models(self) -> None:
        models_used: list[str] = []

        def fake_completion(*args, **kwargs):
            del args
            models_used.append(str(kwargs["model"]))
            if kwargs["model"] == "gpt-5.4-nano":
                return _fake_response({"ordered_urls": ["https://example.com/jobs/1"]})
            if len(models_used) <= 2:
                return _fake_response(
                    {
                        "is_job_post": True,
                        "confidence": 0.9,
                        "title": "Backend Engineer",
                        "description": "Build APIs",
                        "salary": "Unknown",
                        "location": "Remote",
                        "posted_at_utc": "",
                        "notes": "",
                    }
                )
            return _fake_response(
                {
                    "older_than_limit": False,
                    "confidence": 0.8,
                    "detected_posted_at_utc": "",
                    "reason": "fresh enough",
                }
            )

        client = AIClient(self._settings(), logging.getLogger("test-ai-client-routing"))
        with patch("job_bot.ai_client.create_chat_completion_with_fallback", side_effect=fake_completion):
            ranked = client._rank_links_sync(
                "https://example.com/jobs",
                [
                    LinkCandidate(
                        url="https://example.com/jobs/1",
                        anchor_text="Backend Engineer",
                        source_page="https://example.com/jobs",
                        structural_score=1.0,
                    )
                ],
            )
            card = client._extract_job_card_sync(
                "https://example.com/jobs",
                "https://example.com/jobs/1",
                "Backend Engineer",
                "<html></html>",
                "",
            )
            age = client._assess_post_age_sync(
                "https://example.com/jobs",
                "https://example.com/jobs/1",
                "Posted today",
                "<html></html>",
                7,
                "2026-03-26T00:00:00Z",
            )

        self.assertEqual(ranked, ["https://example.com/jobs/1"])
        self.assertIsNotNone(card)
        self.assertFalse(age.older_than_limit)
        self.assertEqual(models_used, ["gpt-5.4-nano", "gpt-5.4-mini", "gpt-5.4-mini"])

    def test_ai_client_escalates_low_confidence_extraction_only_when_needed(self) -> None:
        models_used: list[str] = []

        def fake_completion(*args, **kwargs):
            del args
            models_used.append(str(kwargs["model"]))
            if len(models_used) == 1:
                return _fake_response(
                    {
                        "is_job_post": True,
                        "confidence": 0.41,
                        "title": "Backend Engineer",
                        "description": "Unknown",
                        "salary": "Unknown",
                        "location": "Remote",
                        "posted_at_utc": "",
                        "notes": "",
                    }
                )
            return _fake_response(
                {
                    "is_job_post": True,
                    "confidence": 0.88,
                    "title": "Backend Engineer",
                    "description": "Build APIs",
                    "salary": "Unknown",
                    "location": "Remote",
                    "posted_at_utc": "",
                    "notes": "",
                }
            )

        client = AIClient(self._settings(), logging.getLogger("test-ai-client-routing"))
        with patch("job_bot.ai_client.create_chat_completion_with_fallback", side_effect=fake_completion):
            card = client._extract_job_card_sync(
                "https://example.com/jobs",
                "https://example.com/jobs/1",
                "Backend Engineer",
                "<html></html>",
                "",
            )

        self.assertIsNotNone(card)
        assert card is not None
        self.assertGreater(card.confidence, 0.75)
        self.assertEqual(models_used, ["gpt-5.4-mini", "gpt-5.4-mini"])


    def test_ai_client_rejects_multi_job_feed_cards(self) -> None:
        client = AIClient(self._settings(), logging.getLogger("test-ai-client-routing"))

        card = client._job_card_from_json(
            {
                "is_job_post": True,
                "confidence": 0.85,
                "title": "Full-Stack Jobs Feed",
                "description": "RSS feed of remote full-stack, product designer, and SAP roles with apply links.",
                "salary": "Unknown",
                "location": "Anywhere",
                "posted_at_utc": "",
                "notes": "aggregated feed",
            },
            "https://weworkremotely.com",
            "https://weworkremotely.com/remote-jobs/feed",
            "test",
            fallback_language="en",
        )

        self.assertIsNotNone(card)
        assert card is not None
        self.assertFalse(card.is_job_post)

    def test_ai_client_keeps_zero_confidence_for_sparse_valid_job_cards(self) -> None:
        client = AIClient(self._settings(), logging.getLogger("test-ai-client-routing"))

        card = client._job_card_from_json(
            {
                "is_job_post": True,
                "confidence": 0.0,
                "title": "Backend Engineer",
                "description": "Build APIs for customers.",
                "salary": "Unknown",
                "location": "Remote",
                "company": "Acme",
                "posted_at_utc": "",
                "notes": "role-specific posting",
            },
            "https://example.com/jobs",
            "https://example.com/jobs/backend-engineer",
            "test",
            fallback_language="en",
        )

        self.assertIsNotNone(card)
        assert card is not None
        self.assertEqual(card.confidence, 0.0)


class FilterAIModelRoutingTests(unittest.TestCase):
    def test_filter_ai_routes_keywords_and_final_match_to_expected_models(self) -> None:
        models_used: list[str] = []

        def fake_completion(*args, **kwargs):
            del args
            models_used.append(str(kwargs["model"]))
            model = str(kwargs["model"])
            if model == "gpt-5.4-nano":
                return _fake_response(
                    {
                        "items": [
                            {
                                "input_keyword": "ux",
                                "understood_as": "ux",
                                "similar_keywords": ["user experience"],
                            }
                        ],
                        "expanded_keywords": ["ux", "user experience"],
                    }
                )
            if len(models_used) == 1:
                return _fake_response(
                    {
                        "country": "Egypt",
                        "state": "Cairo Governorate",
                        "city": "Cairo",
                        "canonical": "Cairo, Egypt",
                        "confidence": 0.9,
                    }
                )
            return _fake_response(
                {
                    "match": True,
                    "reason": "strong fit",
                    "failed_filters": [],
                }
            )

        ai = FilterAI(
            openai_api_key="test-key",
            openai_model="gpt-5.4-mini",
            logger=logging.getLogger("test-filter-ai-routing"),
            keyword_model="gpt-5.4-nano",
            final_match_model="gpt-5.4-mini",
        )

        with patch("job_bot.filter_ai.create_chat_completion_with_fallback", side_effect=fake_completion):
            location = ai._normalize_location_sync("Cairo")
            interpretation = ai._interpret_keywords_sync(["ux"])
            matched, reason = ai._confirm_post_matches_filters_sync(
                post_x={"title": "UX Designer"},
                user_filters={"keywords": ["ux"]},
                precheck_results={"role": {"matched": True, "reason": "ok"}},
            )

        self.assertEqual(location.country, "Egypt")
        self.assertIn("user experience", interpretation.expanded_keywords)
        self.assertTrue(matched)
        self.assertEqual(reason, "strong fit")
        self.assertEqual(models_used, ["gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.4-mini"])
