from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

from job_bot.human_review_confidence import stabilize_job_confidence
from job_bot.language_utils import normalize_match_text, tokenize_match_text
from job_bot.models import JobCard

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
class LinkState:
    website: str
    url: str
    status: str
    not_job_flags: int
    neglected: bool
    telegram_sent: bool


@dataclass(slots=True)
class HumanReviewItem:
    id: int
    website: str
    url: str
    title: str
    description: str
    location: str
    salary: str
    confidence: float
    reason: str
    source: str
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
    card_json: str


@dataclass(slots=True)
class ClusterRegistration:
    cluster_key: str
    is_duplicate: bool
    canonical_url: str
    canonical_website: str
    canonical_changed: bool = False


@dataclass(slots=True)
class JobIdentity:
    company_signature: str
    role_signature: str
    location_signature: str
    salary_signature: str
    work_mode: str
    description_signature: str


@dataclass(slots=True)
class DiscoveryFeedback:
    preferred_prefixes: tuple[str, ...] = ()
    blocked_prefixes: tuple[str, ...] = ()
    blocked_urls: tuple[str, ...] = ()
    blocked_source_urls: tuple[str, ...] = ()


@dataclass(slots=True)
class AlertHealthSnapshot:
    last_scan_utc: str = ""
    discovered_posts: int = 0
    blocked_sources: int = 0
    blocked_source_urls: tuple[str, ...] = ()
    freshness_rejects: int = 0
    duplicate_prevented: int = 0
    rejected_posts: int = 0


@dataclass(slots=True)
class CrawlerHealthState:
    health_key: str
    severity: str
    state: str
    reason: str
    details_json: str
    updated_at_utc: str


@dataclass(slots=True)
class SiteCooldown:
    website: str
    reason_code: str
    reason: str
    blocked_until_utc: str
    hit_count: int = 0


PREFERRED_SOURCE_RANKS = {
    "toptal.com": 168,
    "contra.com": 156,
    "freelancer.com": 154,
    "peopleperhour.com": 153,
    "guru.com": 152,
    "dribbble.com": 149,
    "workingnotworking.com": 148,
    "worksome.com": 147,
    "weworkremotely.com": 144,
    "workatastartup.com": 140,
    "ycombinator.com": 138,
    "wellfound.com": 126,
    "flexjobs.com": 118,
    "builtin.com": 114,
    "remoteok.com": 110,
    "linkedin.com": 102,
    "indeed.com": 94,
    "glassdoor.com": 90,
    "ziprecruiter.com": 88,
    "ziprecruiter.ie": 88,
    "jooble.org": 84,
    "uiuxjobsboard.com": 82,
}
COMPANY_DOMAIN_STOPWORDS = {
    "inc",
    "llc",
    "ltd",
    "limited",
    "corp",
    "co",
    "company",
    "group",
    "gmbh",
    "plc",
    "sa",
    "ag",
    "bv",
}
ROLE_SIGNATURE_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "job",
    "jobs",
    "role",
    "position",
    "opening",
    "opportunity",
    "vacancy",
    "vacancies",
    "full",
    "time",
    "part",
    "senior",
    "junior",
    "lead",
    "principal",
    "staff",
    "contract",
    "remote",
    "global",
    "onsite",
    "hybrid",
    "вакансия",
    "вакансии",
    "должность",
    "роль",
    "полная",
    "частичная",
    "удаленно",
    "удаленка",
    "гибрид",
    "офис",
    "старший",
    "младший",
    "ведущий",
}
DESCRIPTION_SIGNATURE_STOPWORDS = ROLE_SIGNATURE_STOPWORDS | {
    "build",
    "work",
    "team",
    "teams",
    "using",
    "experience",
    "required",
    "including",
    "company",
    "responsibilities",
    "requirements",
    "обязанности",
    "требования",
    "команда",
    "компании",
    "опыт",
    "используя",
    "работать",
}
LOCATION_SIGNATURE_STOPWORDS = {
    "remote",
    "onsite",
    "hybrid",
    "worldwide",
    "global",
    "office",
    "удаленно",
    "удаленка",
    "гибрид",
    "офис",
    "мир",
}
DOMAIN_SIGNATURE_STOPWORDS = {
    "www",
    "jobs",
    "job",
    "careers",
    "career",
    "vacancy",
    "vacancies",
}


class StateStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._audit_log_path = db_path.parent / "link_activity.jsonl"
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

                CREATE TABLE IF NOT EXISTS seen_links (
                    website TEXT NOT NULL,
                    url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    not_job_flags INTEGER NOT NULL DEFAULT 0,
                    neglected INTEGER NOT NULL DEFAULT 0,
                    telegram_sent INTEGER NOT NULL DEFAULT 0,
                    first_seen_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    last_seen_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    PRIMARY KEY (website, url)
                );

                CREATE TABLE IF NOT EXISTS cycle_stats (
                    cycle_utc TEXT NOT NULL,
                    website TEXT NOT NULL,
                    fetched_pages INTEGER NOT NULL,
                    discovered_links INTEGER NOT NULL,
                    checked_links INTEGER NOT NULL,
                    seen_links_in_row INTEGER NOT NULL,
                    new_cards INTEGER NOT NULL,
                    errors INTEGER NOT NULL,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS discovery_page_feedback (
                    website TEXT NOT NULL,
                    url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    hit_count INTEGER NOT NULL DEFAULT 1,
                    first_seen_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    last_seen_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    PRIMARY KEY (website, url)
                );

                CREATE TABLE IF NOT EXISTS human_review_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    website TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    location TEXT NOT NULL DEFAULT '',
                    salary TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'ai',
                    status TEXT NOT NULL DEFAULT 'pending',
                    reviewer TEXT NOT NULL DEFAULT '',
                    review_notes TEXT NOT NULL DEFAULT '',
                    sent_count INTEGER NOT NULL DEFAULT 0,
                    admin_chat_id INTEGER,
                    admin_message_id INTEGER,
                    dispatched_at_utc TEXT NOT NULL DEFAULT '',
                    last_dispatch_error TEXT NOT NULL DEFAULT '',
                    card_json TEXT NOT NULL DEFAULT '{}',
                    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    reviewed_at_utc TEXT,
                    UNIQUE (website, url)
                );
                CREATE INDEX IF NOT EXISTS idx_human_review_status
                    ON human_review_queue(status, created_at_utc DESC);

                CREATE TABLE IF NOT EXISTS human_review_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    website TEXT NOT NULL,
                    url TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    location TEXT NOT NULL DEFAULT '',
                    salary TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );

                CREATE TABLE IF NOT EXISTS runtime_metrics (
                    metric_key TEXT PRIMARY KEY,
                    metric_value REAL NOT NULL DEFAULT 0,
                    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );

                CREATE TABLE IF NOT EXISTS fetch_strategy_stats (
                    domain TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    timeout_count INTEGER NOT NULL DEFAULT 0,
                    rate_limit_count INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    skipped_count INTEGER NOT NULL DEFAULT 0,
                    total_latency_ms REAL NOT NULL DEFAULT 0,
                    last_status INTEGER,
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    PRIMARY KEY (domain, strategy)
                );

                CREATE TABLE IF NOT EXISTS crawler_health_state (
                    health_key TEXT PRIMARY KEY,
                    severity TEXT NOT NULL DEFAULT 'info',
                    state TEXT NOT NULL DEFAULT 'healthy',
                    reason TEXT NOT NULL DEFAULT '',
                    details_json TEXT NOT NULL DEFAULT '{}',
                    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );

                CREATE TABLE IF NOT EXISTS admin_alert_log (
                    alert_key TEXT PRIMARY KEY,
                    alert_kind TEXT NOT NULL DEFAULT '',
                    alert_message TEXT NOT NULL DEFAULT '',
                    sent_count INTEGER NOT NULL DEFAULT 0,
                    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );

                CREATE TABLE IF NOT EXISTS job_clusters (
                    cluster_key TEXT PRIMARY KEY,
                    canonical_url TEXT NOT NULL,
                    canonical_website TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    company TEXT NOT NULL DEFAULT '',
                    location TEXT NOT NULL DEFAULT '',
                    source_rank INTEGER NOT NULL DEFAULT 0,
                    company_signature TEXT NOT NULL DEFAULT '',
                    role_signature TEXT NOT NULL DEFAULT '',
                    location_signature TEXT NOT NULL DEFAULT '',
                    salary_signature TEXT NOT NULL DEFAULT '',
                    work_mode TEXT NOT NULL DEFAULT '',
                    description_signature TEXT NOT NULL DEFAULT '',
                    first_seen_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );

                CREATE TABLE IF NOT EXISTS job_cluster_sources (
                    cluster_key TEXT NOT NULL,
                    website TEXT NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    company TEXT NOT NULL DEFAULT '',
                    location TEXT NOT NULL DEFAULT '',
                    source_rank INTEGER NOT NULL DEFAULT 0,
                    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    PRIMARY KEY (cluster_key, url)
                );
                CREATE INDEX IF NOT EXISTS idx_job_cluster_sources_cluster
                    ON job_cluster_sources(cluster_key, source_rank DESC, created_at_utc ASC);

                CREATE TABLE IF NOT EXISTS job_cluster_aliases (
                    alias_key TEXT PRIMARY KEY,
                    cluster_key TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE INDEX IF NOT EXISTS idx_job_cluster_aliases_cluster
                    ON job_cluster_aliases(cluster_key, created_at_utc ASC);
                """
            )
            self._ensure_column("seen_links", "not_job_flags", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("seen_links", "neglected", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("seen_links", "telegram_sent", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("human_review_queue", "admin_chat_id", "INTEGER")
            self._ensure_column("human_review_queue", "admin_message_id", "INTEGER")
            self._ensure_column("human_review_queue", "dispatched_at_utc", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("job_clusters", "company_signature", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("job_clusters", "role_signature", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("job_clusters", "location_signature", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("job_clusters", "salary_signature", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("job_clusters", "work_mode", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("job_clusters", "description_signature", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("fetch_strategy_stats", "retry_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("fetch_strategy_stats", "skipped_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("fetch_strategy_stats", "last_status", "INTEGER")
            self._ensure_column("fetch_strategy_stats", "last_error", "TEXT NOT NULL DEFAULT ''")
            self._conn.commit()

    def _ensure_column(self, table: str, column: str, sql_type: str) -> None:
        current_cols = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}  # noqa: S608
        if column not in current_cols:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")  # noqa: S608

    def _append_audit_log(self, *, event_type: str, website: str, url: str, payload: dict[str, object]) -> None:
        entry = {
            "logged_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event_type": event_type,
            "website": website,
            "url": url,
            **payload,
        }
        self._audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._audit_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def increment_runtime_metric(self, metric_key: str, amount: float = 1.0) -> None:
        normalized_key = str(metric_key or "").strip().lower()
        if not normalized_key:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runtime_metrics (metric_key, metric_value)
                VALUES (?, ?)
                ON CONFLICT(metric_key) DO UPDATE SET
                    metric_value = runtime_metrics.metric_value + excluded.metric_value,
                    updated_at_utc = (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                """,
                (normalized_key, float(amount)),
            )
            self._conn.commit()

    def set_runtime_metric(self, metric_key: str, value: float) -> None:
        normalized_key = str(metric_key or "").strip().lower()
        if not normalized_key:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runtime_metrics (metric_key, metric_value)
                VALUES (?, ?)
                ON CONFLICT(metric_key) DO UPDATE SET
                    metric_value = excluded.metric_value,
                    updated_at_utc = (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                """,
                (normalized_key, float(value)),
            )
            self._conn.commit()

    def get_runtime_metric(self, metric_key: str, default: float = 0.0) -> float:
        normalized_key = str(metric_key or "").strip().lower()
        if not normalized_key:
            return float(default)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT metric_value
                FROM runtime_metrics
                WHERE metric_key = ?
                LIMIT 1
                """,
                (normalized_key,),
            ).fetchone()
            if row is None:
                return float(default)
            try:
                return float(row["metric_value"] or 0.0)
            except (TypeError, ValueError):
                return float(default)

    def get_runtime_metrics_snapshot(self, prefixes: tuple[str, ...] = ()) -> dict[str, float]:
        clauses = []
        params: list[object] = []
        for prefix in prefixes:
            normalized_prefix = str(prefix or "").strip().lower()
            if not normalized_prefix:
                continue
            clauses.append("metric_key LIKE ?")
            params.append(f"{normalized_prefix}%")
        where_clause = f"WHERE {' OR '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT metric_key, metric_value
                FROM runtime_metrics
                {where_clause}
                ORDER BY metric_key ASC
                """,
                params,
            ).fetchall()
        snapshot: dict[str, float] = {}
        for row in rows:
            try:
                snapshot[str(row["metric_key"])] = float(row["metric_value"] or 0.0)
            except (TypeError, ValueError):
                snapshot[str(row["metric_key"])] = 0.0
        return snapshot

    def record_fetch_strategy_result(
        self,
        domain: str,
        strategy: str,
        *,
        success: bool,
        latency_ms: float,
        timed_out: bool = False,
        rate_limited: bool = False,
        retries: int = 0,
        skipped: bool = False,
        status: int | None = None,
        error: str = "",
    ) -> None:
        normalized_domain = str(domain or "").strip().lower()
        normalized_strategy = str(strategy or "").strip().lower()
        if not normalized_domain or not normalized_strategy:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO fetch_strategy_stats (
                    domain, strategy, success_count, failure_count, timeout_count,
                    rate_limit_count, retry_count, skipped_count, total_latency_ms,
                    last_status, last_error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain, strategy) DO UPDATE SET
                    success_count = fetch_strategy_stats.success_count + excluded.success_count,
                    failure_count = fetch_strategy_stats.failure_count + excluded.failure_count,
                    timeout_count = fetch_strategy_stats.timeout_count + excluded.timeout_count,
                    rate_limit_count = fetch_strategy_stats.rate_limit_count + excluded.rate_limit_count,
                    retry_count = fetch_strategy_stats.retry_count + excluded.retry_count,
                    skipped_count = fetch_strategy_stats.skipped_count + excluded.skipped_count,
                    total_latency_ms = fetch_strategy_stats.total_latency_ms + excluded.total_latency_ms,
                    last_status = excluded.last_status,
                    last_error = excluded.last_error,
                    updated_at_utc = (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                """,
                (
                    normalized_domain,
                    normalized_strategy,
                    1 if success else 0,
                    0 if success else 1,
                    1 if timed_out else 0,
                    1 if rate_limited else 0,
                    max(0, int(retries)),
                    1 if skipped else 0,
                    max(0.0, float(latency_ms)),
                    int(status) if status is not None else None,
                    str(error or "").strip(),
                ),
            )
            self._conn.commit()

    def get_fetch_strategy_plan(
        self,
        domain: str,
        strategies: list[str],
        *,
        max_count: int,
    ) -> list[str]:
        normalized_domain = str(domain or "").strip().lower()
        normalized_strategies = [str(item or "").strip().lower() for item in strategies if str(item or "").strip()]
        if not normalized_domain or not normalized_strategies:
            return []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT strategy, success_count, failure_count, timeout_count, rate_limit_count,
                       retry_count, skipped_count, total_latency_ms
                FROM fetch_strategy_stats
                WHERE domain = ?
                """,
                (normalized_domain,),
            ).fetchall()
        stats_by_strategy = {str(row["strategy"]): row for row in rows}
        scored: list[tuple[float, str]] = []
        for order_index, strategy in enumerate(normalized_strategies):
            row = stats_by_strategy.get(strategy)
            if row is None:
                score = 50_000.0 - order_index
                scored.append((score, strategy))
                continue
            success_count = int(row["success_count"] or 0)
            failure_count = int(row["failure_count"] or 0)
            timeout_count = int(row["timeout_count"] or 0)
            rate_limit_count = int(row["rate_limit_count"] or 0)
            skipped_count = int(row["skipped_count"] or 0)
            total_attempts = max(1, success_count + failure_count + skipped_count)
            if success_count == 0 and (failure_count + timeout_count + rate_limit_count) >= 3:
                continue
            avg_latency_ms = float(row["total_latency_ms"] or 0.0) / max(1, success_count + failure_count)
            success_rate = success_count / total_attempts
            score = (
                success_rate * 1000.0
                - avg_latency_ms / 1000.0
                - (timeout_count * 25.0)
                - (rate_limit_count * 15.0)
                - order_index
            )
            scored.append((score, strategy))
        scored.sort(key=lambda item: (-item[0], normalized_strategies.index(item[1])))
        ordered = [strategy for _, strategy in scored]
        fallback = [strategy for strategy in normalized_strategies if strategy not in ordered]
        final_plan = ordered + fallback
        return final_plan[: max(1, int(max_count))]

    def set_crawler_health(
        self,
        *,
        health_key: str = "crawler",
        severity: str,
        state: str,
        reason: str,
        details: dict[str, object] | None = None,
    ) -> None:
        normalized_key = str(health_key or "crawler").strip().lower() or "crawler"
        details_json = json.dumps(details or {}, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO crawler_health_state (health_key, severity, state, reason, details_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(health_key) DO UPDATE SET
                    severity = excluded.severity,
                    state = excluded.state,
                    reason = excluded.reason,
                    details_json = excluded.details_json,
                    updated_at_utc = (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                """,
                (
                    normalized_key,
                    str(severity or "info").strip().lower(),
                    str(state or "healthy").strip().lower(),
                    str(reason or "").strip(),
                    details_json,
                ),
            )
            self._conn.commit()

    def get_crawler_health(self, health_key: str = "crawler") -> CrawlerHealthState | None:
        normalized_key = str(health_key or "crawler").strip().lower() or "crawler"
        with self._lock:
            row = self._conn.execute(
                """
                SELECT health_key, severity, state, reason, details_json, updated_at_utc
                FROM crawler_health_state
                WHERE health_key = ?
                LIMIT 1
                """,
                (normalized_key,),
            ).fetchone()
        if row is None:
            return None
        return CrawlerHealthState(
            health_key=str(row["health_key"]),
            severity=str(row["severity"]),
            state=str(row["state"]),
            reason=str(row["reason"]),
            details_json=str(row["details_json"]),
            updated_at_utc=str(row["updated_at_utc"]),
        )

    def should_send_admin_alert(self, alert_key: str, *, suppress_for_seconds: int = 900) -> bool:
        normalized_key = str(alert_key or "").strip().lower()
        if not normalized_key:
            return False
        with self._lock:
            row = self._conn.execute(
                """
                SELECT updated_at_utc
                FROM admin_alert_log
                WHERE alert_key = ?
                LIMIT 1
                """,
                (normalized_key,),
            ).fetchone()
        if row is None or not row["updated_at_utc"]:
            return True
        try:
            last_sent_epoch = datetime.fromisoformat(str(row["updated_at_utc"]).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return True
        return (time.time() - last_sent_epoch) >= max(30, int(suppress_for_seconds))

    def mark_admin_alert_sent(self, alert_key: str, *, alert_kind: str, alert_message: str) -> None:
        normalized_key = str(alert_key or "").strip().lower()
        if not normalized_key:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO admin_alert_log (alert_key, alert_kind, alert_message, sent_count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(alert_key) DO UPDATE SET
                    alert_kind = excluded.alert_kind,
                    alert_message = excluded.alert_message,
                    sent_count = admin_alert_log.sent_count + 1,
                    updated_at_utc = (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                """,
                (
                    normalized_key,
                    str(alert_kind or "").strip(),
                    str(alert_message or "").strip(),
                ),
            )
            self._conn.commit()

    def record_link_observation(self, website: str, url: str, *, status: str, detail: str = "") -> None:
        with self._lock:
            self._append_audit_log(
                event_type="link_observation",
                website=website,
                url=url,
                payload={"status": status, "detail": detail.strip()},
            )

    def record_discovery_page_outcome(self, website: str, url: str, status: str) -> None:
        normalized_status = str(status or "").strip().lower() or "unknown"
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO discovery_page_feedback (website, url, status, hit_count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(website, url) DO UPDATE SET
                    status = excluded.status,
                    hit_count = discovery_page_feedback.hit_count + 1,
                    last_seen_utc = (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                """,
                (website, url, normalized_status),
            )
            self._conn.commit()
            self._append_audit_log(
                event_type="discovery_page",
                website=website,
                url=url,
                payload={"status": normalized_status},
            )

    @staticmethod
    def _parse_utc_iso(value: str) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None

    @staticmethod
    def _format_utc_iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def list_temporarily_blocked_sites(
        self,
        websites: list[str],
        *,
        cooldown_seconds: int = 1800,
        challenge_threshold: int = 2,
    ) -> dict[str, SiteCooldown]:
        normalized_websites = [str(website or "").strip() for website in websites if str(website or "").strip()]
        if not normalized_websites:
            return {}

        placeholders = ", ".join("?" for _ in normalized_websites)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT website, url, status, hit_count, last_seen_utc
                FROM discovery_page_feedback
                WHERE website IN ({placeholders})
                  AND website = url
                  AND status = 'challenge_blocked'
                ORDER BY website ASC
                """,
                normalized_websites,
            ).fetchall()

        now_utc = datetime.now(timezone.utc)
        active_blocks: dict[str, SiteCooldown] = {}
        base_cooldown_seconds = max(300, int(cooldown_seconds))
        minimum_threshold = max(1, int(challenge_threshold))
        for row in rows:
            website = str(row["website"] or "").strip()
            hit_count = max(0, int(row["hit_count"] or 0))
            if not website or hit_count < minimum_threshold:
                continue
            last_seen_utc = self._parse_utc_iso(str(row["last_seen_utc"] or ""))
            if last_seen_utc is None:
                continue
            extra_steps = max(0, hit_count - minimum_threshold)
            cooldown_for = min(base_cooldown_seconds * max(1, 2**extra_steps), 14400)
            blocked_until = last_seen_utc + timedelta(seconds=cooldown_for)
            if blocked_until <= now_utc:
                continue
            blocked_until_utc = self._format_utc_iso(blocked_until)
            active_blocks[website] = SiteCooldown(
                website=website,
                reason_code="challenge_blocked",
                reason=(
                    "Temporarily paused after repeated challenge pages blocked discovery "
                    f"(retry after {blocked_until_utc})."
                ),
                blocked_until_utc=blocked_until_utc,
                hit_count=hit_count,
            )
        return active_blocks

    def get_link_state(self, website: str, url: str) -> LinkState | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT website, url, status, not_job_flags, neglected, telegram_sent
                FROM seen_links
                WHERE website = ? AND url = ?
                LIMIT 1
                """,
                (website, url),
            ).fetchone()
            if row is None:
                return None
            return LinkState(
                website=row["website"],
                url=row["url"],
                status=row["status"],
                not_job_flags=int(row["not_job_flags"]),
                neglected=bool(row["neglected"]),
                telegram_sent=bool(row["telegram_sent"]),
            )

    def mark_status(self, website: str, url: str, status: str, neglected: bool | None = None) -> None:
        with self._lock:
            if neglected is None:
                update_query = """
                INSERT INTO seen_links (website, url, status, not_job_flags, neglected)
                VALUES (?, ?, ?, 0, 0)
                ON CONFLICT(website, url) DO UPDATE SET
                    status = excluded.status,
                    last_seen_utc = (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                """
                params = (website, url, status)
            else:
                update_query = """
                INSERT INTO seen_links (website, url, status, not_job_flags, neglected)
                VALUES (?, ?, ?, 0, ?)
                ON CONFLICT(website, url) DO UPDATE SET
                    status = excluded.status,
                    neglected = excluded.neglected,
                    last_seen_utc = (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                """
                params = (website, url, status, int(neglected))

            self._conn.execute(update_query, params)
            self._conn.commit()
            if status not in {"queued"}:
                self._append_audit_log(
                    event_type="link_status",
                    website=website,
                    url=url,
                    payload={"status": status, "neglected": int(bool(neglected)) if neglected is not None else None},
                )

    def increment_not_job_flag(self, website: str, url: str, max_flags: int = 3) -> LinkState:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO seen_links (website, url, status, not_job_flags, neglected)
                VALUES (?, ?, 'checked_not_job', 1, 0)
                ON CONFLICT(website, url) DO UPDATE SET
                    status = CASE
                        WHEN seen_links.not_job_flags + 1 >= ? THEN 'neglected_not_job'
                        ELSE 'checked_not_job'
                    END,
                    not_job_flags = MIN(seen_links.not_job_flags + 1, ?),
                    neglected = CASE
                        WHEN seen_links.not_job_flags + 1 >= ? THEN 1
                        ELSE seen_links.neglected
                    END,
                    last_seen_utc = (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                """,
                (website, url, max_flags, max_flags, max_flags),
            )
            row = self._conn.execute(
                """
                SELECT website, url, status, not_job_flags, neglected, telegram_sent
                FROM seen_links
                WHERE website = ? AND url = ?
                LIMIT 1
                """,
                (website, url),
            ).fetchone()
            self._conn.commit()
            if row is None:
                raise RuntimeError("Could not update not-job flags")
            self._append_audit_log(
                event_type="link_status",
                website=website,
                url=url,
                payload={
                    "status": row["status"],
                    "not_job_flags": int(row["not_job_flags"]),
                    "neglected": int(bool(row["neglected"])),
                },
            )

            return LinkState(
                website=row["website"],
                url=row["url"],
                status=row["status"],
                not_job_flags=int(row["not_job_flags"]),
                neglected=bool(row["neglected"]),
                telegram_sent=bool(row["telegram_sent"]),
            )

    def mark_job_saved(self, website: str, url: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO seen_links (website, url, status, not_job_flags, neglected, telegram_sent)
                VALUES (?, ?, 'job_saved', 0, 0, 0)
                ON CONFLICT(website, url) DO UPDATE SET
                    status = 'job_saved',
                    not_job_flags = 0,
                    neglected = 0,
                    telegram_sent = seen_links.telegram_sent,
                    last_seen_utc = (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                """,
                (website, url),
            )
            self._conn.commit()
            self._append_audit_log(
                event_type="link_status",
                website=website,
                url=url,
                payload={"status": "job_saved", "neglected": 0},
            )

    def mark_telegram_sent(self, website: str, url: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE seen_links
                SET telegram_sent = 1,
                    last_seen_utc = (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                WHERE website = ? AND url = ?
                """,
                (website, url),
            )
            self._conn.commit()

    @staticmethod
    def _normalized_cluster_text(value: str) -> str:
        tokens = [token for token in normalize_match_text(value).split() if token]
        return " ".join(tokens[:10])

    @staticmethod
    def _extract_domain(value: str) -> str:
        parsed = urlparse(str(value or "").strip())
        domain = (parsed.netloc or str(value or "").strip()).lower()
        return domain[4:] if domain.startswith("www.") else domain

    @staticmethod
    def _normalize_path(value: str) -> str:
        parsed = urlparse(str(value or "").strip())
        segments = [segment.lower() for segment in parsed.path.split("/") if segment]
        if not segments:
            return ""
        return "/" + "/".join(segments)

    @classmethod
    def _path_prefixes(cls, value: str, max_segments: int = 3) -> list[str]:
        normalized_path = cls._normalize_path(value)
        if not normalized_path:
            return []
        segments = [segment for segment in normalized_path.strip("/").split("/") if segment]
        return ["/" + "/".join(segments[:idx]) for idx in range(1, min(len(segments), max_segments) + 1)]

    @classmethod
    def _company_domain_tokens(cls, company: str) -> set[str]:
        tokens = {
            token
            for token in cls._normalized_cluster_text(company).split()
            if token not in COMPANY_DOMAIN_STOPWORDS and len(token) >= 3
        }
        return tokens

    @classmethod
    def _cluster_key_for_card(cls, card: JobCard) -> str:
        title = cls._normalized_cluster_text(card.title)
        company = cls._normalized_cluster_text(getattr(card, "company", ""))
        location = cls._normalized_cluster_text(card.location)
        if not title:
            return ""
        identity = "|".join(part for part in (title, company, location) if part)
        if not identity:
            return ""
        return hashlib.sha1(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _hashed_identity_key(label: str, parts: list[str]) -> str:
        payload = "|".join(part for part in parts if part)
        return hashlib.sha1(f"{label}|{payload}".encode("utf-8")).hexdigest() if payload else ""

    @staticmethod
    def _signature_text(tokens: list[str]) -> str:
        unique_sorted = sorted({token for token in tokens if token})
        return " ".join(unique_sorted)

    @classmethod
    def _signature_tokens(
        cls,
        value: str,
        *,
        limit: int,
        min_length: int = 2,
        stopwords: set[str] | None = None,
    ) -> list[str]:
        filtered: list[str] = []
        for token in tokenize_match_text(value, min_length=min_length, unique=True):
            if stopwords and token in stopwords:
                continue
            if token.isdigit() and len(token) < 3:
                continue
            filtered.append(token)
            if len(filtered) >= limit:
                break
        return filtered

    @staticmethod
    def _salary_signature(value: str) -> str:
        numbers = [match.replace(",", "") for match in re.findall(r"\d[\d,]*", str(value or ""))]
        content = normalize_match_text(value)
        payment_model_markers: list[str] = []
        for label, markers in (
            ("fixed", ("fixed", "flat fee", "one off", "one-off", "per project")),
            ("hourly", ("hourly", "/hr", "per hour", " hour ", "hr")),
            ("retainer", ("retainer", "monthly retainer", "weekly retainer")),
            ("milestone", ("milestone", "per milestone")),
            ("commission", ("commission", "revenue share", "success fee")),
            ("negotiable", ("negotiable", "tbd", "to be discussed")),
        ):
            if any(marker in content for marker in markers):
                payment_model_markers.append(label)
        signature_parts = [*numbers[:3], *payment_model_markers[:2]]
        return "-".join(signature_parts)

    @staticmethod
    def _work_mode_signature(card: JobCard) -> str:
        content = normalize_match_text(" ".join(part for part in (card.title, card.description, card.location) if part))
        if any(
            token in content
            for token in {
                "remote",
                "work from home",
                "wfh",
                "удаленно",
                "удаленка",
                "удаленная",
                "удаленный",
                "удаленная работа",
            }
        ):
            return "remote"
        if any(token in content for token in {"hybrid", "гибрид"}):
            return "hybrid"
        if any(token in content for token in {"onsite", "on site", "office", "офис"}):
            return "onsite"
        return ""

    @classmethod
    def _company_signature_for_card(cls, card: JobCard) -> str:
        company_tokens = cls._signature_tokens(
            getattr(card, "client", "")
            or getattr(card, "requester", "")
            or getattr(card, "company", "")
            or getattr(card, "counterparty", ""),
            limit=4,
            min_length=2,
            stopwords=COMPANY_DOMAIN_STOPWORDS,
        )
        if company_tokens:
            return cls._signature_text(company_tokens)

        domain_tokens = [
            token
            for token in re.split(r"[.\-]+", cls._extract_domain(card.url or card.website))
            if token and token not in DOMAIN_SIGNATURE_STOPWORDS and len(token) >= 3
        ]
        return cls._signature_text(domain_tokens[:4])

    @classmethod
    def _role_signature_for_card(cls, card: JobCard) -> str:
        title_tokens = cls._signature_tokens(
            card.title,
            limit=5,
            min_length=2,
            stopwords=ROLE_SIGNATURE_STOPWORDS,
        )
        description_tokens: list[str] = []
        if len(title_tokens) < 3:
            description_tokens = cls._signature_tokens(
                getattr(card, "scope_summary", "") or card.description,
                limit=3,
                min_length=4,
                stopwords=DESCRIPTION_SIGNATURE_STOPWORDS | set(title_tokens),
            )
        return cls._signature_text([*title_tokens, *description_tokens])

    @classmethod
    def _location_signature_for_card(cls, card: JobCard) -> str:
        return cls._signature_text(
            cls._signature_tokens(
                card.location,
                limit=4,
                min_length=2,
                stopwords=LOCATION_SIGNATURE_STOPWORDS,
            )
        )

    @classmethod
    def _description_signature_for_card(cls, card: JobCard) -> str:
        role_tokens = set(cls._signature_tokens(card.title, limit=6, min_length=2, stopwords=ROLE_SIGNATURE_STOPWORDS))
        company_tokens = set(
            cls._signature_tokens(
                getattr(card, "client", "")
                or getattr(card, "requester", "")
                or getattr(card, "company", ""),
                limit=4,
                min_length=2,
            )
        )
        location_tokens = set(cls._signature_tokens(card.location, limit=4, min_length=2))
        stopwords = DESCRIPTION_SIGNATURE_STOPWORDS | role_tokens | company_tokens | location_tokens
        opportunity_detail_text = " ".join(
            part
            for part in (
                getattr(card, "scope_summary", "") or card.description,
                getattr(card, "skills_required", ""),
                getattr(card, "industry", ""),
                getattr(card, "engagement_type", ""),
                getattr(card, "duration", ""),
                getattr(card, "start_timeline", ""),
                "proposal deadline" if getattr(card, "proposal_deadline", "") else "",
            )
            if part
        )
        return cls._signature_text(
            cls._signature_tokens(opportunity_detail_text, limit=7, min_length=4, stopwords=stopwords)
        )

    @classmethod
    def _identity_for_card(cls, card: JobCard) -> JobIdentity:
        return JobIdentity(
            company_signature=cls._company_signature_for_card(card),
            role_signature=cls._role_signature_for_card(card),
            location_signature=cls._location_signature_for_card(card),
            salary_signature=cls._salary_signature(card.payment_terms or card.salary),
            work_mode=cls._work_mode_signature(card),
            description_signature=cls._description_signature_for_card(card),
        )

    @classmethod
    def _canonical_cluster_key_for_identity(cls, identity: JobIdentity) -> str:
        if not identity.role_signature:
            return ""
        anchor = identity.location_signature or identity.work_mode
        extra = identity.salary_signature or identity.description_signature
        parts = [identity.company_signature, identity.role_signature, anchor, extra]
        return cls._hashed_identity_key("canonical-job-id", parts)

    @classmethod
    def _alias_keys_for_identity(cls, identity: JobIdentity, legacy_cluster_key: str = "") -> set[str]:
        alias_keys: set[str] = set()
        combinations = (
            ("legacy", [legacy_cluster_key] if legacy_cluster_key else []),
            (
                "company-role-location-desc",
                [
                    identity.company_signature,
                    identity.role_signature,
                    identity.location_signature or identity.work_mode,
                    identity.description_signature,
                ],
            ),
            (
                "company-role-location-salary",
                [
                    identity.company_signature,
                    identity.role_signature,
                    identity.location_signature or identity.work_mode,
                    identity.salary_signature,
                ],
            ),
            (
                "company-role-mode-desc",
                [
                    identity.company_signature,
                    identity.role_signature,
                    identity.work_mode,
                    identity.description_signature,
                ],
            ),
        )
        for label, parts in combinations:
            filtered = [part for part in parts if part]
            if len(filtered) < 2:
                continue
            alias_key = cls._hashed_identity_key(label, filtered)
            if alias_key:
                alias_keys.add(alias_key)
        return alias_keys

    @classmethod
    def _find_existing_cluster(
        cls,
        conn: sqlite3.Connection,
        identity: JobIdentity,
        candidate_cluster_keys: list[str],
        alias_keys: set[str],
    ) -> sqlite3.Row | None:
        if candidate_cluster_keys:
            placeholders = ", ".join("?" for _ in candidate_cluster_keys)
            row = conn.execute(
                f"""
                SELECT cluster_key, canonical_url, canonical_website, source_rank,
                       company_signature, role_signature, location_signature,
                       salary_signature, work_mode, description_signature
                FROM job_clusters
                WHERE cluster_key IN ({placeholders})
                ORDER BY source_rank DESC, updated_at_utc DESC
                LIMIT 1
                """,
                tuple(candidate_cluster_keys),
            ).fetchone()
            if row is not None:
                return row

        if alias_keys:
            placeholders = ", ".join("?" for _ in alias_keys)
            row = conn.execute(
                f"""
                SELECT jc.cluster_key, jc.canonical_url, jc.canonical_website, jc.source_rank,
                       jc.company_signature, jc.role_signature, jc.location_signature,
                       jc.salary_signature, jc.work_mode, jc.description_signature
                FROM job_cluster_aliases AS jca
                JOIN job_clusters AS jc
                  ON jc.cluster_key = jca.cluster_key
                WHERE jca.alias_key IN ({placeholders})
                ORDER BY jc.source_rank DESC, jc.updated_at_utc DESC
                LIMIT 1
                """,
                tuple(alias_keys),
            ).fetchone()
            if row is not None:
                return row

        if not identity.company_signature or not identity.role_signature:
            return None

        candidates = conn.execute(
            """
            SELECT cluster_key, canonical_url, canonical_website, source_rank,
                   company_signature, role_signature, location_signature,
                   salary_signature, work_mode, description_signature
            FROM job_clusters
            WHERE company_signature = ?
            ORDER BY updated_at_utc DESC
            LIMIT 40
            """,
            (identity.company_signature,),
        ).fetchall()
        best_row: sqlite3.Row | None = None
        best_score = -1
        for row in candidates:
            row_role_tokens = {token for token in str(row["role_signature"] or "").split() if token}
            row_description_tokens = {token for token in str(row["description_signature"] or "").split() if token}
            role_overlap = len({token for token in identity.role_signature.split() if token} & row_role_tokens)
            description_overlap = len(
                {token for token in identity.description_signature.split() if token} & row_description_tokens
            )
            if role_overlap == 0 and description_overlap < 2:
                continue
            score = 0
            if role_overlap >= 2:
                score += 2
            elif role_overlap >= 1:
                score += 1
            if identity.location_signature and str(row["location_signature"] or "") == identity.location_signature:
                score += 3
            if identity.work_mode and str(row["work_mode"] or "") == identity.work_mode:
                score += 1
            if identity.salary_signature and str(row["salary_signature"] or "") == identity.salary_signature:
                score += 1
            if identity.description_signature and str(row["description_signature"] or "") == identity.description_signature:
                score += 2
            elif description_overlap >= 2:
                score += 2
            elif description_overlap >= 1:
                score += 1
            if (
                not identity.location_signature
                and identity.description_signature
                and str(row["description_signature"] or "") == identity.description_signature
            ):
                score += 1
            if score > best_score:
                best_score = score
                best_row = row
        return best_row if best_score >= 4 else None

    @staticmethod
    def _store_alias_keys(conn: sqlite3.Connection, cluster_key: str, alias_keys: set[str]) -> None:
        for alias_key in alias_keys:
            conn.execute(
                """
                INSERT OR IGNORE INTO job_cluster_aliases (alias_key, cluster_key)
                VALUES (?, ?)
                """,
                (alias_key, cluster_key),
            )

    @staticmethod
    def _merge_identity(existing: sqlite3.Row | None, identity: JobIdentity) -> JobIdentity:
        if existing is None:
            return identity
        return JobIdentity(
            company_signature=identity.company_signature or str(existing["company_signature"] or ""),
            role_signature=identity.role_signature or str(existing["role_signature"] or ""),
            location_signature=identity.location_signature or str(existing["location_signature"] or ""),
            salary_signature=identity.salary_signature or str(existing["salary_signature"] or ""),
            work_mode=identity.work_mode or str(existing["work_mode"] or ""),
            description_signature=identity.description_signature or str(existing["description_signature"] or ""),
        )

    @classmethod
    def _source_rank_for_card(cls, card: JobCard) -> int:
        domain = cls._extract_domain(card.url or card.website)
        company_tokens = cls._company_domain_tokens(getattr(card, "company", ""))
        if company_tokens and any(token in domain for token in company_tokens):
            return 400
        if domain not in PREFERRED_SOURCE_RANKS:
            return 300
        return PREFERRED_SOURCE_RANKS[domain]

    def register_job_cluster(self, card: JobCard) -> ClusterRegistration:
        legacy_cluster_key = self._cluster_key_for_card(card)
        identity = self._identity_for_card(card)
        cluster_key = self._canonical_cluster_key_for_identity(identity) or legacy_cluster_key
        if not cluster_key:
            return ClusterRegistration(
                cluster_key="",
                is_duplicate=False,
                canonical_url=card.url,
                canonical_website=card.website,
            )

        source_rank = self._source_rank_for_card(card)
        alias_keys = self._alias_keys_for_identity(identity, legacy_cluster_key=legacy_cluster_key)
        candidate_cluster_keys = [value for value in {cluster_key, legacy_cluster_key} if value]
        with self._lock:
            existing = self._find_existing_cluster(self._conn, identity, candidate_cluster_keys, alias_keys)
            resolved_cluster_key = str(existing["cluster_key"]) if existing is not None else cluster_key
            self._conn.execute(
                """
                INSERT OR IGNORE INTO job_cluster_sources (
                    cluster_key, website, url, title, company, location, source_rank
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_cluster_key,
                    card.website,
                    card.url,
                    card.title,
                    getattr(card, "company", ""),
                    card.location,
                    source_rank,
                ),
            )
            self._store_alias_keys(self._conn, resolved_cluster_key, alias_keys)
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO job_clusters (
                        cluster_key, canonical_url, canonical_website, title, company, location, source_rank,
                        company_signature, role_signature, location_signature, salary_signature, work_mode,
                        description_signature
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resolved_cluster_key,
                        card.url,
                        card.website,
                        card.title,
                        getattr(card, "company", ""),
                        card.location,
                        source_rank,
                        identity.company_signature,
                        identity.role_signature,
                        identity.location_signature,
                        identity.salary_signature,
                        identity.work_mode,
                        identity.description_signature,
                    ),
                )
                self._conn.commit()
                return ClusterRegistration(
                    cluster_key=resolved_cluster_key,
                    is_duplicate=False,
                    canonical_url=card.url,
                    canonical_website=card.website,
                )

            canonical_url = str(existing["canonical_url"])
            canonical_website = str(existing["canonical_website"])
            canonical_changed = False
            merged_identity = self._merge_identity(existing, identity)
            existing_rank = int(existing["source_rank"] or 0)
            if source_rank > existing_rank:
                self._conn.execute(
                    """
                    UPDATE job_clusters
                    SET canonical_url = ?,
                        canonical_website = ?,
                        title = ?,
                        company = ?,
                        location = ?,
                        source_rank = ?,
                        company_signature = ?,
                        role_signature = ?,
                        location_signature = ?,
                        salary_signature = ?,
                        work_mode = ?,
                        description_signature = ?,
                        updated_at_utc = (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                    WHERE cluster_key = ?
                    """,
                    (
                        card.url,
                        card.website,
                        card.title,
                        getattr(card, "company", ""),
                        card.location,
                        source_rank,
                        merged_identity.company_signature,
                        merged_identity.role_signature,
                        merged_identity.location_signature,
                        merged_identity.salary_signature,
                        merged_identity.work_mode,
                        merged_identity.description_signature,
                        resolved_cluster_key,
                    ),
                )
                canonical_url = card.url
                canonical_website = card.website
                canonical_changed = True
            else:
                self._conn.execute(
                    """
                    UPDATE job_clusters
                    SET company_signature = ?,
                        role_signature = ?,
                        location_signature = ?,
                        salary_signature = ?,
                        work_mode = ?,
                        description_signature = ?,
                        updated_at_utc = (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                    WHERE cluster_key = ?
                    """,
                    (
                        merged_identity.company_signature,
                        merged_identity.role_signature,
                        merged_identity.location_signature,
                        merged_identity.salary_signature,
                        merged_identity.work_mode,
                        merged_identity.description_signature,
                        resolved_cluster_key,
                    ),
                )
            self._conn.commit()
            return ClusterRegistration(
                cluster_key=resolved_cluster_key,
                is_duplicate=True,
                canonical_url=canonical_url,
                canonical_website=canonical_website,
                canonical_changed=canonical_changed,
            )

    def get_discovery_feedback(self, website: str) -> DiscoveryFeedback:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT url, status, not_job_flags
                FROM seen_links
                WHERE website = ?
                """,
                (website,),
            ).fetchall()
            discovery_rows = self._conn.execute(
                """
                SELECT url, status, hit_count
                FROM discovery_page_feedback
                WHERE website = ?
                """,
                (website,),
            ).fetchall()

        good_prefix_counts: Counter[str] = Counter()
        bad_prefix_counts: Counter[str] = Counter()
        blocked_urls: set[str] = set()
        blocked_source_urls: set[str] = set()

        for row in rows:
            url = str(row["url"])
            status = str(row["status"]).strip().lower()
            not_job_flags = int(row["not_job_flags"] or 0)
            prefixes = self._path_prefixes(url)
            if status == "job_saved":
                good_prefix_counts.update(prefixes)
                continue
            if status not in {
                "checked_not_job",
                "neglected_not_job",
                "neglected_old_post",
                "neglected_filter_mismatch",
                "fetch_failed",
                "neglected_duplicate_cluster",
            }:
                continue

            weight = max(1, not_job_flags)
            if status == "fetch_failed":
                weight = 2
            if status == "neglected_duplicate_cluster":
                weight = 2
            for prefix in prefixes:
                bad_prefix_counts[prefix] += weight
            if status in {"fetch_failed", "neglected_duplicate_cluster"} or status != "checked_not_job" or not_job_flags >= 2:
                blocked_urls.add(url)

        for row in discovery_rows:
            url = str(row["url"]).strip()
            status = str(row["status"]).strip().lower()
            hit_count = int(row["hit_count"] or 0)
            prefixes = self._path_prefixes(url)
            if status == "search_results_source":
                good_prefix_counts.update(prefixes)
                continue
            if status in {"job_post_source", "opportunity_detail_source"}:
                blocked_source_urls.add(url)
                continue
            if status == "challenge_blocked":
                blocked_source_urls.add(url)
                for prefix in prefixes:
                    bad_prefix_counts[prefix] += max(2, hit_count)
                continue
            if status not in {"fetch_failed"} and not (
                status.startswith("non_search_source") or status.startswith("non_feed_source")
            ):
                continue
            if hit_count >= 2:
                blocked_source_urls.add(url)
            weight = max(1, hit_count)
            for prefix in prefixes:
                bad_prefix_counts[prefix] += weight

        preferred_prefixes = tuple(
            prefix
            for prefix, count in sorted(good_prefix_counts.items(), key=lambda item: (-item[1], len(item[0]), item[0]))
            if count >= 1
        )
        blocked_prefixes = tuple(
            prefix
            for prefix, count in sorted(bad_prefix_counts.items(), key=lambda item: (-item[1], len(item[0]), item[0]))
            if count >= 2 and good_prefix_counts.get(prefix, 0) == 0
        )
        return DiscoveryFeedback(
            preferred_prefixes=preferred_prefixes,
            blocked_prefixes=blocked_prefixes,
            blocked_urls=tuple(sorted(blocked_urls)),
            blocked_source_urls=tuple(sorted(blocked_source_urls)),
        )

    def get_pending_telegram_links(self, website: str, limit: int = 10) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT url
                FROM seen_links
                WHERE website = ?
                  AND status = 'job_saved'
                  AND telegram_sent = 0
                  AND neglected = 0
                ORDER BY last_seen_utc DESC
                LIMIT ?
                """,
                (website, limit),
            ).fetchall()
            return [str(row["url"]) for row in rows]

    def save_cycle_stats(
        self,
        cycle_utc: str,
        website: str,
        fetched_pages: int,
        discovered_links: int,
        checked_links: int,
        seen_links_in_row: int,
        new_cards: int,
        errors: int,
        last_error: str | None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO cycle_stats (
                    cycle_utc, website, fetched_pages, discovered_links,
                    checked_links, seen_links_in_row, new_cards, errors, last_error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle_utc,
                    website,
                    fetched_pages,
                    discovered_links,
                    checked_links,
                    seen_links_in_row,
                    new_cards,
                    errors,
                    last_error,
                ),
            )
            self._conn.commit()
        self.increment_runtime_metric("cycles.total", 1.0)
        self.increment_runtime_metric("cycles.discovered_links", float(max(0, discovered_links)))
        self.increment_runtime_metric("cycles.new_cards", float(max(0, new_cards)))
        self.increment_runtime_metric("cycles.errors", float(max(0, errors)))

    def summarize_recent_cycle_window(self, *, since_utc: str) -> dict[str, float]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    COUNT(*) AS cycles,
                    COALESCE(SUM(discovered_links), 0) AS discovered_links,
                    COALESCE(SUM(new_cards), 0) AS new_cards,
                    COALESCE(SUM(errors), 0) AS errors
                FROM cycle_stats
                WHERE cycle_utc >= ?
                """,
                (since_utc,),
            ).fetchone()
        if row is None:
            return {"cycles": 0.0, "discovered_links": 0.0, "new_cards": 0.0, "errors": 0.0}
        return {
            "cycles": float(row["cycles"] or 0),
            "discovered_links": float(row["discovered_links"] or 0),
            "new_cards": float(row["new_cards"] or 0),
            "errors": float(row["errors"] or 0),
        }

    def summarize_alert_health(self, websites: list[str], *, since_utc: str) -> AlertHealthSnapshot:
        normalized_websites = [str(website or "").strip() for website in websites if str(website or "").strip()]
        if not normalized_websites:
            return AlertHealthSnapshot()

        placeholders = ", ".join("?" for _ in normalized_websites)
        cycle_params = [*normalized_websites]
        recent_cycle_params = [*normalized_websites, since_utc]
        recent_seen_params = [*normalized_websites, since_utc]

        with self._lock:
            last_scan_row = self._conn.execute(
                f"""
                SELECT MAX(cycle_utc) AS last_scan_utc
                FROM cycle_stats
                WHERE website IN ({placeholders})
                """,
                cycle_params,
            ).fetchone()
            discovered_row = self._conn.execute(
                f"""
                SELECT COALESCE(SUM(discovered_links), 0) AS discovered_posts
                FROM cycle_stats
                WHERE website IN ({placeholders})
                  AND cycle_utc >= ?
                """,
                recent_cycle_params,
            ).fetchone()
            blocked_rows = self._conn.execute(
                f"""
                SELECT DISTINCT website
                FROM (
                    SELECT website
                    FROM cycle_stats
                    WHERE website IN ({placeholders})
                      AND cycle_utc >= ?
                      AND (errors > 0 OR COALESCE(last_error, '') != '')
                    UNION
                    SELECT website
                    FROM discovery_page_feedback
                    WHERE website IN ({placeholders})
                      AND last_seen_utc >= ?
                      AND status = 'challenge_blocked'
                )
                ORDER BY website ASC
                """,
                [*normalized_websites, since_utc, *normalized_websites, since_utc],
            ).fetchall()
            seen_row = self._conn.execute(
                f"""
                SELECT
                    COALESCE(SUM(CASE WHEN status = 'neglected_old_post' THEN 1 ELSE 0 END), 0) AS freshness_rejects,
                    COALESCE(SUM(CASE WHEN status = 'neglected_duplicate_cluster' THEN 1 ELSE 0 END), 0) AS duplicate_prevented,
                    COALESCE(
                        SUM(
                            CASE
                                WHEN status IN (
                                    'neglected_old_post',
                                    'neglected_not_job',
                                    'neglected_duplicate_cluster',
                                    'neglected_filter_mismatch'
                                ) THEN 1
                                ELSE 0
                            END
                        ),
                        0
                    ) AS rejected_posts
                FROM seen_links
                WHERE website IN ({placeholders})
                  AND last_seen_utc >= ?
                """,
                recent_seen_params,
            ).fetchone()

        blocked_source_urls = tuple(str(row["website"]) for row in blocked_rows)
        return AlertHealthSnapshot(
            last_scan_utc=str(last_scan_row["last_scan_utc"]) if last_scan_row and last_scan_row["last_scan_utc"] else "",
            discovered_posts=int(discovered_row["discovered_posts"] or 0) if discovered_row else 0,
            blocked_sources=len(blocked_source_urls),
            blocked_source_urls=blocked_source_urls,
            freshness_rejects=int(seen_row["freshness_rejects"] or 0) if seen_row else 0,
            duplicate_prevented=int(seen_row["duplicate_prevented"] or 0) if seen_row else 0,
            rejected_posts=int(seen_row["rejected_posts"] or 0) if seen_row else 0,
        )

    def queue_human_review_item(
        self,
        card: JobCard,
        *,
        reason: str,
        source: str = "ai_low_confidence",
    ) -> int:
        stabilized_confidence = stabilize_job_confidence(
            card.confidence,
            is_job_post=bool(getattr(card, "is_relevant_opportunity", False) or card.is_job_post),
            title=card.title,
            description=card.description,
            location=card.location,
            salary=card.payment_terms or card.salary,
            company=card.counterparty or card.company,
            posted_at_utc=card.posted_at_utc,
            notes=card.notes,
        )
        card_json = json.dumps(
            {
                "website": card.website,
                "url": card.url,
                "title": card.title,
                "description": card.description,
                "location": card.location,
                "salary": card.payment_terms or card.salary,
                "is_job_post": card.is_job_post,
                "is_relevant_opportunity": getattr(card, "is_relevant_opportunity", card.is_job_post),
                "confidence": stabilized_confidence,
                "extraction_method": card.extraction_method,
                "company": card.company,
                "client": getattr(card, "client", ""),
                "requester": getattr(card, "requester", ""),
                "language": card.language,
                "notes": card.notes,
                "posted_at_utc": card.posted_at_utc,
                "extracted_at_utc": card.extracted_at_utc,
                "opportunity_kind": getattr(card, "opportunity_kind", ""),
                "scope_summary": getattr(card, "scope_summary", ""),
                "budget": getattr(card, "budget", card.payment_terms or card.salary),
                "duration": getattr(card, "duration", ""),
                "commitment_level": getattr(card, "commitment_level", ""),
                "skills_required": getattr(card, "skills_required", ""),
                "proposal_deadline": getattr(card, "proposal_deadline", ""),
                "start_timeline": getattr(card, "start_timeline", ""),
                "industry": getattr(card, "industry", ""),
                "engagement_type": getattr(card, "engagement_type", ""),
                "remote_location_constraint": getattr(card, "remote_location_constraint", ""),
                "contact_url": getattr(card, "contact_url", ""),
            },
            ensure_ascii=False,
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO human_review_queue (
                    website, url, title, description, location, salary, confidence,
                    reason, source, status, reviewer, review_notes, sent_count,
                    admin_chat_id, admin_message_id, dispatched_at_utc, last_dispatch_error,
                    card_json, created_at_utc, updated_at_utc, reviewed_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', '', 0, NULL, NULL, '', '', ?, 
                        (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                        (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                        NULL)
                ON CONFLICT(website, url) DO UPDATE SET
                    title = excluded.title,
                    description = excluded.description,
                    location = excluded.location,
                    salary = excluded.salary,
                    confidence = excluded.confidence,
                    reason = excluded.reason,
                    source = excluded.source,
                    status = 'pending',
                    reviewer = '',
                    review_notes = '',
                    sent_count = 0,
                    admin_chat_id = NULL,
                    admin_message_id = NULL,
                    dispatched_at_utc = '',
                    last_dispatch_error = '',
                    card_json = excluded.card_json,
                    updated_at_utc = (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    reviewed_at_utc = NULL
                """,
                (
                    card.website,
                    card.url,
                    card.title,
                    card.description,
                    card.location,
                    card.payment_terms or card.salary,
                    stabilized_confidence,
                    reason.strip(),
                    source.strip() or "ai_low_confidence",
                    card_json,
                ),
            )
            row = self._conn.execute(
                """
                SELECT id
                FROM human_review_queue
                WHERE website = ? AND url = ?
                LIMIT 1
                """,
                (card.website, card.url),
            ).fetchone()
            self._conn.commit()
            if row is None:
                raise RuntimeError("Could not enqueue human review item")
            return int(row["id"])

    def list_human_review_items(
        self,
        status: str = "pending",
        limit: int = 100,
        *,
        only_undispatched: bool = False,
    ) -> list[HumanReviewItem]:
        with self._lock:
            undispatched_clause = (
                "AND COALESCE(admin_message_id, 0) = 0 "
                "AND COALESCE(last_dispatch_error, '') NOT LIKE 'suppressed:%'"
                if only_undispatched
                else ""
            )
            rows = self._conn.execute(
                f"""
                SELECT id, website, url, title, description, location, salary, confidence,
                       reason, source, status, reviewer, review_notes, sent_count,
                       admin_chat_id, admin_message_id, dispatched_at_utc, last_dispatch_error,
                       created_at_utc, updated_at_utc, reviewed_at_utc, card_json
                FROM human_review_queue
                WHERE status = ?
                  {undispatched_clause}
                ORDER BY created_at_utc ASC
                LIMIT ?
                """,
                (status.strip().lower(), limit),
            ).fetchall()
            return [self._row_to_human_review_item(row) for row in rows]

    def get_human_review_item(self, item_id: int) -> HumanReviewItem | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, website, url, title, description, location, salary, confidence,
                       reason, source, status, reviewer, review_notes, sent_count,
                       admin_chat_id, admin_message_id, dispatched_at_utc, last_dispatch_error,
                       created_at_utc, updated_at_utc, reviewed_at_utc, card_json
                FROM human_review_queue
                WHERE id = ?
                LIMIT 1
                """,
                (item_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_human_review_item(row)

    def update_human_review_confidence(self, item_id: int, confidence: float) -> None:
        normalized = max(0.0, min(float(confidence), 1.0))
        with self._lock:
            row = self._conn.execute(
                """
                SELECT card_json
                FROM human_review_queue
                WHERE id = ?
                LIMIT 1
                """,
                (item_id,),
            ).fetchone()
            if row is None:
                return
            try:
                payload = json.loads(str(row["card_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            payload["confidence"] = normalized
            self._conn.execute(
                """
                UPDATE human_review_queue
                SET confidence = ?,
                    card_json = ?,
                    updated_at_utc = (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                WHERE id = ?
                """,
                (normalized, json.dumps(payload, ensure_ascii=False), item_id),
            )
            self._conn.commit()

    def mark_human_review_dispatched(self, item_id: int, *, admin_chat_id: int, admin_message_id: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE human_review_queue
                SET admin_chat_id = ?,
                    admin_message_id = ?,
                    dispatched_at_utc = (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    last_dispatch_error = '',
                    updated_at_utc = (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                WHERE id = ?
                """,
                (int(admin_chat_id), int(admin_message_id), item_id),
            )
            self._conn.commit()

    def mark_human_review_dispatch_error(self, item_id: int, *, error: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE human_review_queue
                SET last_dispatch_error = ?,
                    updated_at_utc = (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                WHERE id = ?
                """,
                (str(error or "").strip(), item_id),
            )
            self._conn.commit()

    def set_human_review_decision(
        self,
        item_id: int,
        *,
        decision: str,
        reviewer: str = "",
        notes: str = "",
        sent_count: int = 0,
        last_dispatch_error: str = "",
    ) -> None:
        normalized = decision.strip().lower()
        if normalized not in {"approved_sent", "approved_no_send", "neglected", "dismissed"}:
            normalized = "dismissed"
        with self._lock:
            row = self._conn.execute(
                """
                SELECT website, url, reason, title, description, location, salary, confidence
                FROM human_review_queue
                WHERE id = ?
                LIMIT 1
                """,
                (item_id,),
            ).fetchone()
            if row is None:
                return

            self._conn.execute(
                """
                UPDATE human_review_queue
                SET status = ?,
                    reviewer = ?,
                    review_notes = ?,
                    sent_count = ?,
                    last_dispatch_error = ?,
                    reviewed_at_utc = (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at_utc = (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                WHERE id = ?
                """,
                (
                    normalized,
                    reviewer.strip(),
                    notes.strip(),
                    max(0, int(sent_count)),
                    last_dispatch_error.strip(),
                    item_id,
                ),
            )

            if normalized in {"approved_sent", "approved_no_send", "neglected"}:
                self._conn.execute(
                    """
                    INSERT INTO human_review_feedback (
                        website, url, decision, reason, title, description, location, salary, confidence
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(row["website"]),
                        str(row["url"]),
                        normalized,
                        str(row["reason"]),
                        str(row["title"]),
                        str(row["description"]),
                        str(row["location"]),
                        str(row["salary"]),
                        float(row["confidence"] or 0.0),
                    ),
                )
            self._conn.commit()

    def build_review_guidance(self, limit: int = 12) -> str:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT decision, reason, title, description, location, salary
                FROM human_review_feedback
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            if not rows:
                return ""

            lines: list[str] = []
            for row in rows:
                decision = str(row["decision"]).strip().upper()
                reason = str(row["reason"]).strip()
                title = str(row["title"]).strip()
                location = str(row["location"]).strip()
                payment_terms = str(row["salary"]).strip()
                description = str(row["description"]).strip()
                lines.append(
                    f"{decision} | title={title} | location={location} | payment={payment_terms} | "
                    f"reason={reason} | desc={description}"
                )
            return "\n".join(lines[:limit])

    @staticmethod
    def _row_to_human_review_item(row: sqlite3.Row) -> HumanReviewItem:
        return HumanReviewItem(
            id=int(row["id"]),
            website=str(row["website"]),
            url=str(row["url"]),
            title=str(row["title"]),
            description=str(row["description"]),
            location=str(row["location"]),
            salary=str(row["salary"]),
            confidence=float(row["confidence"] or 0.0),
            reason=str(row["reason"]),
            source=str(row["source"]),
            status=str(row["status"]),
            reviewer=str(row["reviewer"]),
            review_notes=str(row["review_notes"]),
            sent_count=int(row["sent_count"] or 0),
            admin_chat_id=int(row["admin_chat_id"]) if row["admin_chat_id"] is not None else None,
            admin_message_id=int(row["admin_message_id"]) if row["admin_message_id"] is not None else None,
            dispatched_at_utc=str(row["dispatched_at_utc"] or ""),
            last_dispatch_error=str(row["last_dispatch_error"]),
            created_at_utc=str(row["created_at_utc"]),
            updated_at_utc=str(row["updated_at_utc"]),
            reviewed_at_utc=str(row["reviewed_at_utc"]) if row["reviewed_at_utc"] else None,
            card_json=str(row["card_json"]),
        )

    def reset_dashboard_stats(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                DELETE FROM cycle_stats;
                DELETE FROM seen_links;
                DELETE FROM human_review_queue;
                DELETE FROM human_review_feedback;
                DELETE FROM job_clusters;
                DELETE FROM job_cluster_sources;
                DELETE FROM job_cluster_aliases;
                DELETE FROM runtime_metrics;
                DELETE FROM fetch_strategy_stats;
                DELETE FROM crawler_health_state;
                DELETE FROM admin_alert_log;
                DELETE FROM sqlite_sequence
                WHERE name IN ('human_review_queue', 'human_review_feedback');
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
