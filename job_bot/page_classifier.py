from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlsplit

from job_bot.language_utils import normalize_match_text


SEARCH_RESULT_URL_HINTS = (
    "/search",
    "/jobs/search",
    "/careers/search",
    "/vacancies/search",
    "/search/vacancy",
    "/vacancy/search",
    "/category/",
    "/categories/",
    "/projects",
    "/project-search",
    "/briefs",
    "/requests",
    "/rfp",
    "/tenders",
    "/opportunities",
    "/freelance-jobs",
    "/board/",
    "/boards/",
)
JOB_POST_URL_HINTS = (
    "/job/",
    "/jobs/view/",
    "/jobs/details/",
    "/position/",
    "/posting/",
    "/vacancy/",
    "/vacancies/",
)
PROJECT_POST_URL_HINTS = (
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
    "/tenders/",
    "/statement-of-work",
    "/scope-of-work",
    "/consulting/",
    "/proposal/",
)
RFP_URL_HINTS = (
    "/rfp/",
    "/request-for-proposal",
    "/tender/",
    "/tenders/",
    "/procurement/",
    "/statement-of-work",
)
CONSULTING_URL_HINTS = (
    "/consulting/",
    "/brief/",
    "/tenders/",
    "/scope-of-work",
    "/statement-of-work",
    "/retainer/",
)
SEARCH_QUERY_KEYS = {
    "q",
    "query",
    "search",
    "page",
    "sort",
    "offset",
    "loc",
    "location",
    "text",
    "keyword",
    "keywords",
    "budget",
    "rate",
    "skills",
    "term",
}
SEARCH_RESULT_TEXT_HINTS = (
    "jobs found",
    "projects found",
    "search results",
    "sort by",
    "filter by",
    "filters",
    "load more",
    "showing",
    "results",
    "latest projects",
    "project board",
    "open requests",
    "latest opportunities",
    "проекты для фрилансеров",
    "заказы для фрилансеров",
    "вакансии",
    "результаты поиска",
    "найдено проектов",
    "найдено вакансий",
    "лента вакансий",
    "лента проектов",
    "проекты",
    "тендеры",
)
JOB_POST_TEXT_HINTS = (
    "apply now",
    "job description",
    "responsibilities",
    "requirements",
    "qualifications",
    "employment type",
    "about the role",
    "about this job",
    "обязанности",
    "требования",
    "условия работы",
    "вакансия",
    "описание вакансии",
)
PROJECT_POST_TEXT_HINTS = (
    "project scope",
    "scope of work",
    "statement of work",
    "deliverables",
    "submit proposal",
    "send proposal",
    "proposal deadline",
    "hourly rate",
    "fixed price",
    "budget",
    "payment",
    "project length",
    "engagement type",
    "client",
    "requester",
    "техническое задание",
    "тз",
    "заказчик",
    "стоимость",
    "бюджет",
    "сроки",
    "формат работы",
    "нужен дизайнер",
    "ищем ux/ui",
)
RFP_TEXT_HINTS = (
    "request for proposal",
    "rfp",
    "submit proposal",
    "proposal due",
    "submission deadline",
    "statement of work",
    "тендер",
    "запрос предложений",
    "конкурс",
    "срок подачи",
)
CONSULTING_TEXT_HINTS = (
    "consulting brief",
    "consulting project",
    "advisory",
    "retainer",
    "scope of work",
    "statement of work",
    "консультация",
    "консалтинг",
    "аудит",
    "advisor",
)
LIST_OR_FEED_TEXT_HINTS = (
    "jobs feed",
    "job feed",
    "rss feed",
    "vacancy feed",
    "latest jobs",
    "job roundup",
    "job digest",
    "list of jobs",
    "project board",
    "project feed",
    "latest projects",
    "latest opportunities",
    "open requests",
    "request board",
    "consulting opportunities",
    "лента вакансий",
    "лента проектов",
    "список вакансий",
    "список проектов",
    "новые вакансии",
    "новые проекты",
    "тендеры",
    "открытые запросы",
)
FILTER_SORT_HINTS = (
    "filter",
    "filters",
    "sort",
    "sorting",
)
PAGINATION_HINTS = (
    "page",
    "pagination",
    "next",
    "previous",
    "load more",
)
REACH_OUT_CTA_HINTS = (
    "apply",
    "view details",
    "save",
    "submit proposal",
    "send proposal",
    "contact client",
    "reach out",
    "inquire",
    "view project",
)
LOCATION_HINTS = ("location", "remote", "timezone", "country")
PAYMENT_HINTS = ("salary", "compensation", "budget", "rate", "payment")
GENERIC_LISTING_PATH_SEGMENTS = {
    "jobs",
    "careers",
    "vacancies",
    "vacancy",
    "roles",
    "search",
    "category",
    "categories",
    "projects",
    "project",
    "contracts",
    "contract",
    "briefs",
    "brief",
    "rfp",
    "rfps",
    "requests",
    "opportunities",
    "gigs",
    "gig",
    "freelance",
    "board",
}
MIXED_FEED_HINTS = (
    "opportunities",
    "projects and jobs",
    "contract and full-time",
    "open roles and projects",
    "mixed opportunities",
    "вакансии и проекты",
    "проекты и вакансии",
    "работа и проекты",
)
NEGATIVE_PAGE_HINTS = (
    "candidate",
    "candidates",
    "resume",
    "resumes",
    "profile",
    "profiles",
    "employer profile",
    "company profile",
    "knowledge base",
    "help center",
    "blog",
    "article",
    "articles",
    "кандидат",
    "кандидаты",
    "резюме",
    "профиль",
    "профили",
    "работодатель",
    "база знаний",
    "справка",
    "статья",
    "статьи",
    "блог",
)
_HREF_RE = re.compile(r"""href=["'](?P<href>[^"']+)["']""", flags=re.IGNORECASE)
_DETAIL_LINK_PATTERNS = (
    re.compile(r"/jobs/view/", flags=re.IGNORECASE),
    re.compile(r"/jobs/details/", flags=re.IGNORECASE),
    re.compile(r"/job/", flags=re.IGNORECASE),
    re.compile(r"/position/", flags=re.IGNORECASE),
    re.compile(r"/posting/", flags=re.IGNORECASE),
    re.compile(r"/vacancy/\d+", flags=re.IGNORECASE),
    re.compile(r"/vacancies/\d+", flags=re.IGNORECASE),
    re.compile(r"/project/", flags=re.IGNORECASE),
    re.compile(r"/projects/[^/?#]{4,}", flags=re.IGNORECASE),
    re.compile(r"/contract/", flags=re.IGNORECASE),
    re.compile(r"/freelance/", flags=re.IGNORECASE),
    re.compile(r"/gig/", flags=re.IGNORECASE),
    re.compile(r"/brief/", flags=re.IGNORECASE),
    re.compile(r"/rfp/", flags=re.IGNORECASE),
    re.compile(r"/request-for-proposal", flags=re.IGNORECASE),
    re.compile(r"/consulting/", flags=re.IGNORECASE),
    re.compile(r"[?&]jk=[a-z0-9]{8,}", flags=re.IGNORECASE),
)

OPPORTUNITY_DETAIL_KINDS = {"job_post", "project_post", "request_for_proposal", "consulting_brief"}
OPPORTUNITY_FEED_KINDS = {"search_results", "project_search_results", "mixed_opportunity_feed"}


def _normalized_text(value: str) -> str:
    return normalize_match_text(unquote(str(value or "")))


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower().strip(".")


def _last_path_segment(url: str) -> str:
    path = urlsplit(url).path.strip("/")
    if not path:
        return ""
    return normalize_match_text(unquote(path.split("/")[-1]))


def _path_depth(url: str) -> int:
    return len([segment for segment in urlsplit(url).path.split("/") if segment])


def _url_query_keys(url: str) -> set[str]:
    return {key.lower() for key, _ in parse_qsl(urlsplit(url).query, keep_blank_values=False)}


def _hint_hits(blob: str, hints: tuple[str, ...]) -> int:
    return sum(blob.count(hint) for hint in hints if hint)


def _detail_link_count(html: str) -> int:
    unique_links: set[str] = set()
    for match in _HREF_RE.finditer(str(html or "")):
        href = unquote(str(match.group("href") or "").strip())
        if not href:
            continue
        if any(pattern.search(href) for pattern in _DETAIL_LINK_PATTERNS):
            unique_links.add(href.lower())
        if len(unique_links) >= 16:
            break
    return len(unique_links)


def is_opportunity_detail_kind(page_kind: str) -> bool:
    return page_kind in OPPORTUNITY_DETAIL_KINDS


def is_opportunity_feed_kind(page_kind: str) -> bool:
    return page_kind in OPPORTUNITY_FEED_KINDS


def classify_page_kind(url: str, *, html: str = "", text: str = "") -> str:
    lower_url = unquote(str(url or "")).lower()
    normalized_html = _normalized_text(html[:60000])
    normalized_text = _normalized_text(text)
    normalized_blob = " ".join(part for part in (normalized_text, normalized_html) if part).strip()
    lower_html = str(html or "").lower()
    host = _host(url)
    path = unquote(urlsplit(url).path.lower().rstrip("/") or "/")
    last_segment = _last_path_segment(url)
    query_keys = _url_query_keys(url)
    detail_link_count = _detail_link_count(html)

    search_score = 0
    job_score = 0
    project_score = 0
    rfp_score = 0
    consulting_score = 0
    negative_hits = _hint_hits(normalized_blob, NEGATIVE_PAGE_HINTS)

    if any(hint in lower_url for hint in SEARCH_RESULT_URL_HINTS):
        search_score += 3
    if query_keys & SEARCH_QUERY_KEYS:
        search_score += 2
    if any(hint in normalized_blob for hint in SEARCH_RESULT_TEXT_HINTS):
        search_score += 2
    if _hint_hits(normalized_blob, FILTER_SORT_HINTS) >= 1:
        search_score += 1
    if _hint_hits(normalized_blob, PAGINATION_HINTS) >= 1 and (detail_link_count >= 2 or bool(query_keys)):
        search_score += 1
    if _hint_hits(normalized_blob, REACH_OUT_CTA_HINTS) >= 3:
        search_score += 2
    if any(hint in normalized_blob for hint in LIST_OR_FEED_TEXT_HINTS):
        search_score += 4
    if detail_link_count >= 3:
        search_score += 3
    if last_segment in GENERIC_LISTING_PATH_SEGMENTS:
        search_score += 1

    if any(hint in lower_url for hint in JOB_POST_URL_HINTS):
        job_score += 3
    if '"@type":"jobposting"' in lower_html or '"@type": "jobposting"' in lower_html:
        job_score += 5
    if any(hint in normalized_blob for hint in JOB_POST_TEXT_HINTS):
        job_score += 2
    if any(hint in normalized_blob for hint in PAYMENT_HINTS) and any(hint in normalized_blob for hint in LOCATION_HINTS):
        job_score += 1
    if _hint_hits(normalized_blob, REACH_OUT_CTA_HINTS) >= 1 and detail_link_count <= 1:
        job_score += 1
    if _path_depth(url) >= 2 and last_segment and last_segment not in GENERIC_LISTING_PATH_SEGMENTS:
        if "-" in last_segment or re.search(r"\d", last_segment):
            job_score += 1

    if any(hint in lower_url for hint in PROJECT_POST_URL_HINTS):
        project_score += 3
    if any(hint in normalized_blob for hint in PROJECT_POST_TEXT_HINTS):
        project_score += 3
    if any(hint in lower_url for hint in PROJECT_POST_URL_HINTS) and _path_depth(url) >= 2 and last_segment and last_segment not in GENERIC_LISTING_PATH_SEGMENTS:
        project_score += 1
    if _hint_hits(normalized_blob, REACH_OUT_CTA_HINTS) >= 1 and any(
        hint in normalized_blob for hint in ("budget", "rate", "proposal", "client", "scope")
    ):
        project_score += 1
    if _path_depth(url) >= 2 and last_segment and last_segment not in GENERIC_LISTING_PATH_SEGMENTS:
        if "-" in last_segment or re.search(r"\d", last_segment):
            project_score += 1

    if any(hint in lower_url for hint in RFP_URL_HINTS):
        rfp_score += 3
    if any(hint in lower_url for hint in RFP_URL_HINTS) and _path_depth(url) >= 2 and last_segment and last_segment not in GENERIC_LISTING_PATH_SEGMENTS:
        rfp_score += 2
    if any(hint in normalized_blob for hint in RFP_TEXT_HINTS):
        rfp_score += 4

    if any(hint in lower_url for hint in CONSULTING_URL_HINTS):
        consulting_score += 2
    if any(hint in lower_url for hint in CONSULTING_URL_HINTS) and _path_depth(url) >= 2 and last_segment and last_segment not in GENERIC_LISTING_PATH_SEGMENTS:
        consulting_score += 2
    if any(hint in normalized_blob for hint in CONSULTING_TEXT_HINTS):
        consulting_score += 4

    if host.endswith("linkedin.com") and path.startswith("/jobs/") and not path.startswith("/jobs/view/"):
        search_score += 4
    if host.endswith("uiuxjobsboard.com") and path in {"/design-jobs", "/design-jobs/"}:
        search_score += 4
    if host.endswith("weworkremotely.com") and path.startswith("/categories/"):
        search_score += 4
    if host.endswith("workatastartup.com") and path.startswith("/jobs"):
        search_score += 4
    if host.endswith("ycombinator.com") and path == "/jobs":
        search_score += 4
    if host.endswith("wellfound.com") and path.startswith("/role/"):
        search_score += 4
    if host.endswith("builtin.com") and path.startswith("/jobs"):
        search_score += 4
    if host.endswith("flexjobs.com") and path.startswith("/remote-jobs/"):
        search_score += 4
    if host.endswith("fl.ru") and path.startswith("/projects") and not re.search(r"/projects/\d+/[^/]+\.html$", path):
        search_score += 4
    if host.endswith("freelance.ru") and path.startswith("/project/search"):
        search_score += 4
    if host.endswith("workspace.ru") and (
        path in {"/tenders", "/tenders/", "/web-design", "/web-design/"}
        or (path.startswith("/tenders/") and not re.search(r"/tenders/[^/]+-\d+$", path))
    ):
        search_score += 4
    if host.endswith("kwork.ru") and path == "/projects":
        search_score += 4
    if host.endswith("weworkremotely.com") and path.startswith("/remote-jobs/") and path != "/remote-jobs/new":
        job_score += 4
    if host.endswith("workatastartup.com") and "/companies/" in path and "/jobs/" in path:
        job_score += 4
    if host.endswith("ycombinator.com") and "/companies/" in path and "/jobs/" in path:
        job_score += 4
    if host.endswith("wellfound.com") and (("/company/" in path and "/jobs/" in path) or path.startswith("/jobs/")):
        job_score += 4
    if host.endswith("ziprecruiter.ie") and path.startswith("/jobs/") and path != "/jobs/search":
        job_score += 4
    if host.endswith("ziprecruiter.com") and path.startswith("/jobs/") and path != "/jobs/search":
        job_score += 4
    if host.endswith("indeed.com") and path == "/viewjob" and "jk" in query_keys:
        job_score += 4
    if host.endswith("fl.ru") and re.search(r"/projects/\d+/[^/]+\.html$", path):
        project_score += 4
    if host.endswith("freelance.ru") and re.search(r"/projects/[^/]+-\d+\.html$", path):
        project_score += 4
    if host.endswith("freelance.ru") and re.search(r"/tender/view/\d+", path):
        rfp_score += 4
    if host.endswith("workspace.ru") and re.search(r"/tenders/[^/]+-\d+$", path):
        rfp_score += 4
    if host.endswith("kwork.ru") and re.search(r"/projects/\d+/view$", path):
        project_score += 4

    if any(hint in normalized_blob for hint in LIST_OR_FEED_TEXT_HINTS):
        job_score = max(0, job_score - 2)
        project_score = max(0, project_score - 2)
        rfp_score = max(0, rfp_score - 2)
        consulting_score = max(0, consulting_score - 2)
    if negative_hits:
        search_score = max(0, search_score - 2)
        job_score = max(0, job_score - 4)
        project_score = max(0, project_score - 4)
        rfp_score = max(0, rfp_score - 3)
        consulting_score = max(0, consulting_score - 3)
    if detail_link_count >= 3 and '"@type":"jobposting"' not in lower_html:
        job_score = max(0, job_score - 2)
        project_score = max(0, project_score - 1)

    if rfp_score >= max(4, search_score, job_score, project_score, consulting_score):
        return "request_for_proposal"
    if consulting_score >= max(4, search_score, job_score, project_score):
        return "consulting_brief"
    if project_score >= max(4, search_score, job_score):
        return "project_post"
    if job_score >= max(3, search_score, project_score):
        return "job_post"

    if search_score >= 3:
        project_signal = (
            project_score >= 2
            or any(hint in normalized_blob for hint in PROJECT_POST_TEXT_HINTS + RFP_TEXT_HINTS + CONSULTING_TEXT_HINTS)
            or any(term in normalized_blob for term in ("project", "projects", "contract", "contracts", "freelance", "brief", "briefs", "scope", "scopes"))
        )
        job_signal = job_score >= 2 or any(hint in normalized_blob for hint in JOB_POST_TEXT_HINTS)
        if (
            any(hint in normalized_blob for hint in MIXED_FEED_HINTS)
            or (project_signal and job_signal)
            or ("opportunities" in lower_url and any(hint in normalized_blob for hint in ("project", "contract", "freelance", "gig", "role", "roles")))
        ):
            return "mixed_opportunity_feed"
        if project_signal or any(hint in lower_url for hint in ("/projects", "/freelance-jobs", "/briefs", "/requests", "/rfp", "/web-design")):
            return "project_search_results"
        return "search_results"
    return "other"


def url_looks_like_search_or_job_page(url: str) -> bool:
    return classify_page_kind(url) in OPPORTUNITY_DETAIL_KINDS | OPPORTUNITY_FEED_KINDS
