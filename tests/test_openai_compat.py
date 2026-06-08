from __future__ import annotations

import logging
import time
import unittest
from unittest.mock import patch

from job_bot import openai_compat
from job_bot.openai_compat import create_chat_completion_with_fallback


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str = "{}") -> None:
        self.choices = [_FakeChoice(content)]


class OpenAICompatTests(unittest.TestCase):
    def setUp(self) -> None:
        openai_compat._UNSUPPORTED_PARAMS_BY_MODEL.clear()

    def test_retry_removes_temperature_then_response_format_when_model_rejects_them(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_create(**kwargs):
            calls.append(dict(kwargs))
            if len(calls) == 1:
                raise Exception(
                    "Error code: 400 - {'error': {'message': \"Unsupported value: 'temperature' does not support 0 "
                    "with this model. Only the default (1) value is supported.\"}}"
                )
            if len(calls) == 2:
                raise Exception("response_format is not supported with this model")
            return _FakeResponse('{"ok": true}')

        response = create_chat_completion_with_fallback(
            fake_create,
            model="gpt-4o-mini",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": "{}"},
            ],
            logger=logging.getLogger("test-openai-compat"),
            log_label="test-openai-compat",
        )

        self.assertIsInstance(response, _FakeResponse)
        self.assertEqual(len(calls), 3)
        self.assertIn("temperature", calls[0])
        self.assertIn("response_format", calls[0])
        self.assertNotIn("temperature", calls[1])
        self.assertIn("response_format", calls[1])
        self.assertNotIn("temperature", calls[2])
        self.assertNotIn("response_format", calls[2])

    def test_gpt_5_models_skip_temperature_upfront(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_create(**kwargs):
            calls.append(dict(kwargs))
            return _FakeResponse('{"ok": true}')

        response = create_chat_completion_with_fallback(
            fake_create,
            model="gpt-5-mini",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": "{}"},
            ],
            logger=logging.getLogger("test-openai-compat"),
            log_label="test-openai-compat",
        )

        self.assertIsInstance(response, _FakeResponse)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("temperature", calls[0])
        self.assertIn("response_format", calls[0])

    def test_retries_transient_timeout_with_backoff(self) -> None:
        calls: list[dict[str, object]] = []

        def fake_create(**kwargs):
            calls.append(dict(kwargs))
            if len(calls) == 1:
                raise Exception("request timed out while contacting OpenAI")
            return _FakeResponse('{"ok": true}')

        with patch("job_bot.openai_compat.time.sleep") as mocked_sleep:
            response = create_chat_completion_with_fallback(
                fake_create,
                model="gpt-4o-mini",
                temperature=0,
                response_format={"type": "json_object"},
                timeout_seconds=12,
                retry_budget=1,
                messages=[
                    {"role": "system", "content": "Return JSON only."},
                    {"role": "user", "content": "{}"},
                ],
                logger=logging.getLogger("test-openai-compat"),
                log_label="test-openai-compat",
            )

        self.assertIsInstance(response, _FakeResponse)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["timeout"], 12.0)
        mocked_sleep.assert_called_once()

    def test_strict_timeout_aborts_slow_openai_call(self) -> None:
        def fake_create(**kwargs):
            del kwargs
            time.sleep(0.2)
            return _FakeResponse('{"ok": true}')

        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            create_chat_completion_with_fallback(
                fake_create,
                model="gpt-4o-mini",
                temperature=0,
                response_format={"type": "json_object"},
                timeout_seconds=0.05,
                retry_budget=0,
                messages=[
                    {"role": "system", "content": "Return JSON only."},
                    {"role": "user", "content": "{}"},
                ],
                logger=logging.getLogger("test-openai-compat"),
                log_label="test-openai-compat",
            )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.15)


if __name__ == "__main__":
    unittest.main()
