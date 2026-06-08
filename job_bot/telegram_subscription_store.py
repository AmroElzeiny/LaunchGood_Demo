from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from job_bot.project_filters import normalize_project_preferences as normalize_project_preferences_impl


def utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return format_utc_iso(utc_now_dt())


def format_utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalize_website(value: str) -> str:
    raw_value = value.strip()
    parsed = urlparse(raw_value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        filtered_query = [
            (key, item_value)
            for key, item_value in parse_qsl(parsed.query, keep_blank_values=False)
            if not key.strip().lower().startswith("utm_")
            and key.strip().lower() not in {"gclid", "gbraid", "wbraid", "fbclid", "mc_cid", "mc_eid", "vjk", "currentjobid"}
        ]
        normalized_netloc = parsed.netloc.lower()
        normalized_path = parsed.path.rstrip("/")
        normalized = parsed._replace(
            netloc=normalized_netloc,
            path=normalized_path,
            query=urlencode(filtered_query, doseq=True),
            fragment="",
        )
        return urlunparse(normalized)
    return raw_value.rstrip("/")


def normalize_website_currency(value: object) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in {"USD", "$", "DOLLAR", "DOLLARS"}:
        return "USD"
    if normalized in {"RUB", "RUR", "₽", "RUBLE", "RUBLES"}:
        return "RUB"
    return ""


def strip_www_prefix(value: str) -> str:
    lowered = value.strip().lower()
    if lowered.startswith("www."):
        return lowered[4:]
    return lowered


def extract_domain(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.netloc:
        return strip_www_prefix(parsed.netloc)
    return strip_www_prefix(value)


DISABLED_WEBSITE_DOMAINS = {"hh.ru", "rabota.ru", "upwork.com"}
DISABLED_WEBSITE_URLS = {
    normalize_website("https://www.fl.ru/projects/category/dizajn/web-dizajner-verstalschik-dizajn/"),
}


def is_disabled_website(value: str) -> bool:
    normalized_value = normalize_website(value)
    if normalized_value in DISABLED_WEBSITE_URLS:
        return True
    domain = extract_domain(value)
    return bool(domain) and any(domain == blocked or domain.endswith(f".{blocked}") for blocked in DISABLED_WEBSITE_DOMAINS)


def infer_website_currency(value: str) -> str:
    domain = extract_domain(value)
    if domain.endswith(".ru"):
        return "RUB"
    return "USD"


def _coerce_float_or_none(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


SUPPORTED_UI_LANGUAGES = {"en", "ru"}
CANONICAL_ROLE_TITLE = "UX/UI Designer"


def normalize_ui_language(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in SUPPORTED_UI_LANGUAGES else ""


def normalize_role_title(value: object) -> str:
    cleaned = " ".join(str(value or "").split()).strip()
    return CANONICAL_ROLE_TITLE if cleaned else ""


def _coerce_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "y", "on", "allow", "allowed"}:
        return True
    if normalized in {"0", "false", "no", "n", "off", "block", "blocked"}:
        return False
    return default

def normalize_project_preferences(value: object) -> dict[str, object]:
    return normalize_project_preferences_impl(value)


PAID_COMPLETED_STATUSES = {"finished", "confirmed"}
PAYMENT_FINAL_STATUSES = PAID_COMPLETED_STATUSES | {"failed", "expired", "cancelled"}
LOCK_ERROR_MARKERS = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
)


class ResilientSQLiteConnection(sqlite3.Connection):
    _MAX_RETRIES = 6
    _INITIAL_RETRY_DELAY_SECONDS = 0.05

    @staticmethod
    def _is_lock_error(error: sqlite3.OperationalError) -> bool:
        lowered = str(error).lower()
        return any(marker in lowered for marker in LOCK_ERROR_MARKERS)

    def _with_lock_retry(self, operation: object, *args: object, **kwargs: object) -> object:
        delay_seconds = self._INITIAL_RETRY_DELAY_SECONDS
        for attempt in range(self._MAX_RETRIES + 1):
            try:
                return operation(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if attempt >= self._MAX_RETRIES or not self._is_lock_error(exc):
                    raise
                time.sleep(delay_seconds)
                delay_seconds = min(delay_seconds * 2, 0.5)
        raise RuntimeError("unreachable lock-retry fallback")

    def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:
        return self._with_lock_retry(super().execute, sql, parameters)

    def executemany(self, sql: str, seq_of_parameters: object, /) -> sqlite3.Cursor:
        return self._with_lock_retry(super().executemany, sql, seq_of_parameters)

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        return self._with_lock_retry(super().executescript, sql_script)

    def commit(self) -> None:
        self._with_lock_retry(super().commit)


@dataclass(slots=True)
class ActiveSubscription:
    user_id: int
    username: str
    plan: str
    started_at_utc: str
    ends_at_utc: str
    is_active: bool
    source: str
    updated_at_utc: str


@dataclass(slots=True)
class QueuedNotification:
    queue_id: int
    user_id: int
    username: str
    card_url: str
    message_text: str
    delivery_event_id: int | None
    available_after_utc: str
    created_at_utc: str


@dataclass(slots=True)
class QueuedDeliveryJob:
    queue_id: int
    website: str
    card_url: str
    card_json: str
    status: str
    attempt_count: int
    available_after_utc: str
    created_at_utc: str
    updated_at_utc: str
    last_error: str
    started_processing_at_utc: str
    completed_at_utc: str


@dataclass(slots=True)
class PaymentSession:
    local_payment_id: str
    user_id: int
    username: str
    plan: str
    duration_days: int
    amount_usd: float
    provider_payment_id: str
    payment_url: str
    status: str
    created_at_utc: str
    updated_at_utc: str


@dataclass(slots=True)
class QueuedSystemNotice:
    notice_id: int
    user_id: int
    username: str
    notice_key: str
    status: str
    attempt_count: int
    available_after_utc: str
    created_at_utc: str
    updated_at_utc: str
    sent_at_utc: str
    last_error: str


@dataclass(slots=True)
class DeliveryEvent:
    event_id: int
    user_id: int
    username: str
    card_url: str
    card_title: str
    card_company: str
    card_location: str
    card_website: str
    match_reason: str
    telegram_chat_id: int | None
    telegram_message_id: int | None
    sent_at_utc: str
    created_at_utc: str


@dataclass(slots=True)
class ManualReviewCase:
    review_id: int
    user_id: int
    username: str
    card_url: str
    card_website: str
    card_title: str
    card_company: str
    card_location: str
    card_salary: str
    card_language: str
    card_json: str
    review_kind: str
    ambiguity_summary: str
    match_reason: str
    role_title: str
    location_preference: str
    salary_preference: str
    include_no_salary: bool
    keywords_text: str
    context_json: str
    status: str
    reviewer: str
    review_notes: str
    sent_count: int
    admin_chat_id: int | None
    admin_message_id: int | None
    dispatched_at_utc: str
    last_dispatch_error: str
    created_at_utc: str
    updated_at_utc: str
    reviewed_at_utc: str | None


class SubscriptionStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            timeout=60.0,
            factory=ResilientSQLiteConnection,
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = Lock()
        self._conn.execute("PRAGMA busy_timeout=60000;")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA busy_timeout=60000;

                CREATE TABLE IF NOT EXISTS subscriptions (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL DEFAULT '',
                    plan TEXT NOT NULL,
                    started_at_utc TEXT NOT NULL,
                    ends_at_utc TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    source TEXT NOT NULL DEFAULT 'trial',
                    reminder_24h_sent INTEGER NOT NULL DEFAULT 0,
                    expiration_notice_sent INTEGER NOT NULL DEFAULT 0,
                    updated_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trial_claims (
                    user_id INTEGER PRIMARY KEY,
                    claimed_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id INTEGER PRIMARY KEY,
                    ui_language TEXT NOT NULL DEFAULT '',
                    notifications_enabled INTEGER NOT NULL DEFAULT 1,
                    search_method TEXT NOT NULL DEFAULT 'opportunities',
                    role_title TEXT NOT NULL DEFAULT '',
                    location TEXT NOT NULL DEFAULT '',
                    location_country TEXT NOT NULL DEFAULT '',
                    location_state TEXT NOT NULL DEFAULT '',
                    location_city TEXT NOT NULL DEFAULT '',
                    location_confidence REAL NOT NULL DEFAULT 0,
                    salary_range_usd TEXT NOT NULL DEFAULT '',
                    salary_min_usd REAL,
                    salary_max_usd REAL,
                    salary_currency TEXT NOT NULL DEFAULT 'USD',
                    salary_confidence REAL NOT NULL DEFAULT 0,
                    include_no_salary INTEGER NOT NULL DEFAULT 1,
                    project_preferences_json TEXT NOT NULL DEFAULT '{}',
                    delivery_mode TEXT NOT NULL DEFAULT 'instant',
                    timezone_offset_minutes INTEGER NOT NULL DEFAULT 0,
                    updated_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_mute_windows (
                    user_id INTEGER NOT NULL,
                    hour_start INTEGER NOT NULL CHECK(hour_start >= 0 AND hour_start <= 23),
                    PRIMARY KEY (user_id, hour_start)
                );

                CREATE TABLE IF NOT EXISTS user_websites (
                    user_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    currency TEXT NOT NULL DEFAULT '',
                    created_at_utc TEXT NOT NULL,
                    UNIQUE(user_id, url)
                );

                CREATE TABLE IF NOT EXISTS user_keywords (
                    user_id INTEGER NOT NULL,
                    keyword TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    UNIQUE(user_id, keyword)
                );

                CREATE TABLE IF NOT EXISTS user_spheres (
                    user_id INTEGER NOT NULL,
                    sphere TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    UNIQUE(user_id, sphere)
                );

                CREATE TABLE IF NOT EXISTS notification_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    card_url TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    delivery_event_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'queued',
                    available_after_utc TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    sent_at_utc TEXT,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS system_notice_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    notice_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    available_after_utc TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    sent_at_utc TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    UNIQUE(user_id, notice_key)
                );
                CREATE INDEX IF NOT EXISTS idx_system_notice_queue_status
                    ON system_notice_queue(status, available_after_utc ASC, created_at_utc ASC);

                CREATE TABLE IF NOT EXISTS delivery_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    card_url TEXT NOT NULL,
                    card_title TEXT NOT NULL DEFAULT '',
                    card_company TEXT NOT NULL DEFAULT '',
                    card_location TEXT NOT NULL DEFAULT '',
                    card_website TEXT NOT NULL DEFAULT '',
                    match_reason TEXT NOT NULL DEFAULT '',
                    telegram_chat_id INTEGER,
                    telegram_message_id INTEGER,
                    sent_at_utc TEXT NOT NULL DEFAULT '',
                    created_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_delivery_events_user_created
                    ON delivery_events(user_id, created_at_utc DESC);

                CREATE TABLE IF NOT EXISTS delivery_job_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    website TEXT NOT NULL DEFAULT '',
                    card_url TEXT NOT NULL,
                    card_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    available_after_utc TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '',
                    started_processing_at_utc TEXT NOT NULL DEFAULT '',
                    completed_at_utc TEXT NOT NULL DEFAULT '',
                    UNIQUE(card_url)
                );
                CREATE INDEX IF NOT EXISTS idx_delivery_job_queue_status
                    ON delivery_job_queue(status, available_after_utc ASC, created_at_utc ASC);

                CREATE TABLE IF NOT EXISTS match_feedback (
                    delivery_event_id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    vote TEXT NOT NULL,
                    raw_feedback TEXT NOT NULL DEFAULT '',
                    feedback_summary TEXT NOT NULL DEFAULT '',
                    job_title_issue TEXT NOT NULL DEFAULT '',
                    location_issue TEXT NOT NULL DEFAULT '',
                    salary_issue TEXT NOT NULL DEFAULT '',
                    other_issue TEXT NOT NULL DEFAULT '',
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_match_feedback_user_updated
                    ON match_feedback(user_id, updated_at_utc DESC);

                CREATE TABLE IF NOT EXISTS payment_sessions (
                    local_payment_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    plan TEXT NOT NULL,
                    duration_days INTEGER NOT NULL,
                    amount_usd REAL NOT NULL,
                    provider_payment_id TEXT NOT NULL,
                    payment_url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS filter_audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    filter_name TEXT NOT NULL,
                    filter_value TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS filter_mismatch_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    card_url TEXT NOT NULL,
                    card_website TEXT NOT NULL DEFAULT '',
                    mismatch_stage TEXT NOT NULL DEFAULT '',
                    reason_code TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    created_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_filter_mismatch_user_created
                    ON filter_mismatch_events(user_id, created_at_utc DESC);

                CREATE TABLE IF NOT EXISTS inactivity_reports (
                    user_id INTEGER PRIMARY KEY,
                    anchor_utc TEXT NOT NULL DEFAULT '',
                    sent_at_utc TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS manual_review_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    card_url TEXT NOT NULL,
                    card_website TEXT NOT NULL DEFAULT '',
                    card_title TEXT NOT NULL DEFAULT '',
                    card_company TEXT NOT NULL DEFAULT '',
                    card_location TEXT NOT NULL DEFAULT '',
                    card_salary TEXT NOT NULL DEFAULT '',
                    card_language TEXT NOT NULL DEFAULT 'en',
                    card_json TEXT NOT NULL DEFAULT '{}',
                    review_kind TEXT NOT NULL DEFAULT 'ambiguous_match',
                    ambiguity_summary TEXT NOT NULL DEFAULT '',
                    match_reason TEXT NOT NULL DEFAULT '',
                    role_title TEXT NOT NULL DEFAULT '',
                    location_preference TEXT NOT NULL DEFAULT '',
                    salary_preference TEXT NOT NULL DEFAULT '',
                    include_no_salary INTEGER NOT NULL DEFAULT 1,
                    keywords_text TEXT NOT NULL DEFAULT '',
                    context_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    reviewer TEXT NOT NULL DEFAULT '',
                    review_notes TEXT NOT NULL DEFAULT '',
                    sent_count INTEGER NOT NULL DEFAULT 0,
                    admin_chat_id INTEGER,
                    admin_message_id INTEGER,
                    dispatched_at_utc TEXT NOT NULL DEFAULT '',
                    last_dispatch_error TEXT NOT NULL DEFAULT '',
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    reviewed_at_utc TEXT,
                    UNIQUE(user_id, card_url)
                );
                CREATE INDEX IF NOT EXISTS idx_manual_review_cases_status
                    ON manual_review_cases(status, admin_message_id, created_at_utc ASC);

                CREATE TABLE IF NOT EXISTS user_flags (
                    user_id INTEGER PRIMARY KEY,
                    landing_seen INTEGER NOT NULL DEFAULT 0,
                    first_start_admin_notified INTEGER NOT NULL DEFAULT 0,
                    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                """
            )
            self._ensure_column("subscriptions", "source", "TEXT NOT NULL DEFAULT 'trial'")
            self._ensure_column("subscriptions", "reminder_24h_sent", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("subscriptions", "expiration_notice_sent", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("notification_queue", "last_error", "TEXT")
            self._ensure_column("match_feedback", "raw_feedback", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("match_feedback", "feedback_summary", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("match_feedback", "job_title_issue", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("match_feedback", "location_issue", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("match_feedback", "salary_issue", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("match_feedback", "other_issue", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("user_preferences", "ui_language", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("user_preferences", "search_method", "TEXT NOT NULL DEFAULT 'opportunities'")
            self._ensure_column("user_preferences", "role_title", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("user_preferences", "location", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("user_preferences", "location_country", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("user_preferences", "location_state", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("user_preferences", "location_city", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("user_preferences", "location_confidence", "REAL NOT NULL DEFAULT 0")
            self._ensure_column("user_preferences", "salary_range_usd", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("user_preferences", "salary_min_usd", "REAL")
            self._ensure_column("user_preferences", "salary_max_usd", "REAL")
            self._ensure_column("user_preferences", "salary_currency", "TEXT NOT NULL DEFAULT 'USD'")
            self._ensure_column("user_preferences", "salary_confidence", "REAL NOT NULL DEFAULT 0")
            self._ensure_column("user_preferences", "include_no_salary", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column("user_preferences", "project_preferences_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column("user_preferences", "delivery_mode", "TEXT NOT NULL DEFAULT 'instant'")
            self._ensure_column("user_preferences", "timezone_offset_minutes", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("user_websites", "currency", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("notification_queue", "delivery_event_id", "INTEGER")
            self._ensure_column("system_notice_queue", "username", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("system_notice_queue", "status", "TEXT NOT NULL DEFAULT 'pending'")
            self._ensure_column("system_notice_queue", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("system_notice_queue", "available_after_utc", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("system_notice_queue", "updated_at_utc", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("system_notice_queue", "sent_at_utc", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("system_notice_queue", "last_error", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("delivery_events", "telegram_chat_id", "INTEGER")
            self._ensure_column("delivery_events", "telegram_message_id", "INTEGER")
            self._ensure_column("delivery_events", "sent_at_utc", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("delivery_job_queue", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("delivery_job_queue", "last_error", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("delivery_job_queue", "started_processing_at_utc", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("delivery_job_queue", "completed_at_utc", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("manual_review_cases", "card_company", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("manual_review_cases", "card_language", "TEXT NOT NULL DEFAULT 'en'")
            self._ensure_column("manual_review_cases", "context_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column("filter_mismatch_events", "reason_code", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("user_flags", "first_start_admin_notified", "INTEGER NOT NULL DEFAULT 0")
            self._conn.execute(
                """
                UPDATE user_preferences
                SET search_method = 'opportunities'
                WHERE search_method IS NULL OR lower(search_method) NOT IN ('opportunities', 'jobs_contracts_projects')
                """
            )
            self._conn.execute(
                """
                UPDATE user_preferences
                SET project_preferences_json = '{}'
                WHERE project_preferences_json IS NULL OR trim(project_preferences_json) = ''
                """
            )
            self._conn.execute(
                """
                UPDATE user_preferences
                SET ui_language = ''
                WHERE ui_language IS NULL OR trim(ui_language) = '' OR lower(trim(ui_language)) NOT IN ('en', 'ru')
                """
            )
            self._conn.execute(
                """
                UPDATE user_preferences
                SET role_title = ?
                WHERE trim(role_title) <> ''
                """,
                (CANONICAL_ROLE_TITLE,),
            )
            self._conn.execute(
                """
                UPDATE user_websites
                SET currency = CASE
                    WHEN lower(url) LIKE '%.ru%' THEN 'RUB'
                    ELSE 'USD'
                END
                WHERE currency IS NULL OR trim(currency) = ''
                """
            )
            self._queue_disabled_website_notices()
            self._purge_disabled_websites()
            self._conn.commit()

    def _purge_disabled_websites(self) -> None:
        if not DISABLED_WEBSITE_DOMAINS and not DISABLED_WEBSITE_URLS:
            return
        clauses: list[str] = []
        parameters: list[str] = []
        for domain in sorted(DISABLED_WEBSITE_DOMAINS):
            clauses.append("lower(url) LIKE ?")
            parameters.append(f"%{domain}%")
        for exact_url in sorted(DISABLED_WEBSITE_URLS):
            clauses.append("rtrim(lower(url), '/') = ?")
            parameters.append(exact_url.lower())
        self._conn.execute(
            f"DELETE FROM user_websites WHERE {' OR '.join(clauses)}",  # noqa: S608
            tuple(parameters),
        )

    def _queue_disabled_website_notices(self) -> None:
        if not DISABLED_WEBSITE_DOMAINS and not DISABLED_WEBSITE_URLS:
            return
        now_utc = utc_now_iso()
        for domain in sorted(DISABLED_WEBSITE_DOMAINS):
            rows = self._conn.execute(
                """
                SELECT DISTINCT uw.user_id, COALESCE(NULLIF(s.username, ''), '') AS username
                FROM user_websites AS uw
                LEFT JOIN subscriptions AS s
                  ON s.user_id = uw.user_id
                WHERE lower(uw.url) LIKE ?
                ORDER BY uw.user_id ASC
                """,
                (f"%{domain}%",),
            ).fetchall()
            notice_key = f"website_removed:{domain}"
            for row in rows:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO system_notice_queue (
                        user_id,
                        username,
                        notice_key,
                        status,
                        attempt_count,
                        available_after_utc,
                        created_at_utc,
                        updated_at_utc,
                        sent_at_utc,
                        last_error
                    )
                    VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, '', '')
                    """,
                    (
                        int(row["user_id"]),
                        str(row["username"] or ""),
                        notice_key,
                        now_utc,
                        now_utc,
                        now_utc,
                    ),
                )
        for exact_url in sorted(DISABLED_WEBSITE_URLS):
            rows = self._conn.execute(
                """
                SELECT DISTINCT uw.user_id, COALESCE(NULLIF(s.username, ''), '') AS username
                FROM user_websites AS uw
                LEFT JOIN subscriptions AS s
                  ON s.user_id = uw.user_id
                WHERE rtrim(lower(uw.url), '/') = ?
                ORDER BY uw.user_id ASC
                """,
                (exact_url.lower(),),
            ).fetchall()
            notice_key = f"website_removed:{exact_url}"
            for row in rows:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO system_notice_queue (
                        user_id,
                        username,
                        notice_key,
                        status,
                        attempt_count,
                        available_after_utc,
                        created_at_utc,
                        updated_at_utc,
                        sent_at_utc,
                        last_error
                    )
                    VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, '', '')
                    """,
                    (
                        int(row["user_id"]),
                        str(row["username"] or ""),
                        notice_key,
                        now_utc,
                        now_utc,
                        now_utc,
                    ),
                )

    def _ensure_column(self, table: str, column: str, sql_type: str) -> None:
        columns = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}  # noqa: S608
        if column not in columns:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")  # noqa: S608

    def _deactivate_expired_subscriptions(self) -> None:
        now_utc = utc_now_iso()
        self._conn.execute(
            """
            UPDATE subscriptions
            SET is_active = 0, updated_at_utc = ?
            WHERE is_active = 1 AND ends_at_utc <= ?
            """,
            (now_utc, now_utc),
        )
        self._conn.execute(
            """
            UPDATE notification_queue
            SET status = 'cancelled',
                last_error = 'subscription_inactive'
            WHERE status = 'queued'
              AND user_id IN (
                  SELECT user_id
                  FROM subscriptions
                  WHERE is_active = 0 AND ends_at_utc <= ?
              )
            """,
            (now_utc,),
        )
        self._conn.execute(
            """
            UPDATE user_preferences
            SET notifications_enabled = 0,
                updated_at_utc = ?
            WHERE user_id IN (
                SELECT user_id
                FROM subscriptions
                WHERE is_active = 0 AND ends_at_utc <= ?
            )
            """,
            (now_utc, now_utc),
        )
        # This helper is invoked from read paths; commit to avoid leaving a write
        # transaction open and holding SQLite locks across requests/processes.
        self._conn.commit()

    def upsert_trial_subscription(
        self,
        user_id: int,
        username: str,
        started_at_utc: str,
        ends_at_utc: str,
    ) -> bool:
        updated_at_utc = utc_now_iso()
        with self._lock:
            already_claimed = self._conn.execute(
                "SELECT 1 FROM trial_claims WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            if already_claimed is not None:
                return False
            self._conn.execute(
                """
                INSERT INTO trial_claims (user_id, claimed_at_utc)
                VALUES (?, ?)
                """,
                (user_id, updated_at_utc),
            )
            self._conn.execute(
                """
                INSERT INTO subscriptions (
                    user_id, username, plan, started_at_utc, ends_at_utc, is_active, source,
                    reminder_24h_sent, expiration_notice_sent, updated_at_utc
                )
                VALUES (?, ?, 'trial_7d', ?, ?, 1, 'trial', 0, 0, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    plan = 'trial_7d',
                    started_at_utc = excluded.started_at_utc,
                    ends_at_utc = excluded.ends_at_utc,
                    is_active = 1,
                    source = 'trial',
                    reminder_24h_sent = 0,
                    expiration_notice_sent = 0,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (user_id, username, started_at_utc, ends_at_utc, updated_at_utc),
            )
            self._conn.commit()
            return True

    def has_claimed_trial(self, user_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM trial_claims WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            return row is not None

    def clear_all_trial_claims(self) -> int:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM trial_claims")
            self._conn.commit()
            return int(cursor.rowcount or 0)

    def activate_paid_subscription(
        self,
        user_id: int,
        username: str,
        plan: str,
        duration_days: int,
    ) -> ActiveSubscription:
        started_dt = utc_now_dt()
        with self._lock:
            existing_row = self._conn.execute(
                """
                SELECT started_at_utc, ends_at_utc, is_active
                FROM subscriptions
                WHERE user_id = ?
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            extension_base_dt = started_dt
            if existing_row is not None and bool(existing_row["is_active"]):
                try:
                    existing_ends_dt = parse_utc_iso(str(existing_row["ends_at_utc"]))
                except ValueError:
                    existing_ends_dt = started_dt
                if existing_ends_dt > extension_base_dt:
                    extension_base_dt = existing_ends_dt
            ends_dt = extension_base_dt + timedelta(days=duration_days)
            started_at_utc = format_utc_iso(started_dt)
            ends_at_utc = format_utc_iso(ends_dt)
            updated_at_utc = utc_now_iso()
            self._conn.execute(
                """
                INSERT INTO subscriptions (
                    user_id, username, plan, started_at_utc, ends_at_utc, is_active, source,
                    reminder_24h_sent, expiration_notice_sent, updated_at_utc
                )
                VALUES (?, ?, ?, ?, ?, 1, 'payment', 0, 0, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    plan = excluded.plan,
                    started_at_utc = excluded.started_at_utc,
                    ends_at_utc = excluded.ends_at_utc,
                    is_active = 1,
                    source = 'payment',
                    reminder_24h_sent = 0,
                    expiration_notice_sent = 0,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (user_id, username, plan, started_at_utc, ends_at_utc, updated_at_utc),
            )
            self._conn.commit()

        return ActiveSubscription(
            user_id=user_id,
            username=username,
            plan=plan,
            started_at_utc=started_at_utc,
            ends_at_utc=ends_at_utc,
            is_active=True,
            source="payment",
            updated_at_utc=updated_at_utc,
        )

    def get_active_subscription(self, user_id: int) -> ActiveSubscription | None:
        with self._lock:
            self._deactivate_expired_subscriptions()
            row = self._conn.execute(
                """
                SELECT user_id, username, plan, started_at_utc, ends_at_utc, is_active, source, updated_at_utc
                FROM subscriptions
                WHERE user_id = ? AND is_active = 1
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return None
            return ActiveSubscription(
                user_id=int(row["user_id"]),
                username=str(row["username"]),
                plan=str(row["plan"]),
                started_at_utc=str(row["started_at_utc"]),
                ends_at_utc=str(row["ends_at_utc"]),
                is_active=bool(row["is_active"]),
                source=str(row["source"]),
                updated_at_utc=str(row["updated_at_utc"]),
            )

    def get_active_subscribers(self) -> list[tuple[int, str]]:
        with self._lock:
            self._deactivate_expired_subscriptions()
            rows = self._conn.execute(
                """
                SELECT user_id, username
                FROM subscriptions
                WHERE is_active = 1
                ORDER BY updated_at_utc DESC
                """
            ).fetchall()
            return [(int(row["user_id"]), str(row["username"])) for row in rows]

    def is_user_subscribed(self, user_id: int) -> bool:
        return self.get_active_subscription(user_id) is not None

    def set_notification_preference(self, user_id: int, enabled: bool) -> None:
        updated_at_utc = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO user_preferences (user_id, notifications_enabled, updated_at_utc)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    notifications_enabled = excluded.notifications_enabled,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (user_id, int(enabled), updated_at_utc),
            )
            self._conn.commit()

    def _ensure_user_preferences_row(self, user_id: int) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO user_preferences (
                user_id, ui_language, notifications_enabled, search_method, role_title, location,
                location_country, location_state, location_city, location_confidence,
                salary_range_usd, salary_min_usd, salary_max_usd, salary_currency, salary_confidence,
                include_no_salary, project_preferences_json, delivery_mode, timezone_offset_minutes, updated_at_utc
            )
            VALUES (?, '', 1, 'opportunities', '', '', '', '', '', 0, '', NULL, NULL, 'USD', 0, 1, '{}', 'instant', 0, ?)
            """,
            (user_id, utc_now_iso()),
        )

    def set_ui_language(self, user_id: int, ui_language: str) -> None:
        normalized = normalize_ui_language(ui_language)
        with self._lock:
            self._ensure_user_preferences_row(user_id)
            self._conn.execute(
                """
                UPDATE user_preferences
                SET ui_language = ?, updated_at_utc = ?
                WHERE user_id = ?
                """,
                (normalized, utc_now_iso(), user_id),
            )
            self._conn.commit()

    def get_ui_language(self, user_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT ui_language FROM user_preferences WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            if row is None:
                return ""
            return normalize_ui_language(row["ui_language"])

    def set_search_method(self, user_id: int, search_method: str) -> None:
        normalized = str(search_method or "").strip().lower()
        if normalized not in {"opportunities", "jobs_contracts_projects"}:
            normalized = "opportunities"
        with self._lock:
            self._ensure_user_preferences_row(user_id)
            self._conn.execute(
                """
                UPDATE user_preferences
                SET search_method = ?, updated_at_utc = ?
                WHERE user_id = ?
                """,
                (normalized, utc_now_iso(), user_id),
            )
            self._conn.commit()

    def get_search_method(self, user_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT search_method FROM user_preferences WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            if row is None:
                return "opportunities"
            value = str(row["search_method"] or "").strip().lower()
            if value in {"jobs", "jobs_contracts_projects"}:
                return "jobs_contracts_projects"
            if value == "opportunities":
                return value
            return "opportunities"

    def set_role_preference(self, user_id: int, role_title: str) -> None:
        normalized_role = normalize_role_title(role_title)
        with self._lock:
            self._ensure_user_preferences_row(user_id)
            self._conn.execute(
                """
                UPDATE user_preferences
                SET role_title = ?, updated_at_utc = ?
                WHERE user_id = ?
                """,
                (normalized_role, utc_now_iso(), user_id),
            )
            self._conn.commit()

    def get_role_preference(self, user_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT role_title FROM user_preferences WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            if row is None:
                return ""
            return normalize_role_title(row["role_title"])

    def set_location_preference(
        self,
        user_id: int,
        location: str,
        *,
        country: str = "",
        state: str = "",
        city: str = "",
        confidence: float = 0.0,
    ) -> None:
        confidence_value = _coerce_float_or_none(confidence) or 0.0
        with self._lock:
            self._ensure_user_preferences_row(user_id)
            self._conn.execute(
                """
                UPDATE user_preferences
                SET location = ?,
                    location_country = ?,
                    location_state = ?,
                    location_city = ?,
                    location_confidence = ?,
                    updated_at_utc = ?
                WHERE user_id = ?
                """,
                (
                    location.strip(),
                    country.strip(),
                    state.strip(),
                    city.strip(),
                    max(0.0, min(confidence_value, 1.0)),
                    utc_now_iso(),
                    user_id,
                ),
            )
            self._conn.commit()

    def get_location_preference(self, user_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT location FROM user_preferences WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            if row is None:
                return ""
            return str(row["location"]).strip()

    def get_location_structured(self, user_id: int) -> dict[str, object]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT location, location_country, location_state, location_city, location_confidence
                FROM user_preferences
                WHERE user_id = ?
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return {
                    "location": "",
                    "country": "",
                    "state": "",
                    "city": "",
                    "confidence": 0.0,
                }
            return {
                "location": str(row["location"]).strip(),
                "country": str(row["location_country"]).strip(),
                "state": str(row["location_state"]).strip(),
                "city": str(row["location_city"]).strip(),
                "confidence": float(row["location_confidence"] or 0.0),
            }

    def set_salary_range_preference(
        self,
        user_id: int,
        salary_range_usd: str,
        *,
        min_usd: float | None = None,
        max_usd: float | None = None,
        currency: str = "USD",
        confidence: float = 0.0,
    ) -> None:
        confidence_value = _coerce_float_or_none(confidence) or 0.0
        with self._lock:
            self._ensure_user_preferences_row(user_id)
            self._conn.execute(
                """
                UPDATE user_preferences
                SET salary_range_usd = ?,
                    salary_min_usd = ?,
                    salary_max_usd = ?,
                    salary_currency = ?,
                    salary_confidence = ?,
                    updated_at_utc = ?
                WHERE user_id = ?
                """,
                (
                    salary_range_usd.strip(),
                    _coerce_float_or_none(min_usd),
                    _coerce_float_or_none(max_usd),
                    currency.strip().upper() or "USD",
                    max(0.0, min(confidence_value, 1.0)),
                    utc_now_iso(),
                    user_id,
                ),
            )
            self._conn.commit()

    def get_salary_range_preference(self, user_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT salary_range_usd FROM user_preferences WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            if row is None:
                return ""
            return str(row["salary_range_usd"]).strip()

    def get_salary_range_structured(self, user_id: int) -> dict[str, object]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT salary_range_usd, salary_min_usd, salary_max_usd, salary_currency, salary_confidence
                FROM user_preferences
                WHERE user_id = ?
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return {
                    "salary_range_usd": "",
                    "min_usd": None,
                    "max_usd": None,
                    "currency": "USD",
                    "confidence": 0.0,
                }
            return {
                "salary_range_usd": str(row["salary_range_usd"]).strip(),
                "min_usd": _coerce_float_or_none(row["salary_min_usd"]),
                "max_usd": _coerce_float_or_none(row["salary_max_usd"]),
                "currency": str(row["salary_currency"]).strip().upper() or "USD",
                "confidence": float(row["salary_confidence"] or 0.0),
            }

    def set_include_no_salary(self, user_id: int, enabled: bool) -> None:
        with self._lock:
            self._ensure_user_preferences_row(user_id)
            self._conn.execute(
                """
                UPDATE user_preferences
                SET include_no_salary = ?,
                    updated_at_utc = ?
                WHERE user_id = ?
                """,
                (
                    int(enabled),
                    utc_now_iso(),
                    user_id,
                ),
            )
            self._conn.commit()

    def get_include_no_salary(self, user_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT include_no_salary FROM user_preferences WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            if row is None:
                return True
            return bool(row["include_no_salary"])

    def set_project_preferences(self, user_id: int, preferences: dict[str, object]) -> None:
        normalized = normalize_project_preferences(preferences)
        with self._lock:
            self._ensure_user_preferences_row(user_id)
            self._conn.execute(
                """
                UPDATE user_preferences
                SET project_preferences_json = ?,
                    updated_at_utc = ?
                WHERE user_id = ?
                """,
                (
                    json.dumps(normalized, ensure_ascii=False, sort_keys=True),
                    utc_now_iso(),
                    user_id,
                ),
            )
            self._conn.commit()

    def get_project_preferences(self, user_id: int) -> dict[str, object]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT project_preferences_json, include_no_salary
                FROM user_preferences
                WHERE user_id = ?
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return normalize_project_preferences({})
            return normalize_project_preferences(str(row["project_preferences_json"] or ""))

    def set_delivery_mode(self, user_id: int, mode: str) -> None:
        normalized = mode.strip().lower()
        if normalized not in {"instant", "digest"}:
            normalized = "instant"
        with self._lock:
            self._ensure_user_preferences_row(user_id)
            self._conn.execute(
                """
                UPDATE user_preferences
                SET delivery_mode = ?, updated_at_utc = ?
                WHERE user_id = ?
                """,
                (normalized, utc_now_iso(), user_id),
            )
            self._conn.commit()

    def get_delivery_mode(self, user_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT delivery_mode FROM user_preferences WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            if row is None:
                return "instant"
            normalized = str(row["delivery_mode"]).strip().lower()
            return normalized if normalized in {"instant", "digest"} else "instant"

    def set_timezone_offset_minutes(self, user_id: int, offset_minutes: int) -> None:
        bounded_offset = max(-720, min(int(offset_minutes), 840))
        with self._lock:
            self._ensure_user_preferences_row(user_id)
            self._conn.execute(
                """
                UPDATE user_preferences
                SET timezone_offset_minutes = ?, updated_at_utc = ?
                WHERE user_id = ?
                """,
                (bounded_offset, utc_now_iso(), user_id),
            )
            self._conn.commit()

    def get_timezone_offset_minutes(self, user_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT timezone_offset_minutes FROM user_preferences WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            if row is None:
                return 0
            try:
                return max(-720, min(int(row["timezone_offset_minutes"] or 0), 840))
            except (TypeError, ValueError):
                return 0

    def get_notification_preference(self, user_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT notifications_enabled
                FROM user_preferences
                WHERE user_id = ?
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return True
            return bool(row["notifications_enabled"])

    def has_alert_configuration(self, user_id: int) -> bool:
        return bool(self.get_role_preference(user_id))

    def get_alert_updated_at_utc(self, user_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT updated_at_utc
                FROM user_preferences
                WHERE user_id = ?
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return ""
            return str(row["updated_at_utc"]).strip()

    def replace_mute_windows(self, user_id: int, hours: set[int]) -> None:
        normalized_hours = {hour for hour in hours if 0 <= hour <= 23}
        with self._lock:
            self._conn.execute("DELETE FROM user_mute_windows WHERE user_id = ?", (user_id,))
            if normalized_hours:
                self._conn.executemany(
                    """
                    INSERT INTO user_mute_windows (user_id, hour_start)
                    VALUES (?, ?)
                    """,
                    [(user_id, hour) for hour in sorted(normalized_hours)],
                )
            self._conn.commit()

    def get_mute_windows(self, user_id: int) -> set[int]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT hour_start
                FROM user_mute_windows
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchall()
            return {int(row["hour_start"]) for row in rows}

    def replace_quiet_hours_range(self, user_id: int, start_hour: int, end_hour: int) -> None:
        normalized_start = max(0, min(int(start_hour), 23))
        normalized_end = max(0, min(int(end_hour), 23))
        muted_hours: set[int] = set()
        probe = normalized_start
        for _ in range(24):
            if probe == normalized_end:
                break
            muted_hours.add(probe)
            probe = (probe + 1) % 24
        self.replace_mute_windows(user_id, muted_hours)

    def clear_quiet_hours(self, user_id: int) -> None:
        self.replace_mute_windows(user_id, set())

    def get_quiet_hours_range(self, user_id: int) -> tuple[int, int] | None:
        mute_windows = self.get_mute_windows(user_id)
        if not mute_windows:
            return None
        for start_hour in range(24):
            previous_hour = (start_hour - 1) % 24
            if start_hour not in mute_windows or previous_hour in mute_windows:
                continue
            probe = start_hour
            visited: set[int] = set()
            while probe in mute_windows and probe not in visited:
                visited.add(probe)
                probe = (probe + 1) % 24
            if visited == mute_windows:
                return (start_hour, probe)
        return None

    def _local_time(self, user_id: int, at_utc: datetime) -> datetime:
        return at_utc + timedelta(minutes=self.get_timezone_offset_minutes(user_id))

    def _utc_from_local(self, user_id: int, local_dt: datetime) -> datetime:
        return local_dt - timedelta(minutes=self.get_timezone_offset_minutes(user_id))

    def should_deliver_now(self, user_id: int, at_utc: datetime | None = None) -> bool:
        if not self.get_notification_preference(user_id):
            return False
        if at_utc is None:
            at_utc = utc_now_dt()
        local_dt = self._local_time(user_id, at_utc)
        mute_windows = self.get_mute_windows(user_id)
        return local_dt.hour not in mute_windows

    def next_allowed_delivery_time(self, user_id: int, from_utc: datetime | None = None) -> str:
        if from_utc is None:
            from_utc = utc_now_dt()

        if not self.get_notification_preference(user_id):
            return format_utc_iso(from_utc + timedelta(hours=6))

        local_dt = self._local_time(user_id, from_utc)
        mute_windows = self.get_mute_windows(user_id)
        if local_dt.hour not in mute_windows:
            return format_utc_iso(from_utc)

        probe_local = local_dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        for _ in range(48):
            if probe_local.hour not in mute_windows:
                return format_utc_iso(self._utc_from_local(user_id, probe_local))
            probe_local += timedelta(hours=1)
        return format_utc_iso(from_utc + timedelta(hours=1))

    def next_digest_delivery_time(self, user_id: int, from_utc: datetime | None = None) -> str:
        if from_utc is None:
            from_utc = utc_now_dt()
        local_dt = self._local_time(user_id, from_utc)
        next_bucket_local = local_dt.replace(minute=0, second=0, microsecond=0)
        next_bucket_hour = ((local_dt.hour // 6) + 1) * 6
        if next_bucket_hour >= 24:
            next_bucket_local = next_bucket_local.replace(hour=0) + timedelta(days=1)
        else:
            next_bucket_local = next_bucket_local.replace(hour=next_bucket_hour)
        candidate_utc = self._utc_from_local(user_id, next_bucket_local)
        return self.next_allowed_delivery_time(user_id, from_utc=candidate_utc)

    def add_user_website(self, user_id: int, url: str, *, currency: str = "") -> bool:
        normalized_url = normalize_website(url)
        if is_disabled_website(normalized_url):
            return False
        normalized_currency = normalize_website_currency(currency) or infer_website_currency(normalized_url)
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO user_websites (user_id, url, currency, created_at_utc)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, normalized_url, normalized_currency, utc_now_iso()),
            )
            inserted = bool(cursor.rowcount)
            if not inserted:
                self._conn.execute(
                    """
                    UPDATE user_websites
                    SET currency = ?
                    WHERE user_id = ? AND url = ?
                    """,
                    (normalized_currency, user_id, normalized_url),
                )
            self._conn.commit()
            return inserted

    def get_user_websites(self, user_id: int) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT url
                FROM user_websites
                WHERE user_id = ?
                ORDER BY created_at_utc ASC
                """,
                (user_id,),
            ).fetchall()
            ordered: list[str] = []
            seen: set[str] = set()
            for row in rows:
                normalized_url = normalize_website(str(row["url"]))
                if is_disabled_website(normalized_url) or normalized_url in seen:
                    continue
                seen.add(normalized_url)
                ordered.append(normalized_url)
            return ordered

    def get_user_websites_with_currency(self, user_id: int) -> list[tuple[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT url, currency
                FROM user_websites
                WHERE user_id = ?
                ORDER BY created_at_utc ASC
                """,
                (user_id,),
            ).fetchall()
            ordered: list[tuple[str, str]] = []
            seen: set[str] = set()
            for row in rows:
                normalized_url = normalize_website(str(row["url"]))
                if is_disabled_website(normalized_url) or normalized_url in seen:
                    continue
                seen.add(normalized_url)
                ordered.append(
                    (
                        normalized_url,
                        normalize_website_currency(row["currency"]) or infer_website_currency(normalized_url),
                    )
                )
            return ordered

    def get_user_website_currency(self, user_id: int, url: str) -> str:
        normalized_url = normalize_website(url)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT currency
                FROM user_websites
                WHERE user_id = ? AND url = ?
                LIMIT 1
                """,
                (user_id, normalized_url),
            ).fetchone()
            if row is None:
                return infer_website_currency(normalized_url)
            return normalize_website_currency(row["currency"]) or infer_website_currency(normalized_url)

    def clear_user_websites(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM user_websites WHERE user_id = ?", (user_id,))
            self._conn.commit()

    def get_all_user_websites(self, *, active_only: bool = False) -> list[str]:
        with self._lock:
            if active_only:
                self._deactivate_expired_subscriptions()
                rows = self._conn.execute(
                    """
                    SELECT DISTINCT uw.url
                    FROM user_websites AS uw
                    JOIN subscriptions AS s
                      ON s.user_id = uw.user_id
                    WHERE s.is_active = 1
                    ORDER BY uw.url ASC
                    """
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT DISTINCT url
                    FROM user_websites
                    ORDER BY url ASC
                    """
                ).fetchall()
            ordered: list[str] = []
            seen: set[str] = set()
            for row in rows:
                normalized_url = normalize_website(str(row["url"]))
                if is_disabled_website(normalized_url) or normalized_url in seen:
                    continue
                seen.add(normalized_url)
                ordered.append(normalized_url)
            return ordered

    def get_active_website_selection_counts(self) -> dict[str, int]:
        with self._lock:
            self._deactivate_expired_subscriptions()
            rows = self._conn.execute(
                """
                SELECT uw.url, COUNT(DISTINCT uw.user_id) AS active_user_count
                FROM user_websites AS uw
                JOIN subscriptions AS s
                  ON s.user_id = uw.user_id
                WHERE s.is_active = 1
                GROUP BY uw.url
                ORDER BY active_user_count DESC, uw.url ASC
                """
            ).fetchall()
            return {
                normalize_website(str(row["url"])): max(1, int(row["active_user_count"] or 0))
                for row in rows
                if str(row["url"]).strip()
                and not is_disabled_website(str(row["url"]))
            }

    def filter_selected_websites(self, candidate_websites: list[str], *, active_only: bool = True) -> list[str]:
        selected_websites = {
            normalize_website(url)
            for url in self.get_all_user_websites(active_only=active_only)
            if str(url).strip()
        }
        if not selected_websites:
            return []

        filtered: list[str] = []
        seen: set[str] = set()
        for website in candidate_websites:
            raw_website = str(website).strip()
            if not raw_website:
                continue
            normalized = normalize_website(raw_website)
            if normalized in seen:
                continue
            seen.add(normalized)
            if normalized in selected_websites:
                filtered.append(raw_website)
        return filtered

    def user_selected_website(self, user_id: int, candidate_website: str) -> bool:
        selected = self.get_user_websites(user_id)
        if not selected:
            return False
        candidate_domain = extract_domain(candidate_website)
        for item in selected:
            item_domain = extract_domain(item)
            if candidate_domain == item_domain or candidate_domain.endswith(f".{item_domain}"):
                return True
        return False

    def add_keywords(self, user_id: int, keywords: list[str]) -> None:
        normalized_keywords = sorted(
            {
                keyword.strip().lower()
                for keyword in keywords
                if keyword.strip()
            }
        )[:50]
        if not normalized_keywords:
            return
        with self._lock:
            self._conn.executemany(
                """
                INSERT OR IGNORE INTO user_keywords (user_id, keyword, created_at_utc)
                VALUES (?, ?, ?)
                """,
                [(user_id, keyword, utc_now_iso()) for keyword in normalized_keywords],
            )
            self._conn.commit()

    def replace_keywords(self, user_id: int, keywords: list[str]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM user_keywords WHERE user_id = ?", (user_id,))
            self._conn.commit()
        self.add_keywords(user_id, keywords)

    def clear_keywords(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM user_keywords WHERE user_id = ?", (user_id,))
            self._conn.commit()

    def get_user_keywords(self, user_id: int) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT keyword
                FROM user_keywords
                WHERE user_id = ?
                ORDER BY keyword ASC
                """,
                (user_id,),
            ).fetchall()
            return [str(row["keyword"]) for row in rows]

    def replace_user_spheres(self, user_id: int, spheres: list[str]) -> None:
        normalized_spheres = sorted(
            {
                sphere.strip().lower()
                for sphere in spheres
                if sphere.strip()
            }
        )
        with self._lock:
            self._conn.execute("DELETE FROM user_spheres WHERE user_id = ?", (user_id,))
            if normalized_spheres:
                self._conn.executemany(
                    """
                    INSERT OR IGNORE INTO user_spheres (user_id, sphere, created_at_utc)
                    VALUES (?, ?, ?)
                    """,
                    [(user_id, sphere, utc_now_iso()) for sphere in normalized_spheres],
                )
            self._conn.commit()

    def get_user_spheres(self, user_id: int) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT sphere
                FROM user_spheres
                WHERE user_id = ?
                ORDER BY sphere ASC
                """,
                (user_id,),
            ).fetchall()
            return [str(row["sphere"]) for row in rows]

    def clear_user_alert(self, user_id: int) -> None:
        with self._lock:
            self._ensure_user_preferences_row(user_id)
            self._conn.execute(
                """
                UPDATE user_preferences
                SET notifications_enabled = 0,
                    role_title = '',
                    location = '',
                    location_country = '',
                    location_state = '',
                    location_city = '',
                    location_confidence = 0,
                    salary_range_usd = '',
                    salary_min_usd = NULL,
                    salary_max_usd = NULL,
                    salary_currency = 'USD',
                    salary_confidence = 0,
                    include_no_salary = 1,
                    project_preferences_json = '{}',
                    updated_at_utc = ?
                WHERE user_id = ?
                """,
                (utc_now_iso(), user_id),
            )
            self._conn.execute("DELETE FROM user_websites WHERE user_id = ?", (user_id,))
            self._conn.execute("DELETE FROM user_keywords WHERE user_id = ?", (user_id,))
            self._conn.execute("DELETE FROM user_spheres WHERE user_id = ?", (user_id,))
            self._conn.execute(
                """
                UPDATE notification_queue
                SET status = 'cancelled',
                    last_error = 'alert_deleted'
                WHERE user_id = ?
                  AND status = 'queued'
                """,
                (user_id,),
            )
            self._conn.commit()

    def has_seen_landing(self, user_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT landing_seen FROM user_flags WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            if row is None:
                return False
            return bool(row["landing_seen"])

    def mark_seen_landing(self, user_id: int) -> None:
        now_utc = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO user_flags (user_id, landing_seen, created_at_utc, updated_at_utc)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    landing_seen = 1,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (user_id, now_utc, now_utc),
            )
            self._conn.commit()

    def has_first_start_admin_notified(self, user_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT first_start_admin_notified FROM user_flags WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            if row is None:
                return False
            return bool(row["first_start_admin_notified"])

    def mark_first_start_admin_notified(self, user_id: int) -> None:
        now_utc = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO user_flags (user_id, first_start_admin_notified, created_at_utc, updated_at_utc)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    first_start_admin_notified = 1,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (user_id, now_utc, now_utc),
            )
            self._conn.commit()

    def list_due_system_notices(self, limit: int = 100) -> list[QueuedSystemNotice]:
        now_utc = utc_now_iso()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, user_id, username, notice_key, status, attempt_count,
                       available_after_utc, created_at_utc, updated_at_utc, sent_at_utc, last_error
                FROM system_notice_queue
                WHERE status = 'pending'
                  AND available_after_utc <= ?
                ORDER BY created_at_utc ASC, id ASC
                LIMIT ?
                """,
                (now_utc, max(1, limit)),
            ).fetchall()
            return [
                QueuedSystemNotice(
                    notice_id=int(row["id"]),
                    user_id=int(row["user_id"]),
                    username=str(row["username"] or ""),
                    notice_key=str(row["notice_key"] or ""),
                    status=str(row["status"] or "pending"),
                    attempt_count=max(0, int(row["attempt_count"] or 0)),
                    available_after_utc=str(row["available_after_utc"] or ""),
                    created_at_utc=str(row["created_at_utc"] or ""),
                    updated_at_utc=str(row["updated_at_utc"] or ""),
                    sent_at_utc=str(row["sent_at_utc"] or ""),
                    last_error=str(row["last_error"] or ""),
                )
                for row in rows
            ]

    def mark_system_notice_sent(self, notice_id: int) -> None:
        now_utc = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                UPDATE system_notice_queue
                SET status = 'sent',
                    sent_at_utc = ?,
                    updated_at_utc = ?,
                    last_error = ''
                WHERE id = ?
                """,
                (now_utc, now_utc, notice_id),
            )
            self._conn.commit()

    def reschedule_system_notice(self, notice_id: int, *, attempt_count: int, last_error: str) -> None:
        now_dt = utc_now_dt()
        next_attempt_utc = format_utc_iso(now_dt + timedelta(minutes=min(720, 5 * (2 ** max(0, attempt_count)))))
        now_utc = format_utc_iso(now_dt)
        with self._lock:
            self._conn.execute(
                """
                UPDATE system_notice_queue
                SET attempt_count = ?,
                    available_after_utc = ?,
                    updated_at_utc = ?,
                    last_error = ?
                WHERE id = ?
                """,
                (
                    max(1, attempt_count + 1),
                    next_attempt_utc,
                    now_utc,
                    str(last_error or "")[:1000],
                    notice_id,
                ),
            )
            self._conn.commit()

    def list_subscriptions_for_24h_reminder(self) -> list[ActiveSubscription]:
        return self.list_subscriptions_for_expiry_reminder()

    def list_subscriptions_for_expiry_reminder(self) -> list[ActiveSubscription]:
        now_utc = utc_now_iso()
        trial_horizon_utc = format_utc_iso(utc_now_dt() + timedelta(hours=12))
        paid_horizon_utc = format_utc_iso(utc_now_dt() + timedelta(days=3))
        with self._lock:
            self._deactivate_expired_subscriptions()
            rows = self._conn.execute(
                """
                SELECT user_id, username, plan, started_at_utc, ends_at_utc, is_active, source, updated_at_utc
                FROM subscriptions
                WHERE is_active = 1
                  AND reminder_24h_sent = 0
                  AND (
                      (source = 'trial' AND ends_at_utc > ? AND ends_at_utc <= ?)
                      OR
                      (source = 'payment' AND ends_at_utc > ? AND ends_at_utc <= ?)
                  )
                ORDER BY ends_at_utc ASC
                """,
                (now_utc, trial_horizon_utc, now_utc, paid_horizon_utc),
            ).fetchall()
            return [
                ActiveSubscription(
                    user_id=int(row["user_id"]),
                    username=str(row["username"]),
                    plan=str(row["plan"]),
                    started_at_utc=str(row["started_at_utc"]),
                    ends_at_utc=str(row["ends_at_utc"]),
                    is_active=bool(row["is_active"]),
                    source=str(row["source"]),
                    updated_at_utc=str(row["updated_at_utc"]),
                )
                for row in rows
            ]

    def mark_24h_reminder_sent(self, user_id: int) -> None:
        self.mark_expiry_reminder_sent(user_id)

    def mark_expiry_reminder_sent(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE subscriptions
                SET reminder_24h_sent = 1, updated_at_utc = ?
                WHERE user_id = ?
                """,
                (utc_now_iso(), user_id),
            )
            self._conn.commit()

    def list_recently_expired_subscriptions(self) -> list[ActiveSubscription]:
        now_utc = utc_now_iso()
        with self._lock:
            self._deactivate_expired_subscriptions()
            rows = self._conn.execute(
                """
                SELECT user_id, username, plan, started_at_utc, ends_at_utc, is_active, source, updated_at_utc
                FROM subscriptions
                WHERE is_active = 0
                  AND expiration_notice_sent = 0
                  AND ends_at_utc <= ?
                ORDER BY ends_at_utc ASC
                """,
                (now_utc,),
            ).fetchall()
            return [
                ActiveSubscription(
                    user_id=int(row["user_id"]),
                    username=str(row["username"]),
                    plan=str(row["plan"]),
                    started_at_utc=str(row["started_at_utc"]),
                    ends_at_utc=str(row["ends_at_utc"]),
                    is_active=bool(row["is_active"]),
                    source=str(row["source"]),
                    updated_at_utc=str(row["updated_at_utc"]),
                )
                for row in rows
            ]

    def mark_expiration_notice_sent(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE subscriptions
                SET expiration_notice_sent = 1, updated_at_utc = ?
                WHERE user_id = ?
                """,
                (utc_now_iso(), user_id),
            )
            self._conn.commit()

    def get_latest_subscription(self, user_id: int) -> ActiveSubscription | None:
        with self._lock:
            self._deactivate_expired_subscriptions()
            row = self._conn.execute(
                """
                SELECT user_id, username, plan, started_at_utc, ends_at_utc, is_active, source, updated_at_utc
                FROM subscriptions
                WHERE user_id = ?
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return None
            return ActiveSubscription(
                user_id=int(row["user_id"]),
                username=str(row["username"]),
                plan=str(row["plan"]),
                started_at_utc=str(row["started_at_utc"]),
                ends_at_utc=str(row["ends_at_utc"]),
                is_active=bool(row["is_active"]),
                source=str(row["source"]),
                updated_at_utc=str(row["updated_at_utc"]),
            )

    def log_filter_change(self, user_id: int, filter_name: str, filter_value: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO filter_audit_logs (user_id, filter_name, filter_value, created_at_utc)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, filter_name.strip(), filter_value.strip(), utc_now_iso()),
            )
            self._conn.commit()

    def list_filter_changes(self, user_id: int, limit: int = 50) -> list[tuple[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT filter_name, filter_value
                FROM filter_audit_logs
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
            return [(str(row["filter_name"]), str(row["filter_value"])) for row in rows]

    def clear_filter_changes(self, user_id: int, filter_name: str | None = None) -> None:
        with self._lock:
            if filter_name is None:
                self._conn.execute(
                    "DELETE FROM filter_audit_logs WHERE user_id = ?",
                    (user_id,),
                )
            else:
                self._conn.execute(
                    """
                    DELETE FROM filter_audit_logs
                    WHERE user_id = ? AND lower(filter_name) = lower(?)
                    """,
                    (user_id, filter_name.strip()),
                )
            self._conn.commit()

    def get_latest_filter_change_at_utc(self, user_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT created_at_utc
                FROM filter_audit_logs
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return ""
            return str(row["created_at_utc"]).strip()

    def queue_notification(
        self,
        user_id: int,
        username: str,
        card_url: str,
        message_text: str,
        available_after_utc: str,
        delivery_event_id: int | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO notification_queue (
                    user_id, username, card_url, message_text, delivery_event_id, status, available_after_utc, created_at_utc
                )
                VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (user_id, username, card_url, message_text, delivery_event_id, available_after_utc, utc_now_iso()),
            )
            self._conn.commit()

    def queue_delivery_job(
        self,
        *,
        website: str,
        card_url: str,
        card_json: str,
        available_after_utc: str | None = None,
    ) -> int:
        queued_at_utc = available_after_utc or utc_now_iso()
        created_at_utc = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO delivery_job_queue (
                    website, card_url, card_json, status, attempt_count,
                    available_after_utc, created_at_utc, updated_at_utc, last_error,
                    started_processing_at_utc, completed_at_utc
                )
                VALUES (?, ?, ?, 'queued', 0, ?, ?, ?, '', '', '')
                ON CONFLICT(card_url) DO UPDATE SET
                    website = excluded.website,
                    card_json = excluded.card_json,
                    status = CASE
                        WHEN delivery_job_queue.status = 'completed' THEN delivery_job_queue.status
                        ELSE 'queued'
                    END,
                    available_after_utc = CASE
                        WHEN delivery_job_queue.status = 'completed' THEN delivery_job_queue.available_after_utc
                        ELSE excluded.available_after_utc
                    END,
                    updated_at_utc = excluded.updated_at_utc,
                    last_error = CASE
                        WHEN delivery_job_queue.status = 'completed' THEN delivery_job_queue.last_error
                        ELSE ''
                    END,
                    started_processing_at_utc = CASE
                        WHEN delivery_job_queue.status = 'completed' THEN delivery_job_queue.started_processing_at_utc
                        ELSE ''
                    END,
                    completed_at_utc = CASE
                        WHEN delivery_job_queue.status = 'completed' THEN delivery_job_queue.completed_at_utc
                        ELSE ''
                    END
                """,
                (
                    website.strip(),
                    card_url.strip(),
                    card_json,
                    queued_at_utc,
                    created_at_utc,
                    created_at_utc,
                ),
            )
            row = self._conn.execute(
                """
                SELECT id
                FROM delivery_job_queue
                WHERE card_url = ?
                LIMIT 1
                """,
                (card_url.strip(),),
            ).fetchone()
            self._conn.commit()
            if row is None:
                raise RuntimeError("Could not queue delivery job")
            return int(row["id"])

    def claim_due_delivery_jobs(self, limit: int = 1) -> list[QueuedDeliveryJob]:
        now_utc = utc_now_iso()
        claimed: list[QueuedDeliveryJob] = []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, website, card_url, card_json, status, attempt_count,
                       available_after_utc, created_at_utc, updated_at_utc, last_error,
                       started_processing_at_utc, completed_at_utc
                FROM delivery_job_queue
                WHERE status = 'queued'
                  AND available_after_utc <= ?
                ORDER BY available_after_utc ASC, id ASC
                LIMIT ?
                """,
                (now_utc, limit),
            ).fetchall()
            for row in rows:
                cursor = self._conn.execute(
                    """
                    UPDATE delivery_job_queue
                    SET status = 'processing',
                        started_processing_at_utc = CASE
                            WHEN COALESCE(started_processing_at_utc, '') = '' THEN ?
                            ELSE started_processing_at_utc
                        END,
                        updated_at_utc = ?
                    WHERE id = ?
                      AND status = 'queued'
                    """,
                    (now_utc, now_utc, int(row["id"])),
                )
                if int(cursor.rowcount or 0) <= 0:
                    continue
                claimed.append(
                    QueuedDeliveryJob(
                        queue_id=int(row["id"]),
                        website=str(row["website"]),
                        card_url=str(row["card_url"]),
                        card_json=str(row["card_json"]),
                        status="processing",
                        attempt_count=int(row["attempt_count"] or 0),
                        available_after_utc=str(row["available_after_utc"]),
                        created_at_utc=str(row["created_at_utc"]),
                        updated_at_utc=now_utc,
                        last_error=str(row["last_error"] or ""),
                        started_processing_at_utc=str(row["started_processing_at_utc"] or ""),
                        completed_at_utc=str(row["completed_at_utc"] or ""),
                    )
                )
            self._conn.commit()
        return claimed

    def mark_delivery_job_completed(self, queue_id: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE delivery_job_queue
                SET status = 'completed',
                    completed_at_utc = ?,
                    updated_at_utc = ?
                WHERE id = ?
                """,
                (utc_now_iso(), utc_now_iso(), queue_id),
            )
            self._conn.commit()

    def reschedule_delivery_job(self, queue_id: int, *, available_after_utc: str, last_error: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE delivery_job_queue
                SET status = 'queued',
                    attempt_count = attempt_count + 1,
                    available_after_utc = ?,
                    last_error = ?,
                    started_processing_at_utc = '',
                    updated_at_utc = ?
                WHERE id = ?
                """,
                (available_after_utc, last_error.strip(), utc_now_iso(), queue_id),
            )
            self._conn.commit()

    def fail_delivery_job(self, queue_id: int, *, last_error: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE delivery_job_queue
                SET status = 'failed',
                    attempt_count = attempt_count + 1,
                    last_error = ?,
                    completed_at_utc = '',
                    updated_at_utc = ?
                WHERE id = ?
                """,
                (last_error.strip(), utc_now_iso(), queue_id),
            )
            self._conn.commit()

    def get_job_delivery_queue_backlog(self) -> int:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM delivery_job_queue
                WHERE status IN ('queued', 'processing')
                """
            ).fetchone()
            return int(row["total"] or 0) if row is not None else 0

    def get_average_delivery_delay_seconds(self) -> float:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT AVG(strftime('%s', completed_at_utc) - strftime('%s', created_at_utc)) AS avg_seconds
                FROM delivery_job_queue
                WHERE status = 'completed'
                  AND COALESCE(completed_at_utc, '') != ''
                """
            ).fetchone()
        return float(row["avg_seconds"] or 0.0) if row is not None else 0.0

    def get_average_match_time_seconds(self) -> float:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT AVG(strftime('%s', completed_at_utc) - strftime('%s', started_processing_at_utc)) AS avg_seconds
                FROM delivery_job_queue
                WHERE status = 'completed'
                  AND COALESCE(completed_at_utc, '') != ''
                  AND COALESCE(started_processing_at_utc, '') != ''
                """
            ).fetchone()
        return float(row["avg_seconds"] or 0.0) if row is not None else 0.0

    def get_due_notifications(self, limit: int = 100) -> list[QueuedNotification]:
        now_utc = utc_now_iso()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, user_id, username, card_url, message_text, delivery_event_id, available_after_utc, created_at_utc
                FROM notification_queue
                WHERE status = 'queued' AND available_after_utc <= ?
                ORDER BY available_after_utc ASC, id ASC
                LIMIT ?
                """,
                (now_utc, limit),
            ).fetchall()
            return [
                QueuedNotification(
                    queue_id=int(row["id"]),
                    user_id=int(row["user_id"]),
                    username=str(row["username"]),
                    card_url=str(row["card_url"]),
                    message_text=str(row["message_text"]),
                    delivery_event_id=int(row["delivery_event_id"]) if row["delivery_event_id"] is not None else None,
                    available_after_utc=str(row["available_after_utc"]),
                    created_at_utc=str(row["created_at_utc"]),
                )
                for row in rows
            ]

    def record_delivery_event(
        self,
        *,
        user_id: int,
        username: str,
        card_url: str,
        card_title: str,
        card_company: str,
        card_location: str,
        card_website: str,
        match_reason: str,
    ) -> int:
        created_at_utc = utc_now_iso()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO delivery_events (
                    user_id, username, card_url, card_title, card_company,
                    card_location, card_website, match_reason, created_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    username,
                    card_url,
                    card_title.strip(),
                    card_company.strip(),
                    card_location.strip(),
                    card_website.strip(),
                    match_reason.strip(),
                    created_at_utc,
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def get_delivery_event(self, event_id: int) -> DeliveryEvent | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, user_id, username, card_url, card_title, card_company,
                       card_location, card_website, match_reason, telegram_chat_id,
                       telegram_message_id, sent_at_utc, created_at_utc
                FROM delivery_events
                WHERE id = ?
                LIMIT 1
                """,
                (event_id,),
            ).fetchone()
            if row is None:
                return None
            return DeliveryEvent(
                event_id=int(row["id"]),
                user_id=int(row["user_id"]),
                username=str(row["username"]),
                card_url=str(row["card_url"]),
                card_title=str(row["card_title"]),
                card_company=str(row["card_company"]),
                card_location=str(row["card_location"]),
                card_website=str(row["card_website"]),
                match_reason=str(row["match_reason"]),
                telegram_chat_id=int(row["telegram_chat_id"]) if row["telegram_chat_id"] is not None else None,
                telegram_message_id=int(row["telegram_message_id"]) if row["telegram_message_id"] is not None else None,
                sent_at_utc=str(row["sent_at_utc"] or ""),
                created_at_utc=str(row["created_at_utc"]),
            )

    def mark_delivery_event_sent(
        self,
        event_id: int,
        *,
        telegram_chat_id: int,
        telegram_message_id: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE delivery_events
                SET telegram_chat_id = ?,
                    telegram_message_id = ?,
                    sent_at_utc = ?
                WHERE id = ?
                """,
                (int(telegram_chat_id), int(telegram_message_id), utc_now_iso(), int(event_id)),
            )
            self._conn.commit()

    def get_latest_delivery_event_at_utc(self, user_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT created_at_utc
                FROM delivery_events
                WHERE user_id = ?
                ORDER BY created_at_utc DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return ""
            return str(row["created_at_utc"]).strip()

    def count_delivery_events(self, user_id: int, *, since_utc: str | None = None) -> int:
        with self._lock:
            if since_utc:
                row = self._conn.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM delivery_events
                    WHERE user_id = ?
                      AND created_at_utc >= ?
                    """,
                    (user_id, since_utc),
                ).fetchone()
            else:
                row = self._conn.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM delivery_events
                    WHERE user_id = ?
                    """,
                    (user_id,),
                ).fetchone()
            return int(row["total"] or 0) if row is not None else 0

    def count_sent_notifications(self, *, since_utc: str | None = None) -> int:
        with self._lock:
            if since_utc:
                row = self._conn.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM notification_queue
                    WHERE status = 'sent'
                      AND sent_at_utc IS NOT NULL
                      AND sent_at_utc >= ?
                    """,
                    (since_utc,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM notification_queue
                    WHERE status = 'sent'
                    """
                ).fetchone()
            return int(row["total"] or 0) if row is not None else 0

    def get_delivery_backlog(self) -> int:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    COALESCE((SELECT COUNT(*) FROM notification_queue WHERE status = 'queued'), 0)
                    + COALESCE((SELECT COUNT(*) FROM delivery_job_queue WHERE status IN ('queued', 'processing')), 0)
                    AS total
                """
            ).fetchone()
            return int(row["total"] or 0) if row is not None else 0

    def record_filter_mismatch_event(
        self,
        *,
        user_id: int,
        username: str,
        card_url: str,
        card_website: str,
        mismatch_stage: str,
        reason_code: str = "",
        reason: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO filter_mismatch_events (
                    user_id, username, card_url, card_website, mismatch_stage, reason_code, reason, created_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    username,
                    card_url.strip(),
                    card_website.strip(),
                    mismatch_stage.strip(),
                    reason_code.strip(),
                    reason.strip(),
                    utc_now_iso(),
                ),
            )
            self._conn.commit()

    def count_filter_mismatch_events(self, user_id: int, *, since_utc: str | None = None) -> int:
        with self._lock:
            if since_utc:
                row = self._conn.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM filter_mismatch_events
                    WHERE user_id = ?
                      AND created_at_utc >= ?
                    """,
                    (user_id, since_utc),
                ).fetchone()
            else:
                row = self._conn.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM filter_mismatch_events
                    WHERE user_id = ?
                    """,
                    (user_id,),
                ).fetchone()
            return int(row["total"] or 0) if row is not None else 0

    def get_filter_mismatch_stage_counts(self, user_id: int, *, since_utc: str | None = None) -> list[tuple[str, int]]:
        with self._lock:
            if since_utc:
                rows = self._conn.execute(
                    """
                    SELECT mismatch_stage, COUNT(*) AS total
                    FROM filter_mismatch_events
                    WHERE user_id = ?
                      AND created_at_utc >= ?
                    GROUP BY mismatch_stage
                    ORDER BY total DESC, mismatch_stage ASC
                    """,
                    (user_id, since_utc),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT mismatch_stage, COUNT(*) AS total
                    FROM filter_mismatch_events
                    WHERE user_id = ?
                    GROUP BY mismatch_stage
                    ORDER BY total DESC, mismatch_stage ASC
                    """,
                    (user_id,),
                ).fetchall()
            return [(str(row["mismatch_stage"]), int(row["total"] or 0)) for row in rows]

    def get_filter_mismatch_reason_code_counts(
        self,
        *,
        since_utc: str | None = None,
        user_id: int | None = None,
    ) -> list[tuple[str, int]]:
        clauses: list[str] = ["COALESCE(reason_code, '') != ''"]
        params: list[object] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(int(user_id))
        if since_utc:
            clauses.append("created_at_utc >= ?")
            params.append(since_utc)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT reason_code, COUNT(*) AS total
                FROM filter_mismatch_events
                WHERE {" AND ".join(clauses)}
                GROUP BY reason_code
                ORDER BY total DESC, reason_code ASC
                """
                ,
                tuple(params),
            ).fetchall()
            return [(str(row["reason_code"]), int(row["total"] or 0)) for row in rows]

    @staticmethod
    def _row_to_manual_review_case(row: sqlite3.Row) -> ManualReviewCase:
        return ManualReviewCase(
            review_id=int(row["id"]),
            user_id=int(row["user_id"]),
            username=str(row["username"]),
            card_url=str(row["card_url"]),
            card_website=str(row["card_website"]),
            card_title=str(row["card_title"]),
            card_company=str(row["card_company"]),
            card_location=str(row["card_location"]),
            card_salary=str(row["card_salary"]),
            card_language=str(row["card_language"] or "en"),
            card_json=str(row["card_json"]),
            review_kind=str(row["review_kind"]),
            ambiguity_summary=str(row["ambiguity_summary"]),
            match_reason=str(row["match_reason"]),
            role_title=str(row["role_title"]),
            location_preference=str(row["location_preference"]),
            salary_preference=str(row["salary_preference"]),
            include_no_salary=bool(row["include_no_salary"]),
            keywords_text=str(row["keywords_text"]),
            context_json=str(row["context_json"] or "{}"),
            status=str(row["status"]),
            reviewer=str(row["reviewer"]),
            review_notes=str(row["review_notes"]),
            sent_count=int(row["sent_count"] or 0),
            admin_chat_id=int(row["admin_chat_id"]) if row["admin_chat_id"] is not None else None,
            admin_message_id=int(row["admin_message_id"]) if row["admin_message_id"] is not None else None,
            dispatched_at_utc=str(row["dispatched_at_utc"] or ""),
            last_dispatch_error=str(row["last_dispatch_error"] or ""),
            created_at_utc=str(row["created_at_utc"]),
            updated_at_utc=str(row["updated_at_utc"]),
            reviewed_at_utc=str(row["reviewed_at_utc"]) if row["reviewed_at_utc"] else None,
        )

    def queue_manual_review_case(
        self,
        *,
        user_id: int,
        username: str,
        card_url: str,
        card_website: str,
        card_title: str,
        card_company: str,
        card_location: str,
        card_salary: str,
        card_language: str,
        card_payload: dict[str, object],
        review_kind: str,
        ambiguity_summary: str,
        match_reason: str,
        role_title: str,
        location_preference: str,
        salary_preference: str,
        include_no_salary: bool,
        keywords: list[str],
        context_payload: dict[str, object],
    ) -> int:
        created_at_utc = utc_now_iso()
        card_json = json.dumps(card_payload, ensure_ascii=False, separators=(",", ":"))
        context_json = json.dumps(context_payload, ensure_ascii=False, separators=(",", ":"))
        keywords_text = ", ".join(str(keyword).strip() for keyword in keywords if str(keyword).strip())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO manual_review_cases (
                    user_id, username, card_url, card_website, card_title, card_company,
                    card_location, card_salary, card_language, card_json, review_kind,
                    ambiguity_summary, match_reason, role_title, location_preference,
                    salary_preference, include_no_salary, keywords_text, context_json,
                    status, reviewer, review_notes, sent_count, admin_chat_id, admin_message_id,
                    dispatched_at_utc, last_dispatch_error, created_at_utc, updated_at_utc, reviewed_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', '', 0, NULL, NULL, '', '', ?, ?, NULL)
                ON CONFLICT(user_id, card_url) DO UPDATE SET
                    username = excluded.username,
                    card_website = excluded.card_website,
                    card_title = excluded.card_title,
                    card_company = excluded.card_company,
                    card_location = excluded.card_location,
                    card_salary = excluded.card_salary,
                    card_language = excluded.card_language,
                    card_json = excluded.card_json,
                    review_kind = excluded.review_kind,
                    ambiguity_summary = excluded.ambiguity_summary,
                    match_reason = excluded.match_reason,
                    role_title = excluded.role_title,
                    location_preference = excluded.location_preference,
                    salary_preference = excluded.salary_preference,
                    include_no_salary = excluded.include_no_salary,
                    keywords_text = excluded.keywords_text,
                    context_json = excluded.context_json,
                    status = CASE
                        WHEN manual_review_cases.status = 'pending' THEN 'pending'
                        ELSE manual_review_cases.status
                    END,
                    reviewer = CASE
                        WHEN manual_review_cases.status = 'pending' THEN ''
                        ELSE manual_review_cases.reviewer
                    END,
                    review_notes = CASE
                        WHEN manual_review_cases.status = 'pending' THEN ''
                        ELSE manual_review_cases.review_notes
                    END,
                    sent_count = CASE
                        WHEN manual_review_cases.status = 'pending' THEN 0
                        ELSE manual_review_cases.sent_count
                    END,
                    admin_chat_id = CASE
                        WHEN manual_review_cases.status = 'pending' THEN manual_review_cases.admin_chat_id
                        ELSE manual_review_cases.admin_chat_id
                    END,
                    admin_message_id = CASE
                        WHEN manual_review_cases.status = 'pending' THEN manual_review_cases.admin_message_id
                        ELSE manual_review_cases.admin_message_id
                    END,
                    dispatched_at_utc = CASE
                        WHEN manual_review_cases.status = 'pending' THEN manual_review_cases.dispatched_at_utc
                        ELSE manual_review_cases.dispatched_at_utc
                    END,
                    last_dispatch_error = CASE
                        WHEN manual_review_cases.status = 'pending' THEN ''
                        ELSE manual_review_cases.last_dispatch_error
                    END,
                    updated_at_utc = excluded.updated_at_utc,
                    reviewed_at_utc = CASE
                        WHEN manual_review_cases.status = 'pending' THEN NULL
                        ELSE manual_review_cases.reviewed_at_utc
                    END
                """,
                (
                    user_id,
                    username,
                    card_url.strip(),
                    card_website.strip(),
                    card_title.strip(),
                    card_company.strip(),
                    card_location.strip(),
                    card_salary.strip(),
                    (card_language or "en").strip(),
                    card_json,
                    review_kind.strip() or "ambiguous_match",
                    ambiguity_summary.strip(),
                    match_reason.strip(),
                    role_title.strip(),
                    location_preference.strip(),
                    salary_preference.strip(),
                    1 if include_no_salary else 0,
                    keywords_text,
                    context_json,
                    created_at_utc,
                    created_at_utc,
                ),
            )
            row = self._conn.execute(
                """
                SELECT id
                FROM manual_review_cases
                WHERE user_id = ? AND card_url = ?
                LIMIT 1
                """,
                (user_id, card_url.strip()),
            ).fetchone()
            self._conn.commit()
            if row is None:
                raise RuntimeError("Could not enqueue manual review case")
            return int(row["id"])

    def list_manual_review_cases(self, *, status: str = "pending", only_undispatched: bool = False, limit: int = 50) -> list[ManualReviewCase]:
        clauses = ["status = ?"]
        params: list[object] = [status.strip().lower()]
        if only_undispatched:
            clauses.append("admin_message_id IS NULL")
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT id, user_id, username, card_url, card_website, card_title, card_company,
                       card_location, card_salary, card_language, card_json, review_kind,
                       ambiguity_summary, match_reason, role_title, location_preference,
                       salary_preference, include_no_salary, keywords_text, context_json, status,
                       reviewer, review_notes, sent_count, admin_chat_id, admin_message_id,
                       dispatched_at_utc, last_dispatch_error, created_at_utc, updated_at_utc, reviewed_at_utc
                FROM manual_review_cases
                WHERE {" AND ".join(clauses)}
                ORDER BY created_at_utc ASC, id ASC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
            return [self._row_to_manual_review_case(row) for row in rows]

    def get_manual_review_case(self, review_id: int) -> ManualReviewCase | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, user_id, username, card_url, card_website, card_title, card_company,
                       card_location, card_salary, card_language, card_json, review_kind,
                       ambiguity_summary, match_reason, role_title, location_preference,
                       salary_preference, include_no_salary, keywords_text, context_json, status,
                       reviewer, review_notes, sent_count, admin_chat_id, admin_message_id,
                       dispatched_at_utc, last_dispatch_error, created_at_utc, updated_at_utc, reviewed_at_utc
                FROM manual_review_cases
                WHERE id = ?
                LIMIT 1
                """,
                (review_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_manual_review_case(row)

    def mark_manual_review_dispatched(self, review_id: int, *, admin_chat_id: int, admin_message_id: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE manual_review_cases
                SET admin_chat_id = ?,
                    admin_message_id = ?,
                    dispatched_at_utc = ?,
                    last_dispatch_error = '',
                    updated_at_utc = ?
                WHERE id = ?
                  AND status = 'pending'
                """,
                (admin_chat_id, admin_message_id, utc_now_iso(), utc_now_iso(), review_id),
            )
            self._conn.commit()

    def mark_manual_review_dispatch_error(self, review_id: int, *, error: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE manual_review_cases
                SET last_dispatch_error = ?,
                    updated_at_utc = ?
                WHERE id = ?
                  AND status = 'pending'
                """,
                (error.strip(), utc_now_iso(), review_id),
            )
            self._conn.commit()

    def set_manual_review_decision(
        self,
        review_id: int,
        *,
        decision: str,
        reviewer: str = "",
        notes: str = "",
        sent_count: int = 0,
    ) -> None:
        normalized = decision.strip().lower()
        if normalized not in {"approved_sent", "approved_no_send", "neglected", "dismissed"}:
            normalized = "dismissed"
        with self._lock:
            self._conn.execute(
                """
                UPDATE manual_review_cases
                SET status = ?,
                    reviewer = ?,
                    review_notes = ?,
                    sent_count = ?,
                    reviewed_at_utc = ?,
                    updated_at_utc = ?
                WHERE id = ?
                """,
                (
                    normalized,
                    reviewer.strip(),
                    notes.strip(),
                    max(0, int(sent_count)),
                    utc_now_iso(),
                    utc_now_iso(),
                    review_id,
                ),
            )
            self._conn.commit()

    def get_last_inactivity_report_anchor_utc(self, user_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT anchor_utc
                FROM inactivity_reports
                WHERE user_id = ?
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return ""
            return str(row["anchor_utc"]).strip()

    def get_last_inactivity_report_sent_at_utc(self, user_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT sent_at_utc
                FROM inactivity_reports
                WHERE user_id = ?
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return ""
            return str(row["sent_at_utc"]).strip()

    def mark_inactivity_report_sent(self, user_id: int, *, anchor_utc: str) -> None:
        sent_at_utc = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO inactivity_reports (user_id, anchor_utc, sent_at_utc)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    anchor_utc = excluded.anchor_utc,
                    sent_at_utc = excluded.sent_at_utc
                """,
                (user_id, anchor_utc.strip(), sent_at_utc),
            )
            self._conn.commit()

    def save_match_feedback(self, event_id: int, user_id: int, vote: str) -> None:
        normalized_vote = vote.strip().lower()
        if normalized_vote not in {"up", "down"}:
            raise ValueError("vote must be 'up' or 'down'")
        now_utc = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO match_feedback (delivery_event_id, user_id, vote, created_at_utc, updated_at_utc)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(delivery_event_id) DO UPDATE SET
                    vote = excluded.vote,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (event_id, user_id, normalized_vote, now_utc, now_utc),
            )
            self._conn.commit()

    def save_match_feedback_details(
        self,
        event_id: int,
        user_id: int,
        *,
        raw_feedback: str,
        feedback_summary: str = "",
        job_title_issue: str = "",
        location_issue: str = "",
        salary_issue: str = "",
        other_issue: str = "",
    ) -> None:
        now_utc = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                UPDATE match_feedback
                SET raw_feedback = ?,
                    feedback_summary = ?,
                    job_title_issue = ?,
                    location_issue = ?,
                    salary_issue = ?,
                    other_issue = ?,
                    updated_at_utc = ?
                WHERE delivery_event_id = ?
                  AND user_id = ?
                """,
                (
                    raw_feedback.strip(),
                    feedback_summary.strip(),
                    job_title_issue.strip(),
                    location_issue.strip(),
                    salary_issue.strip(),
                    other_issue.strip(),
                    now_utc,
                    event_id,
                    user_id,
                ),
            )
            self._conn.commit()

    def build_match_feedback_guidance(self, user_id: int, limit: int = 8) -> str:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    mf.vote,
                    mf.raw_feedback,
                    mf.feedback_summary,
                    mf.job_title_issue,
                    mf.location_issue,
                    mf.salary_issue,
                    mf.other_issue,
                    de.card_title,
                    de.card_company,
                    de.card_location,
                    de.card_website,
                    de.match_reason
                FROM match_feedback AS mf
                JOIN delivery_events AS de
                  ON de.id = mf.delivery_event_id
                WHERE mf.user_id = ?
                ORDER BY mf.updated_at_utc DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
            if not rows:
                return ""
            lines: list[str] = []
            for row in rows:
                vote = "LIKED" if str(row["vote"]).strip().lower() == "up" else "DISLIKED"
                title = str(row["card_title"]).strip()
                company = str(row["card_company"]).strip()
                location = str(row["card_location"]).strip()
                website = str(row["card_website"]).strip()
                reason = str(row["match_reason"]).strip()
                feedback_summary = str(row["feedback_summary"]).strip()
                raw_feedback = str(row["raw_feedback"]).strip()
                issue_parts = [
                    f"title_issue={str(row['job_title_issue']).strip()}" if str(row["job_title_issue"]).strip() else "",
                    f"location_issue={str(row['location_issue']).strip()}" if str(row["location_issue"]).strip() else "",
                    f"salary_issue={str(row['salary_issue']).strip()}" if str(row["salary_issue"]).strip() else "",
                    f"other_issue={str(row['other_issue']).strip()}" if str(row["other_issue"]).strip() else "",
                ]
                compact_issues = " | ".join(part for part in issue_parts if part)
                lines.append(
                    f"{vote} | title={title} | company={company} | location={location} | "
                    f"website={website} | why={reason}"
                    + (f" | feedback_summary={feedback_summary}" if feedback_summary else "")
                    + (f" | {compact_issues}" if compact_issues else "")
                    + (f" | user_feedback={raw_feedback}" if raw_feedback else "")
                )
            return "\n".join(lines)

    def mark_notification_sent(self, queue_id: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE notification_queue
                SET status = 'sent',
                    sent_at_utc = ?,
                    last_error = NULL
                WHERE id = ?
                """,
                (utc_now_iso(), queue_id),
            )
            self._conn.commit()

    def reschedule_notification(self, queue_id: int, available_after_utc: str, last_error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE notification_queue
                SET available_after_utc = ?, last_error = ?
                WHERE id = ?
                """,
                (available_after_utc, last_error, queue_id),
            )
            self._conn.commit()

    def cancel_notification(self, queue_id: int, reason: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE notification_queue
                SET status = 'cancelled',
                    last_error = ?
                WHERE id = ?
                """,
                (reason, queue_id),
            )
            self._conn.commit()

    def cancel_queued_notifications_for_user(self, user_id: int, reason: str) -> int:
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE notification_queue
                SET status = 'cancelled',
                    last_error = ?
                WHERE user_id = ?
                  AND status = 'queued'
                """,
                (reason, user_id),
            )
            self._conn.commit()
            return int(cursor.rowcount or 0)

    def create_payment_session(
        self,
        user_id: int,
        username: str,
        plan: str,
        duration_days: int,
        amount_usd: float,
        provider_payment_id: str,
        payment_url: str,
        status: str = "waiting",
    ) -> str:
        local_payment_id = uuid.uuid4().hex[:16]
        created_at_utc = utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO payment_sessions (
                    local_payment_id, user_id, username, plan, duration_days, amount_usd,
                    provider_payment_id, payment_url, status, created_at_utc, updated_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    local_payment_id,
                    user_id,
                    username,
                    plan,
                    duration_days,
                    float(amount_usd),
                    provider_payment_id,
                    payment_url,
                    status.lower(),
                    created_at_utc,
                    created_at_utc,
                ),
            )
            self._conn.commit()
        return local_payment_id

    def get_payment_session(self, local_payment_id: str) -> PaymentSession | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT local_payment_id, user_id, username, plan, duration_days, amount_usd,
                       provider_payment_id, payment_url, status, created_at_utc, updated_at_utc
                FROM payment_sessions
                WHERE local_payment_id = ?
                LIMIT 1
                """,
                (local_payment_id,),
            ).fetchone()
            if row is None:
                return None
            return PaymentSession(
                local_payment_id=str(row["local_payment_id"]),
                user_id=int(row["user_id"]),
                username=str(row["username"]),
                plan=str(row["plan"]),
                duration_days=int(row["duration_days"]),
                amount_usd=float(row["amount_usd"]),
                provider_payment_id=str(row["provider_payment_id"]),
                payment_url=str(row["payment_url"]),
                status=str(row["status"]).lower(),
                created_at_utc=str(row["created_at_utc"]),
                updated_at_utc=str(row["updated_at_utc"]),
            )

    def update_payment_status(self, local_payment_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE payment_sessions
                SET status = ?, updated_at_utc = ?
                WHERE local_payment_id = ?
                """,
                (status.lower(), utc_now_iso(), local_payment_id),
            )
            self._conn.commit()

    def cancel_payment(self, local_payment_id: str) -> None:
        self.update_payment_status(local_payment_id, "failed")

    def list_pending_payment_sessions(self, limit: int = 100) -> list[PaymentSession]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT local_payment_id, user_id, username, plan, duration_days, amount_usd,
                       provider_payment_id, payment_url, status, created_at_utc, updated_at_utc
                FROM payment_sessions
                WHERE status NOT IN ('finished', 'confirmed', 'failed', 'expired', 'cancelled')
                ORDER BY updated_at_utc ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                PaymentSession(
                    local_payment_id=str(row["local_payment_id"]),
                    user_id=int(row["user_id"]),
                    username=str(row["username"]),
                    plan=str(row["plan"]),
                    duration_days=int(row["duration_days"]),
                    amount_usd=float(row["amount_usd"]),
                    provider_payment_id=str(row["provider_payment_id"]),
                    payment_url=str(row["payment_url"]),
                    status=str(row["status"]).lower(),
                    created_at_utc=str(row["created_at_utc"]),
                    updated_at_utc=str(row["updated_at_utc"]),
                )
                for row in rows
            ]

    def reset_dashboard_stats(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                DELETE FROM notification_queue;
                DELETE FROM delivery_job_queue;
                DELETE FROM delivery_events;
                DELETE FROM match_feedback;
                DELETE FROM filter_audit_logs;
                DELETE FROM filter_mismatch_events;
                DELETE FROM inactivity_reports;
                DELETE FROM manual_review_cases;
                DELETE FROM payment_sessions;
                DELETE FROM sqlite_sequence
                WHERE name IN (
                    'notification_queue',
                    'delivery_job_queue',
                    'delivery_events',
                    'match_feedback',
                    'filter_audit_logs',
                    'filter_mismatch_events',
                    'manual_review_cases',
                    'payment_sessions'
                );
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
