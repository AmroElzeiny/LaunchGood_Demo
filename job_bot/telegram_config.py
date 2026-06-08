from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from job_bot.subscription_admin_notifier import DEFAULT_SUBSCRIPTION_ADMIN_CHAT_ID


TOKEN_PLACEHOLDERS = {
    "",
    "YOUR_TELEGRAM_BOT_TOKEN_HERE",
    "YOUR_TELEGRAM_BOT_API_TOKEN_HERE",
}
NOWPAYMENTS_PLACEHOLDERS = {
    "",
    "YOUR_NOWPAYMENTS_API_KEY_HERE",
}


@dataclass(slots=True)
class TelegramBotSettings:
    telegram_bot_token: str
    telegram_subs_db_path: Path
    telegram_user_log_dir: Path
    state_db_path: Path
    subscription_admin_chat_ids: tuple[int, ...]
    nowpayments_api_key: str
    nowpayments_base_url: str
    nowpayments_email: str
    nowpayments_password: str
    scrape_sites_file_path: Path
    spheres_file_path: Path
    sphere_websites_file_path: Path
    openai_api_key: str
    openai_model: str
    log_level: str
    no_posts_report_minutes: int = 0
    openai_model_website_guard: str = ""
    openai_model_keyword_expansion: str = ""
    openai_model_filter_match: str = ""


def _model_env(name: str, fallback: str) -> str:
    value = os.getenv(name, "").strip()
    return value or fallback


def _subscription_admin_chat_ids_from_env() -> tuple[int, ...]:
    raw_multi = os.getenv("SUBSCRIPTION_ADMIN_CHAT_IDS", "").strip()
    raw_single = os.getenv("SUBSCRIPTION_ADMIN_CHAT_ID", "").strip()
    source = raw_multi or raw_single
    if not source:
        return (DEFAULT_SUBSCRIPTION_ADMIN_CHAT_ID,)
    ordered: list[int] = []
    seen: set[int] = set()
    for token in re.split(r"[\s,;|]+", source):
        cleaned = token.strip()
        if not cleaned:
            continue
        try:
            chat_id = int(cleaned)
        except ValueError as exc:
            raise ValueError("SUBSCRIPTION_ADMIN_CHAT_IDS must contain only integer Telegram chat IDs.") from exc
        if chat_id in seen:
            continue
        seen.add(chat_id)
        ordered.append(chat_id)
    return tuple(ordered) if ordered else (DEFAULT_SUBSCRIPTION_ADMIN_CHAT_ID,)


def load_telegram_settings(cwd: Path) -> TelegramBotSettings:
    load_dotenv(cwd / ".env")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if token in TOKEN_PLACEHOLDERS:
        raise ValueError("TELEGRAM_BOT_TOKEN is missing. Put your real token in .env.")

    subs_db_path = Path(os.getenv("TELEGRAM_SUBS_DB_PATH", str(cwd / "state" / "telegram_subscriptions.db")))
    user_log_dir = Path(os.getenv("TELEGRAM_USER_LOG_DIR", str(cwd / "state" / "user_logs")))
    state_db_path = Path(os.getenv("STATE_DB_PATH", str(cwd / "state" / "job_bot_state.db")))
    scrape_sites_file_path = Path(os.getenv("SCRAPE_SITES_FILE", str(cwd / "links for UX UI.txt")))
    spheres_file_path = Path(os.getenv("SPHERES_FILE_PATH", str(cwd / "spheres.txt")))
    sphere_websites_file_path = Path(
        os.getenv("SPHERE_WEBSITES_FILE_PATH", str(cwd / "sphere_websites.txt"))
    )

    nowpayments_api_key = os.getenv("NOWPAYMENTS_API_KEY", "").strip()
    if nowpayments_api_key in NOWPAYMENTS_PLACEHOLDERS:
        nowpayments_api_key = ""

    openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"

    return TelegramBotSettings(
        telegram_bot_token=token,
        telegram_subs_db_path=subs_db_path,
        telegram_user_log_dir=user_log_dir,
        state_db_path=state_db_path,
        subscription_admin_chat_ids=_subscription_admin_chat_ids_from_env(),
        nowpayments_api_key=nowpayments_api_key,
        nowpayments_base_url=os.getenv("NOWPAYMENTS_BASE_URL", "https://api.nowpayments.io").strip(),
        nowpayments_email=os.getenv("NOWPAY_EMAIL", "").strip(),
        nowpayments_password=os.getenv("NOWPAY_PASSWORD", "").strip(),
        scrape_sites_file_path=scrape_sites_file_path,
        spheres_file_path=spheres_file_path,
        sphere_websites_file_path=sphere_websites_file_path,
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_model=openai_model,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        no_posts_report_minutes=max(0, int(os.getenv("NO_POSTS_REPORT_MINUTES", "0"))),
        openai_model_website_guard=_model_env("OPENAI_MODEL_WEBSITE_GUARD", openai_model),
        openai_model_keyword_expansion=_model_env("OPENAI_MODEL_KEYWORD_EXPANSION", openai_model),
        openai_model_filter_match=_model_env("OPENAI_MODEL_FILTER_MATCH", openai_model),
    )
