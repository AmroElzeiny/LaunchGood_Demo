from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from job_bot.extractors import page_to_text
from job_bot.fetching import ResilientFetcher

from prospect_system.dashboard_state import ScrapedPage
from prospect_system.prospect_config import ProspectSettings


_ASSET_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".pdf",
    ".zip",
    ".css",
    ".js",
    ".xml",
    ".json",
    ".ico",
    ".mp4",
    ".mov",
)

_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")


@dataclass(slots=True)
class ProspectLink:
    url: str
    anchor_text: str
    source_page: str
    structural_score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "anchor_text": self.anchor_text,
            "source_page": self.source_page,
            "structural_score": round(self.structural_score, 3),
        }


@dataclass(slots=True)
class ScrapeOutcome:
    page: ScrapedPage | None
    blocked: bool = False
    error: str = ""


@dataclass(slots=True)
class _FetcherSettings:
    request_timeout_ms: int
    verify_ssl: bool
    headless_browser: bool
    use_scrapling_cloudflare_solver: bool
    enable_browser_tabs: bool
    enable_site_profiles: bool
    fetch_strategy_timeout_ms: int
    network_retry_budget: int
    network_backoff_base_seconds: float
    browser_reprobe_cooldown_seconds: int
    max_fetch_strategies_per_url: int


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.metadata: dict[str, str] = {}
        self._inside_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "title":
            self._inside_title = True
            return
        if tag.lower() == "meta":
            key = (
                attr_map.get("name")
                or attr_map.get("property")
                or attr_map.get("itemprop")
                or ""
            ).strip().lower()
            content = " ".join(str(attr_map.get("content") or "").split()).strip()
            if key and content:
                self.metadata[key] = content
        if tag.lower() == "link":
            rel = str(attr_map.get("rel") or "").strip().lower()
            href = str(attr_map.get("href") or "").strip()
            if rel == "canonical" and href:
                self.metadata["canonical"] = href

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._inside_title = False

    def handle_data(self, data: str) -> None:
        if self._inside_title and data.strip():
            self.title_parts.append(data.strip())

    @property
    def title(self) -> str:
        return " ".join(" ".join(self.title_parts).split()).strip()


def normalize_input_urls(raw_value: str) -> tuple[list[str], list[str]]:
    urls: list[str] = []
    errors: list[str] = []
    seen: set[str] = set()
    for raw_item in re.split(r"[\n,]+", str(raw_value or "")):
        item = raw_item.strip().strip("'\"")
        if not item:
            continue
        if not item.lower().startswith(("http://", "https://")):
            item = f"https://{item}"
        parsed = urlsplit(item)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or any(char.isspace() for char in parsed.netloc):
            errors.append(f"Invalid URL: {raw_item.strip()}")
            continue
        normalized = _canonicalize_url(item, item)
        if not normalized:
            errors.append(f"Invalid URL: {raw_item.strip()}")
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        urls.append(normalized)
    return urls, errors


def _canonicalize_url(raw_url: str, base_url: str) -> str | None:
    resolved = urljoin(base_url, raw_url.strip())
    parsed = urlsplit(resolved)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=False)
            if not key.lower().startswith("utm_")
        ],
        doseq=True,
    )
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), path, query, ""))


def _host(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    return parsed.netloc.lower().strip(".")


def _same_site(url_a: str, url_b: str) -> bool:
    host_a = _host(url_a)
    host_b = _host(url_b)
    if not host_a or not host_b:
        return False
    return host_a == host_b or host_a.endswith("." + host_b) or host_b.endswith("." + host_a)


def _path_depth(url: str) -> int:
    return len([part for part in urlsplit(url).path.split("/") if part])


def _looks_like_asset(url: str) -> bool:
    lowered = urlsplit(url).path.lower()
    return any(lowered.endswith(suffix) for suffix in _ASSET_SUFFIXES)


def _link_score(url: str, anchor_text: str, root_url: str, source_page: str) -> float:
    parsed = urlsplit(url)
    anchor_len = len(anchor_text.strip())
    score = 0.0
    if _same_site(url, root_url):
        score += 3.0
    depth = _path_depth(url)
    score += min(depth, 5) * 0.4
    if 4 <= anchor_len <= 120:
        score += 0.7
    if "-" in parsed.path or "_" in parsed.path:
        score += 0.25
    if source_page != root_url:
        score += 0.15
    if parsed.query:
        score -= 0.25
    if len(parsed.path) <= 1:
        score -= 0.5
    return score


def _extract_metadata(html: str, final_url: str) -> tuple[str, dict[str, str]]:
    parser = _MetadataParser()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        return "", {}
    metadata = dict(parser.metadata)
    if "canonical" in metadata:
        canonical = _canonicalize_url(metadata["canonical"], final_url)
        if canonical:
            metadata["canonical"] = canonical
    return parser.title, metadata


def _extract_internal_links(page: Any, source_page: str, root_url: str) -> list[ProspectLink]:
    found: dict[str, ProspectLink] = {}
    try:
        anchors = page.css("a")
    except Exception:
        anchors = []
    for anchor in anchors or []:
        href = (getattr(anchor, "attrib", None) or {}).get("href")
        if not href:
            continue
        canonical = _canonicalize_url(str(href), source_page)
        if not canonical or canonical == _canonicalize_url(source_page, source_page):
            continue
        if _looks_like_asset(canonical) or not _same_site(canonical, root_url):
            continue
        try:
            anchor_text = " ".join(str(anchor.css("::text").get() or "").split()).strip()
        except Exception:
            anchor_text = ""
        candidate = ProspectLink(
            url=canonical,
            anchor_text=anchor_text,
            source_page=source_page,
            structural_score=_link_score(canonical, anchor_text, root_url, source_page),
        )
        existing = found.get(candidate.url)
        if existing is None or candidate.structural_score > existing.structural_score:
            found[candidate.url] = candidate
    ordered = sorted(found.values(), key=lambda item: item.structural_score, reverse=True)
    return ordered[:80]


def extract_contact_email(text: str, html: str = "") -> str:
    blob = f"{text or ''}\n{html or ''}"
    for match in _EMAIL_RE.findall(blob):
        lowered = match.lower()
        if lowered.endswith((".png", ".jpg", ".jpeg", ".webp")):
            continue
        return match
    return ""


class ProspectScraper:
    def __init__(self, settings: ProspectSettings, logger: logging.Logger) -> None:
        timeout_ms = max(1000, int(settings.scraping_timeout_seconds * 1000))
        fetcher_settings = _FetcherSettings(
            request_timeout_ms=timeout_ms,
            verify_ssl=settings.verify_ssl,
            headless_browser=settings.headless_browser,
            use_scrapling_cloudflare_solver=settings.use_scrapling_cloudflare_solver,
            enable_browser_tabs=settings.prospect_enable_browser_fetch,
            enable_site_profiles=settings.enable_site_profiles,
            fetch_strategy_timeout_ms=timeout_ms,
            network_retry_budget=settings.network_retry_budget,
            network_backoff_base_seconds=settings.network_backoff_base_seconds,
            browser_reprobe_cooldown_seconds=settings.browser_reprobe_cooldown_seconds,
            max_fetch_strategies_per_url=settings.max_fetch_strategies_per_url,
        )
        self.settings = settings
        self.logger = logger
        self.fetcher = ResilientFetcher(fetcher_settings, logger)

    async def scrape_page(self, url: str, *, root_url: str) -> ScrapeOutcome:
        try:
            result = await self.fetcher.fetch(url)
        except Exception as exc:  # noqa: BLE001
            return ScrapeOutcome(page=None, error=str(exc))
        if result is None:
            reason = self.fetcher.last_failure_reason(url)
            if reason == "challenge_or_interstitial":
                return ScrapeOutcome(page=None, blocked=True, error="Blocked by challenge or interstitial.")
            return ScrapeOutcome(page=None, error="Website was unreachable or returned no readable HTML.")
        if self._is_blocked_page(result.url, result.html):
            return ScrapeOutcome(page=None, blocked=True, error="Blocked by Captcha, Cloudflare, or access challenge.")

        text = page_to_text(result.page)
        title, metadata = _extract_metadata(result.html, result.url)
        links = _extract_internal_links(result.page, result.url, root_url)
        if not text.strip():
            description = metadata.get("description") or metadata.get("og:description") or metadata.get("twitter:description") or ""
            text = " ".join(part for part in (title, description) if part).strip()
        return ScrapeOutcome(
            page=ScrapedPage(
                url=url,
                final_url=result.url,
                title=title,
                text=text,
                metadata=metadata,
                links=[link.to_dict() for link in links],
                status=result.status,
                strategy=result.strategy,
            )
        )

    @staticmethod
    def _is_blocked_page(response_url: str, html: str) -> bool:
        if ResilientFetcher._looks_like_cloudflare_challenge(response_url, html):
            return True
        lowered = f"{response_url}\n{html}".lower()
        markers = (
            "captcha",
            "recaptcha",
            "hcaptcha",
            "bot challenge",
            "access challenge",
            "access denied",
            "verify you are human",
            "unusual traffic",
            "security check",
            "checking your browser",
        )
        return any(marker in lowered for marker in markers)
