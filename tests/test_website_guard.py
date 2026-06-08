from __future__ import annotations

import logging
import unittest

from job_bot.website_guard import WebsiteGuard


class WebsiteGuardHeuristicTests(unittest.TestCase):
    def test_rejects_talent_directory_urls(self) -> None:
        guard = WebsiteGuard(openai_api_key="", openai_model="gpt-5-mini", logger=logging.getLogger("test-guard"))

        review = guard._review_sync("https://example.com/search/talent?nbs=1&q=uxui")

        self.assertFalse(review.accepted)
        self.assertIn("talent/profile directory", review.reason)

    def test_accepts_regular_job_search_urls(self) -> None:
        guard = WebsiteGuard(openai_api_key="", openai_model="gpt-5-mini", logger=logging.getLogger("test-guard"))

        review = guard._review_sync("https://example.com/search/jobs?nbs=1&q=uxui&sort=recency")

        self.assertTrue(review.accepted)

    def test_accepts_russian_job_search_urls(self) -> None:
        guard = WebsiteGuard(openai_api_key="", openai_model="gpt-5-mini", logger=logging.getLogger("test-guard"))

        review = guard._review_sync("https://workspace.ru/web-design/")

        self.assertTrue(review.accepted)


    def test_accepts_generic_russian_search_url(self) -> None:
        guard = WebsiteGuard(openai_api_key="", openai_model="gpt-5-mini", logger=logging.getLogger("test-guard"))

        review = guard._review_sync("https://jobs.example/search?query=%D0%B4%D0%B8%D0%B7%D0%B0%D0%B9%D0%BD%D0%B5%D1%80")

        self.assertTrue(review.accepted)


if __name__ == "__main__":
    unittest.main()
