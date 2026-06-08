from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from openai import OpenAI

from job_bot.language_utils import normalize_match_text
from job_bot.openai_compat import create_chat_completion_with_fallback
from job_bot.page_classifier import classify_page_kind, is_opportunity_detail_kind, is_opportunity_feed_kind


SUSPICIOUS_TOKENS = {
    "malware",
    "phishing",
    "crack",
    "keygen",
    "torrent",
    ".exe",
    ".apk",
    ".bat",
}
JOB_HINT_TOKENS = {
    "job",
    "jobs",
    "career",
    "careers",
    "freelance",
    "project",
    "projects",
    "contract",
    "consulting",
    "rfp",
    "brief",
    "proposal",
    "vacancy",
    "vacancies",
    "hiring",
    "work",
    "\u0440\u0430\u0431\u043e\u0442\u0430",
    "\u0432\u0430\u043a\u0430\u043d\u0441\u0438\u044f",
    "\u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0438",
    "\u043a\u0430\u0440\u044c\u0435\u0440\u0430",
    "\u0444\u0440\u0438\u043b\u0430\u043d\u0441",
    "\u043d\u0430\u0439\u043c",
}
SEARCH_RESULTS_HINT_TOKENS = {
    "search",
    "results",
    "jobs",
    "projects",
    "contracts",
    "opportunities",
    "briefs",
    "requests",
    "careers",
    "listings",
    "filter",
    "sort",
    "page",
    "\u043f\u043e\u0438\u0441\u043a",
    "\u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u044b",
    "\u0444\u0438\u043b\u044c\u0442\u0440",
    "\u0441\u043e\u0440\u0442\u0438\u0440\u043e\u0432\u043a\u0430",
    "\u043d\u0430\u0439\u0434\u0435\u043d\u043e",
}
NON_JOB_DIRECTORY_URL_HINTS = (
    "/search/talent",
    "/talent/",
    "/candidate/",
    "/candidates/",
    "/profile/",
    "/profiles/",
    "/people/",
    "/resume/",
    "/resumes/",
)
NON_JOB_DIRECTORY_PAGE_HINTS = (
    "browse talent",
    "candidate profile",
    "candidate profiles",
    "find talent",
    "freelancer profile",
    "hire talent",
    "people directory",
    "talent marketplace",
    "talent profiles",
    "top freelancers",
    "\u0431\u0430\u0437\u0430 \u0440\u0435\u0437\u044e\u043c\u0435",
    "\u0440\u0435\u0437\u044e\u043c\u0435 \u043a\u0430\u043d\u0434\u0438\u0434\u0430\u0442\u043e\u0432",
    "\u043f\u043e\u0438\u0441\u043a \u0440\u0435\u0437\u044e\u043c\u0435",
)


@dataclass(slots=True)
class WebsiteReview:
    accepted: bool
    reason: str


class WebsiteGuard:
    def __init__(self, openai_api_key: str, openai_model: str, logger: logging.Logger) -> None:
        self.logger = logger
        self.openai_api_key = openai_api_key.strip()
        self.openai_model = openai_model.strip() or "gpt-4o-mini"
        self.client = OpenAI(api_key=self.openai_api_key) if self.openai_api_key else None

    async def review(self, website_url: str) -> WebsiteReview:
        return await asyncio.to_thread(self._review_sync, website_url)

    def _review_sync(self, website_url: str) -> WebsiteReview:
        parsed = urlparse(website_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return WebsiteReview(accepted=False, reason="URL format is invalid.")

        lowered_url = website_url.lower()
        if any(token in lowered_url for token in SUSPICIOUS_TOKENS):
            return WebsiteReview(accepted=False, reason="The URL looks suspicious for security reasons.")

        page_text = self._fetch_preview_text(website_url)
        lowered_page = page_text.lower()
        normalized_url = normalize_match_text(website_url)
        normalized_page = normalize_match_text(page_text)
        page_kind = classify_page_kind(website_url, html=page_text, text=page_text)

        if any(token in lowered_page for token in {"malware", "virus download", "phishing"}):
            return WebsiteReview(accepted=False, reason="The website content looks suspicious.")
        if self._looks_like_non_job_directory(website_url, page_text):
            return WebsiteReview(
                accepted=False,
                reason="It looks like a talent/profile directory, not an opportunity listings page we can monitor.",
            )

        heuristic_job_related = is_opportunity_feed_kind(page_kind) or is_opportunity_detail_kind(page_kind) or any(
            token in normalized_url or token in normalized_page for token in JOB_HINT_TOKENS
        )
        heuristic_search_results = self._looks_like_search_results_page(website_url, page_text)

        if self.client is None:
            if heuristic_job_related and heuristic_search_results:
                return WebsiteReview(accepted=True, reason="Accepted by heuristic safety/opportunity-feed checks.")
            if is_opportunity_detail_kind(page_kind):
                return WebsiteReview(
                    accepted=False,
                    reason="It looks like a single opportunity detail page, not a feed page the bot can monitor continuously.",
                )
            if not heuristic_search_results:
                return WebsiteReview(
                    accepted=False,
                    reason="It does not look like an opportunity feed or listing page we can monitor.",
                )
            return WebsiteReview(
                accepted=False,
                reason="It does not look clearly enough like an opportunity source we can monitor.",
            )

        try:
            response = create_chat_completion_with_fallback(
                self.client.chat.completions.create,
                model=self.openai_model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a strict, professional security and opportunity-source reviewer. "
                            "Return JSON only with fields: "
                            "safe(bool), job_related(bool), is_search_results_page(bool), reason(string)."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "website_url": website_url,
                                "page_preview": page_text[:6000],
                                "classifier_page_kind": page_kind,
                                "task": (
                                    "Accept only if safe, clearly opportunity-related in any language, "
                                    "and the URL/page is a search-results, careers, project-board, contract-board, "
                                    "request-board, or opportunity-listing page "
                                    "(not a home page, not a single opportunity detail page, and not a talent/profile/"
                                    "candidate directory)."
                                ),
                            }
                        ),
                    },
                ],
                logger=self.logger,
                log_label="website-guard",
            )
            raw = response.choices[0].message.content or "{}"
            data = json.loads(raw)
            safe = self._coerce_bool(data.get("safe", False))
            job_related = self._coerce_bool(data.get("job_related", False))
            is_search_results_page = self._coerce_bool(data.get("is_search_results_page", False))
            reason = str(data.get("reason", "")).strip() or "Rejected by AI policy check."
            if safe and job_related and is_search_results_page:
                return WebsiteReview(accepted=True, reason=reason)
            if safe and job_related and not is_search_results_page:
                return WebsiteReview(
                    accepted=False,
                    reason=(
                        reason
                        or "It does not look like an opportunity feed or listing page we can monitor."
                    ),
                )
            return WebsiteReview(accepted=False, reason=reason)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[website-guard] AI review failed: %s", str(exc))
            if heuristic_job_related and heuristic_search_results:
                return WebsiteReview(accepted=True, reason="Accepted by fallback heuristic.")
            if is_opportunity_detail_kind(page_kind):
                return WebsiteReview(
                    accepted=False,
                    reason="It looks like a single opportunity detail page, not a feed page the bot can monitor continuously.",
                )
            if not heuristic_search_results:
                return WebsiteReview(
                    accepted=False,
                    reason="It does not look like an opportunity feed or listing page we can monitor.",
                )
            return WebsiteReview(
                accepted=False,
                reason="It does not look clearly enough like an opportunity source we can monitor.",
            )

    @staticmethod
    def _coerce_bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1"}
        return False

    def _fetch_preview_text(self, website_url: str) -> str:
        request = urllib.request.Request(
            url=website_url,
            headers={"User-Agent": "ZapLanceBot/1.0"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
                content_type = str(response.headers.get("Content-Type", "")).lower()
                if "text" not in content_type and "html" not in content_type:
                    return ""
                return response.read(120_000).decode("utf-8", errors="replace")
        except (urllib.error.HTTPError, urllib.error.URLError):
            return ""

    @staticmethod
    def _looks_like_non_job_directory(website_url: str, page_text: str) -> bool:
        lower_url = website_url.lower()
        normalized_page = normalize_match_text(page_text)
        if any(token in lower_url for token in NON_JOB_DIRECTORY_URL_HINTS):
            return True
        page_kind = classify_page_kind(website_url, html=page_text, text=page_text)
        if is_opportunity_feed_kind(page_kind) or is_opportunity_detail_kind(page_kind):
            return False
        return any(token in normalized_page for token in NON_JOB_DIRECTORY_PAGE_HINTS)

    @staticmethod
    def _looks_like_search_results_page(website_url: str, page_text: str) -> bool:
        page_kind = classify_page_kind(website_url, html=page_text, text=page_text)
        if is_opportunity_feed_kind(page_kind):
            return True
        if is_opportunity_detail_kind(page_kind):
            return False

        parsed = urlparse(website_url.strip())
        normalized_url = normalize_match_text(website_url)
        normalized_path = normalize_match_text(parsed.path)
        normalized_query = normalize_match_text(parsed.query)
        normalized_page = normalize_match_text(page_text)

        score = 0
        if parsed.query:
            score += 1
        if any(token in normalized_url for token in SEARCH_RESULTS_HINT_TOKENS):
            score += 1
        if any(token in normalized_query for token in {"q", "query", "search", "sort", "page", "text"}):
            score += 1
        if any(
            token in normalized_path
            for token in {
                "search",
                "jobs",
                "projects",
                "contracts",
                "opportunities",
                "careers",
                "listing",
                "category",
                "role",
                "vacancies",
                "vacancy",
                "\u043f\u043e\u0438\u0441\u043a",
                "\u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0438",
                "\u0432\u0430\u043a\u0430\u043d\u0441\u0438\u044f",
            }
        ):
            score += 1
        if any(token in normalized_page for token in SEARCH_RESULTS_HINT_TOKENS):
            score += 1
        return score >= 2
