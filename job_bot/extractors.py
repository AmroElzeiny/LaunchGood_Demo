from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

from job_bot.date_parsing import find_date_in_text_to_iso, parse_date_text_to_iso
from job_bot.language_utils import detect_language, normalize_match_text
from job_bot.models import OpportunityCard
from job_bot.opportunity_fields import OPPORTUNITY_FIELD_DICTIONARY


_PROJECT_MARKERS = (
    "project",
    "contract",
    "freelance",
    "gig",
    "brief",
    "rfp",
    "request for proposal",
    "scope",
    "deliverables",
    "consulting",
    "proposal",
    "retainer",
    "client",
    "requester",
    "проект",
    "тендер",
    "заказ",
    "бриф",
    "запрос предложений",
    "консалтинг",
    "заказчик",
)
_DETAIL_MARKERS = (
    "budget",
    "rate",
    "payment",
    "fixed price",
    "hourly",
    "duration",
    "deadline",
    "timeline",
    "start",
    "бюджет",
    "ставка",
    "оплата",
    "срок",
    "дедлайн",
    "отклик",
)
_LISTING_TYPE_HINTS = {"itemlist", "collectionpage", "searchresultspage", "searchresults"}
_METADATA_ONLY_TYPES = {"faqpage", "webpage"}
_ARTICLE_TYPES = {"article", "newsarticle", "blogposting"}
_PROJECT_KIND_ALIASES = {
    "project_post": "project_post",
    "contract_post": "contract_post",
    "job_post": "mixed_opportunity_post",
    "request_for_proposal": "rfp_post",
    "rfp_post": "rfp_post",
    "consulting_brief": "consulting_request",
    "consulting_request": "consulting_request",
    "mixed_opportunity_feed": "mixed_opportunity_post",
    "mixed_opportunity_post": "mixed_opportunity_post",
}
_CARD_KIND_MAP = {
    "project_post": "project_post",
    "contract_post": "project_post",
    "rfp_post": "request_for_proposal",
    "consulting_request": "consulting_brief",
    "mixed_opportunity_post": "project_post",
}
_MONEY_RE = re.compile(
    r"((?:usd|eur|gbp|cad|aud|aed|egp|rub|\$|€|£|₽|руб)\s*)?\d[\d,]*(?:\.\d+)?"
    r"(?:\s*(?:-|to)\s*(?:(?:usd|eur|gbp|cad|aud|aed|egp|rub|\$|€|£|₽|руб)\s*)?\d[\d,]*(?:\.\d+)?)?"
    r"(?:\s*(?:per\s+hour|/hr|/hour|hourly|fixed(?:\s|-)?price|budget|retainer|milestone|commission|в\s+час|в\s+месяц|за\s+проект|руб(?:лей)?))?",
    flags=re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"(?:\d{4}-\d{2}-\d{2}(?:[T ][^ ]+)?)|(?:[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4})|(?:\d{1,2}\s+[A-Z][a-z]{2,8}\s+\d{4})"
)
_TITLE_RE = re.compile(r"<title[^>]*>(?P<value>.*?)</title>", flags=re.IGNORECASE | re.DOTALL)
_META_RE = re.compile(r"<meta\b(?P<attrs>[^>]+)>", flags=re.IGNORECASE)
_LINK_RE = re.compile(r"<a\b(?P<attrs>[^>]*)>(?P<label>.*?)</a>", flags=re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(
    r"(?P<name>[\w:-]+)\s*=\s*(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<bare>[^\s>]+))",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DomTemplate:
    name: str
    page_kinds: tuple[str, ...]
    selectors: dict[str, tuple[str, ...]]
    positive_markers: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()


DOM_TEMPLATES: tuple[DomTemplate, ...] = (
    DomTemplate(
        name="contra_opportunity_detail",
        domains=("contra.com",),
        page_kinds=("project_post", "contract_post", "mixed_opportunity_post"),
        positive_markers=("opportunity", "budget", "skills"),
        selectors={
            "title": ("[data-testid='opportunity-title']", ".opportunity-title", "h1"),
            "counterparty": ("[data-testid='company-name']", ".opportunity-company"),
            "scope_summary": ("[data-testid='opportunity-description']", ".opportunity-description", "article", "main"),
            "payment_terms": ("[data-testid='budget']", ".opportunity-budget", ".budget"),
            "engagement_summary": ("[data-testid='engagement-type']", ".engagement-type"),
            "duration": ("[data-testid='duration']", ".duration"),
            "proposal_deadline": ("[data-testid='deadline']", ".proposal-deadline"),
            "skills_required": ("[data-testid='skills']", ".skills li", ".skills span"),
            "industry": ("[data-testid='industry']", ".industry"),
        },
    ),
    DomTemplate(
        name="project_brief_template",
        page_kinds=("project_post", "contract_post", "mixed_opportunity_post"),
        positive_markers=("deliverables", "budget", "client", "proposal"),
        selectors={
            "title": ("main h1", "article h1", "h1", ".project-title", ".brief-title"),
            "counterparty": (".client-name", ".requester-name", ".company-name"),
            "scope_summary": (".project-description", ".brief-description", ".description", "article", "main"),
            "payment_terms": (".budget", ".rate", ".payment"),
            "engagement_summary": (".engagement-type", ".contract-type", ".project-type"),
            "duration": (".duration", ".project-length", ".timeline"),
            "start_timeline": (".start-date", ".kickoff"),
            "proposal_deadline": (".proposal-deadline", ".deadline"),
            "opportunity_location": (".remote-location", ".location", ".timezone"),
            "skills_required": (".skills li", ".skills span", ".requirements li"),
            "industry": (".industry", ".niche", ".sector"),
        },
    ),
    DomTemplate(
        name="rfp_template",
        page_kinds=("rfp_post",),
        positive_markers=("request for proposal", "proposal due", "submission deadline", "scope of work"),
        selectors={
            "title": ("main h1", "article h1", "h1", ".rfp-title"),
            "counterparty": (".issuer", ".client-name", ".company-name"),
            "scope_summary": (".rfp-description", ".scope-of-work", "article", "main"),
            "payment_terms": (".budget", ".estimated-budget", ".payment"),
            "duration": (".duration", ".term"),
            "proposal_deadline": (".proposal-deadline", ".deadline", ".submission-deadline"),
            "start_timeline": (".start-date", ".timeline"),
            "opportunity_location": (".location", ".region", ".eligibility"),
            "skills_required": (".requirements li", ".skills li", ".skills span"),
            "industry": (".industry", ".sector", ".category"),
        },
    ),
    DomTemplate(
        name="consulting_request_template",
        page_kinds=("consulting_request",),
        positive_markers=("consulting", "advisory", "audit", "retainer"),
        selectors={
            "title": ("main h1", "article h1", "h1", ".consulting-title"),
            "counterparty": (".client-name", ".requester-name", ".company-name"),
            "scope_summary": (".consulting-description", ".scope-of-work", "article", "main"),
            "payment_terms": (".budget", ".rate", ".retainer", ".payment"),
            "engagement_summary": (".engagement-type", ".contract-type", ".retainer-type"),
            "duration": (".duration", ".timeline", ".term"),
            "start_timeline": (".start-date", ".kickoff"),
            "proposal_deadline": (".proposal-deadline", ".deadline"),
            "opportunity_location": (".location", ".remote-location", ".timezone"),
            "skills_required": (".skills li", ".skills span", ".requirements li"),
            "industry": (".industry", ".sector", ".niche"),
        },
    ),
)


EXTRACTOR_REGISTRY: dict[str, tuple[str, ...]] = {
    "project_post": ("contra_opportunity_detail", "project_brief_template"),
    "contract_post": ("contra_opportunity_detail", "project_brief_template"),
    "rfp_post": ("rfp_template",),
    "consulting_request": ("consulting_request_template",),
    "mixed_opportunity_post": ("project_brief_template",),
}


def page_to_text(page: Any) -> str:
    try:
        texts = page.css("body ::text").getall()
    except Exception:  # noqa: BLE001
        return ""
    lines = [" ".join(str(part or "").split()) for part in texts if str(part or "").strip()]
    return " ".join(_unique(lines)).strip()


def _page_lines(page: Any) -> list[str]:
    try:
        texts = page.css("body ::text").getall()
    except Exception:  # noqa: BLE001
        return []
    lines: list[str] = []
    for part in texts:
        normalized = " ".join(str(part or "").split())
        if not normalized:
            continue
        for line in re.split(r"[\n\r]+", normalized):
            cleaned = line.strip(" |:-")
            if cleaned:
                lines.append(cleaned)
    return _unique(lines)


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    items: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        lowered = cleaned.lower()
        if not cleaned or lowered in seen:
            continue
        seen.add(lowered)
        items.append(cleaned)
    return items


def _iter_ld_json_objects(page: Any) -> list[dict[str, Any]]:
    try:
        scripts = page.css('script[type="application/ld+json"]::text').getall()
    except Exception:  # noqa: BLE001
        return []
    objects: list[dict[str, Any]] = []

    def _collect(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("@graph"), list):
                for nested in value["@graph"]:
                    _collect(nested)
            else:
                objects.append(value)
        elif isinstance(value, list):
            for item in value:
                _collect(item)

    for block in scripts:
        try:
            _collect(json.loads(str(block or "").strip()))
        except json.JSONDecodeError:
            continue
    return objects


def _type_values(value: dict[str, Any]) -> list[str]:
    type_value = value.get("@type")
    if isinstance(type_value, list):
        return [str(item).strip().lower() for item in type_value if str(item).strip()]
    return [str(type_value).strip().lower()] if str(type_value or "").strip() else []


def _clean_html_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return " ".join(text.split()).strip()


def _parse_attrs(raw_attrs: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(str(raw_attrs or "")):
        value = match.group("dq") or match.group("sq") or match.group("bare") or ""
        attrs[str(match.group("name") or "").strip().lower()] = value.strip()
    return attrs


def _text_from_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split()).strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "; ".join(part for part in (_text_from_value(item) for item in value) if part).strip(" ;")
    if isinstance(value, dict):
        for key in ("name", "title", "headline", "description", "text", "@id", "url"):
            nested = _text_from_value(value.get(key))
            if nested:
                return nested
        return ""
    return str(value).strip()


def _payment_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    if isinstance(value, list):
        for item in value:
            extracted = _payment_text(item)
            if extracted:
                return extracted
        return ""
    if not isinstance(value, dict):
        return ""
    direct = value.get("price") or value.get("valueAmount") or value.get("amount")
    currency = value.get("currency") or value.get("priceCurrency")
    minimum = value.get("minValue")
    maximum = value.get("maxValue")
    unit = value.get("unitText") or value.get("unitCode")
    pieces: list[str] = []
    if direct is not None:
        pieces.append(str(direct).strip())
    elif minimum is not None or maximum is not None:
        pieces.append(" - ".join(str(item).strip() for item in (minimum, maximum) if item is not None and str(item).strip()))
    if currency:
        pieces.append(str(currency).strip())
    if unit:
        pieces.append(str(unit).strip())
    if any(pieces):
        return " ".join(piece for piece in pieces if piece).strip()
    for key in ("budget", "estimatedCost", "priceRange", "priceSpecification", "offers", "value"):
        extracted = _payment_text(value.get(key))
        if extracted:
            return extracted
    return ""


def _location_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        for item in value:
            extracted = _location_text(item)
            if extracted:
                return extracted
        return ""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return str(value).strip()
    address = value.get("address", value)
    if isinstance(address, dict):
        parts = [
            address.get("streetAddress"),
            address.get("addressLocality"),
            address.get("addressRegion"),
            address.get("postalCode"),
            address.get("addressCountry"),
        ]
        clean = [str(part).strip() for part in parts if str(part or "").strip()]
        if clean:
            return ", ".join(clean)
    return _text_from_value(address)


def _counterparty_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        for item in value:
            extracted = _counterparty_text(item)
            if extracted:
                return extracted
        return ""
    if isinstance(value, dict):
        for key in ("name", "legalName", "alternateName"):
            extracted = _text_from_value(value.get(key))
            if extracted:
                return extracted
    return _text_from_value(value)


def _url_text(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip() or default
    if isinstance(value, list):
        for item in value:
            extracted = _url_text(item, default="")
            if extracted:
                return extracted
        return default
    if isinstance(value, dict):
        for key in ("url", "@id", "mainEntityOfPage", "sameAs"):
            extracted = _url_text(value.get(key), default="")
            if extracted:
                return extracted
    return default


def _normalize_iso_datetime(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return parse_date_text_to_iso(raw) or str(value).strip()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _selector_texts(page: Any, selector: str) -> list[str]:
    try:
        result = page.css(selector)
    except Exception:  # noqa: BLE001
        return []
    raw_values: list[Any] = []
    getter_all = getattr(result, "getall", None)
    if callable(getter_all):
        try:
            raw_values = list(getter_all())
        except Exception:  # noqa: BLE001
            raw_values = []
    if not raw_values:
        getter_one = getattr(result, "get", None)
        if callable(getter_one):
            try:
                one_value = getter_one()
            except Exception:  # noqa: BLE001
                one_value = None
            if one_value is not None:
                raw_values = [one_value]
    return _unique([_clean_html_text(str(value or "")) for value in raw_values if _clean_html_text(str(value or ""))])


def _selector_first_text(page: Any, selectors: tuple[str, ...], *, join_values: bool = False) -> str:
    for selector in selectors:
        values = _selector_texts(page, selector)
        if not values:
            continue
        return "; ".join(values) if join_values else values[0]
    return ""


def _field_markers(field_key: str) -> tuple[str, ...]:
    field_spec = OPPORTUNITY_FIELD_DICTIONARY.get(field_key, {})
    extra_markers = {
        "scope_summary": ("описание", "задача", "что нужно", "тз", "техническое задание", "бриф"),
        "payment_terms": ("бюджет", "ставка", "оплата", "стоимость"),
        "engagement_summary": ("формат работы", "тип проекта", "тип контракта"),
        "duration": ("срок", "сроки", "длительность"),
        "start_timeline": ("старт", "начало", "когда начать"),
        "proposal_deadline": ("дедлайн", "срок подачи", "срок отклика"),
        "opportunity_location": ("локация", "местоположение", "регион", "удаленно"),
        "skills_required": ("навыки", "требования", "скиллы", "инструменты"),
        "counterparty": ("заказчик", "клиент", "компания"),
        "industry": ("индустрия", "ниша", "сфера", "отрасль"),
    }
    base_markers = [str(marker) for marker in field_spec.get("markers", ()) if str(marker).strip()]
    return tuple(base_markers + list(extra_markers.get(field_key, ())))


def _line_with_markers(lines: list[str], markers: tuple[str, ...]) -> str:
    normalized_markers = [normalize_match_text(marker) for marker in markers if marker]
    for raw_line in lines:
        normalized_line = normalize_match_text(raw_line)
        if any(marker in normalized_line for marker in normalized_markers):
            return raw_line.strip()
    return ""


def _value_from_labelled_lines(lines: list[str], field_key: str) -> str:
    for line in lines:
        for marker in _field_markers(field_key):
            pattern = re.compile(rf"{re.escape(marker)}\s*[:\-|]\s*(?P<value>.+)", flags=re.IGNORECASE)
            match = pattern.search(line)
            if match:
                return str(match.group("value") or "").strip()
    return ""


def _money_from_text(text: str) -> str:
    match = _MONEY_RE.search(str(text or ""))
    return match.group(0).strip() if match else ""


def _date_from_text(text: str, *, prefer_future: bool = False) -> str:
    match = _DATE_RE.search(str(text or ""))
    if match:
        return _normalize_iso_datetime(match.group(0).strip()) or match.group(0).strip()
    return find_date_in_text_to_iso(str(text or ""), prefer_future=prefer_future)


def _html_metadata(html: str, url: str) -> dict[str, str]:
    metadata: dict[str, str] = {"contact_url": url}
    title_match = _TITLE_RE.search(str(html or ""))
    if title_match:
        metadata["title"] = _clean_html_text(title_match.group("value"))
    for match in _META_RE.finditer(str(html or "")):
        attrs = _parse_attrs(match.group("attrs"))
        key = normalize_match_text(attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "")
        content = _clean_html_text(attrs.get("content", ""))
        if not key or not content:
            continue
        if key in {"og title", "twitter title", "title"} and not metadata.get("title"):
            metadata["title"] = content
        elif key in {"description", "og description", "twitter description"} and not metadata.get("description"):
            metadata["description"] = content
        elif key in {"og url", "twitter url"}:
            metadata["contact_url"] = content
    return metadata


def _contact_url_from_html(html: str, url: str) -> str:
    best_href = ""
    best_score = -1
    for match in _LINK_RE.finditer(str(html or "")):
        attrs = _parse_attrs(match.group("attrs"))
        href = str(attrs.get("href", "")).strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        label = normalize_match_text(_clean_html_text(match.group("label")))
        score = 0
        if any(marker in label for marker in _field_markers("contact_url")):
            score += 3
        if any(marker in label for marker in ("submit proposal", "proposal", "contact", "reach out", "apply", "inquire")):
            score += 2
        if any(marker in label for marker in ("отклик", "откликнуться", "подать заявку", "отправить предложение", "связаться")):
            score += 2
        if score > best_score:
            best_href = urljoin(url, href)
            best_score = score
    return best_href or url


def _normalize_project_kind(page_kind: str, blob: str = "", url: str = "") -> str:
    normalized = _PROJECT_KIND_ALIASES.get(str(page_kind or "").strip().lower(), "")
    if normalized:
        return normalized
    combined = normalize_match_text(f"{blob} {url}")
    if any(marker in combined for marker in ("request for proposal", "rfp", "tender", "procurement")):
        return "rfp_post"
    if any(marker in combined for marker in ("consulting", "consultant", "advisory", "retainer", "audit")):
        return "consulting_request"
    if any(marker in combined for marker in ("contract", "retainer", "freelance", "gig")):
        return "contract_post"
    return "project_post"


def _card_kind(project_kind: str) -> str:
    return _CARD_KIND_MAP.get(project_kind, "project_post")


def _has_strong_project_markers(text: str) -> bool:
    normalized = normalize_match_text(text)
    project_hits = sum(1 for marker in _PROJECT_MARKERS if marker in normalized)
    detail_hits = sum(1 for marker in _DETAIL_MARKERS if marker in normalized)
    return project_hits >= 2 or (project_hits >= 1 and detail_hits >= 1)


def _candidate_score(values: dict[str, str]) -> int:
    weights = {
        "title": 4,
        "scope_summary": 4,
        "counterparty": 2,
        "payment_terms": 3,
        "engagement_summary": 1,
        "duration": 1,
        "start_timeline": 1,
        "proposal_deadline": 1,
        "opportunity_location": 1,
        "skills_required": 1,
        "industry": 1,
        "contact_url": 2,
    }
    return sum(weight for key, weight in weights.items() if str(values.get(key, "") or "").strip())


def _build_card(
    website: str,
    url: str,
    values: dict[str, str],
    *,
    project_kind: str,
    confidence: float,
    extraction_method: str,
    notes: str,
) -> OpportunityCard | None:
    title = str(values.get("title") or "").strip()
    scope_summary = str(values.get("scope_summary") or "").strip()
    if not title:
        return None
    if not scope_summary:
        scope_summary = title
    payment_terms = str(values.get("payment_terms") or "").strip()
    counterparty = str(values.get("counterparty") or "").strip()
    location = str(values.get("opportunity_location") or "").strip()
    card_kind = _card_kind(project_kind)
    card = OpportunityCard(
        website=website,
        url=url,
        title=" ".join(title.split()[:8]) or "Unknown",
        description=" ".join(scope_summary.split()[:45]) or "Unknown",
        salary=payment_terms or "Unknown",
        location=location or "Unknown",
        is_job_post=card_kind == "job_post",
        confidence=float(confidence),
        extraction_method=extraction_method,
        posted_at_utc=_normalize_iso_datetime(values.get("posted_at_utc", "")),
        notes=notes,
        company=counterparty,
        language=detect_language(
            text=" ".join(
                part
                for part in (
                    title,
                    scope_summary,
                    counterparty,
                    payment_terms,
                    location,
                    values.get("skills_required", ""),
                    values.get("engagement_summary", ""),
                )
                if part
            )
        ),
        is_relevant_opportunity=True,
        opportunity_kind=card_kind,
        client=counterparty,
        requester=counterparty,
        scope_summary=" ".join(scope_summary.split()[:45]) or "Unknown",
        budget=payment_terms or "Unknown",
        duration=str(values.get("duration") or "").strip(),
        commitment_level="",
        skills_required=str(values.get("skills_required") or "").strip(),
        proposal_deadline=_normalize_iso_datetime(values.get("proposal_deadline", "")) or str(values.get("proposal_deadline") or "").strip(),
        start_timeline=_normalize_iso_datetime(values.get("start_timeline", "")) or str(values.get("start_timeline") or "").strip(),
        industry=str(values.get("industry") or "").strip(),
        engagement_type=str(values.get("engagement_summary") or "").strip(),
        remote_location_constraint=location or "Unknown",
        contact_url=str(values.get("contact_url") or url).strip() or url,
    )
    return card


def _structured_blob(obj: dict[str, Any], page_text: str) -> str:
    parts = [
        _text_from_value(obj.get("name")),
        _text_from_value(obj.get("title")),
        _text_from_value(obj.get("headline")),
        _text_from_value(obj.get("description")),
        _text_from_value(obj.get("text")),
        _text_from_value(obj.get("articleBody")),
        _text_from_value(obj.get("category")),
        _text_from_value(obj.get("serviceType")),
        _text_from_value(obj.get("keywords")),
        _text_from_value(obj.get("industry")),
        page_text,
    ]
    return normalize_match_text(" ".join(part for part in parts if part))


def extract_structured_opportunity_card(
    website: str,
    url: str,
    page: Any,
    *,
    page_kind: str = "",
    page_text: str = "",
    html: str = "",
) -> OpportunityCard | None:
    page_text = page_text or page_to_text(page)
    objects = _iter_ld_json_objects(page)
    metadata = _html_metadata(html, url)
    for obj in objects:
        type_values = _type_values(obj)
        if not type_values or not (set(type_values) & _METADATA_ONLY_TYPES):
            continue
        title = _text_from_value(obj.get("name") or obj.get("headline") or obj.get("title"))
        description = _text_from_value(obj.get("description") or obj.get("text"))
        contact_url = _url_text(obj.get("url") or obj.get("@id"), default="")
        if title and not metadata.get("title"):
            metadata["title"] = title
        if description and not metadata.get("description"):
            metadata["description"] = description
        if contact_url:
            metadata["contact_url"] = contact_url
    best_card: OpportunityCard | None = None
    best_score = -1
    for obj in objects:
        type_values = _type_values(obj)
        if any(value in _LISTING_TYPE_HINTS for value in type_values):
            continue
        if type_values and set(type_values) & _METADATA_ONLY_TYPES:
            continue
        blob = _structured_blob(obj, page_text)
        if not any("jobposting" in value for value in type_values):
            if set(type_values) & _ARTICLE_TYPES and not _has_strong_project_markers(blob):
                continue
            if not _has_strong_project_markers(blob):
                continue
        values = {
            "title": _text_from_value(obj.get("title") or obj.get("name") or obj.get("headline")) or metadata.get("title", ""),
            "counterparty": _counterparty_text(
                obj.get("hiringOrganization")
                or obj.get("provider")
                or obj.get("organization")
                or obj.get("author")
                or obj.get("creator")
                or obj.get("seller")
                or obj.get("publisher")
            ),
            "scope_summary": _text_from_value(obj.get("description") or obj.get("text") or obj.get("articleBody")) or metadata.get("description", ""),
            "payment_terms": _payment_text(
                obj.get("budget")
                or obj.get("baseSalary")
                or obj.get("estimatedSalary")
                or obj.get("estimatedCost")
                or obj.get("priceRange")
                or obj.get("offers")
                or obj.get("priceSpecification")
                or obj.get("price")
            ),
            "engagement_summary": _text_from_value(
                obj.get("engagementType")
                or obj.get("employmentType")
                or obj.get("contractType")
                or obj.get("workType")
                or obj.get("serviceType")
                or obj.get("category")
            ),
            "duration": _text_from_value(obj.get("duration") or obj.get("timeRequired") or obj.get("estimatedDuration")),
            "start_timeline": _normalize_iso_datetime(obj.get("startDate") or obj.get("availabilityStarts") or obj.get("startTime")),
            "proposal_deadline": _normalize_iso_datetime(obj.get("validThrough") or obj.get("applicationDeadline") or obj.get("deadline")),
            "opportunity_location": _location_text(obj.get("jobLocation") or obj.get("location") or obj.get("contentLocation") or obj.get("areaServed") or obj.get("eligibleRegion")),
            "skills_required": _text_from_value(obj.get("skills") or obj.get("competencyRequirements") or obj.get("requirements") or obj.get("knowsAbout")),
            "industry": _text_from_value(obj.get("industry") or obj.get("about") or obj.get("sector")),
            "contact_url": _url_text(obj.get("url"), default=metadata.get("contact_url", url)) or metadata.get("contact_url", url),
            "posted_at_utc": _normalize_iso_datetime(obj.get("datePosted") or obj.get("datePublished") or obj.get("validFrom") or obj.get("dateCreated")),
        }
        project_kind = "job_post" if any("jobposting" in value for value in type_values) else _normalize_project_kind(page_kind, blob=blob, url=url)
        if "request for proposal" in blob or "rfp" in blob:
            project_kind = "rfp_post"
        elif any(marker in blob for marker in ("consulting", "consultant", "retainer", "audit", "advisory")):
            project_kind = "consulting_request"
        card = _build_card(
            website,
            url,
            values,
            project_kind=project_kind,
            confidence=0.78 if project_kind == "job_post" else 0.74,
            extraction_method="local-jsonld-fallback",
            notes="Extracted from structured JSON-LD metadata.",
        )
        if card is None:
            continue
        score = _candidate_score(values)
        if score > best_score:
            best_card = card
            best_score = score
    return best_card


def _host(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    return (parsed.netloc or str(value or "").strip()).lower().strip(".")


def _extract_page_title(page: Any, metadata: dict[str, str]) -> str:
    selector_title = _selector_first_text(page, ("main h1", "article h1", "h1"))
    if selector_title:
        return selector_title
    if metadata.get("title"):
        return metadata["title"]
    lines = _page_lines(page)
    return lines[0] if lines else ""


def _scope_summary(lines: list[str], metadata: dict[str, str]) -> str:
    labeled = _value_from_labelled_lines(lines, "scope_summary")
    if labeled:
        return labeled
    summary_line = _line_with_markers(
        lines,
        ("deliverables", "scope", "project overview", "what we need", "brief", "описание", "что нужно", "тз", "техническое задание", "бриф"),
    )
    if summary_line:
        return summary_line
    return metadata.get("description", "") or (lines[0] if lines else "")


def _template_values(template: DomTemplate, *, page: Any, html: str, url: str, metadata: dict[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for field_key, selectors in template.selectors.items():
        values[field_key] = _selector_first_text(page, selectors, join_values=field_key == "skills_required")
    if not values.get("title"):
        values["title"] = _extract_page_title(page, metadata)
    if not values.get("scope_summary"):
        values["scope_summary"] = metadata.get("description", "")
    values["contact_url"] = _contact_url_from_html(html, url)
    return values


def _heuristic_values(*, page: Any, html: str, url: str, page_text: str, metadata: dict[str, str]) -> dict[str, str]:
    lines = _page_lines(page)
    values = {
        "title": _extract_page_title(page, metadata),
        "counterparty": _value_from_labelled_lines(lines, "counterparty"),
        "scope_summary": _scope_summary(lines, metadata),
        "payment_terms": _value_from_labelled_lines(lines, "payment_terms"),
        "engagement_summary": _value_from_labelled_lines(lines, "engagement_summary"),
        "duration": _value_from_labelled_lines(lines, "duration"),
        "start_timeline": _value_from_labelled_lines(lines, "start_timeline"),
        "proposal_deadline": _value_from_labelled_lines(lines, "proposal_deadline"),
        "opportunity_location": _value_from_labelled_lines(lines, "opportunity_location"),
        "skills_required": _value_from_labelled_lines(lines, "skills_required"),
        "industry": _value_from_labelled_lines(lines, "industry"),
        "contact_url": _contact_url_from_html(html, url),
    }
    if not values["payment_terms"]:
        values["payment_terms"] = _money_from_text(_line_with_markers(lines, _field_markers("payment_terms")) or page_text)
    if not values["duration"]:
        values["duration"] = _line_with_markers(lines, ("duration", "project length", "contract length", "timeline", "срок", "сроки", "длительность"))
    if not values["start_timeline"]:
        values["start_timeline"] = _date_from_text(_line_with_markers(lines, ("start", "kickoff", "begin")), prefer_future=True)
    if not values["proposal_deadline"]:
        values["proposal_deadline"] = _date_from_text(
            _line_with_markers(lines, ("proposal deadline", "apply by", "deadline", "proposal due", "submission deadline", "дедлайн", "срок подачи", "срок отклика")),
            prefer_future=True,
        )
    if not values["skills_required"]:
        values["skills_required"] = _line_with_markers(lines, ("skills", "requirements", "tools", "experience with", "навыки", "требования", "скиллы", "инструменты"))
    if not values["counterparty"]:
        values["counterparty"] = _line_with_markers(lines, ("client", "requester", "company", "organization", "agency", "founder", "заказчик", "клиент", "компания"))
    if not values["industry"]:
        values["industry"] = _line_with_markers(lines, ("industry", "niche", "sector", "vertical", "индустрия", "ниша", "сфера", "отрасль"))
    if not values["opportunity_location"]:
        values["opportunity_location"] = _line_with_markers(lines, ("remote", "location", "timezone", "country", "region", "удаленно", "локация", "регион", "страна"))
    if not values["engagement_summary"]:
        values["engagement_summary"] = _line_with_markers(
            lines,
            ("engagement type", "contract type", "project type", "retainer", "consulting", "hourly", "формат работы", "тип проекта", "почасово", "консалтинг"),
        )
    return values


def extract_domain_opportunity_card(
    website: str,
    url: str,
    page: Any,
    *,
    page_kind: str = "",
    page_text: str = "",
    html: str = "",
) -> OpportunityCard | None:
    page_text = page_text or page_to_text(page)
    metadata = _html_metadata(html, url)
    host = _host(url or website)
    project_kind = _normalize_project_kind(page_kind, blob=page_text, url=url)
    blob = normalize_match_text(f"{page_text} {html[:8000]}")
    template_names = EXTRACTOR_REGISTRY.get(project_kind, EXTRACTOR_REGISTRY["project_post"])

    best_card: OpportunityCard | None = None
    best_score = -1
    for template in DOM_TEMPLATES:
        if template.name not in template_names:
            continue
        if template.domains and not any(host == domain or host.endswith(f".{domain}") for domain in template.domains):
            continue
        if template.positive_markers and not any(marker in blob for marker in template.positive_markers):
            continue
        values = _template_values(template, page=page, html=html, url=url, metadata=metadata)
        score = _candidate_score(values)
        if score < 11:
            continue
        card = _build_card(
            website,
            url,
            values,
            project_kind=project_kind,
            confidence=0.70,
            extraction_method=f"local-dom-template-fallback:{template.name}",
            notes=f"Extracted from the {template.name.replace('_', ' ')} DOM template.",
        )
        if card is None:
            continue
        if score > best_score:
            best_card = card
            best_score = score

    if best_card is not None:
        return best_card

    values = _heuristic_values(page=page, html=html, url=url, page_text=page_text, metadata=metadata)
    if _candidate_score(values) < 7:
        return None
    if not _has_strong_project_markers(normalize_match_text(f"{page_text} {' '.join(values.values())} {url}")):
        return None
    return _build_card(
        website,
        url,
        values,
        project_kind=project_kind,
        confidence=0.63,
        extraction_method="local-heuristic-dom-fallback",
        notes="Extracted from heuristic DOM signals on a freelance opportunity page.",
    )


def local_fallback_card(
    website: str,
    url: str,
    page: Any,
    *,
    page_kind: str = "",
    page_text: str = "",
    html: str = "",
) -> OpportunityCard | None:
    page_text = page_text or page_to_text(page)
    structured_card = extract_structured_opportunity_card(
        website,
        url,
        page,
        page_kind=page_kind,
        page_text=page_text,
        html=html,
    )
    if structured_card is not None:
        return structured_card
    return extract_domain_opportunity_card(
        website,
        url,
        page,
        page_kind=page_kind,
        page_text=page_text,
        html=html,
    )
