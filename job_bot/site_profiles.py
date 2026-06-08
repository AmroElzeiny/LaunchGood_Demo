from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class SiteProfile:
    key: str
    domains: tuple[str, ...]
    browser_locale: str = ""
    fetch_order: tuple[str, ...] = ()
    max_fetch_strategies: int | None = None
    allow_click_detail: bool = True
    blocked_detail_url_patterns: tuple[str, ...] = ()
    discovery_selectors: tuple[str, ...] = ()
    pagination_rules: tuple[str, ...] = ()
    wait_rules: tuple[str, ...] = ()
    fallback_rules: tuple[str, ...] = ()
    notes: str = ""


SITE_PROFILES: tuple[SiteProfile, ...] = (
    SiteProfile(
        key="contra",
        domains=("contra.com",),
        fetch_order=("static_verified", "curl_cffi", "dynamic", "stealthy"),
        max_fetch_strategies=4,
        allow_click_detail=False,
        blocked_detail_url_patterns=("/hire/", "/portfolio/", "/profile/", "/users/"),
        discovery_selectors=("a[href*='/opportunities/']", "a[href*='opportunities?']"),
        pagination_rules=("prefer opportunities feeds and search URLs",),
        wait_rules=("static-first",),
        fallback_rules=("avoid creator portfolio/profile pages during discovery",),
        notes="Contra discovery should stay focused on opportunities feeds and briefs.",
    ),
    SiteProfile(
        key="dribbble",
        domains=("dribbble.com",),
        fetch_order=("static_verified", "curl_cffi", "dynamic", "stealthy"),
        max_fetch_strategies=4,
        allow_click_detail=False,
        blocked_detail_url_patterns=("/shots/", "/designers/", "/stories/"),
        discovery_selectors=("a[href*='/jobs/']",),
        pagination_rules=("prefer jobs board listings with detail links",),
        wait_rules=("static-first",),
        fallback_rules=("ignore portfolio and shot pages while looking for contract work",),
        notes="Dribbble should act like a project board, not a portfolio feed.",
    ),
    SiteProfile(
        key="workatastartup",
        domains=("workatastartup.com",),
        fetch_order=("dynamic", "stealthy", "static_verified", "curl_cffi"),
        max_fetch_strategies=4,
        allow_click_detail=False,
        blocked_detail_url_patterns=("/founders/", "/candidate/", "/candidates/"),
        discovery_selectors=("a[href*='/companies/'][href*='/jobs/']", "a[href^='/jobs/l/']"),
        pagination_rules=("prefer the public /jobs feeds and role-specific /jobs/l/ pages",),
        wait_rules=("let the JS app hydrate before extracting anchors",),
        fallback_rules=("fall back to embedded job URLs when the static shell has not rendered links yet",),
        notes="workatastartup.com is JS-heavy and benefits from dynamic-first discovery.",
    ),
    SiteProfile(
        key="ycombinator_jobs",
        domains=("ycombinator.com",),
        fetch_order=("dynamic", "stealthy", "static_verified", "curl_cffi"),
        max_fetch_strategies=4,
        allow_click_detail=False,
        blocked_detail_url_patterns=("/blog/", "/people/", "/videos/"),
        discovery_selectors=("a[href*='/companies/'][href*='/jobs/']",),
        pagination_rules=("prefer the /jobs feed over general content pages",),
        wait_rules=("let the job feed hydrate before collecting company job links",),
        fallback_rules=("fall back to embedded JSON job URLs when anchor tags are sparse",),
        notes="ycombinator.com/jobs behaves like a JS-fed opportunity board.",
    ),
    SiteProfile(
        key="fl_ru",
        domains=("fl.ru",),
        browser_locale="ru-RU",
        fetch_order=("curl_cffi", "static_verified", "dynamic", "stealthy"),
        max_fetch_strategies=4,
        allow_click_detail=False,
        blocked_detail_url_patterns=("/freelancers/", "/users/", "/portfolio/", "/resume/"),
        discovery_selectors=("a[href*='/projects/'][href$='.html']",),
        pagination_rules=("prefer /projects feeds and category project pages", "avoid freelancer profiles and portfolio paths"),
        wait_rules=("static-first on category and project list pages",),
        fallback_rules=("promote direct /projects/<id>/...html links discovered from category feeds",),
        notes="fl.ru exposes project detail URLs directly on the public projects feed.",
    ),
    SiteProfile(
        key="freelance_ru",
        domains=("freelance.ru",),
        browser_locale="ru-RU",
        fetch_order=("curl_cffi", "static_verified", "dynamic", "stealthy"),
        max_fetch_strategies=4,
        allow_click_detail=False,
        blocked_detail_url_patterns=("/freelancers/", "/user/", "/portfolio/", "/contest/"),
        discovery_selectors=("a[href*='/projects/'][href$='.html']", "a[href*='/tender/view/']"),
        pagination_rules=("prefer /project/search and direct /projects/<slug>-<id>.html links",),
        wait_rules=("static-first on the public project search feed",),
        fallback_rules=("treat freelancer portfolios and contest hubs as non-detail discovery sources",),
        notes="freelance.ru has stable project-detail URLs and some tender detail pages.",
    ),
    SiteProfile(
        key="workspace_ru",
        domains=("workspace.ru",),
        browser_locale="ru-RU",
        fetch_order=("static_verified", "curl_cffi", "dynamic", "stealthy"),
        max_fetch_strategies=4,
        allow_click_detail=False,
        blocked_detail_url_patterns=("/people/", "/agencies/", "/project-news/"),
        discovery_selectors=("a[href*='/tenders/'][href*='-']",),
        pagination_rules=("prefer /tenders feeds and public category pages such as /web-design/",),
        wait_rules=("static-first on tender category pages",),
        fallback_rules=("ignore agency and people directories while extracting tender detail links",),
        notes="workspace.ru public tenders pages expose Russian-language brief and tender posts.",
    ),
    SiteProfile(
        key="kwork_projects",
        domains=("kwork.ru",),
        browser_locale="ru-RU",
        fetch_order=("dynamic", "curl_cffi", "static_verified", "stealthy"),
        max_fetch_strategies=4,
        allow_click_detail=False,
        blocked_detail_url_patterns=("/categories/", "/user/", "/portfolio/", "/kworks/"),
        discovery_selectors=("a[href*='/projects/'][href*='/view']", "a[href*='/projects/'][href*='?']"),
        pagination_rules=("prefer the public /projects board over service-category pages",),
        wait_rules=("let the projects board hydrate before collecting embedded project links",),
        fallback_rules=("fall back to embedded project URLs from the board state when anchors are sparse",),
        notes="kwork.ru/projects is JS-rich and benefits from Russian locale plus embedded-link discovery.",
    ),
    SiteProfile(
        key="weworkremotely",
        domains=("weworkremotely.com",),
        fetch_order=("static_verified", "curl_cffi", "dynamic", "stealthy"),
        max_fetch_strategies=4,
        allow_click_detail=False,
        blocked_detail_url_patterns=("/categories/", "/companies/", "/remote-jobs/search"),
        discovery_selectors=("section.jobs a[href*='/remote-jobs/']",),
        pagination_rules=("prefer direct remote-jobs detail links from category pages",),
        wait_rules=("static-first",),
        fallback_rules=("do not spend browser time on category-only pages",),
        notes="weworkremotely.com is usually best handled with static-first fetching.",
    ),
    SiteProfile(
        key="wellfound",
        domains=("wellfound.com",),
        fetch_order=("dynamic", "stealthy", "curl_cffi", "static_verified"),
        max_fetch_strategies=4,
        allow_click_detail=False,
        blocked_detail_url_patterns=("/candidate/", "/candidates/", "/profile/", "/profiles/"),
        discovery_selectors=("a[href*='/jobs/']", "a[href*='/company/'][href*='/jobs/']"),
        pagination_rules=("prefer role and company-job feeds instead of profile or candidate pages",),
        wait_rules=("prefer browser-backed strategies because static requests may be blocked",),
        fallback_rules=("ignore candidate/profile surfaces while tracking job links",),
        notes="wellfound.com commonly needs browser rendering or anti-bot-tolerant fetching.",
    ),
    SiteProfile(
        key="builtin",
        domains=("builtin.com",),
        fetch_order=("static_verified", "curl_cffi", "dynamic", "stealthy"),
        max_fetch_strategies=4,
        allow_click_detail=False,
        blocked_detail_url_patterns=("/company/", "/companies/", "/auth/", "/profile/"),
        discovery_selectors=("a[href*='/job/']",),
        pagination_rules=("prefer filtered /jobs search pages and direct /job/ detail links",),
        wait_rules=("static-first on search pages",),
        fallback_rules=("ignore company and auth pages during discovery",),
        notes="builtin.com exposes stable server-rendered job links on listing pages.",
    ),
    SiteProfile(
        key="flexjobs",
        domains=("flexjobs.com",),
        fetch_order=("curl_cffi", "dynamic", "stealthy", "static_verified"),
        max_fetch_strategies=4,
        allow_click_detail=False,
        blocked_detail_url_patterns=("/career-advice/", "/events/", "/company-guide/", "/account/"),
        discovery_selectors=("a[href*='/job/']", "a[href*='/remote-jobs/']"),
        pagination_rules=("prefer filtered remote-job result pages with posted-date ordering",),
        wait_rules=("be ready to escalate beyond plain static fetches on slower pages",),
        fallback_rules=("ignore advice and account surfaces while looking for job detail links",),
        notes="flexjobs.com can be slower or more defensive, so curl/dynamic fallbacks matter.",
    ),
)


def get_site_profile(website: str) -> SiteProfile | None:
    normalized_domain = _normalized_domain(website)
    if not normalized_domain:
        return None
    for profile in SITE_PROFILES:
        for domain in profile.domains:
            if normalized_domain == domain or normalized_domain.endswith(f".{domain}"):
                return profile
    return None


def _normalized_domain(website: str) -> str:
    raw = str(website or "").strip()
    parsed = urlparse(raw)
    domain = parsed.netloc or raw
    return domain.lower().strip().lstrip(".")


def url_matches_profile_pattern(url: str, patterns: tuple[str, ...]) -> bool:
    lowered = str(url or "").strip().lower()
    return any(pattern.lower() in lowered for pattern in patterns)
