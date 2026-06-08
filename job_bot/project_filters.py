from __future__ import annotations

import json
import re
from typing import Final

from job_bot.language_utils import normalize_match_text


ACTIVE_PROJECT_FILTER_FIELDS: Final[tuple[str, ...]] = (
    "deliverables",
    "minimum_payment_usd",
    "maximum_payment_usd",
    "minimum_payment_rub",
    "maximum_payment_rub",
)

FULL_PROJECT_FILTER_FIELDS: Final[tuple[str, ...]] = (
    "deliverables",
    "minimum_payment_usd",
    "maximum_payment_usd",
    "minimum_payment_rub",
    "maximum_payment_rub",
    "allow_hidden_budget",
    "duration_preferences",
    "urgent_only",
    "retainer_ok",
    "consulting_ok",
    "team_or_agency_ok",
    "engagement_types",
    "payment_types",
    "client_types",
    "proposal_style",
    "timezone_preferences",
    "language_requirements",
    "recurring_preferences",
)

_BOOL_TRUE_TOKENS: Final[set[str]] = {
    "1",
    "true",
    "yes",
    "y",
    "on",
    "allow",
    "allowed",
    "include",
    "enabled",
    "ok",
    "да",
    "д",
    "вкл",
    "включить",
    "включено",
    "разрешить",
    "разрешено",
}

_BOOL_FALSE_TOKENS: Final[set[str]] = {
    "0",
    "false",
    "no",
    "n",
    "off",
    "block",
    "blocked",
    "exclude",
    "disabled",
    "нет",
    "н",
    "выкл",
    "выключить",
    "выключено",
    "запретить",
    "запрещено",
}

PROJECT_FILTER_CLEAR_TOKENS: Final[set[str]] = {
    "clear",
    "remove",
    "delete",
    "reset",
    "none",
    "empty",
    "очистить",
    "удалить",
    "сбросить",
    "убрать",
    "нет",
    "пусто",
}

_FIELD_SPECS: Final[dict[str, dict[str, object]]] = {
    "deliverables": {
        "kind": "list",
        "labels": {"en": "Deliverables", "ru": "Что нужно сделать"},
        "examples": {
            "en": "landing pages, dashboards, design systems",
            "ru": "лендинги, дашборды, дизайн-системы",
        },
        "aliases": (
            "deliverables",
            "deliverable",
            "scope",
            "project scope",
            "sub-specialty",
            "sub specialty",
            "what needs to be delivered",
            "deliverable scope",
            "результат",
            "результаты",
            "результат работы",
            "что нужно сделать",
            "что нужно",
            "объем работ",
            "объём работ",
            "задача",
            "задачи",
            "тз",
        ),
    },
    "minimum_payment_usd": {
        "kind": "number",
        "labels": {"en": "Min Payment USD", "ru": "Мин. оплата USD"},
        "examples": {"en": "1500", "ru": "1500"},
        "aliases": (
            "min payment usd",
            "minimum payment usd",
            "min usd",
            "minimum usd",
            "min price usd",
            "minimum budget usd",
            "usd minimum",
            "минимальная оплата usd",
            "мин оплата usd",
            "минимум usd",
            "минимальная сумма usd",
            "доллары минимум",
        ),
    },
    "maximum_payment_usd": {
        "kind": "number",
        "labels": {"en": "Max Payment USD", "ru": "Макс. оплата USD"},
        "examples": {"en": "5000", "ru": "5000"},
        "aliases": (
            "max payment usd",
            "maximum payment usd",
            "max usd",
            "maximum usd",
            "max price usd",
            "maximum budget usd",
            "usd maximum",
            "максимальная оплата usd",
            "макс оплата usd",
            "максимум usd",
            "максимальная сумма usd",
            "доллары максимум",
        ),
    },
    "minimum_payment_rub": {
        "kind": "number",
        "labels": {"en": "Min Payment RUB", "ru": "Мин. оплата RUB"},
        "examples": {"en": "120000", "ru": "120000"},
        "aliases": (
            "min payment rub",
            "minimum payment rub",
            "min rub",
            "minimum rub",
            "min payment rubles",
            "minimum payment rubles",
            "рубли минимум",
            "минимальная оплата rub",
            "минимальная оплата руб",
            "мин оплата руб",
            "минимум руб",
            "почасовая ставка руб",
            "фиксированный бюджет руб",
        ),
    },
    "maximum_payment_rub": {
        "kind": "number",
        "labels": {"en": "Max Payment RUB", "ru": "Макс. оплата RUB"},
        "examples": {"en": "350000", "ru": "350000"},
        "aliases": (
            "max payment rub",
            "maximum payment rub",
            "max rub",
            "maximum rub",
            "max payment rubles",
            "maximum payment rubles",
            "рубли максимум",
            "максимальная оплата rub",
            "максимальная оплата руб",
            "макс оплата руб",
            "максимум руб",
        ),
    },
    "allow_hidden_budget": {
        "kind": "bool",
        "labels": {"en": "Allow Hidden Budget", "ru": "Можно без бюджета"},
        "examples": {"en": "yes", "ru": "да"},
        "aliases": (
            "allow hidden budget",
            "hidden budget",
            "budget visibility",
            "скрытый бюджет",
            "видимость бюджета",
            "бюджет обязателен",
        ),
    },
    "duration_preferences": {
        "kind": "list",
        "labels": {"en": "Timeline / Duration", "ru": "Срок проекта"},
        "examples": {"en": "1-2 weeks, under 1 month", "ru": "1-2 недели, до месяца"},
        "aliases": ("timeline", "duration", "project length", "contract length", "term", "срок", "сроки", "длительность"),
    },
    "urgent_only": {
        "kind": "bool",
        "labels": {"en": "Urgent Only", "ru": "Только срочные"},
        "examples": {"en": "no", "ru": "нет"},
        "aliases": ("urgent", "urgency", "срочно", "срочность"),
    },
    "retainer_ok": {
        "kind": "bool",
        "labels": {"en": "Retainer OK", "ru": "Абонентский формат"},
        "examples": {"en": "yes", "ru": "да"},
        "aliases": ("retainer", "monthly retainer", "weekly retainer", "ретейнер", "абонентский формат"),
    },
    "consulting_ok": {
        "kind": "bool",
        "labels": {"en": "Consulting OK", "ru": "Консультации"},
        "examples": {"en": "yes", "ru": "да"},
        "aliases": ("consulting", "advisory", "consultant", "консалтинг", "консультации"),
    },
    "team_or_agency_ok": {
        "kind": "bool",
        "labels": {"en": "Team / Agency OK", "ru": "Команда или агентство"},
        "examples": {"en": "no", "ru": "нет"},
        "aliases": (
            "team or agency",
            "agency",
            "team",
            "studio",
            "команда/агентство",
            "команда / агентство",
            "агентство",
            "команда",
            "студия",
        ),
    },
    "engagement_types": {
        "kind": "list",
        "labels": {"en": "Engagement", "ru": "Формат сотрудничества"},
        "examples": {"en": "project, consulting", "ru": "проект, консультации"},
        "aliases": ("engagement", "engagement type", "work format", "format", "формат работы", "тип проекта"),
        "value_aliases": {
            "project": ("project", "contract", "one-off", "проект", "разовый"),
            "retainer": ("retainer", "ретейнер", "абонентский формат"),
            "consulting": ("consulting", "advisory", "консалтинг", "консультации"),
        },
    },
    "payment_types": {
        "kind": "list",
        "labels": {"en": "Payment Type", "ru": "Формат оплаты"},
        "examples": {"en": "fixed, hourly", "ru": "фикс, почасово"},
        "aliases": ("payment type", "budget type", "тип оплаты", "тип бюджета"),
        "value_aliases": {
            "fixed": ("fixed", "fixed price", "flat fee", "фикс", "фиксированный"),
            "hourly": ("hourly", "per hour", "/hr", "почасово", "почасовая ставка"),
            "retainer": ("retainer", "ретейнер", "абонентский формат"),
            "hidden_budget": ("hidden budget", "undisclosed", "скрытый бюджет"),
        },
    },
    "client_types": {
        "kind": "list",
        "labels": {"en": "Client Type", "ru": "Кто заказчик"},
        "examples": {"en": "direct client, agency", "ru": "прямой клиент, агентство"},
        "aliases": ("client type", "заказчик", "тип заказчика", "клиент"),
        "value_aliases": {
            "direct_client": ("direct client", "client direct", "прямой клиент"),
            "team": ("team", "in-house team", "команда"),
            "agency": ("agency", "studio", "агентство", "студия"),
        },
    },
    "proposal_style": {
        "kind": "list",
        "labels": {"en": "Proposal Style", "ru": "Формат отклика"},
        "examples": {"en": "quick apply, tailored", "ru": "быстрый отклик, персональный отклик"},
        "aliases": ("proposal style", "apply style", "style of proposal", "стиль отклика"),
    },
    "timezone_preferences": {
        "kind": "list",
        "labels": {"en": "Timezone", "ru": "Часовой пояс"},
        "examples": {"en": "UTC+2, Europe-friendly", "ru": "UTC+3, Европа"},
        "aliases": ("timezone", "time zone", "часовой пояс", "таймзона"),
    },
    "language_requirements": {
        "kind": "list",
        "labels": {"en": "Language Requirement", "ru": "Язык"},
        "examples": {"en": "English, Russian", "ru": "английский, русский"},
        "aliases": ("language", "language requirement", "required language", "язык", "требование по языку"),
        "value_aliases": {
            "english": ("english", "английский", "en"),
            "russian": ("russian", "русский", "ru"),
            "bilingual": ("bilingual", "both", "оба языка", "английский и русский"),
        },
    },
    "recurring_preferences": {
        "kind": "list",
        "labels": {"en": "Recurring Preference", "ru": "Регулярность"},
        "examples": {"en": "recurring, one-off", "ru": "регулярно, разовый проект"},
        "aliases": ("recurring", "repeat work", "повторяемость", "регулярность", "повторяемость задач"),
        "value_aliases": {
            "recurring": ("recurring", "ongoing", "regular", "регулярно", "постоянно"),
            "one_off": ("one-off", "one off", "разово", "один раз"),
        },
    },
}

_PROJECT_PREFERENCE_DEFAULTS: Final[dict[str, object]] = {
    "deliverables": [],
    "minimum_payment_usd": None,
    "maximum_payment_usd": None,
    "minimum_payment_rub": None,
    "maximum_payment_rub": None,
    "allow_hidden_budget": None,
    "duration_preferences": [],
    "urgent_only": None,
    "retainer_ok": None,
    "consulting_ok": None,
    "team_or_agency_ok": None,
    "engagement_types": [],
    "payment_types": [],
    "client_types": [],
    "proposal_style": [],
    "timezone_preferences": [],
    "language_requirements": [],
    "recurring_preferences": [],
}

_LIST_FIELDS: Final[set[str]] = {
    field_name
    for field_name, spec in _FIELD_SPECS.items()
    if spec.get("kind") == "list"
}
_NUMBER_FIELDS: Final[set[str]] = {
    field_name
    for field_name, spec in _FIELD_SPECS.items()
    if spec.get("kind") == "number"
}
_BOOL_FIELDS: Final[set[str]] = {
    field_name
    for field_name, spec in _FIELD_SPECS.items()
    if spec.get("kind") == "bool"
}


def _normalize_language(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"en", "ru"} else "en"


def project_filter_label(language: object, field_name: str) -> str:
    normalized = _normalize_language(language)
    labels = _FIELD_SPECS.get(field_name, {}).get("labels", {})
    if isinstance(labels, dict):
        return str(labels.get(normalized) or labels.get("en") or field_name.replace("_", " ").title())
    return field_name.replace("_", " ").title()


def project_filter_example(language: object, field_name: str) -> str:
    normalized = _normalize_language(language)
    examples = _FIELD_SPECS.get(field_name, {}).get("examples", {})
    if isinstance(examples, dict):
        return str(examples.get(normalized) or examples.get("en") or "")
    return ""


def project_filter_clear_tokens() -> set[str]:
    return set(PROJECT_FILTER_CLEAR_TOKENS)


def parse_project_filter_number(value: object) -> float | None:
    normalized = str(value or "").replace(",", "").replace(" ", "").strip()
    match = re.search(r"(-?\d+(?:\.\d+)?)", normalized)
    if match is None:
        return None
    try:
        parsed = float(match.group(1))
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def parse_project_filter_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    normalized = normalize_match_text(str(value or ""))
    if not normalized:
        return None
    if normalized in _BOOL_TRUE_TOKENS:
        return True
    if normalized in _BOOL_FALSE_TOKENS:
        return False
    return None


def _normalize_text_list(value: object) -> list[str]:
    raw_items: list[str] = []
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = re.split(r"[,;|\n]+", value)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            raw_items.extend(re.split(r"[,;|\n]+", str(item)))
    else:
        raw_items = [str(value)]

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        cleaned = re.sub(r"\s+", " ", str(raw_item or "").strip()).strip(" ,;|")
        if not cleaned:
            continue
        lowered = normalize_match_text(cleaned)
        if not lowered or lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(cleaned)
    return normalized


def _canonicalize_list_value(field_name: str, raw_value: str) -> str:
    spec = _FIELD_SPECS.get(field_name, {})
    value_aliases = spec.get("value_aliases", {})
    if not isinstance(value_aliases, dict):
        return raw_value
    normalized_value = normalize_match_text(raw_value)
    for canonical, aliases in value_aliases.items():
        normalized_aliases = {normalize_match_text(str(alias)) for alias in aliases}
        if normalized_value in normalized_aliases:
            return str(canonical)
    return raw_value


def normalize_project_filter_values(field_name: str, raw_value: object) -> list[str]:
    values = [_canonicalize_list_value(field_name, item) for item in _normalize_text_list(raw_value)]
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        dedupe_key = normalize_match_text(value)
        if not dedupe_key or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append(value)
    return normalized


def normalize_project_preferences(value: object) -> dict[str, object]:
    raw: dict[str, object] = {}
    if isinstance(value, dict):
        raw = dict(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped:
            try:
                loaded = json.loads(stripped)
            except json.JSONDecodeError:
                loaded = {}
            if isinstance(loaded, dict):
                raw = dict(loaded)

    if "minimum_payment_usd" not in raw:
        legacy_candidates = [
            parse_project_filter_number(raw.get("minimum_fixed_budget_usd")),
            parse_project_filter_number(raw.get("minimum_hourly_rate_usd")),
        ]
        legacy_candidates = [candidate for candidate in legacy_candidates if candidate is not None]
        if legacy_candidates:
            raw["minimum_payment_usd"] = min(legacy_candidates)

    normalized: dict[str, object] = {}
    for field_name in _LIST_FIELDS:
        normalized[field_name] = normalize_project_filter_values(field_name, raw.get(field_name))
    for field_name in _NUMBER_FIELDS:
        normalized[field_name] = parse_project_filter_number(raw.get(field_name))
    for field_name in _BOOL_FIELDS:
        normalized[field_name] = parse_project_filter_bool(raw.get(field_name))
    return {**_PROJECT_PREFERENCE_DEFAULTS, **normalized}


def _field_alias_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for field_name, spec in _FIELD_SPECS.items():
        lookup[normalize_match_text(field_name)] = field_name
        for alias in spec.get("aliases", ()):
            lookup[normalize_match_text(str(alias))] = field_name
    return lookup


def _split_preference_segments(text: str) -> list[str]:
    return [segment.strip(" •-\t") for segment in re.split(r"[\n;]+", text) if segment.strip()]


def parse_project_preferences_text(text: str, current: dict[str, object] | None = None) -> dict[str, object]:
    preferences = normalize_project_preferences(current or {})
    normalized_text = str(text or "").strip()
    if not normalized_text:
        return preferences

    alias_lookup = _field_alias_lookup()
    parsed_any = False
    for raw_segment in _split_preference_segments(normalized_text):
        segment = raw_segment.strip()
        if not segment:
            continue
        key_text = ""
        raw_value = ""
        if ":" in segment:
            key_text, raw_value = [part.strip() for part in segment.split(":", 1)]
        elif "=" in segment:
            key_text, raw_value = [part.strip() for part in segment.split("=", 1)]
        else:
            normalized_segment = normalize_match_text(segment)
            for alias, field_name in sorted(alias_lookup.items(), key=lambda item: len(item[0]), reverse=True):
                if not alias:
                    continue
                if normalized_segment == alias:
                    key_text = alias
                    raw_value = "yes"
                    break
                if normalized_segment.startswith(f"{alias} "):
                    key_text = alias
                    raw_value = segment[len(alias) :].strip(" -")
                    break
                if normalized_segment.startswith(f"{alias} -"):
                    key_text = alias
                    raw_value = segment[len(alias) + 1 :].strip()
                    break
        field_name = alias_lookup.get(normalize_match_text(key_text))
        if not field_name:
            continue
        parsed_any = True
        raw_value = raw_value.strip()
        if normalize_match_text(raw_value) in PROJECT_FILTER_CLEAR_TOKENS:
            if field_name in _LIST_FIELDS:
                preferences[field_name] = []
            else:
                preferences[field_name] = None
            continue
        if field_name in _NUMBER_FIELDS:
            preferences[field_name] = parse_project_filter_number(raw_value)
            continue
        if field_name in _BOOL_FIELDS:
            parsed_bool = parse_project_filter_bool(raw_value)
            preferences[field_name] = True if parsed_bool is None and not raw_value else parsed_bool
            continue
        current_values = list(preferences.get(field_name) or [])
        merged_values = current_values + normalize_project_filter_values(field_name, raw_value)
        preferences[field_name] = normalize_project_filter_values(field_name, merged_values)

    if not parsed_any:
        preferences["deliverables"] = normalize_project_filter_values("deliverables", normalized_text)
    return preferences


def project_filter_value_labels(language: object, field_name: str) -> dict[str, str]:
    normalized = _normalize_language(language)
    if field_name == "payment_types":
        return {
            "fixed": "Фиксированный" if normalized == "ru" else "Fixed",
            "hourly": "Почасовой" if normalized == "ru" else "Hourly",
            "retainer": "Абонентский формат" if normalized == "ru" else "Retainer",
            "hidden_budget": "Скрытый бюджет" if normalized == "ru" else "Hidden budget",
        }
    if field_name == "engagement_types":
        return {
            "project": "Проект" if normalized == "ru" else "Project",
            "retainer": "Абонентский формат" if normalized == "ru" else "Retainer",
            "consulting": "Консультации" if normalized == "ru" else "Consulting",
        }
    if field_name == "client_types":
        return {
            "direct_client": "Прямой клиент" if normalized == "ru" else "Direct client",
            "team": "Команда" if normalized == "ru" else "Team",
            "agency": "Агентство" if normalized == "ru" else "Agency",
        }
    if field_name == "language_requirements":
        return {
            "english": "Английский" if normalized == "ru" else "English",
            "russian": "Русский" if normalized == "ru" else "Russian",
            "bilingual": "Оба языка" if normalized == "ru" else "Bilingual",
        }
    if field_name == "recurring_preferences":
        return {
            "recurring": "Регулярно" if normalized == "ru" else "Recurring",
            "one_off": "Разово" if normalized == "ru" else "One-off",
        }
    return {}


def _format_money(value: object, *, currency: str) -> str:
    amount = float(value)
    if currency == "RUB":
        return f"₽{amount:,.0f}"
    return f"${amount:,.0f}"


def format_project_filter_value(language: object, field_name: str, value: object) -> str:
    normalized = _normalize_language(language)
    if field_name == "minimum_fixed_budget_usd":
        field_name = "minimum_payment_usd"
    elif field_name == "minimum_hourly_rate_usd":
        field_name = "minimum_payment_usd"
    if value is None or value == "":
        return "Нет" if normalized == "ru" else "None"
    if isinstance(value, bool):
        return "Да" if value and normalized == "ru" else "Нет" if normalized == "ru" else "Yes" if value else "No"
    if field_name in {"minimum_payment_usd", "maximum_payment_usd"}:
        return _format_money(value, currency="USD")
    if field_name in {"minimum_payment_rub", "maximum_payment_rub"}:
        return _format_money(value, currency="RUB")
    if isinstance(value, list):
        labels = project_filter_value_labels(normalized, field_name)
        return ", ".join(labels.get(str(item).lower(), str(item)) for item in value) or ("Нет" if normalized == "ru" else "None")
    labels = project_filter_value_labels(normalized, field_name)
    return labels.get(str(value).lower(), str(value))


def project_preferences_display_lines(
    preferences: dict[str, object] | object,
    *,
    language: object = "en",
    fields: tuple[str, ...] | list[str] | None = None,
    limit: int = 10,
) -> list[str]:
    normalized = normalize_project_preferences(preferences)
    ordered_fields = tuple(fields or FULL_PROJECT_FILTER_FIELDS)
    lines: list[str] = []
    for field_name in ordered_fields:
        value = normalized.get(field_name)
        if value in (None, "", []):
            continue
        lines.append(
            f"{project_filter_label(language, field_name)}: {format_project_filter_value(language, field_name, value)}"
        )
        if len(lines) >= limit:
            break
    return lines
