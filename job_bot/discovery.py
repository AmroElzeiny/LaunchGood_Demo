from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from html import unescape
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from job_bot.ai_client import AIClient
from job_bot.config import Settings
from job_bot.extractors import page_to_text
from job_bot.fetching import ResilientFetcher
from job_bot.models import LinkCandidate
from job_bot.page_classifier import (
    classify_page_kind,
    is_opportunity_detail_kind,
    is_opportunity_feed_kind,
    url_looks_like_search_or_job_page,
)
from job_bot.site_profiles import SiteProfile, get_site_profile, url_matches_profile_pattern
from job_bot.storage import DiscoveryFeedback


@dataclass(slots=True)
class DiscoveryResult:
    candidates: list[LinkCandidate]
    fetched_pages: int


STRONG_NEGATIVE_URL_PARTS = {
    "about",
    "community",
    "contact",
    "cookie",
    "cookies",
    "directory",
    "forgot-password",
    "guest-controls",
    "help",
    "legal",
    "login",
    "password",
    "policy",
    "privacy",
    "register",
    "security",
    "signin",
    "signup",
    "support",
    "terms",
}
WEAK_NEGATIVE_PATTERNS = (
    "/candidate/",
    "/candidates/",
    "/careers/",
    "/category/",
    "/categories/",
    "/community",
    "/company/",
    "/content/",
    "/create-company-account",
    "/directory/",
    "/forgot-password",
    "/hire",
    "/hubs/",
    "/jobs/jobs-in-",
    "/promo/",
    "/profile/",
    "/profiles/",
    "/remote-jobs/new",
    "/search",
    "/search/talent",
    "/talent/",
    "/talent/post-a-job",
)
POSITIVE_DETAIL_PATTERNS = (
    "/desc/",
    "/job/",
    "/jobs/view/",
    "/opportunities/",
    "/project/",
    "/projects/",
    "/contract/",
    "/contracts/",
    "/freelance/",
    "/gig/",
    "/gigs/",
    "/brief/",
    "/briefs/",
    "/rfp/",
    "/request-for-proposal",
    "/statement-of-work",
    "/scope-of-work",
    "/consulting/",
    "/proposal/",
    "/tender/view/",
    "/tenders/",
    "/position/",
    "/posting/",
    "/remote-jobs/",
    "/vacancy/",
)
DISCOVERY_SEARCH_FALLBACK_MIN_LINKS = 3
DISCOVERY_HARD_MAX_CANDIDATES_PER_SITE = 100
EMBEDDED_CANDIDATE_URL_PATTERNS = (
    re.compile(r"https?://[^\"'<>\\\s]+", flags=re.IGNORECASE),
    re.compile(
        r"/(?:companies/[^\"'<>\\\s]+/jobs/[^\"'<>\\\s]+|job/[^\"'<>\\\s]+|jobs/view/[^\"'<>\\\s]+|jobs/details/[^\"'<>\\\s]+|"
        r"vacancy/\d[^\"'<>\\\s]*|remote-jobs/[^\"'<>\\\s]+|role/[^\"'<>\\\s]+|projects/\d+/[^\"'<>\\\s]+\.html|"
        r"projects/[^\"'<>\\\s]+-\d+\.html|projects/\d+/view[^\"'<>\\\s]*|tenders/[^\"'<>\\\s]+-\d+/?|tender/view/\d[^\"'<>\\\s]*)",
        flags=re.IGNORECASE,
    ),
)


def _same_site(host_a: str, host_b: str) -> bool:
    host_a = host_a.lower().strip(".")
    host_b = host_b.lower().strip(".")
    if host_a == host_b:
        return True
    return host_a.endswith("." + host_b) or host_b.endswith("." + host_a)


def canonicalize_url(raw_url: str, base_url: str) -> str | None:
    resolved = urljoin(base_url, raw_url.strip())
    parsed = urlsplit(resolved)
    if parsed.scheme not in {"http", "https"}:
        return None

    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]

    cleaned_query_pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if not k.lower().startswith("utm_")
    ]
    query = urlencode(cleaned_query_pairs, doseq=True)
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), path, query, ""))


def _looks_like_asset(url: str) -> bool:
    lower = url.lower()
    asset_suffixes = (
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".webp",
        ".pdf",
        ".rss",
        ".atom",
        ".zip",
        ".css",
        ".js",
        ".xml",
        ".json",
        ".ico",
    )
    return lower.endswith(asset_suffixes)


def _path_depth(url: str) -> int:
    path = urlsplit(url).path
    return len([segment for segment in path.split("/") if segment])


def _strong_negative_url_hint(url: str, anchor_text: str) -> bool:
    lower_url = url.lower()
    path_segments = [segment for segment in urlsplit(url).path.lower().split("/") if segment]
    lower_anchor = anchor_text.strip().lower()
    if any(pattern in lower_url for pattern in POSITIVE_DETAIL_PATTERNS):
        return False
    if any(segment in STRONG_NEGATIVE_URL_PARTS for segment in path_segments):
        return True
    if any(
        pattern in lower_url
        for pattern in (
            "/candidate/",
            "/candidates/",
            "/guest-controls",
            "/forgot-password",
            "/post-a-job",
            "/profile/",
            "/profiles/",
            "/promo/resume",
            "/remote-jobs/new",
            "/search/talent",
            "/talent/",
        )
    ):
        return True
    if lower_anchor in {
        "about",
        "browse talent",
        "blog",
        "community",
        "contact",
        "cookies",
        "help",
        "hire talent",
        "privacy",
        "post a job",
        "sign in",
        "sign up",
        "support",
        "terms",
    }:
        return True
    return False


def _history_score(url: str, discovery_feedback: DiscoveryFeedback | None) -> float:
    if discovery_feedback is None:
        return 0.0
    normalized_url = url.strip().lower()
    if normalized_url in {item.strip().lower() for item in discovery_feedback.blocked_urls}:
        return -6.0

    normalized_path = urlsplit(url).path.lower().rstrip("/")
    score = 0.0
    preferred_hits = [
        prefix
        for prefix in discovery_feedback.preferred_prefixes
        if normalized_path == prefix or normalized_path.startswith(prefix + "/")
    ]
    blocked_hits = [
        prefix
        for prefix in discovery_feedback.blocked_prefixes
        if normalized_path == prefix or normalized_path.startswith(prefix + "/")
    ]
    if preferred_hits:
        score += 2.4 + (0.2 * min(len(preferred_hits), 3))
    if blocked_hits and not preferred_hits:
        score -= 3.0 + (0.25 * min(len(blocked_hits), 4))
    return score


def _is_feedback_blocked(
    url: str,
    discovery_feedback: DiscoveryFeedback | None,
    *,
    source_page: bool = False,
) -> bool:
    if discovery_feedback is None:
        return False
    normalized_url = url.strip().lower()
    blocked_urls = discovery_feedback.blocked_source_urls if source_page else discovery_feedback.blocked_urls
    if normalized_url in {item.strip().lower() for item in blocked_urls}:
        return True

    normalized_path = urlsplit(url).path.lower().rstrip("/")
    preferred_hits = [
        prefix
        for prefix in discovery_feedback.preferred_prefixes
        if normalized_path == prefix or normalized_path.startswith(prefix + "/")
    ]
    if preferred_hits:
        return False

    blocked_hits = [
        prefix
        for prefix in discovery_feedback.blocked_prefixes
        if normalized_path == prefix or normalized_path.startswith(prefix + "/")
    ]
    return bool(blocked_hits)


def _structural_score(
    url: str,
    anchor_text: str,
    source_page: str,
    root_url: str,
    discovery_feedback: DiscoveryFeedback | None = None,
) -> float:
    parsed = urlsplit(url)
    root = urlsplit(root_url)

    score = 0.0
    if _same_site(parsed.netloc, root.netloc):
        score += 2.0
    depth = _path_depth(url)
    score += min(depth, 5) * 0.35

    if parsed.query:
        score += 0.2
    if parsed.fragment:
        score -= 0.4
    if len(parsed.path) > 8:
        score += 0.25
    if parsed.path and parsed.path[-1].isdigit():
        score += 0.3
    if "-" in parsed.path or "_" in parsed.path:
        score += 0.2

    anchor_len = len(anchor_text.strip())
    if 10 <= anchor_len <= 220:
        score += 0.5
    if source_page != root_url:
        score += 0.2

    lower_url = url.lower()
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if any(pattern in lower_url for pattern in POSITIVE_DETAIL_PATTERNS):
        score += 2.2
    if host.endswith("ziprecruiter.ie") and path.startswith("/jobs/") and path != "/jobs/search":
        score += 2.2
    if host.endswith("ziprecruiter.com") and path.startswith("/jobs/") and path != "/jobs/search":
        score += 2.2
    if host.endswith("indeed.com") and path == "/viewjob":
        score += 2.2
    if any(pattern in lower_url for pattern in WEAK_NEGATIVE_PATTERNS):
        score -= 1.6
    if re.search(r"/jobs/[^/]*jobs(?:-|$)", parsed.path.lower()) and "/jobs/view/" not in lower_url:
        score -= 1.8
    if re.search(r"/company/[^/]+/?$", parsed.path.lower()):
        score -= 1.4
    if anchor_len < 4:
        score -= 0.25
    if len(parsed.query) > 180:
        score -= 0.25
    score += _history_score(url, discovery_feedback)
    return score


def _extract_links(
    page: Any,
    source_page: str,
    root_url: str,
    discovery_feedback: DiscoveryFeedback | None = None,
    profile: SiteProfile | None = None,
) -> list[LinkCandidate]:
    found: dict[str, LinkCandidate] = {}

    def _add_candidate(canonical: str, text: str, *, score_boost: float = 0.0) -> None:
        if _looks_like_asset(canonical):
            return
        if not url_looks_like_search_or_job_page(canonical):
            return
        if not _same_site(urlsplit(canonical).netloc, urlsplit(root_url).netloc):
            return
        if _is_feedback_blocked(canonical, discovery_feedback):
            return
        if _strong_negative_url_hint(canonical, text):
            return
        score = _structural_score(canonical, text, source_page, root_url, discovery_feedback=discovery_feedback) + score_boost
        existing = found.get(canonical)
        candidate = LinkCandidate(
            url=canonical,
            anchor_text=text,
            source_page=source_page,
            structural_score=score,
        )
        if existing is None or candidate.structural_score > existing.structural_score:
            found[canonical] = candidate

    raw_html = str(getattr(page, "html", "") or "")
    normalized_embedded_html = unescape(raw_html.replace("\\u002F", "/").replace("\\/", "/"))

    if profile is not None and profile.discovery_selectors:
        for selector in profile.discovery_selectors:
            try:
                selected_nodes = page.css(selector)
            except Exception:
                continue
            for anchor in selected_nodes or []:
                href = (getattr(anchor, "attrib", None) or {}).get("href")
                if not href:
                    continue
                canonical = canonicalize_url(href, source_page)
                if not canonical:
                    continue
                text = " ".join((anchor.css("::text").get() or "").split())
                _add_candidate(canonical, text, score_boost=3.0)

    for anchor in page.css("a"):
        href = (anchor.attrib or {}).get("href")
        if not href:
            continue
        canonical = canonicalize_url(href, source_page)
        if not canonical:
            continue
        text = " ".join((anchor.css("::text").get() or "").split())
        _add_candidate(canonical, text)

    for pattern in EMBEDDED_CANDIDATE_URL_PATTERNS:
        for match in pattern.findall(normalized_embedded_html):
            canonical = canonicalize_url(str(match or ""), source_page)
            if not canonical or canonical == source_page:
                continue
            _add_candidate(canonical, "", score_boost=1.5)
    return list(found.values())


def _analyze_discovery_page(
    page: Any,
    *,
    root_url: str,
    discovery_feedback: DiscoveryFeedback | None,
    profile: SiteProfile | None,
    logger: logging.Logger,
    log_url: str,
) -> tuple[str, list[LinkCandidate]]:
    page_kind = classify_page_kind(page.url, html=page.html, text=page_to_text(page.page))
    if is_opportunity_detail_kind(page_kind):
        return page_kind, []

    extracted_links = _extract_links(
        page.page,
        source_page=page.url,
        root_url=root_url,
        discovery_feedback=discovery_feedback,
        profile=profile,
    )
    if not is_opportunity_feed_kind(page_kind) and len(extracted_links) >= DISCOVERY_SEARCH_FALLBACK_MIN_LINKS:
        logger.info(
            "[discover] treating %s as an opportunity feed because %s candidate links were found despite classifier=%s",
            log_url,
            len(extracted_links),
            page_kind,
        )
        return "mixed_opportunity_feed", extracted_links
    return page_kind, extracted_links


async def discover_candidates(
    website: str,
    fetcher: ResilientFetcher,
    ai_client: AIClient,
    settings: Settings,
    logger: logging.Logger,
    interactive_fetch: Callable[[str], Awaitable[Any | None]] | None = None,
    discovery_feedback: DiscoveryFeedback | None = None,
    discovery_event_logger: Callable[[str, str, str], None] | None = None,
) -> DiscoveryResult:
    fetch_page = interactive_fetch or fetcher.fetch
    profile = get_site_profile(website) if bool(getattr(settings, "enable_site_profiles", True)) else None

    def _fetch_failure_status(url: str) -> str:
        getter = getattr(fetcher, "last_failure_reason", None)
        if callable(getter):
            reason = str(getter(url) or "").strip().lower()
            if reason == "challenge_or_interstitial":
                return "challenge_blocked"
        return "fetch_failed"

    root_page = await fetch_page(website)
    if root_page is None:
        if callable(discovery_event_logger):
            discovery_event_logger(website, website, _fetch_failure_status(website))
        return DiscoveryResult(candidates=[], fetched_pages=0)

    fetched_pages = 1
    discovered: dict[str, LinkCandidate] = {}
    root_kind, root_links = _analyze_discovery_page(
        root_page,
        root_url=website,
        discovery_feedback=discovery_feedback,
        profile=profile,
        logger=logger,
        log_url=website,
    )

    if interactive_fetch is not None and (root_kind == "other" or (root_kind == "search_results" and not root_links)):
        fallback_root_page = await fetcher.fetch(website)
        if fallback_root_page is not None:
            fetched_pages += 1
            fallback_kind, fallback_links = _analyze_discovery_page(
                fallback_root_page,
                root_url=website,
                discovery_feedback=discovery_feedback,
                profile=profile,
                logger=logger,
                log_url=website,
            )
        fallback_rank = (
            2 if is_opportunity_detail_kind(fallback_kind) else 1 if is_opportunity_feed_kind(fallback_kind) else 0,
            len(fallback_links),
        )
        root_rank = (
            2 if is_opportunity_detail_kind(root_kind) else 1 if is_opportunity_feed_kind(root_kind) else 0,
            len(root_links),
        )
        if fallback_rank > root_rank:
            logger.info(
                "[discover] using resilient fetch for %s because interactive fetch yielded kind=%s links=%s but fallback yielded kind=%s links=%s",
                website,
                root_kind,
                len(root_links),
                fallback_kind,
                len(fallback_links),
            )
            root_page = fallback_root_page
            root_kind = fallback_kind
            root_links = fallback_links
        elif callable(discovery_event_logger):
            discovery_event_logger(website, website, _fetch_failure_status(website))

    def _insert(items: list[LinkCandidate]) -> None:
        for item in items:
            existing = discovered.get(item.url)
            if existing is None or item.structural_score > existing.structural_score:
                discovered[item.url] = item

    if is_opportunity_detail_kind(root_kind):
        if callable(discovery_event_logger):
            discovery_event_logger(website, root_page.url, "opportunity_detail_source")
        canonical_root = canonicalize_url(root_page.url, website)
        if canonical_root is not None:
            discovered[canonical_root] = LinkCandidate(
                url=canonical_root,
                anchor_text="opportunity detail",
                source_page=root_page.url,
                structural_score=8.0 + _history_score(canonical_root, discovery_feedback),
            )
    elif not is_opportunity_feed_kind(root_kind):
        if callable(discovery_event_logger):
            discovery_event_logger(website, root_page.url, f"non_feed_source:{root_kind}")
        logger.info("[discover] skipping %s because the root page does not look like an opportunity feed or detail page", website)
        return DiscoveryResult(candidates=[], fetched_pages=fetched_pages)
    elif callable(discovery_event_logger):
        discovery_event_logger(website, root_page.url, "opportunity_feed_source")

    if is_opportunity_feed_kind(root_kind):
        _insert(root_links)

    seed_pages = sorted(
        {candidate.url for candidate in root_links if _path_depth(candidate.url) <= 2 and candidate.url != root_page.url},
        key=lambda u: (_path_depth(u), len(urlsplit(u).query)),
    )
    seed_pages = [
        seed_url
        for seed_url in seed_pages
        if not _is_feedback_blocked(seed_url, discovery_feedback, source_page=True)
        and not (
            profile is not None and url_matches_profile_pattern(seed_url, profile.blocked_detail_url_patterns)
        )
    ][: settings.max_seed_pages_per_site]

    for seed_url in seed_pages:
        seed_page = await fetch_page(seed_url)
        if seed_page is None:
            if callable(discovery_event_logger):
                discovery_event_logger(website, seed_url, _fetch_failure_status(seed_url))
            continue
        fetched_pages += 1
        seed_kind, seed_links = _analyze_discovery_page(
            seed_page,
            root_url=website,
            discovery_feedback=discovery_feedback,
            profile=profile,
            logger=logger,
            log_url=seed_url,
        )
        if is_opportunity_detail_kind(seed_kind):
            if callable(discovery_event_logger):
                discovery_event_logger(website, seed_page.url, "opportunity_detail_source")
            canonical_seed = canonicalize_url(seed_page.url, website)
            if canonical_seed is not None:
                _insert(
                    [
                        LinkCandidate(
                            url=canonical_seed,
                            anchor_text="opportunity detail",
                            source_page=seed_page.url,
                            structural_score=7.0 + _history_score(canonical_seed, discovery_feedback),
                        )
                    ]
                )
            continue
        if not is_opportunity_feed_kind(seed_kind):
            if callable(discovery_event_logger):
                discovery_event_logger(website, seed_page.url, f"non_feed_source:{seed_kind}")
            continue
        if callable(discovery_event_logger):
            discovery_event_logger(website, seed_page.url, "opportunity_feed_source")
        _insert(seed_links)

    candidates = list(discovered.values())
    candidates = [candidate for candidate in candidates if candidate.url != canonicalize_url(website, website)]
    if profile is not None and profile.blocked_detail_url_patterns:
        candidates = [
            candidate
            for candidate in candidates
            if not url_matches_profile_pattern(candidate.url, profile.blocked_detail_url_patterns)
        ]
    candidates.sort(key=lambda item: item.structural_score, reverse=True)
    candidate_limit = max(1, min(int(settings.max_candidates_per_site), DISCOVERY_HARD_MAX_CANDIDATES_PER_SITE))
    if len(candidates) > candidate_limit:
        logger.info(
            "[discover] limiting %s candidates on %s to %s for this cycle",
            len(candidates),
            website,
            candidate_limit,
        )
    candidates = candidates[:candidate_limit]

    if not candidates:
        return DiscoveryResult(candidates=[], fetched_pages=fetched_pages)

    try:
        ai_ranked = await ai_client.rank_links(website, candidates)
        if ai_ranked:
            by_url = {candidate.url: candidate for candidate in candidates}
            ranked_list: list[LinkCandidate] = []
            seen = set()
            for ranked_url in ai_ranked:
                candidate = by_url.get(ranked_url)
                if candidate and candidate.url not in seen:
                    ranked_list.append(candidate)
                    seen.add(candidate.url)
            for candidate in candidates:
                if candidate.url not in seen:
                    ranked_list.append(candidate)
            candidates = ranked_list
    except Exception as exc:  # noqa: BLE001
        logger.warning("[discover] AI ranking failed on %s: %s", website, str(exc))

    return DiscoveryResult(candidates=candidates, fetched_pages=fetched_pages)
