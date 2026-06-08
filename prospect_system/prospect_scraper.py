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
_UTILITY_LINK_TOKENS = (
    "login",
    "log-in",
    "logout",
    "log-out",
    "signin",
    "sign-in",
    "signout",
    "sign-out",
    "signup",
    "sign-up",
    "cart",
    "checkout",
    "account",
    "admin",
)
_SOCIAL_SHARE_QUERY_KEYS = (
    "share",
    "share_url",
    "shareurl",
    "share_link",
    "sharelink",
    "social",
)
_DOM_CONTEXT_TAGS = {"header", "nav", "footer", "main", "article", "aside", "section"}
_DOM_CONTEXT_MARKERS = (
    ("navigation", "nav"),
    ("nav", "nav"),
    ("menu", "menu"),
    ("footer", "footer"),
    ("header", "header"),
    ("banner", "header"),
    ("main", "main"),
    ("content", "main"),
    ("article", "article"),
    ("card", "card"),
    ("button", "button"),
)
BLOCKED_HTTP_STATUSES = (401, 403, 407, 429, 451)


@dataclass(slots=True)
class ProspectLink:
    url: str
    anchor_text: str
    source_page: str
    structural_score: float
    title: str = ""
    aria_label: str = ""
    surrounding_text: str = ""
    dom_context: str = ""
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "anchor_text": self.anchor_text,
            "title": self.title,
            "aria_label": self.aria_label,
            "surrounding_text": self.surrounding_text,
            "dom_context": self.dom_context,
            "path": self.path,
            "source_page": self.source_page,
            "structural_score": round(self.structural_score, 3),
        }


@dataclass(slots=True)
class ScrapeOutcome:
    page: ScrapedPage | None
    blocked: bool = False
    error: str = ""
    status_code: int = 0
    final_url: str = ""


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
    blocked_http_statuses: tuple[int, ...]


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
                (
                    attr_map.get("name")
                    or attr_map.get("property")
                    or attr_map.get("itemprop")
                    or ""
                )
                .strip()
                .lower()
            )
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


class _InternalLinkHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self._tag_stack: list[tuple[str, str]] = []
        self._current_anchor: dict[str, Any] | None = None
        self._recent_text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = {key.lower(): value or "" for key, value in attrs}
        context = _dom_context_for_tag(tag, attr_map)
        self._tag_stack.append((tag, context))
        if tag == "a":
            self._current_anchor = {
                "href": attr_map.get("href", ""),
                "title": _clean_spaces(attr_map.get("title", "")),
                "aria_label": _clean_spaces(attr_map.get("aria-label", "")),
                "text_parts": [],
                "surrounding_before": self._recent_text(),
                "dom_context": self._current_dom_context(),
            }
        elif self._current_anchor is not None and tag == "img":
            alt_text = _clean_spaces(
                attr_map.get("alt", "") or attr_map.get("title", "")
            )
            if alt_text:
                self._current_anchor["text_parts"].append(alt_text)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self._current_anchor is not None:
            anchor_text = _clean_spaces(" ".join(self._current_anchor["text_parts"]))
            surrounding_text = _clean_spaces(
                " ".join(
                    part
                    for part in (
                        self._current_anchor.get("surrounding_before", ""),
                        anchor_text,
                    )
                    if str(part or "").strip()
                )
            )
            self.links.append(
                {
                    "href": str(self._current_anchor.get("href") or ""),
                    "anchor_text": anchor_text,
                    "title": str(self._current_anchor.get("title") or ""),
                    "aria_label": str(self._current_anchor.get("aria_label") or ""),
                    "surrounding_text": surrounding_text,
                    "dom_context": str(self._current_anchor.get("dom_context") or ""),
                }
            )
            self._current_anchor = None
        while self._tag_stack:
            current_tag, _ = self._tag_stack.pop()
            if current_tag == tag:
                break

    def handle_data(self, data: str) -> None:
        cleaned = _clean_spaces(data)
        if not cleaned:
            return
        if self._current_anchor is not None:
            self._current_anchor["text_parts"].append(cleaned)
        self._recent_text_parts.append(cleaned)
        self._recent_text_parts = self._recent_text_parts[-10:]

    def _current_dom_context(self) -> str:
        for _, context in reversed(self._tag_stack):
            if context:
                return context
        return "body"

    def _recent_text(self) -> str:
        return _trim_text(" ".join(self._recent_text_parts[-6:]), limit=260)


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
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or any(char.isspace() for char in parsed.netloc)
        ):
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


def _clean_spaces(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _trim_text(value: Any, *, limit: int) -> str:
    cleaned = _clean_spaces(value)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."


def _host(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    return parsed.netloc.lower().strip(".")


def _same_site(url_a: str, url_b: str) -> bool:
    host_a = _host(url_a)
    host_b = _host(url_b)
    if not host_a or not host_b:
        return False
    return (
        host_a == host_b
        or host_a.endswith("." + host_b)
        or host_b.endswith("." + host_a)
    )


def _path_depth(url: str) -> int:
    return len([part for part in urlsplit(url).path.split("/") if part])


def _looks_like_asset(url: str) -> bool:
    lowered = urlsplit(url).path.lower()
    return any(lowered.endswith(suffix) for suffix in _ASSET_SUFFIXES)


def _looks_like_utility_link(url: str) -> bool:
    parsed = urlsplit(url)
    path_segments = {
        segment.strip().lower().replace("_", "-")
        for segment in parsed.path.split("/")
        if segment.strip()
    }
    if any(token in path_segments for token in _UTILITY_LINK_TOKENS):
        return True
    query_keys = {
        key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    }
    return any(key in query_keys for key in _SOCIAL_SHARE_QUERY_KEYS)


def _dom_context_for_tag(tag: str, attrs: dict[str, str]) -> str:
    lowered_tag = tag.lower()
    if lowered_tag in _DOM_CONTEXT_TAGS:
        return lowered_tag
    role = attrs.get("role", "").lower()
    class_id = f"{attrs.get('class', '')} {attrs.get('id', '')}".lower()
    for marker, label in _DOM_CONTEXT_MARKERS:
        if marker in role or marker in class_id:
            return label
    return ""


def _link_score(
    url: str,
    anchor_text: str,
    root_url: str,
    source_page: str,
    *,
    title: str = "",
    aria_label: str = "",
    surrounding_text: str = "",
    dom_context: str = "",
) -> float:
    parsed = urlsplit(url)
    signal_text = _clean_spaces(
        " ".join([anchor_text, title, aria_label, surrounding_text])
    )
    signal_len = len(signal_text)
    score = 0.0
    if _same_site(url, root_url):
        score += 3.0
    depth = _path_depth(url)
    score += min(depth, 5) * 0.2
    if 4 <= signal_len <= 260:
        score += min(signal_len / 90.0, 1.4)
    if dom_context in {"main", "article", "section", "card", "button"}:
        score += 0.35
    elif dom_context in {"nav", "menu"}:
        score += 0.15
    elif dom_context == "footer":
        score -= 0.05
    if source_page != root_url:
        score += 0.15
    if parsed.query:
        score -= 0.45
    if len(parsed.path) <= 1:
        score -= 0.7
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


def _raw_links_from_html(html: str) -> list[dict[str, str]]:
    parser = _InternalLinkHTMLParser()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        return []
    return parser.links


def _raw_links_from_selector(page: Any) -> list[dict[str, str]]:
    try:
        anchors = page.css("a")
    except Exception:
        anchors = []
    raw_links: list[dict[str, str]] = []
    for anchor in anchors or []:
        attrib = getattr(anchor, "attrib", None) or {}
        href = attrib.get("href")
        if not href:
            continue
        try:
            anchor_text = " ".join(
                str(anchor.css("::text").get() or "").split()
            ).strip()
        except Exception:
            anchor_text = ""
        raw_links.append(
            {
                "href": str(href),
                "anchor_text": anchor_text,
                "title": _clean_spaces(attrib.get("title", "")),
                "aria_label": _clean_spaces(attrib.get("aria-label", "")),
                "surrounding_text": anchor_text,
                "dom_context": "",
            }
        )
    return raw_links


def _extract_internal_links(
    page: Any, html: str, source_page: str, root_url: str
) -> list[ProspectLink]:
    found: dict[str, ProspectLink] = {}
    raw_links = _raw_links_from_html(html) or _raw_links_from_selector(page)
    source_canonical = _canonicalize_url(source_page, source_page)
    for raw_link in raw_links:
        href = raw_link.get("href", "")
        if not href:
            continue
        canonical = _canonicalize_url(href, source_page)
        if not canonical or canonical == source_canonical:
            continue
        if (
            _looks_like_asset(canonical)
            or _looks_like_utility_link(canonical)
            or not _same_site(canonical, root_url)
        ):
            continue
        anchor_text = _trim_text(raw_link.get("anchor_text", ""), limit=140)
        title = _trim_text(raw_link.get("title", ""), limit=140)
        aria_label = _trim_text(raw_link.get("aria_label", ""), limit=140)
        surrounding_text = _trim_text(raw_link.get("surrounding_text", ""), limit=320)
        dom_context = _clean_spaces(raw_link.get("dom_context", ""))
        candidate = ProspectLink(
            url=canonical,
            anchor_text=anchor_text,
            title=title,
            aria_label=aria_label,
            surrounding_text=surrounding_text,
            dom_context=dom_context,
            path=urlsplit(canonical).path or "/",
            source_page=source_page,
            structural_score=_link_score(
                canonical,
                anchor_text,
                root_url,
                source_page,
                title=title,
                aria_label=aria_label,
                surrounding_text=surrounding_text,
                dom_context=dom_context,
            ),
        )
        existing = found.get(candidate.url)
        if existing is None or candidate.structural_score > existing.structural_score:
            found[candidate.url] = candidate
    ordered = sorted(
        found.values(), key=lambda item: item.structural_score, reverse=True
    )
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
            blocked_http_statuses=BLOCKED_HTTP_STATUSES,
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
                return ScrapeOutcome(
                    page=None,
                    blocked=True,
                    error="Blocked by challenge or interstitial.",
                )
            return ScrapeOutcome(
                page=None, error="Website was unreachable or returned no readable HTML."
            )
        status = int(result.status or 0)
        if status in BLOCKED_HTTP_STATUSES:
            return ScrapeOutcome(
                page=None,
                blocked=True,
                error=f"Blocked or forbidden response: HTTP {status}",
                status_code=status,
                final_url=result.url,
            )
        if self._is_blocked_page(result.url, result.html):
            return ScrapeOutcome(
                page=None,
                blocked=True,
                error="Blocked by Captcha, Cloudflare, or access challenge.",
                status_code=status,
                final_url=result.url,
            )

        text = page_to_text(result.page)
        title, metadata = _extract_metadata(result.html, result.url)
        links = _extract_internal_links(result.page, result.html, result.url, root_url)
        if not text.strip():
            description = (
                metadata.get("description")
                or metadata.get("og:description")
                or metadata.get("twitter:description")
                or ""
            )
            text = " ".join(part for part in (title, description) if part).strip()
        return ScrapeOutcome(
            page=ScrapedPage(
                url=url,
                final_url=result.url,
                title=title,
                text=text,
                metadata=metadata,
                links=[link.to_dict() for link in links],
                discovered_links=[link.to_dict() for link in links],
                status_code=result.status,
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
            "cloudflare",
            "bot challenge",
            "access challenge",
            "access denied",
            "access to this page has been denied",
            "forbidden",
            "verify you are human",
            "unusual traffic",
            "security check",
            "checking your browser",
            "please enable cookies",
            "automated traffic",
        )
        return any(marker in lowered for marker in markers)
