from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import re

from dotenv import load_dotenv


@dataclass(slots=True)
class Settings:
    openai_api_key: str
    openai_model: str
    websites: list[str]
    agent_count: int
    cycle_seconds: int
    max_cards_per_cycle: int
    seen_streak_stop: int
    max_candidates_per_site: int
    max_seed_pages_per_site: int
    state_db_path: Path
    output_file_path: Path
    headless_browser: bool
    log_level: str
    request_timeout_ms: int
    verify_ssl: bool
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_subs_db_path: Path
    telegram_user_log_dir: Path
    websites_file_path: Path
    neglect_post_if_filters_miss: bool
    enable_human_review_queue: bool
    human_review_confidence_threshold: float
    human_review_min_confidence_threshold: float = 0.30
    max_job_post_age_days: int = 7
    openai_model_website_guard: str = ""
    openai_model_link_ranking: str = ""
    openai_model_keyword_expansion: str = ""
    openai_model_extraction: str = ""
    openai_model_post_age: str = ""
    openai_model_filter_match: str = ""
    use_scrapling_cloudflare_solver: bool = False
    human_review_chat_id: str = ""
    site_cycle_timeout_seconds: int = 900
    site_cycle_idle_timeout_seconds: int = 900
    site_cycle_hard_timeout_seconds: int = 14400
    browser_reprobe_cooldown_seconds: int = 600
    extraction_low_confidence_threshold: float = 0.72
    openai_request_timeout_seconds: float = 45.0
    ai_retry_budget: int = 2
    ai_backoff_base_seconds: float = 1.5
    fetch_strategy_timeout_ms: int = 20000
    network_retry_budget: int = 2
    network_backoff_base_seconds: float = 1.5
    link_ranking_timeout_seconds: int = 35
    extraction_timeout_seconds: int = 120
    age_check_timeout_seconds: int = 45
    max_fetch_strategies_per_url: int = 4
    enable_baseline_website_fallback: bool = False
    scraper_paused: bool = False
    subscription_admin_chat_ids: tuple[int, ...] = ()
    no_candidates_alert_window_minutes: int = 120
    no_delivery_alert_window_minutes: int = 180
    enable_ai_extraction: bool = True
    enable_ai_age_check: bool = True
    enable_browser_tabs: bool = True
    enable_site_profiles: bool = True
    reduced_quality_fallback_mode: bool = True
    delivery_worker_count: int = 2
    delivery_worker_poll_seconds: float = 5.0
    delivery_job_retry_budget: int = 3
    delivery_job_backoff_seconds: float = 20.0
    enable_manual_challenge_flow: bool = False
    manual_challenge_timeout_seconds: int = 900
    manual_challenge_poll_seconds: float = 2.0


def _parse_bool(value: str, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def load_websites_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


def _load_websites(cwd: Path) -> list[str]:
    websites_file_path = Path(os.getenv("SCRAPE_SITES_FILE", str(cwd / "links for UX UI.txt")))
    websites = load_websites_file(websites_file_path)
    if len(websites) < 1:
        raise ValueError("You must provide at least 1 website in SCRAPE_SITES_FILE.")
    return websites


def _model_env(name: str, fallback: str) -> str:
    value = os.getenv(name, "").strip()
    return value or fallback


def _human_review_chat_id_from_env() -> str:
    direct = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if direct and direct != "YOUR_TELEGRAM_CHAT_ID_HERE":
        return direct

    fallback_source = os.getenv("SUBSCRIPTION_ADMIN_CHAT_IDS", "").strip() or os.getenv(
        "SUBSCRIPTION_ADMIN_CHAT_ID",
        "",
    ).strip()
    for token in re.split(r"[\s,;|]+", fallback_source):
        cleaned = token.strip()
        if not cleaned:
            continue
        try:
            int(cleaned)
        except ValueError:
            continue
        return cleaned
    return ""


def _parse_int_tuple_from_env(*env_names: str) -> tuple[int, ...]:
    ordered: list[int] = []
    seen: set[int] = set()
    for env_name in env_names:
        raw = os.getenv(env_name, "").strip()
        if not raw:
            continue
        for token in re.split(r"[\s,;|]+", raw):
            cleaned = token.strip()
            if not cleaned:
                continue
            try:
                value = int(cleaned)
            except ValueError:
                continue
            if value in seen:
                continue
            seen.add(value)
            ordered.append(value)
    return tuple(ordered)


def load_settings(cwd: Path) -> Settings:
    load_dotenv(cwd / ".env")

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_MODEL", "YOUR_MODEL_NAME_HERE").strip()
    if not api_key or api_key == "YOUR_OPENAI_API_KEY_HERE":
        raise ValueError("OPENAI_API_KEY is missing. Put your real key in .env.")
    if not model or model == "YOUR_MODEL_NAME_HERE":
        raise ValueError("OPENAI_MODEL is missing. Put your target model name in .env.")
    website_guard_model = _model_env("OPENAI_MODEL_WEBSITE_GUARD", model)
    link_ranking_model = _model_env("OPENAI_MODEL_LINK_RANKING", model)
    keyword_expansion_model = _model_env("OPENAI_MODEL_KEYWORD_EXPANSION", model)
    extraction_model = _model_env("OPENAI_MODEL_EXTRACTION", model)
    post_age_model = _model_env("OPENAI_MODEL_POST_AGE", model)
    filter_match_model = _model_env("OPENAI_MODEL_FILTER_MATCH", model)

    websites_file_path = Path(os.getenv("SCRAPE_SITES_FILE", str(cwd / "links for UX UI.txt")))
    websites = load_websites_file(websites_file_path)
    if len(websites) < 1:
        raise ValueError("You must provide at least 1 website in SCRAPE_SITES_FILE.")
    agent_count = int(os.getenv("AGENT_COUNT", "1"))
    if agent_count < 1:
        raise ValueError("AGENT_COUNT must be >= 1.")

    cycle_seconds = int(os.getenv("CYCLE_SECONDS", "60"))
    max_cards = int(os.getenv("MAX_CARDS_PER_CYCLE", "3"))
    seen_streak_stop = int(os.getenv("SEEN_STREAK_STOP", "3"))
    max_candidates = int(os.getenv("MAX_CANDIDATES_PER_SITE", "150"))
    max_seed_pages = int(os.getenv("MAX_SEED_PAGES_PER_SITE", "4"))
    request_timeout_ms = int(os.getenv("REQUEST_TIMEOUT_MS", "45000"))
    legacy_site_cycle_timeout_seconds = int(os.getenv("SITE_CYCLE_TIMEOUT_SECONDS", "900"))
    site_cycle_idle_timeout_seconds = int(
        os.getenv("SITE_CYCLE_IDLE_TIMEOUT_SECONDS", str(legacy_site_cycle_timeout_seconds))
    )
    site_cycle_hard_timeout_seconds = int(
        os.getenv(
            "SITE_CYCLE_HARD_TIMEOUT_SECONDS",
            str(max(site_cycle_idle_timeout_seconds * 6, 14400)),
        )
    )
    human_review_threshold = float(os.getenv("HUMAN_REVIEW_CONFIDENCE_THRESHOLD", "0.62"))
    human_review_min_threshold = float(os.getenv("HUMAN_REVIEW_MIN_CONFIDENCE_THRESHOLD", "0.30"))
    max_job_post_age_days = int(os.getenv("MAX_JOB_POST_AGE_DAYS", "7"))
    browser_reprobe_cooldown_seconds = int(os.getenv("BROWSER_REPROBE_COOLDOWN_SECONDS", "600"))
    extraction_low_confidence_threshold = float(
        os.getenv(
            "EXTRACTION_LOW_CONFIDENCE_THRESHOLD",
            str(max(human_review_threshold + 0.08, 0.72)),
        )
    )
    openai_request_timeout_seconds = float(os.getenv("OPENAI_REQUEST_TIMEOUT_SECONDS", "45"))
    ai_retry_budget = int(os.getenv("AI_RETRY_BUDGET", "2"))
    ai_backoff_base_seconds = float(os.getenv("AI_BACKOFF_BASE_SECONDS", "1.5"))
    fetch_strategy_timeout_ms = int(os.getenv("FETCH_STRATEGY_TIMEOUT_MS", "20000"))
    network_retry_budget = int(os.getenv("NETWORK_RETRY_BUDGET", "2"))
    network_backoff_base_seconds = float(os.getenv("NETWORK_BACKOFF_BASE_SECONDS", "1.5"))
    link_ranking_timeout_seconds = int(os.getenv("LINK_RANKING_TIMEOUT_SECONDS", "35"))
    extraction_timeout_seconds = int(os.getenv("EXTRACTION_TIMEOUT_SECONDS", "120"))
    age_check_timeout_seconds = int(os.getenv("AGE_CHECK_TIMEOUT_SECONDS", "45"))
    max_fetch_strategies_per_url = int(os.getenv("MAX_FETCH_STRATEGIES_PER_URL", "4"))
    enable_baseline_website_fallback = _parse_bool(
        os.getenv("ENABLE_BASELINE_WEBSITE_FALLBACK"),
        default=False,
    )
    scraper_paused = _parse_bool(os.getenv("SCRAPER_PAUSED"), default=False)
    subscription_admin_chat_ids = _parse_int_tuple_from_env(
        "SUBSCRIPTION_ADMIN_CHAT_IDS",
        "SUBSCRIPTION_ADMIN_CHAT_ID",
        "TELEGRAM_CHAT_ID",
    )
    no_candidates_alert_window_minutes = int(os.getenv("NO_CANDIDATES_ALERT_WINDOW_MINUTES", "120"))
    no_delivery_alert_window_minutes = int(os.getenv("NO_DELIVERY_ALERT_WINDOW_MINUTES", "180"))
    enable_ai_extraction = _parse_bool(os.getenv("ENABLE_AI_EXTRACTION"), default=True)
    enable_ai_age_check = _parse_bool(os.getenv("ENABLE_AI_AGE_CHECK"), default=True)
    enable_browser_tabs = _parse_bool(os.getenv("ENABLE_BROWSER_TABS"), default=True)
    enable_site_profiles = _parse_bool(os.getenv("ENABLE_SITE_PROFILES"), default=True)
    reduced_quality_fallback_mode = _parse_bool(os.getenv("REDUCED_QUALITY_FALLBACK_MODE"), default=True)
    delivery_worker_count = int(os.getenv("DELIVERY_WORKER_COUNT", "2"))
    delivery_worker_poll_seconds = float(os.getenv("DELIVERY_WORKER_POLL_SECONDS", "5"))
    delivery_job_retry_budget = int(os.getenv("DELIVERY_JOB_RETRY_BUDGET", "3"))
    delivery_job_backoff_seconds = float(os.getenv("DELIVERY_JOB_BACKOFF_SECONDS", "20"))
    enable_manual_challenge_flow = _parse_bool(os.getenv("ENABLE_MANUAL_CHALLENGE_FLOW"), default=False)
    manual_challenge_timeout_seconds = int(os.getenv("MANUAL_CHALLENGE_TIMEOUT_SECONDS", "900"))
    manual_challenge_poll_seconds = float(os.getenv("MANUAL_CHALLENGE_POLL_SECONDS", "2"))
    if max_job_post_age_days < 1:
        raise ValueError("MAX_JOB_POST_AGE_DAYS must be >= 1.")
    if site_cycle_idle_timeout_seconds < 30:
        raise ValueError("SITE_CYCLE_IDLE_TIMEOUT_SECONDS must be >= 30.")
    if site_cycle_hard_timeout_seconds <= site_cycle_idle_timeout_seconds:
        raise ValueError("SITE_CYCLE_HARD_TIMEOUT_SECONDS must be greater than SITE_CYCLE_IDLE_TIMEOUT_SECONDS.")
    if browser_reprobe_cooldown_seconds < 30:
        raise ValueError("BROWSER_REPROBE_COOLDOWN_SECONDS must be >= 30.")
    extraction_low_confidence_threshold = max(0.0, min(extraction_low_confidence_threshold, 1.0))
    max_candidates = max(1, min(max_candidates, 100))
    if openai_request_timeout_seconds < 5:
        raise ValueError("OPENAI_REQUEST_TIMEOUT_SECONDS must be >= 5.")
    if ai_retry_budget < 0:
        raise ValueError("AI_RETRY_BUDGET must be >= 0.")
    if ai_backoff_base_seconds <= 0:
        raise ValueError("AI_BACKOFF_BASE_SECONDS must be > 0.")
    if fetch_strategy_timeout_ms < 1000:
        raise ValueError("FETCH_STRATEGY_TIMEOUT_MS must be >= 1000.")
    if network_retry_budget < 0:
        raise ValueError("NETWORK_RETRY_BUDGET must be >= 0.")
    if network_backoff_base_seconds <= 0:
        raise ValueError("NETWORK_BACKOFF_BASE_SECONDS must be > 0.")
    if link_ranking_timeout_seconds < 5:
        raise ValueError("LINK_RANKING_TIMEOUT_SECONDS must be >= 5.")
    if extraction_timeout_seconds < 10:
        raise ValueError("EXTRACTION_TIMEOUT_SECONDS must be >= 10.")
    if age_check_timeout_seconds < 5:
        raise ValueError("AGE_CHECK_TIMEOUT_SECONDS must be >= 5.")
    if max_fetch_strategies_per_url < 1:
        raise ValueError("MAX_FETCH_STRATEGIES_PER_URL must be >= 1.")
    if no_candidates_alert_window_minutes < 5:
        raise ValueError("NO_CANDIDATES_ALERT_WINDOW_MINUTES must be >= 5.")
    if no_delivery_alert_window_minutes < 5:
        raise ValueError("NO_DELIVERY_ALERT_WINDOW_MINUTES must be >= 5.")
    if delivery_worker_count < 1:
        raise ValueError("DELIVERY_WORKER_COUNT must be >= 1.")
    if delivery_worker_poll_seconds <= 0:
        raise ValueError("DELIVERY_WORKER_POLL_SECONDS must be > 0.")
    if delivery_job_retry_budget < 0:
        raise ValueError("DELIVERY_JOB_RETRY_BUDGET must be >= 0.")
    if delivery_job_backoff_seconds <= 0:
        raise ValueError("DELIVERY_JOB_BACKOFF_SECONDS must be > 0.")
    if manual_challenge_timeout_seconds < 30:
        raise ValueError("MANUAL_CHALLENGE_TIMEOUT_SECONDS must be >= 30.")
    if manual_challenge_poll_seconds <= 0:
        raise ValueError("MANUAL_CHALLENGE_POLL_SECONDS must be > 0.")
    human_review_threshold = max(0.0, min(human_review_threshold, 1.0))
    human_review_min_threshold = max(0.0, min(human_review_min_threshold, human_review_threshold))

    state_db = Path(os.getenv("STATE_DB_PATH", str(cwd / "state" / "job_bot_state.db")))
    output_file = Path(os.getenv("OUTPUT_FILE_PATH", str(cwd / "output" / "job_cards.txt")))
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    telegram_subs_db_path = Path(
        os.getenv("TELEGRAM_SUBS_DB_PATH", str(cwd / "state" / "telegram_subscriptions.db"))
    )
    telegram_user_log_dir = Path(
        os.getenv("TELEGRAM_USER_LOG_DIR", str(cwd / "state" / "user_logs"))
    )
    if telegram_bot_token in {"YOUR_TELEGRAM_BOT_TOKEN_HERE", "YOUR_TELEGRAM_BOT_API_TOKEN_HERE"}:
        telegram_bot_token = ""
    if telegram_chat_id == "YOUR_TELEGRAM_CHAT_ID_HERE":
        telegram_chat_id = ""

    return Settings(
        openai_api_key=api_key,
        openai_model=model,
        websites=websites,
        agent_count=agent_count,
        cycle_seconds=cycle_seconds,
        max_cards_per_cycle=max_cards,
        seen_streak_stop=seen_streak_stop,
        max_candidates_per_site=max_candidates,
        max_seed_pages_per_site=max_seed_pages,
        state_db_path=state_db,
        output_file_path=output_file,
        headless_browser=_parse_bool(os.getenv("HEADLESS_BROWSER"), default=True),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        request_timeout_ms=request_timeout_ms,
        verify_ssl=_parse_bool(os.getenv("VERIFY_SSL"), default=True),
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        telegram_subs_db_path=telegram_subs_db_path,
        telegram_user_log_dir=telegram_user_log_dir,
        websites_file_path=websites_file_path,
        neglect_post_if_filters_miss=_parse_bool(os.getenv("NEGLECT_POST_IF_FILTERS_MISS"), default=False),
        enable_human_review_queue=_parse_bool(os.getenv("ENABLE_HUMAN_REVIEW_QUEUE"), default=True),
        human_review_confidence_threshold=human_review_threshold,
        human_review_min_confidence_threshold=human_review_min_threshold,
        max_job_post_age_days=max_job_post_age_days,
        openai_model_website_guard=website_guard_model,
        openai_model_link_ranking=link_ranking_model,
        openai_model_keyword_expansion=keyword_expansion_model,
        openai_model_extraction=extraction_model,
        openai_model_post_age=post_age_model,
        openai_model_filter_match=filter_match_model,
        use_scrapling_cloudflare_solver=_parse_bool(
            os.getenv("USE_SCRAPLING_CLOUDFLARE_SOLVER"),
            default=False,
        ),
        human_review_chat_id=_human_review_chat_id_from_env(),
        site_cycle_timeout_seconds=site_cycle_idle_timeout_seconds,
        site_cycle_idle_timeout_seconds=site_cycle_idle_timeout_seconds,
        site_cycle_hard_timeout_seconds=site_cycle_hard_timeout_seconds,
        browser_reprobe_cooldown_seconds=browser_reprobe_cooldown_seconds,
        extraction_low_confidence_threshold=extraction_low_confidence_threshold,
        openai_request_timeout_seconds=openai_request_timeout_seconds,
        ai_retry_budget=ai_retry_budget,
        ai_backoff_base_seconds=ai_backoff_base_seconds,
        fetch_strategy_timeout_ms=fetch_strategy_timeout_ms,
        network_retry_budget=network_retry_budget,
        network_backoff_base_seconds=network_backoff_base_seconds,
        link_ranking_timeout_seconds=link_ranking_timeout_seconds,
        extraction_timeout_seconds=extraction_timeout_seconds,
        age_check_timeout_seconds=age_check_timeout_seconds,
        max_fetch_strategies_per_url=max_fetch_strategies_per_url,
        enable_baseline_website_fallback=enable_baseline_website_fallback,
        scraper_paused=scraper_paused,
        subscription_admin_chat_ids=subscription_admin_chat_ids,
        no_candidates_alert_window_minutes=no_candidates_alert_window_minutes,
        no_delivery_alert_window_minutes=no_delivery_alert_window_minutes,
        enable_ai_extraction=enable_ai_extraction,
        enable_ai_age_check=enable_ai_age_check,
        enable_browser_tabs=enable_browser_tabs,
        enable_site_profiles=enable_site_profiles,
        reduced_quality_fallback_mode=reduced_quality_fallback_mode,
        delivery_worker_count=delivery_worker_count,
        delivery_worker_poll_seconds=delivery_worker_poll_seconds,
        delivery_job_retry_budget=delivery_job_retry_budget,
        delivery_job_backoff_seconds=delivery_job_backoff_seconds,
        enable_manual_challenge_flow=enable_manual_challenge_flow,
        manual_challenge_timeout_seconds=manual_challenge_timeout_seconds,
        manual_challenge_poll_seconds=manual_challenge_poll_seconds,
    )
