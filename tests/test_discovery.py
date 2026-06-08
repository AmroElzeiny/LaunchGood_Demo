from __future__ import annotations

import asyncio
import logging
import re
import sys
import types
import unittest
from pathlib import Path

_scrapling_stub = types.ModuleType("scrapling")
_scrapling_stub.DynamicFetcher = object
_scrapling_stub.Fetcher = object
_scrapling_stub.StealthyFetcher = object
sys.modules.setdefault("scrapling", _scrapling_stub)

from job_bot.config import Settings
from job_bot.discovery import discover_candidates
from job_bot.fetching import FetchResult
from job_bot.page_classifier import classify_page_kind
from job_bot.storage import DiscoveryFeedback


def _settings() -> Settings:
    return Settings(
        openai_api_key="test-key",
        openai_model="gpt-5-mini",
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
    )


class _FakeAIClient:
    async def rank_links(self, website, candidates):
        del website, candidates
        return []


class _FakeFetcher:
    def __init__(self, page_map: dict[str, FetchResult]) -> None:
        self.page_map = page_map

    async def fetch(self, url: str) -> FetchResult | None:
        return self.page_map.get(url)


class _FakeTextSelection:
    def __init__(self, values: list[str]) -> None:
        self.values = values

    def getall(self) -> list[str]:
        return list(self.values)

    def get(self) -> str | None:
        return self.values[0] if self.values else None


class _FakeAnchor:
    def __init__(self, href: str, text: str) -> None:
        self.attrib = {"href": href}
        self._text = text

    def css(self, query: str) -> _FakeTextSelection:
        if query == "::text":
            return _FakeTextSelection([self._text] if self._text else [])
        raise ValueError(f"unsupported selector: {query}")


class _FakePage:
    def __init__(self, html: str, selector_map: dict[str, list[_FakeAnchor]] | None = None) -> None:
        self.html = html
        self._anchors = [
            _FakeAnchor(match.group("href"), re.sub(r"\s+", " ", match.group("text")).strip())
            for match in re.finditer(
                r"<a[^>]+href=['\"](?P<href>[^'\"]+)['\"][^>]*>(?P<text>.*?)</a>",
                html,
                flags=re.IGNORECASE | re.DOTALL,
            )
        ]
        self._body_text = re.sub(r"<[^>]+>", " ", html)
        self._selector_map = dict(selector_map or {})

    def css(self, query: str):
        if query in self._selector_map:
            return list(self._selector_map[query])
        if query == "a":
            return list(self._anchors)
        if query == "body ::text":
            return _FakeTextSelection([self._body_text])
        raise ValueError(f"unsupported selector: {query}")


def _page(url: str, html: str, *, selector_map: dict[str, list[_FakeAnchor]] | None = None) -> FetchResult:
    return FetchResult(
        url=url,
        status=200,
        html=html,
        page=_FakePage(html, selector_map=selector_map),
        strategy="test",
    )


class DiscoveryTests(unittest.TestCase):
    def test_discover_candidates_records_challenge_blocked_when_root_fetch_is_blocked(self) -> None:
        website = "https://jobs.example/search"
        recorded_events: list[tuple[str, str, str]] = []

        class _ChallengeFetcher:
            async def fetch(self, url: str):
                del url
                return None

            def last_failure_reason(self, url: str) -> str:
                del url
                return "challenge_or_interstitial"

        result = asyncio.run(
            discover_candidates(
                website=website,
                fetcher=_ChallengeFetcher(),  # type: ignore[arg-type]
                ai_client=_FakeAIClient(),  # type: ignore[arg-type]
                settings=_settings(),
                logger=logging.getLogger("test-discovery"),
                discovery_event_logger=lambda site, url, status: recorded_events.append((site, url, status)),
            )
        )

        self.assertEqual(result.fetched_pages, 0)
        self.assertEqual(result.candidates, [])
        self.assertEqual(recorded_events, [(website, website, "challenge_blocked")])

    def test_discover_candidates_uses_fallback_when_interactive_page_is_empty(self) -> None:
        website = "https://www.linkedin.com/jobs/ux-designer-jobs-worldwide"
        interactive_root = _page(
            website,
            "<html><body><h1>Sign in</h1><p>Jobs</p></body></html>",
        )
        fallback_root = _page(
            website,
            """
            <html><body>
                <a href="/jobs/view/1">First Role</a>
                <a href="/jobs/view/2">Second Role</a>
                <a href="/jobs/view/3">Third Role</a>
            </body></html>
            """,
        )
        fetcher = _FakeFetcher({website: fallback_root})

        async def interactive_fetch(url: str):
            self.assertEqual(url, website)
            return interactive_root

        result = asyncio.run(
            discover_candidates(
                website=website,
                fetcher=fetcher,  # type: ignore[arg-type]
                ai_client=_FakeAIClient(),  # type: ignore[arg-type]
                settings=_settings(),
                logger=logging.getLogger("test-discovery"),
                interactive_fetch=interactive_fetch,
            )
        )

        self.assertEqual(result.fetched_pages, 2)
        self.assertEqual(len(result.candidates), 3)
        self.assertEqual(
            [candidate.url for candidate in result.candidates],
            [
                "https://www.linkedin.com/jobs/view/1",
                "https://www.linkedin.com/jobs/view/2",
                "https://www.linkedin.com/jobs/view/3",
            ],
        )

    def test_weworkremotely_remote_job_urls_classify_as_job_posts(self) -> None:
        self.assertEqual(
            classify_page_kind("https://weworkremotely.com/remote-jobs/contra-product-designer-contra-labs-network"),
            "job_post",
        )

    def test_ziprecruiter_job_urls_classify_as_job_posts(self) -> None:
        self.assertEqual(
            classify_page_kind("https://www.ziprecruiter.ie/jobs/517979886-engineer-2-labelling-assurance-at-cook-group"),
            "job_post",
        )

    def test_indeed_viewjob_urls_classify_as_job_posts(self) -> None:
        self.assertEqual(
            classify_page_kind("https://www.indeed.com/viewjob?jk=f1e2d3c4b5a67890"),
            "job_post",
        )

    def test_project_and_contract_urls_classify_as_project_posts(self) -> None:
        self.assertEqual(
            classify_page_kind("https://example.com/projects/mobile-app-redesign-42"),
            "project_post",
        )
        self.assertEqual(
            classify_page_kind("https://example.com/contracts/fractional-product-design-lead"),
            "project_post",
        )

    def test_project_board_urls_classify_as_project_search_results(self) -> None:
        self.assertEqual(
            classify_page_kind("https://example.com/projects?category=design"),
            "project_search_results",
        )
        self.assertEqual(
            classify_page_kind(
                "https://example.com/board/design-projects",
                text="Latest freelance projects, briefs, and open scopes",
            ),
            "project_search_results",
        )

    def test_rfp_and_consulting_urls_classify_correctly(self) -> None:
        self.assertEqual(
            classify_page_kind("https://example.com/rfp/design-system-modernization"),
            "request_for_proposal",
        )
        self.assertEqual(
            classify_page_kind(
                "https://example.com/consulting/brief/saas-product-audit",
                text="Consulting brief for a product audit and scope definition",
            ),
            "consulting_brief",
        )

    def test_mixed_opportunity_feed_classifies_without_job_only_bias(self) -> None:
        self.assertEqual(
            classify_page_kind(
                "https://example.com/opportunities",
                text="Projects, contracts, freelance gigs, and selective roles",
            ),
            "mixed_opportunity_feed",
        )

    def test_workspace_search_and_project_urls_classify_correctly(self) -> None:
        self.assertEqual(
            classify_page_kind(
                "https://workspace.ru/web-design/",
                text="Тендеры на веб-дизайн Фильтр Сортировка",
            ),
            "project_search_results",
        )
        self.assertEqual(
            classify_page_kind("https://workspace.ru/tenders/redizayn-sayta-123456"),
            "request_for_proposal",
        )

    def test_fl_search_and_project_urls_classify_correctly(self) -> None:
        self.assertEqual(
            classify_page_kind(
                "https://www.fl.ru/projects/?kind=1",
                text="Вакансии дизайнер ux Фильтр Сортировка Откликнуться",
            ),
            "project_search_results",
        )
        self.assertEqual(
            classify_page_kind("https://www.fl.ru/projects/1234567/landing-page-redesign.html"),
            "project_post",
        )

    def test_supported_launch_sources_classify_correctly(self) -> None:
        self.assertEqual(classify_page_kind("https://www.workatastartup.com/jobs"), "search_results")
        self.assertEqual(
            classify_page_kind("https://www.workatastartup.com/companies/acme/jobs/product-designer"),
            "job_post",
        )
        self.assertEqual(classify_page_kind("https://www.ycombinator.com/jobs"), "search_results")
        self.assertEqual(
            classify_page_kind("https://www.ycombinator.com/companies/acme/jobs/product-designer"),
            "job_post",
        )
        self.assertEqual(classify_page_kind("https://wellfound.com/role/ui-ux-designer"), "search_results")
        self.assertEqual(
            classify_page_kind("https://wellfound.com/company/acme/jobs/123-product-designer"),
            "job_post",
        )
        self.assertEqual(
            classify_page_kind("https://builtin.com/jobs/remote/hybrid/office/designer?search=Web+Designer&allLocations=true"),
            "search_results",
        )
        self.assertEqual(
            classify_page_kind("https://www.flexjobs.com/remote-jobs/ux-designer?sortbyposteddate=true&page=1"),
            "search_results",
        )

    def test_russian_project_boards_classify_correctly(self) -> None:
        self.assertEqual(classify_page_kind("https://www.fl.ru/projects/"), "project_search_results")
        self.assertEqual(classify_page_kind("https://www.fl.ru/projects/?kind=1"), "project_search_results")
        self.assertEqual(
            classify_page_kind("https://www.fl.ru/projects/5497585/landing-page-redesign.html"),
            "project_post",
        )
        self.assertEqual(classify_page_kind("https://freelance.ru/project/search"), "project_search_results")
        self.assertEqual(
            classify_page_kind("https://freelance.ru/projects/ux-ui-redesign-1665463.html"),
            "project_post",
        )
        self.assertEqual(classify_page_kind("https://freelance.ru/tender/view/1276"), "request_for_proposal")
        self.assertEqual(classify_page_kind("https://workspace.ru/tenders/"), "project_search_results")
        self.assertEqual(classify_page_kind("https://workspace.ru/web-design/"), "project_search_results")
        self.assertEqual(
            classify_page_kind("https://workspace.ru/tenders/ux-audit-and-redesign-18451"),
            "request_for_proposal",
        )
        self.assertEqual(classify_page_kind("https://kwork.ru/projects"), "project_search_results")
        self.assertEqual(classify_page_kind("https://kwork.ru/projects/987654/view"), "project_post")

    def test_discover_candidates_extracts_embedded_job_urls_from_js_heavy_feeds(self) -> None:
        website = "https://www.ycombinator.com/jobs"
        fetcher = _FakeFetcher(
            {
                website: _page(
                    website,
                    """
                    <html><body>
                    <script>
                    window.__JOBS__ = {"jobPostings":[
                        {"title":"Product Designer","url":"/companies/acme/jobs/product-designer-123"},
                        {"title":"UX Designer","url":"/companies/acme/jobs/ux-designer-456"}
                    ]};
                    </script>
                    </body></html>
                    """,
                )
            }
        )

        result = asyncio.run(
            discover_candidates(
                website=website,
                fetcher=fetcher,  # type: ignore[arg-type]
                ai_client=_FakeAIClient(),  # type: ignore[arg-type]
                settings=_settings(),
                logger=logging.getLogger("test-discovery"),
            )
        )

        self.assertEqual(
            [candidate.url for candidate in result.candidates],
            [
                "https://www.ycombinator.com/companies/acme/jobs/product-designer-123",
                "https://www.ycombinator.com/companies/acme/jobs/ux-designer-456",
            ],
        )

    def test_discover_candidates_extracts_russian_embedded_project_urls_from_js_heavy_feed(self) -> None:
        website = "https://kwork.ru/projects"
        fetcher = _FakeFetcher(
            {
                website: _page(
                    website,
                    """
                    <html><body>
                    <script>
                    window.__PROJECTS__ = {
                        "items": [
                            {"title":"UX аудит","url":"/projects/987654/view"},
                            {"title":"Figma cleanup","url":"/projects/123456/view"}
                        ]
                    };
                    </script>
                    </body></html>
                    """,
                )
            }
        )

        result = asyncio.run(
            discover_candidates(
                website=website,
                fetcher=fetcher,  # type: ignore[arg-type]
                ai_client=_FakeAIClient(),  # type: ignore[arg-type]
                settings=_settings(),
                logger=logging.getLogger("test-discovery"),
            )
        )

        self.assertEqual(
            [candidate.url for candidate in result.candidates],
            [
                "https://kwork.ru/projects/987654/view",
                "https://kwork.ru/projects/123456/view",
            ],
        )

    def test_discover_candidates_caps_results_at_100_per_site(self) -> None:
        website = "https://example.com/jobs"
        links_html = "\n".join(
            f'<a href="/jobs/view/{index}">Role {index}</a>'
            for index in range(120)
        )
        fetcher = _FakeFetcher(
            {
                website: _page(
                    website,
                    f"<html><body>{links_html}</body></html>",
                )
            }
        )
        settings = _settings()
        settings.max_candidates_per_site = 150

        result = asyncio.run(
            discover_candidates(
                website=website,
                fetcher=fetcher,  # type: ignore[arg-type]
                ai_client=_FakeAIClient(),  # type: ignore[arg-type]
                settings=settings,
                logger=logging.getLogger("test-discovery"),
            )
        )

        self.assertEqual(len(result.candidates), 100)

    def test_discover_candidates_skips_known_bad_source_pages(self) -> None:
        website = "https://example.com/jobs"
        fetcher = _FakeFetcher(
            {
                website: _page(
                    website,
                    """
                    <html><body>
                        <a href="/filters">Filters</a>
                        <a href="/jobs/view/1">Role 1</a>
                    </body></html>
                    """,
                )
            }
        )

        result = asyncio.run(
            discover_candidates(
                website=website,
                fetcher=fetcher,  # type: ignore[arg-type]
                ai_client=_FakeAIClient(),  # type: ignore[arg-type]
                settings=_settings(),
                logger=logging.getLogger("test-discovery"),
                discovery_feedback=DiscoveryFeedback(blocked_source_urls=("https://example.com/filters",)),
            )
        )

        self.assertEqual([candidate.url for candidate in result.candidates], ["https://example.com/jobs/view/1"])

    def test_discover_candidates_uses_site_profile_selectors_for_workspace_ru(self) -> None:
        website = "https://workspace.ru/web-design/"
        fetcher = _FakeFetcher(
            {
                website: _page(
                    website,
                    "<html><body><div>workspace tenders</div></body></html>",
                    selector_map={
                        "a[href*='/tenders/'][href*='-']": [
                            _FakeAnchor("/tenders/redizayn-sayta-123456", "Редизайн сайта"),
                        ],
                        "a": [],
                    },
                )
            }
        )

        result = asyncio.run(
            discover_candidates(
                website=website,
                fetcher=fetcher,  # type: ignore[arg-type]
                ai_client=_FakeAIClient(),  # type: ignore[arg-type]
                settings=_settings(),
                logger=logging.getLogger("test-discovery"),
            )
        )

        self.assertEqual([candidate.url for candidate in result.candidates], ["https://workspace.ru/tenders/redizayn-sayta-123456"])


    def test_generic_russian_board_classifies_without_domain_specific_rules(self) -> None:
        self.assertEqual(
            classify_page_kind(
                "https://jobs.example/search?query=%D0%B4%D0%B8%D0%B7%D0%B0%D0%B9%D0%BD%D0%B5%D1%80",
                text=(
                    "\u041d\u0430\u0439\u0434\u0435\u043d\u043e 15 "
                    "\u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0439 "
                    "\u0424\u0438\u043b\u044c\u0442\u0440 "
                    "\u0421\u043e\u0440\u0442\u0438\u0440\u043e\u0432\u043a\u0430"
                ),
            ),
            "search_results",
        )
        self.assertEqual(
            classify_page_kind(
                "https://jobs.example/vacancy/product-designer-482",
                text=(
                    "\u041e\u043f\u0438\u0441\u0430\u043d\u0438\u0435 "
                    "\u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0438 "
                    "\u041e\u0431\u044f\u0437\u0430\u043d\u043d\u043e\u0441\u0442\u0438 "
                    "\u0422\u0440\u0435\u0431\u043e\u0432\u0430\u043d\u0438\u044f"
                ),
            ),
            "job_post",
        )


if __name__ == "__main__":
    unittest.main()
