from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


SHEET_TABS = {
    "approved": "Approved CRM",
    "rejected": "Rejected",
    "review": "Further Review",
    "logs": "Analysis Logs",
    "errors": "Errors",
    "metrics": "Dashboard Metrics",
}

REQUIRED_ENV_NAMES = (
    "PROSPECT_DEMO_WEBSITE_URL",
    "PROSPECT_DEFAULT_TARGET_CRITERIA",
    "PROSPECT_DEFAULT_OUTREACH_GOAL",
    "PROSPECT_MAX_PAGES_TO_SCRAPE",
    "GOOGLE_SHEET_ID",
    "GMAIL_SENDER_EMAIL",
    "AI_MODEL",
    "SCRAPING_TIMEOUT_SECONDS",
    "CAPTCHA_END_MESSAGE",
    "PROSPECT_MANUAL_REVIEW_MINUTES",
)

GOOGLE_CREDENTIAL_ENV_GROUP = ("GOOGLE_SERVICE_ACCOUNT_JSON_PATH", "GOOGLE_SERVICE_ACCOUNT_JSON")
AI_CREDENTIAL_ENV_GROUP = ("OPENAI_API_KEY",)
SMTP_CREDENTIAL_ENV_GROUP = ("SMTP_PASSWORD",)
SUPPORTED_EMAIL_SEND_METHODS = {"smtp", "gmail_api"}


@dataclass(slots=True)
class ProspectSettings:
    base_dir: Path
    demo_website_url: str
    default_target_criteria: str
    default_outreach_goal: str
    max_pages_to_scrape: int
    google_sheet_id: str
    google_service_account_json_path: str
    google_service_account_json: str
    google_service_account_email: str
    gmail_sender_email: str
    telegram_bot_token: str
    prospect_usage_notify_chat_id: str
    email_send_method: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_sender_email: str
    smtp_use_tls: bool
    smtp_use_ssl: bool
    smtp_timeout_seconds: int
    openai_api_key: str
    ai_model: str
    scraping_timeout_seconds: int
    captcha_end_message: str
    manual_review_minutes: float
    log_refresh_seconds: float
    fit_status_rules: list[dict[str, Any]]
    verify_ssl: bool = True
    headless_browser: bool = True
    use_scrapling_cloudflare_solver: bool = False
    enable_browser_tabs: bool = True
    prospect_enable_browser_fetch: bool = False
    enable_site_profiles: bool = True
    openai_request_timeout_seconds: float = 45.0
    ai_retry_budget: int = 2
    ai_backoff_base_seconds: float = 1.5
    network_retry_budget: int = 1
    network_backoff_base_seconds: float = 1.5
    browser_reprobe_cooldown_seconds: int = 600
    max_fetch_strategies_per_url: int = 4
    prospect_log_dir: Path = field(default_factory=lambda: Path("state") / "prospect_logs")
    missing_env_values: list[str] = field(default_factory=list)

    @property
    def is_google_configured(self) -> bool:
        has_env_json = _has_valid_json(self.google_service_account_json)
        has_json_file = False
        if not _is_placeholder(self.google_service_account_json_path):
            path = Path(self.google_service_account_json_path)
            if not path.is_absolute():
                path = self.base_dir / path
            has_json_file = path.exists()
        return bool(
            self.google_sheet_id
            and not _is_placeholder(self.google_sheet_id)
            and (has_env_json or has_json_file)
        )

    @property
    def is_ai_configured(self) -> bool:
        return bool(self.openai_api_key and self.ai_model)

    @property
    def email_sender_email(self) -> str:
        if self.email_send_method == "smtp":
            return self.smtp_sender_email or self.gmail_sender_email or self.smtp_username
        return self.gmail_sender_email

    @property
    def is_smtp_configured(self) -> bool:
        return bool(
            self.smtp_host
            and self.smtp_port > 0
            and self.smtp_username
            and self.smtp_password
            and not _is_placeholder(self.smtp_password)
            and self.email_sender_email
        )

    def fit_status_for_score(self, score: int) -> str:
        score = max(0, min(int(score), 100))
        for rule in self.fit_status_rules:
            try:
                minimum = int(rule.get("min", 0))
                maximum = int(rule.get("max", 100))
            except (TypeError, ValueError):
                continue
            if minimum <= score <= maximum:
                return str(rule.get("label") or "").strip() or "Unknown"
        return "Unknown"


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _fit_status_rules_from_env() -> list[dict[str, Any]]:
    raw = os.getenv("PROSPECT_FIT_STATUS_RULES_JSON", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [item for item in parsed if isinstance(item, dict)]
        except json.JSONDecodeError:
            return []
    return []


def _is_placeholder(value: str) -> bool:
    normalized = str(value or "").strip().upper()
    return not normalized or normalized.startswith("YOUR_") or normalized.endswith("_HERE")


def _has_valid_json(value: str) -> bool:
    if _is_placeholder(value):
        return False
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict)


def _missing_env_values(base_dir: Path | None = None) -> list[str]:
    missing = [name for name in REQUIRED_ENV_NAMES if _is_placeholder(os.getenv(name, ""))]
    method = os.getenv("PROSPECT_EMAIL_SEND_METHOD", "gmail_api").strip().lower() or "gmail_api"
    if method not in SUPPORTED_EMAIL_SEND_METHODS:
        missing.append("PROSPECT_EMAIL_SEND_METHOD must be smtp or gmail_api")
    if method == "smtp":
        for name in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME"):
            if not os.getenv(name, "").strip():
                missing.append(name)
        if _is_placeholder(os.getenv("SMTP_PASSWORD", "")):
            missing.append("SMTP_PASSWORD")
        if not (os.getenv("SMTP_SENDER_EMAIL", "").strip() or os.getenv("GMAIL_SENDER_EMAIL", "").strip()):
            missing.append("SMTP_SENDER_EMAIL or GMAIL_SENDER_EMAIL")
    service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    service_account_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH", "").strip()
    has_env_json = _has_valid_json(service_account_json)
    if service_account_json and not has_env_json:
        missing.append("GOOGLE_SERVICE_ACCOUNT_JSON is invalid JSON")
    has_json_path = not _is_placeholder(service_account_path)
    if has_json_path and base_dir is not None:
        path = Path(service_account_path)
        if not path.is_absolute():
            path = base_dir / path
        has_json_path = path.exists()
        if not has_env_json and not has_json_path:
            missing.append(f"GOOGLE_SERVICE_ACCOUNT_JSON_PATH file not found: {service_account_path}")
    if not has_env_json and not has_json_path:
        missing.append("GOOGLE_SERVICE_ACCOUNT_JSON_PATH or GOOGLE_SERVICE_ACCOUNT_JSON")
    if not any(os.getenv(name, "").strip() for name in AI_CREDENTIAL_ENV_GROUP):
        missing.append("OPENAI_API_KEY")
    if not _fit_status_rules_from_env():
        missing.append("PROSPECT_FIT_STATUS_RULES_JSON")
    return missing


def load_prospect_settings(cwd: Path | None = None) -> ProspectSettings:
    base_dir = (cwd or Path.cwd()).resolve()
    load_dotenv(base_dir / ".env")
    max_pages_to_scrape = max(1, _parse_int("PROSPECT_MAX_PAGES_TO_SCRAPE", 1))
    timeout_seconds = max(1, _parse_int("SCRAPING_TIMEOUT_SECONDS", 30))
    prospect_log_dir = Path(os.getenv("PROSPECT_LOG_DIR", str(base_dir / "state" / "prospect_logs")))
    email_send_method = os.getenv("PROSPECT_EMAIL_SEND_METHOD", "gmail_api").strip().lower() or "gmail_api"
    if email_send_method not in SUPPORTED_EMAIL_SEND_METHODS:
        email_send_method = "gmail_api"
    return ProspectSettings(
        base_dir=base_dir,
        demo_website_url=os.getenv("PROSPECT_DEMO_WEBSITE_URL", "").strip(),
        default_target_criteria=os.getenv("PROSPECT_DEFAULT_TARGET_CRITERIA", "").strip(),
        default_outreach_goal=os.getenv("PROSPECT_DEFAULT_OUTREACH_GOAL", "").strip(),
        max_pages_to_scrape=max_pages_to_scrape,
        google_sheet_id=os.getenv("GOOGLE_SHEET_ID", "").strip(),
        google_service_account_json_path=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH", "").strip(),
        google_service_account_json=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip(),
        google_service_account_email=os.getenv("GOOGLE_SERVICE_ACCOUNT_EMAIL", "").strip(),
        gmail_sender_email=os.getenv("GMAIL_SENDER_EMAIL", "").strip(),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        prospect_usage_notify_chat_id=os.getenv("PROSPECT_USAGE_NOTIFY_CHAT_ID", "100000001").strip(),
        email_send_method=email_send_method,
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=max(0, _parse_int("SMTP_PORT", 587)),
        smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", "").strip(),
        smtp_sender_email=os.getenv("SMTP_SENDER_EMAIL", "").strip(),
        smtp_use_tls=_parse_bool(os.getenv("SMTP_USE_TLS"), default=True),
        smtp_use_ssl=_parse_bool(os.getenv("SMTP_USE_SSL"), default=False),
        smtp_timeout_seconds=max(1, _parse_int("SMTP_TIMEOUT_SECONDS", 30)),
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        ai_model=os.getenv("AI_MODEL", os.getenv("OPENAI_MODEL", "")).strip(),
        scraping_timeout_seconds=timeout_seconds,
        captcha_end_message=os.getenv("CAPTCHA_END_MESSAGE", "").strip(),
        manual_review_minutes=max(0.1, _parse_float("PROSPECT_MANUAL_REVIEW_MINUTES", 10.0)),
        log_refresh_seconds=max(0.5, _parse_float("PROSPECT_LOG_REFRESH_SECONDS", 3.0)),
        fit_status_rules=_fit_status_rules_from_env(),
        verify_ssl=_parse_bool(os.getenv("VERIFY_SSL"), default=True),
        headless_browser=_parse_bool(os.getenv("HEADLESS_BROWSER"), default=True),
        use_scrapling_cloudflare_solver=_parse_bool(os.getenv("USE_SCRAPLING_CLOUDFLARE_SOLVER"), default=False),
        enable_browser_tabs=_parse_bool(os.getenv("ENABLE_BROWSER_TABS"), default=True),
        prospect_enable_browser_fetch=_parse_bool(os.getenv("PROSPECT_ENABLE_BROWSER_FETCH"), default=False),
        enable_site_profiles=_parse_bool(os.getenv("ENABLE_SITE_PROFILES"), default=True),
        openai_request_timeout_seconds=max(5.0, _parse_float("OPENAI_REQUEST_TIMEOUT_SECONDS", 45.0)),
        ai_retry_budget=max(0, _parse_int("AI_RETRY_BUDGET", 2)),
        ai_backoff_base_seconds=max(0.1, _parse_float("AI_BACKOFF_BASE_SECONDS", 1.5)),
        network_retry_budget=max(0, _parse_int("NETWORK_RETRY_BUDGET", 1)),
        network_backoff_base_seconds=max(0.1, _parse_float("NETWORK_BACKOFF_BASE_SECONDS", 1.5)),
        browser_reprobe_cooldown_seconds=max(30, _parse_int("BROWSER_REPROBE_COOLDOWN_SECONDS", 600)),
        max_fetch_strategies_per_url=max(1, _parse_int("MAX_FETCH_STRATEGIES_PER_URL", 4)),
        prospect_log_dir=prospect_log_dir,
        missing_env_values=_missing_env_values(base_dir),
    )
