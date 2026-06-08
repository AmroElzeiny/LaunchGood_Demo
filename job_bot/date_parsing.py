import re
from datetime import datetime, timedelta, timezone


_RUSSIAN_MONTHS = {
    "январь": 1,
    "января": 1,
    "янв": 1,
    "февраль": 2,
    "февраля": 2,
    "фев": 2,
    "март": 3,
    "марта": 3,
    "мар": 3,
    "апрель": 4,
    "апреля": 4,
    "апр": 4,
    "май": 5,
    "мая": 5,
    "июнь": 6,
    "июня": 6,
    "июн": 6,
    "июль": 7,
    "июля": 7,
    "июл": 7,
    "август": 8,
    "августа": 8,
    "авг": 8,
    "сентябрь": 9,
    "сентября": 9,
    "сент": 9,
    "сен": 9,
    "октябрь": 10,
    "октября": 10,
    "окт": 10,
    "ноябрь": 11,
    "ноября": 11,
    "ноя": 11,
    "декабрь": 12,
    "декабря": 12,
    "дек": 12,
}
_RUSSIAN_MONTH_PATTERN = "|".join(sorted(_RUSSIAN_MONTHS, key=len, reverse=True))
_RU_ABSOLUTE_DATE_RE = re.compile(
    rf"(?P<day>\d{{1,2}})\s+(?P<month>{_RUSSIAN_MONTH_PATTERN})(?:\s+(?P<year>20\d{{2}}|\d{{4}}))?",
    flags=re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ][^ ]+)?\b")
_ENGLISH_DATE_PATTERNS = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d")
_RELATIVE_RULES = (
    (re.compile(r"\b(?P<count>\d+)\s+(?:час|часа|часов)\s+назад\b", flags=re.IGNORECASE), "hours", -1),
    (re.compile(r"\b(?P<count>\d+)\s+(?:день|дня|дней)\s+назад\b", flags=re.IGNORECASE), "days", -1),
    (re.compile(r"\b(?P<count>\d+)\s+(?:неделю|недели|недель)\s+назад\b", flags=re.IGNORECASE), "weeks", -1),
    (re.compile(r"\bчерез\s+(?P<count>\d+)\s+(?:час|часа|часов)\b", flags=re.IGNORECASE), "hours", 1),
    (re.compile(r"\bчерез\s+(?P<count>\d+)\s+(?:день|дня|дней)\b", flags=re.IGNORECASE), "days", 1),
    (re.compile(r"\bчерез\s+(?P<count>\d+)\s+(?:неделю|недели|недель)\b", flags=re.IGNORECASE), "weeks", 1),
)
_SIMPLE_RELATIVE_RULES = {
    "сегодня": timedelta(days=0),
    "вчера": timedelta(days=-1),
    "позавчера": timedelta(days=-2),
    "завтра": timedelta(days=1),
    "послезавтра": timedelta(days=2),
}
_DATE_CANDIDATE_PATTERNS = (
    _ISO_DATE_RE,
    _RU_ABSOLUTE_DATE_RE,
    re.compile(r"\b(?:сегодня|вчера|позавчера|завтра|послезавтра)\b", flags=re.IGNORECASE),
    re.compile(r"\b\d+\s+(?:час|часа|часов|день|дня|дней|неделю|недели|недель)\s+назад\b", flags=re.IGNORECASE),
    re.compile(r"\bчерез\s+\d+\s+(?:час|часа|часов|день|дня|дней|неделю|недели|недель)\b", flags=re.IGNORECASE),
)


def _normalized_text(value: str) -> str:
    return str(value or "").strip().lower().replace("ё", "е")


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _assume_year(day: int, month: int, *, now_utc: datetime, prefer_future: bool) -> int:
    candidate = datetime(now_utc.year, month, day, tzinfo=timezone.utc)
    if prefer_future and candidate.date() < now_utc.date():
        return now_utc.year + 1
    if not prefer_future and candidate.date() > now_utc.date() + timedelta(days=2):
        return now_utc.year - 1
    return now_utc.year


def _parse_absolute_date(text: str, *, now_utc: datetime, prefer_future: bool) -> datetime | None:
    normalized = _normalized_text(text)
    iso_match = _ISO_DATE_RE.search(normalized)
    if iso_match:
        raw_iso = iso_match.group(0).replace("z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw_iso)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    ru_match = _RU_ABSOLUTE_DATE_RE.search(normalized)
    if ru_match:
        day = int(ru_match.group("day"))
        month_token = _normalized_text(ru_match.group("month"))
        month = _RUSSIAN_MONTHS.get(month_token)
        if month is not None:
            year_token = ru_match.group("year")
            year = int(year_token) if year_token else _assume_year(day, month, now_utc=now_utc, prefer_future=prefer_future)
            try:
                return datetime(year, month, day, tzinfo=timezone.utc)
            except ValueError:
                return None
    for pattern in _ENGLISH_DATE_PATTERNS:
        try:
            return datetime.strptime(text.strip(), pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_date_text(value: str, *, now_utc: datetime | None = None, prefer_future: bool = False) -> datetime | None:
    now = now_utc.astimezone(timezone.utc) if now_utc is not None else datetime.now(timezone.utc)
    text = _normalized_text(value)
    if not text:
        return None

    for token, delta in _SIMPLE_RELATIVE_RULES.items():
        if re.search(rf"\b{token}\b", text):
            return (now + delta).replace(hour=0, minute=0, second=0, microsecond=0)

    for pattern, unit, direction in _RELATIVE_RULES:
        match = pattern.search(text)
        if match is None:
            continue
        count = int(match.group("count"))
        delta = timedelta(**{unit: count * direction})
        return (now + delta).replace(microsecond=0)

    return _parse_absolute_date(text, now_utc=now, prefer_future=prefer_future)


def parse_date_text_to_iso(value: str, *, now_utc: datetime | None = None, prefer_future: bool = False) -> str:
    parsed = parse_date_text(value, now_utc=now_utc, prefer_future=prefer_future)
    return _iso_utc(parsed) if parsed is not None else ""


def extract_date_candidates(text: str) -> list[str]:
    source = str(text or "")
    seen: set[str] = set()
    ordered: list[str] = []
    for pattern in _DATE_CANDIDATE_PATTERNS:
        for match in pattern.finditer(source):
            value = str(match.group(0) or "").strip()
            lowered = _normalized_text(value)
            if not lowered or lowered in seen:
                continue
            seen.add(lowered)
            ordered.append(value)
    return ordered


def find_date_in_text_to_iso(text: str, *, now_utc: datetime | None = None, prefer_future: bool = False) -> str:
    for candidate in extract_date_candidates(text):
        parsed = parse_date_text_to_iso(candidate, now_utc=now_utc, prefer_future=prefer_future)
        if parsed:
            return parsed
    return ""
