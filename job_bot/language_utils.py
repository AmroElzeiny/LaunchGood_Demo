from __future__ import annotations

import re
import unicodedata


_HTML_LANG_RE = re.compile(r"<html[^>]*\blang=['\"]?(?P<lang>[a-zA-Z-]+)", flags=re.IGNORECASE)
_META_LOCALE_RE = re.compile(
    r"<meta[^>]+(?:content-language|og:locale)[^>]+content=['\"]?(?P<lang>[a-zA-Z_-]+)",
    flags=re.IGNORECASE,
)
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_RUSSIAN_STEM_SUFFIXES = (
    "иями",
    "ями",
    "ами",
    "его",
    "ого",
    "ему",
    "ому",
    "ыми",
    "ими",
    "ией",
    "ий",
    "ый",
    "ой",
    "ая",
    "яя",
    "ое",
    "ее",
    "ые",
    "ие",
    "ам",
    "ям",
    "ах",
    "ях",
    "ов",
    "ев",
    "ом",
    "ем",
    "ую",
    "юю",
    "а",
    "я",
    "ы",
    "и",
    "е",
    "у",
    "ю",
    "о",
)
_RUSSIAN_SYNONYM_PACKS = (
    ("ux", "ui", "uxui", "ux/ui"),
    ("designer", "design", "дизайнер", "дизайн"),
    ("landing", "landing page", "лендинг", "лендинг пейдж"),
    ("dashboard", "дашборд", "дашборды"),
    ("wireframe", "wireframes", "вайрфрейм", "вайрфреймы"),
    ("prototype", "prototypes", "прототип", "прототипы"),
    ("figma", "фигма"),
    ("design system", "design systems", "дизайн система", "дизайн системы"),
    ("brief", "бриф"),
    ("retainer", "ретейнер"),
    ("consulting", "консалтинг", "консультация", "консультации"),
    ("agency", "agencies", "агентство", "агентства", "студия"),
    ("team", "команда", "команды"),
    ("hourly", "per hour", "почасово", "почасовой", "почасовая"),
    ("fixed", "fixed price", "фикс", "фиксированный", "фиксированная"),
    ("remote", "удаленно", "удалённо", "удаленка", "удалёнка"),
)


def contains_cyrillic(value: str) -> bool:
    return bool(_CYRILLIC_RE.search(str(value or "")))


def detect_language(*, text: str = "", html: str = "") -> str:
    html_text = str(html or "")
    for regex in (_HTML_LANG_RE, _META_LOCALE_RE):
        match = regex.search(html_text)
        if match:
            lang = str(match.group("lang") or "").strip().lower()
            if lang.startswith("ru"):
                return "ru"
            if lang.startswith("en"):
                return "en"

    sample = " ".join(part for part in (text, html_text[:4000]) if part).strip()
    if not sample:
        return "en"

    cyrillic_count = len(_CYRILLIC_RE.findall(sample))
    latin_count = len(_LATIN_RE.findall(sample))
    if cyrillic_count >= 6 and cyrillic_count >= max(3, latin_count):
        return "ru"
    return "en"


def normalize_match_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "").strip().lower())
    normalized = normalized.replace("\u0451", "\u0435")
    normalized = normalized.replace("&", " and ")
    normalized = normalized.replace("/", " ")
    normalized = normalized.replace("-", " ")
    normalized = re.sub(r"[^\w+#]+", " ", normalized, flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalized).strip()


def russian_stem(token: str) -> str:
    normalized = normalize_match_text(token)
    if not normalized or not contains_cyrillic(normalized) or len(normalized) <= 4:
        return normalized
    for suffix in _RUSSIAN_STEM_SUFFIXES:
        if normalized.endswith(suffix) and len(normalized) - len(suffix) >= 3:
            return normalized[: -len(suffix)]
    return normalized


def synonym_terms(value: str) -> set[str]:
    normalized = normalize_match_text(value)
    if not normalized:
        return set()
    for pack in _RUSSIAN_SYNONYM_PACKS:
        normalized_pack = {normalize_match_text(item) for item in pack if normalize_match_text(item)}
        if normalized in normalized_pack:
            return normalized_pack
    return {normalized}


def tokenize_with_morphology(value: str, *, min_length: int = 1) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for token in tokenize_match_text(value, min_length=min_length, unique=True):
        candidates = {token, russian_stem(token)}
        for synonym in synonym_terms(token):
            candidates.add(synonym)
            stemmed = russian_stem(synonym)
            if stemmed:
                candidates.add(stemmed)
        for candidate in candidates:
            cleaned = candidate.strip()
            if len(cleaned) < min_length or cleaned in seen:
                continue
            seen.add(cleaned)
            ordered.append(cleaned)
    return ordered


def tokenize_match_text(value: str, *, min_length: int = 1, unique: bool = False) -> list[str]:
    tokens = [token for token in normalize_match_text(value).split() if len(token) >= min_length]
    if not unique:
        return tokens

    ordered: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        ordered.append(token)
    return ordered


def localized_text(language: str, *, en: str, ru: str) -> str:
    return ru if str(language or "").strip().lower().startswith("ru") else en
