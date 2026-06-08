from __future__ import annotations

import copy
import html
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import streamlit as st
import streamlit.components.v1 as components

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from prospect_system.dashboard_state import ProspectAnalysisState
from prospect_system.dashboard_styles import (
    ANOTHER_RUN_LABEL,
    CSS,
    PAGE_TITLE,
    RUN_BUTTON_LABEL,
)
from prospect_system.google_sheets_client import GoogleSheetsClient
from prospect_system.logging_utils import StateJSONLLogger, get_prospect_logger
from prospect_system.prospect_ai_analyzer import ProspectAIAnalyzer
from prospect_system.prospect_config import ProspectSettings, load_prospect_settings
from prospect_system.prospect_decision_handler import ProspectDecisionHandler
from prospect_system.prospect_flow import ProspectFlow
from prospect_system.prospect_scraper import normalize_input_urls
from prospect_system.telegram_notifier import send_prospect_usage_notification

WORKFLOW_STEPS = (
    (1, "Criteria"),
    (2, "Processing"),
    (3, "Output"),
    (4, "Generate draft"),
    (5, "Decision"),
)

# Progress bar sizing knobs. Increase or decrease these values to resize the
# connected step bar without changing the rest of the workflow UI.
PROGRESS_BAR_HEIGHT_PX = 72
PROGRESS_DOT_SIZE_PX = 40
PROGRESS_LINE_TOP_PX = 18
PROGRESS_LABEL_FONT_PX = 11
BLOCKED_PUBLIC_CLOUD_MESSAGE = (
    "Oops! The website security system has blocked my entrance because this system is on a free public cloud service "
    "website. PLEASE USE ANOTHER URL OR USE CACHED DEMO CONTENT"
)
CACHED_DEMO_TEXT_PATH = (
    PROJECT_ROOT / "prospect_system" / "demo_data" / "sample_prospect_page.txt"
)
CACHED_DEMO_WEBSITE_URL = "https://www.launchgood.com/"


@dataclass(slots=True)
class _BackgroundRun:
    state: ProspectAnalysisState | None = None
    done: bool = False
    error: str = ""


@dataclass(slots=True)
class _BackgroundRunRegistry:
    lock: threading.Lock = field(default_factory=threading.Lock)
    runs: dict[str, _BackgroundRun] = field(default_factory=dict)


@st.cache_resource
def _background_run_registry() -> _BackgroundRunRegistry:
    return _BackgroundRunRegistry()


def _repo_root() -> Path:
    return PROJECT_ROOT


def _init_session() -> None:
    st.session_state.setdefault("prospect_url_input", "")
    st.session_state.setdefault("prospect_target_criteria", "")
    st.session_state.setdefault("prospect_outreach_goal", "")
    st.session_state.setdefault("prospect_url_input_preset_clicked", False)
    st.session_state.setdefault("prospect_target_criteria_preset_clicked", False)
    st.session_state.setdefault("prospect_outreach_goal_preset_clicked", False)
    st.session_state.setdefault("prospect_state", None)
    st.session_state.setdefault("prospect_analysis_running", False)
    st.session_state.setdefault("prospect_active_run_id", "")
    st.session_state.setdefault("prospect_run_error", "")
    st.session_state.setdefault("prospect_flash", None)
    st.session_state.setdefault("prospect_current_step", 1)
    st.session_state.setdefault("prospect_last_blocked_popup_key", "")
    st.session_state.setdefault("prospect_selected_card_index", 0)
    st.session_state.setdefault("prospect_last_decision_metric_key", "")
    st.session_state.setdefault(
        "prospect_local_metrics",
        {
            "approved_prospects": 0,
            "rejected_prospects": 0,
            "further_review": 0,
            "generated_outreach_drafts": 0,
        },
    )
    st.session_state.setdefault("prospect_history", [])


def _format_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, remainder = divmod(seconds, 60)
    if minutes <= 0:
        return f"{remainder}s"
    return f"{minutes}m {remainder}s"


def _time_saved_label(
    settings: ProspectSettings, state: ProspectAnalysisState | None
) -> str:
    if state is None:
        return "0m"
    manual_seconds = len(state.prospect_cards) * settings.manual_review_minutes * 60
    saved = max(0.0, manual_seconds - state.processing_time_seconds)
    return _format_seconds(saved)


def _local_metrics(
    settings: ProspectSettings, state: ProspectAnalysisState | None
) -> dict[str, Any]:
    cards = state.prospect_cards if state is not None else []
    local = st.session_state["prospect_local_metrics"]
    scores = [card.fit_score for card in cards]
    uncertainty_flags = sum(len(card.uncertainty_flags) for card in cards)
    return {
        "analyzed_websites": len(cards),
        "approved_prospects": local["approved_prospects"],
        "rejected_prospects": local["rejected_prospects"],
        "average_fit_score": round(sum(scores) / len(scores), 1) if scores else 0,
        "uncertainty_flags": uncertainty_flags,
        "generated_outreach_drafts": local["generated_outreach_drafts"],
        "time_saved_estimate": _time_saved_label(settings, state),
    }


def _sheet_metrics(settings: ProspectSettings) -> dict[str, Any]:
    if not settings.is_google_configured:
        return {}
    try:
        return GoogleSheetsClient(settings).fetch_dashboard_metrics()
    except Exception:
        return {}


def _render_header() -> None:
    st.caption("Prospect Discovery")
    st.title(PAGE_TITLE)
    st.write(
        "This is your AI assistant for discovering and reaching out to potential customers based on your criteria, "
        "after which you decide how to add them to your CRM sheet (DEMO)."
    )


def _render_metrics(
    settings: ProspectSettings, state: ProspectAnalysisState | None
) -> None:
    metrics = _local_metrics(settings, state)
    sheet_metrics = _sheet_metrics(settings)
    for key, value in sheet_metrics.items():
        if key in metrics:
            metrics[key] = round(value, 1) if isinstance(value, float) else value
    metric_items = [
        ("Analyzed Websites", metrics["analyzed_websites"]),
        ("Approved Prospects", metrics["approved_prospects"]),
        ("Rejected Prospects", metrics["rejected_prospects"]),
        ("Average Fit Score", metrics["average_fit_score"]),
        ("Uncertainty Flags", metrics["uncertainty_flags"]),
        ("Outreach Drafts", metrics["generated_outreach_drafts"]),
        ("Time Saved", metrics["time_saved_estimate"]),
    ]
    st.subheader("Statistics")
    cols = st.columns(4)
    for index, (label, value) in enumerate(metric_items):
        with cols[index % 4]:
            st.metric(label, value)


def _render_google_sheet_row(settings: ProspectSettings) -> None:
    st.subheader("CRM Sheet")
    if settings.google_sheet_id:
        sheet_url = (
            f"https://docs.google.com/spreadsheets/d/{settings.google_sheet_id}/edit"
        )
        st.link_button("Open Google Sheet", sheet_url, use_container_width=True)
        return
    st.info("Add GOOGLE_SHEET_ID in .env to show the CRM Google Sheet link.")


def _render_env_status(settings: ProspectSettings) -> None:
    if not settings.missing_env_values:
        return
    with st.expander("Missing .env values", expanded=False):
        st.warning(
            "The dashboard can open, but these values are required for the full end-to-end flow:"
        )
        for name in settings.missing_env_values:
            st.code(name)


def _render_flash_message() -> None:
    flash = st.session_state.get("prospect_flash")
    if not flash:
        return
    level, message = flash
    if level == "success":
        st.success(message)
    elif level == "warning":
        st.warning(message)
    elif level == "error":
        st.error(message)
    else:
        st.info(message)
    st.session_state["prospect_flash"] = None


def _coerce_step(value: Any) -> int | None:
    if isinstance(value, list):
        value = value[0] if value else ""
    try:
        step = int(str(value or "").strip())
    except ValueError:
        return None
    if step not in {step_number for step_number, _ in WORKFLOW_STEPS}:
        return None
    return step


def _sync_step_from_query_params() -> None:
    try:
        step = _coerce_step(st.query_params.get("prospect_step"))
    except Exception:
        step = None
    if step is not None:
        st.session_state["prospect_current_step"] = step


def _current_step() -> int:
    return _coerce_step(st.session_state.get("prospect_current_step")) or 1


def _set_workflow_step(step: int) -> None:
    normalized = _coerce_step(step) or 1
    st.session_state["prospect_current_step"] = normalized
    try:
        st.query_params["prospect_step"] = str(normalized)
    except Exception:
        pass


def _go_to_step(step: int) -> None:
    _set_workflow_step(step)
    st.rerun()


def _step_button(
    label: str,
    step: int,
    key: str,
    *,
    button_type: str = "secondary",
    disabled: bool = False,
) -> None:
    if st.button(
        label, type=button_type, use_container_width=True, key=key, disabled=disabled
    ):
        _go_to_step(step)


def _has_draft(state: ProspectAnalysisState | None) -> bool:
    if state is None:
        return False
    draft = state.outreach_draft or {}
    return any(str(draft.get(key) or "").strip() for key in ("subject", "body", "cta"))


def _latest_blocked_error(state: ProspectAnalysisState | None) -> Any | None:
    if not isinstance(state, ProspectAnalysisState):
        return None
    blocked_markers = (
        "blocked",
        "forbidden",
        "captcha",
        "cloudflare",
        "challenge",
        "access denied",
        "http 401",
        "http 403",
        "http 407",
        "http 429",
        "http 451",
    )
    for error in reversed(state.errors):
        details = error.details or {}
        combined = " ".join(
            [
                str(error.stage or ""),
                str(error.message or ""),
                str(details.get("error_type") or ""),
                str(details.get("error_message") or ""),
                str(details.get("raw_error") or ""),
            ]
        ).lower()
        if any(marker in combined for marker in blocked_markers):
            return error
    return None


def _show_blocked_public_cloud_message(
    state: ProspectAnalysisState, error: Any
) -> None:
    popup_key = f"{state.session_id}:{getattr(error, 'created_at', '')}"
    if st.session_state.get("prospect_last_blocked_popup_key") != popup_key:
        if hasattr(st, "toast"):
            st.toast(BLOCKED_PUBLIC_CLOUD_MESSAGE)
        st.session_state["prospect_last_blocked_popup_key"] = popup_key
    st.error(BLOCKED_PUBLIC_CLOUD_MESSAGE)


def _is_step_complete(
    step: int, current_step: int, state: ProspectAnalysisState | None
) -> bool:
    is_running = bool(st.session_state.get("prospect_analysis_running", False))
    if step == 1:
        return is_running or state is not None
    if step == 2:
        return bool(state and state.prospect_cards and not is_running)
    if step == 3:
        return current_step > 3 or bool(state and state.completed_at)
    if step == 4:
        return _has_draft(state) or current_step > 4
    if step == 5:
        return bool(state and state.completed_at)
    return current_step > step


def _render_workflow_progress(state: ProspectAnalysisState | None) -> None:
    current = _current_step()
    active_index = next(
        (index for index, (step, _) in enumerate(WORKFLOW_STEPS) if step == current),
        0,
    )
    progress_percent = (
        0
        if len(WORKFLOW_STEPS) <= 1
        else (active_index / (len(WORKFLOW_STEPS) - 1)) * 100
    )
    items: list[str] = []
    for step, title in WORKFLOW_STEPS:
        classes = ["step-item"]
        complete = _is_step_complete(step, current, state)
        if complete:
            classes.append("complete")
        if step == current:
            classes.append("active")
        label = f"Step {step} - {title}"
        items.append(f"""
            <a class="{' '.join(classes)}" href="?prospect_step={step}" target="_parent" onclick="window.parent.location.search='?prospect_step={step}'; return false;">
                <span class="step-dot">{'&#10003;' if complete else html.escape(str(step))}</span>
                <span class="step-label">{html.escape(label)}</span>
            </a>
            """)
    components.html(
        f"""
        <style>
            body {{
                margin: 0;
                font-family: Manrope, Arial, sans-serif;
                background: transparent;
                color: #334155;
            }}
            .progress-wrap {{
                position: relative;
                padding: 4px 4px 2px;
            }}
            .progress-line {{
                position: absolute;
                left: 8%;
                right: 8%;
                top: {PROGRESS_LINE_TOP_PX}px;
                height: 3px;
                border-radius: 999px;
                background: #dbe3ef;
                overflow: hidden;
            }}
            .progress-fill {{
                width: {progress_percent:.1f}%;
                height: 100%;
                border-radius: inherit;
                background: linear-gradient(90deg, #0f766e, #2563eb);
                transition: width 180ms ease;
            }}
            .step-grid {{
                position: relative;
                z-index: 1;
                display: grid;
                grid-template-columns: repeat(5, minmax(0, 1fr));
                gap: 4px;
            }}
            .step-item {{
                display: flex;
                flex-direction: column;
                align-items: center;
                gap: 5px;
                min-width: 0;
                text-decoration: none;
                color: #64748b;
            }}
            .step-dot {{
                display: inline-flex;
                align-items: center;
                justify-content: center;
                width: {PROGRESS_DOT_SIZE_PX}px;
                height: {PROGRESS_DOT_SIZE_PX}px;
                border-radius: 999px;
                border: 2px solid #cbd5e1;
                background: #ffffff;
                color: #475569;
                font-size: 12px;
                font-weight: 800;
                box-shadow: 0 1px 2px rgba(15, 23, 42, 0.08);
            }}
            .step-label {{
                color: inherit;
                font-size: {PROGRESS_LABEL_FONT_PX}px;
                font-weight: 800;
                line-height: 1.2;
                text-align: center;
            }}
            .step-item.complete .step-dot {{
                border-color: #0f766e;
                background: #0f766e;
                color: #ffffff;
            }}
            .step-item.active {{
                color: #1d4ed8;
            }}
            .step-item.active .step-dot {{
                border-color: #2563eb;
                background: #2563eb;
                color: #ffffff;
                box-shadow: 0 0 0 4px rgba(37, 99, 235, 0.12);
            }}
            @media (max-width: 680px) {{
                .progress-line {{
                    left: 10%;
                    right: 10%;
                }}
                .step-grid {{
                    gap: 4px;
                }}
                .step-dot {{
                    width: {max(24, PROGRESS_DOT_SIZE_PX - 4)}px;
                    height: {max(24, PROGRESS_DOT_SIZE_PX - 4)}px;
                    font-size: 11px;
                }}
                .step-label {{
                    font-size: {max(10, PROGRESS_LABEL_FONT_PX - 1)}px;
                }}
            }}
        </style>
        <div class="progress-wrap" aria-label="Prospect workflow progress">
            <div class="progress-line"><div class="progress-fill"></div></div>
            <div class="step-grid">{"".join(items)}</div>
        </div>
        """,
        height=PROGRESS_BAR_HEIGHT_PX,
        scrolling=False,
    )


def _store_background_run_state(
    registry: _BackgroundRunRegistry, run_id: str, state: ProspectAnalysisState
) -> None:
    with registry.lock:
        record = registry.runs.setdefault(run_id, _BackgroundRun())
        if record.done and record.error:
            return
        record.state = copy.deepcopy(state)


def _finish_background_run(
    registry: _BackgroundRunRegistry,
    run_id: str,
    *,
    state: ProspectAnalysisState | None = None,
    error: str = "",
) -> None:
    with registry.lock:
        record = registry.runs.setdefault(run_id, _BackgroundRun())
        if state is not None:
            record.state = copy.deepcopy(state)
        record.done = True
        record.error = error


def _read_background_run(run_id: str) -> _BackgroundRun | None:
    if not run_id:
        return None
    registry = _background_run_registry()
    with registry.lock:
        record = registry.runs.get(run_id)
        return copy.deepcopy(record) if record is not None else None


def _background_state_snapshot(
    registry: _BackgroundRunRegistry, run_id: str
) -> ProspectAnalysisState | None:
    with registry.lock:
        record = registry.runs.get(run_id)
        if record is None or record.state is None:
            return None
        return copy.deepcopy(record.state)


def _finish_background_run_with_error(
    registry: _BackgroundRunRegistry,
    run_id: str,
    *,
    settings: ProspectSettings,
    message: str,
    stage: str,
    error_type: str,
) -> None:
    state = _background_state_snapshot(registry, run_id)
    if state is not None:
        error = state.add_error(
            stage=stage,
            message=message,
            details={
                "error_type": error_type,
                "error_message": message,
                "resolved": "No",
            },
        )
        logger = StateJSONLLogger(settings.prospect_log_dir)
        logger.write_error(error)
        if state.ai_step_logs:
            logger.write_step(state, state.ai_step_logs[-1])
        logger.write_state_snapshot(state)
    _finish_background_run(registry, run_id, state=state, error=message)


def _run_with_hard_timeout(call: Any, *, timeout_seconds: float) -> Any:
    timeout_seconds = max(1.0, float(timeout_seconds))
    executor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="prospect-flow-timeout"
    )
    future = executor.submit(call)
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeoutError as exc:
        future.cancel()
        raise TimeoutError(
            f"prospect flow exceeded {timeout_seconds:.0f}s budget"
        ) from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _sync_active_run_from_registry() -> bool:
    run_id = str(st.session_state.get("prospect_active_run_id") or "")
    record = _read_background_run(run_id)
    if record is None:
        return False
    if record.state is not None:
        st.session_state["prospect_state"] = record.state
    if record.done:
        was_running = bool(st.session_state.get("prospect_analysis_running", False))
        st.session_state["prospect_analysis_running"] = False
        st.session_state["prospect_active_run_id"] = ""
        st.session_state["prospect_run_error"] = record.error
        if record.error:
            st.session_state["prospect_flash"] = (
                "error",
                f"Prospect analysis failed: {record.error}",
            )
        return was_running
    return False


def _run_analysis_worker(
    run_id: str,
    *,
    registry: _BackgroundRunRegistry,
    settings: ProspectSettings,
    urls: list[str],
    criteria: str,
    goal: str,
) -> None:
    logger = get_prospect_logger(settings.prospect_log_dir)
    notify_result = send_prospect_usage_notification(settings, input_urls=urls)
    if not notify_result.success:
        logger.warning("[prospect-telegram] %s", notify_result.message)
    usage_alert_status = "Yes" if notify_result.success else "No"
    try:
        flow = ProspectFlow(settings, logger)
        state = _run_with_hard_timeout(
            lambda: flow.run(
                input_urls=urls,
                target_criteria=criteria,
                outreach_goal=goal,
                progress_callback=lambda updated_state: _store_background_run_state(
                    registry, run_id, updated_state
                ),
                timeout_seconds=settings.analysis_timeout_seconds,
            ),
            timeout_seconds=settings.analysis_timeout_seconds,
        )
        state.telegram_or_slack_alert_sent = usage_alert_status
    except TimeoutError:
        timeout_seconds = int(settings.analysis_timeout_seconds)
        _finish_background_run_with_error(
            registry,
            run_id,
            settings=settings,
            message=(
                f"The live scrape and AI analysis took longer than {timeout_seconds} seconds, "
                "so I stopped it cleanly. Please try another URL or run cached demo content."
            ),
            stage="Processing",
            error_type="Timeout",
        )
        return
    except Exception as exc:  # noqa: BLE001
        _finish_background_run(registry, run_id, error=str(exc))
        return
    _finish_background_run(registry, run_id, state=state)


def _run_cached_demo_worker(
    run_id: str,
    *,
    registry: _BackgroundRunRegistry,
    settings: ProspectSettings,
    urls: list[str],
    criteria: str,
    goal: str,
) -> None:
    try:
        cached_text = CACHED_DEMO_TEXT_PATH.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        _finish_background_run_with_error(
            registry,
            run_id,
            settings=settings,
            message=f"Cached demo content could not be loaded: {exc}",
            stage="Processing",
            error_type="Cached Demo",
        )
        return
    try:
        flow = ProspectFlow(settings, get_prospect_logger(settings.prospect_log_dir))
        state = _run_with_hard_timeout(
            lambda: flow.run_cached_demo(
                input_urls=urls,
                target_criteria=criteria,
                outreach_goal=goal,
                cached_text=cached_text,
                progress_callback=lambda updated_state: _store_background_run_state(
                    registry, run_id, updated_state
                ),
                timeout_seconds=settings.analysis_timeout_seconds,
            ),
            timeout_seconds=settings.analysis_timeout_seconds,
        )
        state.telegram_or_slack_alert_sent = "No"
    except TimeoutError:
        timeout_seconds = int(settings.analysis_timeout_seconds)
        _finish_background_run_with_error(
            registry,
            run_id,
            settings=settings,
            message=(
                f"The cached demo analysis took longer than {timeout_seconds} seconds, "
                "so I stopped it cleanly. Please try again or adjust the AI settings."
            ),
            stage="Processing",
            error_type="Timeout",
        )
        return
    except Exception as exc:  # noqa: BLE001
        _finish_background_run(registry, run_id, error=str(exc))
        return
    _finish_background_run(registry, run_id, state=state)


def _discovered_link_count(state: ProspectAnalysisState) -> int:
    urls = {
        str(link.get("url") or "").strip().lower()
        for page in state.scraped_pages
        for link in (page.discovered_links or page.links)
        if str(link.get("url") or "").strip()
    }
    return len(urls)


def _selected_page_count(state: ProspectAnalysisState) -> int:
    return sum(
        1 for page in state.scraped_pages if str(page.selected_for_reason or "").strip()
    )


def _render_scrape_explainability(state: ProspectAnalysisState) -> None:
    discovered_count = _discovered_link_count(state)
    selected_count = _selected_page_count(state)
    skipped_count = len(state.skipped_pages)
    metric_cols = st.columns(4)
    metric_cols[0].metric("Discovered Links", discovered_count)
    metric_cols[1].metric("AI Selected Pages", selected_count)
    metric_cols[2].metric("Scraped Pages", len(state.scraped_pages))
    metric_cols[3].metric("Skipped / Blocked", skipped_count)

    with st.expander("Dynamic scraping details", expanded=False):
        scraped_rows = [
            {
                "URL": page.final_url or page.url,
                "Status": page.status_code or page.status or "N/A",
                "Discovered Links": len(page.discovered_links or page.links),
                "Selected Because": page.selected_for_reason
                or "Starting URL / discovered evidence",
            }
            for page in state.scraped_pages
        ]
        st.caption("Scraped page URLs")
        st.dataframe(
            scraped_rows
            or [
                {
                    "URL": "N/A",
                    "Status": "N/A",
                    "Discovered Links": 0,
                    "Selected Because": "N/A",
                }
            ],
            use_container_width=True,
            hide_index=True,
        )

        selected_rows = [
            {
                "URL": page.final_url or page.url,
                "Why AI Selected It": page.selected_for_reason,
            }
            for page in state.scraped_pages
            if str(page.selected_for_reason or "").strip()
        ]
        st.caption("AI-selected pages")
        st.dataframe(
            selected_rows or [{"URL": "N/A", "Why AI Selected It": "N/A"}],
            use_container_width=True,
            hide_index=True,
        )

        skipped_rows = [
            {
                "URL": page.final_url or page.url,
                "Status": page.status_code or page.status or "N/A",
                "Blocked": "Yes" if page.blocked else "No",
                "Error": page.error or "N/A",
                "Selected Because": page.selected_for_reason or "N/A",
            }
            for page in state.skipped_pages
        ]
        st.caption("Pages skipped or blocked")
        st.dataframe(
            skipped_rows
            or [
                {
                    "URL": "N/A",
                    "Status": "N/A",
                    "Blocked": "N/A",
                    "Error": "N/A",
                    "Selected Because": "N/A",
                }
            ],
            use_container_width=True,
            hide_index=True,
        )


def _render_progress_snapshot(
    state: ProspectAnalysisState, settings: ProspectSettings
) -> None:
    is_running = bool(st.session_state.get("prospect_analysis_running", False))
    eta = state.estimate_remaining_seconds(
        max_pages_to_scrape=settings.max_pages_to_scrape
    )
    pages_scraped = len(state.scraped_pages)
    if is_running:
        status = f"Estimated remaining time: {_format_seconds(eta)}"
    else:
        status = f"Pages scraped: {pages_scraped}/{settings.max_pages_to_scrape}"
    st.info(f"Current URL: {state.current_url or 'waiting'} | {status}")
    if state.scraped_pages or state.skipped_pages:
        _render_scrape_explainability(state)
    if state.ai_step_logs:
        caption = (
            f"Live visual log refreshes every {_format_seconds(settings.log_refresh_seconds)} while analysis is running."
            if is_running
            else "Visual log from the latest analysis."
        )
        st.caption(caption)
        _render_step_log_table(state.ai_step_logs, auto_scroll=True, height=300)


def _render_live_log_refresh(settings: ProspectSettings) -> None:
    @st.fragment(run_every=settings.log_refresh_seconds)
    def _live_log_fragment() -> None:
        completed = _sync_active_run_from_registry()
        if completed:
            st.rerun()
            return
        if not st.session_state.get("prospect_analysis_running", False):
            error = str(st.session_state.get("prospect_run_error") or "")
            if error:
                st.error(f"Prospect analysis failed: {error}")
            state = st.session_state.get("prospect_state")
            if isinstance(state, ProspectAnalysisState) and state.ai_step_logs:
                _render_progress_snapshot(state, settings)
            return
        state = st.session_state.get("prospect_state")
        if not isinstance(state, ProspectAnalysisState):
            st.info("Analysis is starting. Live logs will appear here.")
            return
        _render_progress_snapshot(state, settings)

    _live_log_fragment()


def _set_input_value(key: str, value: str) -> None:
    st.session_state[key] = value
    st.session_state[f"{key}_preset_clicked"] = True


def _clear_legacy_auto_presets(settings: ProspectSettings) -> None:
    preset_pairs = (
        ("prospect_url_input", settings.demo_website_url),
        ("prospect_target_criteria", settings.default_target_criteria),
        ("prospect_outreach_goal", settings.default_outreach_goal),
    )
    for key, preset_value in preset_pairs:
        clicked_key = f"{key}_preset_clicked"
        current_value = str(st.session_state.get(key, "") or "")
        if st.session_state.get(clicked_key, False):
            continue
        if preset_value and current_value == preset_value:
            st.session_state[key] = ""


def _prepare_another_run(state: ProspectAnalysisState) -> None:
    st.session_state["prospect_history"].append(state)
    st.session_state["prospect_state"] = None
    st.session_state["prospect_url_input"] = ""
    st.session_state["prospect_target_criteria"] = ""
    st.session_state["prospect_outreach_goal"] = ""
    st.session_state["prospect_url_input_preset_clicked"] = False
    st.session_state["prospect_target_criteria_preset_clicked"] = False
    st.session_state["prospect_outreach_goal_preset_clicked"] = False
    st.session_state["prospect_analysis_running"] = False
    st.session_state["prospect_active_run_id"] = ""
    st.session_state["prospect_run_error"] = ""
    st.session_state["prospect_flash"] = None
    st.session_state["prospect_selected_card_index"] = 0
    st.session_state["prospect_last_decision_metric_key"] = ""
    _set_workflow_step(1)


def _render_input(settings: ProspectSettings) -> None:
    _clear_legacy_auto_presets(settings)
    st.subheader("Criteria")
    url_cols = st.columns([2, 1])
    with url_cols[0]:
        st.text_input(
            "Website Link - The targeted website to scrape. (In the full version of the system, you can upload a spreadsheet with thousands of links and minimal unnecessary human involvement)",
            key="prospect_url_input",
            placeholder="https://www.site1.com",
        )
        st.button(
            "Use preset",
            use_container_width=True,
            key="use_preset_url",
            on_click=_set_input_value,
            args=("prospect_url_input", settings.demo_website_url),
        )

    cols = st.columns(2)
    with cols[0]:
        st.text_area(
            "Target criteria - Put keywords using commas for what type of companies you are searching for",
            key="prospect_target_criteria",
            height=120,
        )
        st.button(
            "Use preset",
            use_container_width=True,
            key="use_preset_criteria",
            on_click=_set_input_value,
            args=("prospect_target_criteria", settings.default_target_criteria),
        )
    with cols[1]:
        st.text_area(
            "Outreach goal - Put how you want the AI to draft the potential email.",
            key="prospect_outreach_goal",
            height=120,
        )
        st.button(
            "Use preset",
            use_container_width=True,
            key="use_preset_outreach",
            on_click=_set_input_value,
            args=("prospect_outreach_goal", settings.default_outreach_goal),
        )


def _render_step_log_table(
    steps: list[Any], *, auto_scroll: bool = False, height: int = 280
) -> None:
    if auto_scroll:
        _render_scroll_log(steps, height=height)
        return
    rows = [
        {
            "Time": step.created_at,
            "Stage": step.stage or "stage",
            "Message": step.message,
            "URL": step.url,
        }
        for step in steps
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_scroll_log(steps: list[Any], *, height: int) -> None:
    from html import escape

    rows = "\n".join(f"""
        <div class="log-row">
            <div class="log-time">{escape(str(step.created_at))}</div>
            <div class="log-stage">{escape(str(step.stage or 'stage'))}</div>
            <div class="log-message">{escape(str(step.message))}</div>
            <div class="log-url">{escape(str(step.url or ''))}</div>
        </div>
        """ for step in steps)
    components.html(
        f"""
        <style>
            body {{
                margin: 0;
                font-family: Manrope, Arial, sans-serif;
                background: transparent;
                color: #2c628a;
            }}
            #prospect-log {{
                height: {int(height)}px;
                overflow-y: auto;
                border: 1px solid #e3e7ee;
                border-radius: 8px;
                background: #ffffff;
            }}
            .log-row {{
                display: grid;
                grid-template-columns: 150px 105px minmax(240px, 1fr);
                gap: 10px;
                padding: 8px 10px;
                border-bottom: 1px solid #edf0f5;
                font-size: 13px;
                line-height: 1.4;
            }}
            .log-time {{ color: #64748b; }}
            .log-stage {{ color: #0f766e; font-weight: 800; }}
            .log-message {{ color: #2c628a; }}
            .log-url {{
                grid-column: 1 / -1;
                color: #64748b;
                font-size: 12px;
                word-break: break-word;
                margin-left: 265px;
            }}
            @media (max-width: 720px) {{
                .log-row {{
                    grid-template-columns: 1fr;
                    gap: 2px;
                }}
                .log-url {{ margin-left: 0; }}
            }}
        </style>
        <div id="prospect-log">{rows}</div>
        <script>
            const logBox = document.getElementById("prospect-log");
            if (logBox) {{
                logBox.scrollTop = logBox.scrollHeight;
            }}
        </script>
        """,
        height=height + 12,
        scrolling=False,
    )


def _start_analysis(settings: ProspectSettings) -> None:
    urls, errors = normalize_input_urls(st.session_state["prospect_url_input"])
    if errors:
        for error in errors:
            st.error(error)
        return
    if not urls:
        st.error("Enter one valid website URL.")
        return
    if len(urls) > 1:
        st.error("Enter one website URL at a time.")
        return
    criteria = st.session_state["prospect_target_criteria"].strip()
    goal = st.session_state["prospect_outreach_goal"].strip()
    if not criteria or not goal:
        st.error("Target criteria and outreach goal are required.")
        return
    run_id = uuid4().hex
    registry = _background_run_registry()
    initial_state = ProspectAnalysisState.create(
        input_urls=urls,
        target_criteria=criteria,
        outreach_goal=goal,
    )
    initial_state.current_url = urls[0]
    initial_state.log_step(
        f"Analysis queued for {settings.max_pages_to_scrape} page(s)",
        stage="Criteria",
        url=urls[0],
    )
    with registry.lock:
        registry.runs[run_id] = _BackgroundRun(state=initial_state)
    st.session_state["prospect_active_run_id"] = run_id
    st.session_state["prospect_run_error"] = ""
    st.session_state["prospect_flash"] = None
    st.session_state["prospect_state"] = initial_state
    st.session_state["prospect_analysis_running"] = True
    st.session_state["prospect_selected_card_index"] = 0
    st.session_state["prospect_last_decision_metric_key"] = ""
    _set_workflow_step(2)
    thread = threading.Thread(
        target=_run_analysis_worker,
        kwargs={
            "run_id": run_id,
            "registry": registry,
            "settings": settings,
            "urls": urls,
            "criteria": criteria,
            "goal": goal,
        },
        daemon=True,
        name=f"prospect-analysis-{run_id[:8]}",
    )
    thread.start()
    st.rerun()


def _inputs_for_cached_demo(
    settings: ProspectSettings,
    state: ProspectAnalysisState | None,
) -> tuple[list[str], str, str]:
    if isinstance(state, ProspectAnalysisState):
        urls = [url for url in state.input_urls if str(url or "").strip()]
        criteria = state.target_criteria.strip()
        goal = state.outreach_goal.strip()
        return urls or [CACHED_DEMO_WEBSITE_URL], criteria, goal
    urls, _ = normalize_input_urls(
        str(st.session_state.get("prospect_url_input") or "")
    )
    criteria = str(st.session_state.get("prospect_target_criteria") or "").strip()
    goal = str(st.session_state.get("prospect_outreach_goal") or "").strip()
    return (
        urls or [settings.demo_website_url or CACHED_DEMO_WEBSITE_URL],
        criteria,
        goal,
    )


def _start_cached_demo_analysis(
    settings: ProspectSettings, state: ProspectAnalysisState | None = None
) -> None:
    urls, criteria, goal = _inputs_for_cached_demo(settings, state)
    if not criteria or not goal:
        st.error(
            "Target criteria and outreach goal are required before running cached demo content."
        )
        return
    if not CACHED_DEMO_TEXT_PATH.exists():
        st.error(f"Cached demo content is missing: {CACHED_DEMO_TEXT_PATH}")
        return
    run_id = uuid4().hex
    registry = _background_run_registry()
    initial_state = ProspectAnalysisState.create(
        input_urls=urls,
        target_criteria=criteria,
        outreach_goal=goal,
    )
    initial_state.current_url = urls[0] if urls else CACHED_DEMO_WEBSITE_URL
    initial_state.log_step(
        "Cached demo analysis queued",
        stage="Criteria",
        url=initial_state.current_url,
    )
    with registry.lock:
        registry.runs[run_id] = _BackgroundRun(state=initial_state)
    st.session_state["prospect_url_input"] = initial_state.current_url
    st.session_state["prospect_target_criteria"] = criteria
    st.session_state["prospect_outreach_goal"] = goal
    st.session_state["prospect_active_run_id"] = run_id
    st.session_state["prospect_run_error"] = ""
    st.session_state["prospect_flash"] = None
    st.session_state["prospect_state"] = initial_state
    st.session_state["prospect_analysis_running"] = True
    st.session_state["prospect_selected_card_index"] = 0
    st.session_state["prospect_last_decision_metric_key"] = ""
    _set_workflow_step(2)
    thread = threading.Thread(
        target=_run_cached_demo_worker,
        kwargs={
            "run_id": run_id,
            "registry": registry,
            "settings": settings,
            "urls": urls,
            "criteria": criteria,
            "goal": goal,
        },
        daemon=True,
        name=f"prospect-cached-demo-{run_id[:8]}",
    )
    thread.start()
    st.rerun()


def _record_draft_generated(
    settings: ProspectSettings, state: ProspectAnalysisState, card_url: str
) -> None:
    step = state.log_step("Email draft generated", stage="decision", url=card_url)
    logger = StateJSONLLogger(settings.prospect_log_dir)
    logger.write_step(state, step)
    logger.write_state_snapshot(state)
    st.session_state["prospect_local_metrics"]["generated_outreach_drafts"] += 1


def _decision_success(metric_key: str) -> None:
    if metric_key in st.session_state["prospect_local_metrics"]:
        st.session_state["prospect_local_metrics"][metric_key] += 1


def _finish_clicked_action(
    state: ProspectAnalysisState,
    result: Any,
    *,
    metric_key: str = "",
    success_level: str = "success",
) -> None:
    if result.success and metric_key:
        _decision_success(metric_key)
        st.session_state["prospect_last_decision_metric_key"] = metric_key
    st.session_state["prospect_state"] = state
    st.session_state["prospect_flash"] = (
        success_level if result.success else "error",
        result.message,
    )
    _set_workflow_step(5)
    st.rerun()


def _render_card_details(card: Any) -> None:
    with st.container(border=True):
        header_cols = st.columns([3, 1])
        with header_cols[0]:
            st.markdown(f"### {card.company or 'Unknown'}")
            st.write(f"**Website:** {card.website or 'Unknown'}")
        with header_cols[1]:
            st.metric("Fit Score", f"{card.fit_score}/100")
            st.write(card.fit_status or "Unknown")

        field_cols = st.columns(3)
        fields = [
            ("Company", card.company),
            ("Website", card.website),
            ("Category", card.category),
            ("Reason", card.reason),
            ("Pain Point", card.pain_point),
            ("Suggested Offer", card.suggested_offer),
            ("Recommended Contact Type", card.recommended_contact_type),
            ("Confidence", card.confidence),
            ("Next Step", card.next_step),
        ]
        for index, (label, value) in enumerate(fields):
            with field_cols[index % 3]:
                st.markdown(f"**{label}:**")
                st.write(value or "Unknown")

    detail_cols = st.columns(2)
    with detail_cols[0]:
        st.markdown("**Pages scraped**")
        for page_url in card.pages_scraped:
            st.write(page_url)
        st.markdown("**Missing information**")
        st.write(", ".join(card.missing_information) or "None listed")
    with detail_cols[1]:
        st.markdown("**Uncertainty flags**")
        st.write(", ".join(card.uncertainty_flags) or "None listed")
        st.markdown("**AI reasoning summary**")
        st.write(
            card.ai_reasoning_summary
            or card.ai_summary
            or "No reasoning summary provided."
        )
    if card.contact_url:
        st.link_button("Open Contact URL", card.contact_url, use_container_width=True)


def _render_decision_stage(
    settings: ProspectSettings, state: ProspectAnalysisState
) -> None:
    if not state.prospect_cards:
        return
    st.subheader("Output Presentation")
    selected_index = st.selectbox(
        "Prospect card",
        options=list(range(len(state.prospect_cards))),
        format_func=lambda idx: f"{state.prospect_cards[idx].company} - {state.prospect_cards[idx].website}",
    )
    card = state.prospect_cards[selected_index]
    state.prospect_card = card
    _render_card_details(card)

    with st.expander("Raw step log", expanded=False):
        st.caption("Full visual log appears here after an analysis is created.")
        _render_step_log_table(state.ai_step_logs, auto_scroll=True, height=360)

    decision_locked = bool(state.completed_at)
    if state.completed_at:
        st.success("The prospect analysis flow is completed.")
        st.button(
            ANOTHER_RUN_LABEL,
            use_container_width=True,
            on_click=_prepare_another_run,
            args=(state,),
        )

    st.subheader("Decision")
    handler = ProspectDecisionHandler(settings)
    notes = st.text_area(
        "Decision note",
        key=f"decision_note_{state.session_id}_{selected_index}",
        height=80,
    )
    cols = st.columns(3)
    if cols[0].button(
        "Approve to CRM tab",
        use_container_width=True,
        key=f"approve_{state.session_id}_{selected_index}",
        disabled=decision_locked,
    ):
        result = handler.approve_to_crm(state, card=card, notes=notes)
        _finish_clicked_action(state, result, metric_key="approved_prospects")
    if cols[1].button(
        "Move to Further Review",
        use_container_width=True,
        key=f"review_{state.session_id}_{selected_index}",
        disabled=decision_locked,
    ):
        result = handler.move_to_further_review(state, card=card, notes=notes)
        _finish_clicked_action(state, result, metric_key="further_review")
    if cols[2].button(
        "Move to Rejected",
        use_container_width=True,
        key=f"reject_{state.session_id}_{selected_index}",
        disabled=decision_locked,
    ):
        result = handler.reject(state, card=card, notes=notes)
        _finish_clicked_action(
            state, result, metric_key="rejected_prospects", success_level="warning"
        )

    st.divider()
    st.subheader("Outreach Draft")
    analyzer = ProspectAIAnalyzer(
        settings, get_prospect_logger(settings.prospect_log_dir)
    )
    draft_cols = st.columns(3)
    if draft_cols[0].button(
        "Generate an outreach draft",
        use_container_width=True,
        key=f"draft_{state.session_id}_{selected_index}",
    ):
        state.outreach_draft = analyzer.generate_outreach_draft(state, card=card)
        _record_draft_generated(settings, state, card.website)
        st.session_state["prospect_state"] = state
        st.rerun()
    regenerate_instruction = st.text_input(
        "Regeneration instruction",
        key=f"regen_instruction_{state.session_id}_{selected_index}",
        placeholder="Optional direction for a new draft",
    )
    if draft_cols[1].button(
        "Regenerate the draft",
        use_container_width=True,
        key=f"regen_{state.session_id}_{selected_index}",
    ):
        state.outreach_draft = analyzer.generate_outreach_draft(
            state,
            card=card,
            regenerate_instruction=regenerate_instruction,
        )
        _record_draft_generated(settings, state, card.website)
        st.session_state["prospect_state"] = state
        st.rerun()
    if draft_cols[2].button(
        "Move draft to rejected",
        use_container_width=True,
        key=f"draft_reject_{state.session_id}_{selected_index}",
        disabled=decision_locked,
    ):
        result = handler.reject(
            state, card=card, notes=notes or "Rejected after draft review."
        )
        _finish_clicked_action(
            state, result, metric_key="rejected_prospects", success_level="warning"
        )

    if state.outreach_draft:
        st.text_input(
            "Subject", value=state.outreach_draft.get("subject", ""), disabled=True
        )
        st.text_area(
            "Body",
            value=state.outreach_draft.get("body", ""),
            height=220,
            disabled=True,
        )
        st.text_input("CTA", value=state.outreach_draft.get("cta", ""), disabled=True)
        recipient = st.text_input(
            "What is the email to send to?",
            key=f"recipient_{state.session_id}_{selected_index}",
        )
        st.info(
            f"Don't forget, it's a test, so type your email and you will receive the draft to your email "
            f"from {settings.email_sender_email or 'your configured sender email'} within 30 seconds, I promise."
        )
        if st.button(
            "Approve and send",
            use_container_width=True,
            key=f"send_{state.session_id}_{selected_index}",
            disabled=decision_locked,
        ):
            result = handler.send_outreach(state, card=card, recipient_email=recipient)
            _finish_clicked_action(state, result, metric_key="approved_prospects")

    st.divider()
    st.subheader("Re-analyze")
    reanalysis_instruction = st.text_area(
        "New instruction for re-analysis",
        key=f"reanalyze_instruction_{state.session_id}_{selected_index}",
        height=80,
    )
    if st.button(
        "Re-analyze",
        use_container_width=True,
        key=f"reanalyze_{state.session_id}_{selected_index}",
    ):
        flow = ProspectFlow(settings, get_prospect_logger(settings.prospect_log_dir))
        st.session_state["prospect_analysis_running"] = True

        def on_progress(updated_state: ProspectAnalysisState) -> None:
            st.session_state["prospect_state"] = updated_state

        try:
            with st.spinner("Re-analyzing prospect..."):
                updated = flow.reanalyze(
                    state=state,
                    website=card.website,
                    reanalysis_instruction=reanalysis_instruction,
                    progress_callback=on_progress,
                )
        finally:
            st.session_state["prospect_analysis_running"] = False
        st.session_state["prospect_state"] = updated
        st.rerun()

    if False and state.completed_at:
        st.success("Bye bye 👋 The prospect analysis flow is completed.")
        st.button(
            ANOTHER_RUN_LABEL,
            use_container_width=True,
            on_click=_prepare_another_run,
            args=(state,),
        )


def _html_text(value: Any, default: str = "Unknown") -> str:
    cleaned = str(value or "").strip() or default
    return html.escape(cleaned).replace("\n", "<br>")


def _html_list(items: list[str], default: str = "None listed") -> str:
    cleaned = [str(item or "").strip() for item in items if str(item or "").strip()]
    if not cleaned:
        return html.escape(default)
    return "<br>".join(html.escape(item) for item in cleaned)


def _html_link(value: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        return "Unknown"
    escaped = html.escape(cleaned)
    return f'<a href="{escaped}" target="_blank" rel="noreferrer">{escaped}</a>'


def _info_tile(
    label: str,
    value: Any,
    *,
    large: bool = False,
    accent: bool = False,
    raw_html: bool = False,
) -> str:
    classes = ["info-tile"]
    if accent:
        classes.append("accent")
    value_class = "info-value large" if large else "info-value"
    rendered_value = str(value or "") if raw_html else _html_text(value)
    return f"""
    <div class="{' '.join(classes)}">
        <div class="info-label">{html.escape(label)}</div>
        <div class="{value_class}">{rendered_value}</div>
    </div>
    """


def _selected_card_index(state: ProspectAnalysisState) -> int:
    if not state.prospect_cards:
        return 0
    try:
        selected = int(st.session_state.get("prospect_selected_card_index", 0))
    except (TypeError, ValueError):
        selected = 0
    return max(0, min(selected, len(state.prospect_cards) - 1))


def _render_new_card_selector(
    state: ProspectAnalysisState, *, key_suffix: str
) -> tuple[int | None, Any | None]:
    if not state.prospect_cards:
        return None, None
    selected_index = _selected_card_index(state)
    if len(state.prospect_cards) > 1:
        selected_index = st.selectbox(
            "Prospect card",
            options=list(range(len(state.prospect_cards))),
            index=selected_index,
            key=f"prospect_card_selector_{state.session_id}_{key_suffix}",
            format_func=lambda idx: f"{state.prospect_cards[idx].company} - {state.prospect_cards[idx].website}",
        )
    st.session_state["prospect_selected_card_index"] = selected_index
    card = state.prospect_cards[selected_index]
    state.prospect_card = card
    st.session_state["prospect_state"] = state
    return selected_index, card


def _write_field_value(label: str, value: Any = "N/A") -> None:
    st.caption(label)
    st.write(str(value or "N/A"))


def _render_empty_processing_structure() -> None:
    st.info("Processing has not started yet.")
    st.dataframe(
        [
            {
                "Time": "N/A",
                "Stage": "N/A",
                "Message": "N/A",
                "URL": "N/A",
            }
        ],
        use_container_width=True,
        hide_index=True,
    )


def _render_empty_output_structure() -> None:
    st.info("Output is not available yet.")
    with st.container(border=True):
        header_cols = st.columns([3, 1])
        with header_cols[0]:
            st.subheader("N/A")
            st.write("N/A")
        with header_cols[1]:
            st.metric("Fit Score", "N/A")
            st.write("N/A")

        summary_cols = st.columns(3)
        for index, label in enumerate(
            [
                "Category",
                "Country / Region",
                "Target Audience",
                "Contact Email",
                "Contact URL",
                "Confidence",
            ]
        ):
            with summary_cols[index % 3]:
                _write_field_value(label)

        detail_cols = st.columns(2)
        for index, label in enumerate(
            [
                "What They Do",
                "Services / Products",
                "Reason",
                "Why Relevant",
                "Pain Point",
                "Suggested Offer",
                "Recommended Contact",
                "Outreach Angle",
                "Next Step",
                "AI Reasoning Summary",
            ]
        ):
            with detail_cols[index % 2]:
                _write_field_value(label)

    list_cols = st.columns(3)
    for index, label in enumerate(
        ["Signals of Fit", "Missing Information", "Uncertainty Flags"]
    ):
        with list_cols[index]:
            _write_field_value(label)


def _render_empty_draft_structure() -> None:
    st.info("Draft data is not available yet.")
    st.text_input("Subject", value="N/A", disabled=True, key="empty_draft_subject")
    st.text_area("Body", value="N/A", disabled=True, height=180, key="empty_draft_body")
    st.text_input("CTA", value="N/A", disabled=True, key="empty_draft_cta")
    st.text_input(
        "Email to send to", value="N/A", disabled=True, key="empty_draft_recipient"
    )
    action_cols = st.columns(2)
    action_cols[0].button(
        "Send email and move forward",
        use_container_width=True,
        disabled=True,
        key="empty_draft_approve",
    )

    action_cols[1].button(
        "Skip and decide",
        use_container_width=True,
        disabled=True,
        key="empty_draft_skip",
    )


def _render_empty_decision_structure() -> None:
    st.info("Decision data is not available yet.")
    with st.container(border=True):
        preview_cols = st.columns(3)
        for index, label in enumerate(
            [
                "Organization",
                "Website",
                "Fit Score",
                "Fit Status",
                "Category",
                "Contact Email",
                "Recommended Contact",
                "Next Step",
                "Email Draft",
            ]
        ):
            with preview_cols[index % 3]:
                _write_field_value(label)
    st.text_area(
        "Decision note",
        value="N/A",
        disabled=True,
        height=80,
        key="empty_decision_note",
    )
    cols = st.columns(3)
    cols[0].button(
        "Approve CRM",
        use_container_width=True,
        disabled=True,
        key="empty_decision_approve",
    )
    cols[1].button(
        "Move to further review",
        use_container_width=True,
        disabled=True,
        key="empty_decision_review",
    )
    cols[2].button(
        "Move to reject",
        use_container_width=True,
        disabled=True,
        key="empty_decision_reject",
    )


def _render_output_card_details(card: Any) -> None:
    def value(text: Any, default: str = "Unknown") -> str:
        return str(text or "").strip() or default

    def write_field(label: str, text: Any) -> None:
        st.caption(label)
        st.write(value(text))

    with st.container(border=True):
        header_cols = st.columns([3, 1])
        with header_cols[0]:
            st.subheader(value(card.company))
            st.write(value(card.website, ""))
        with header_cols[1]:
            st.metric("Fit Score", f"{card.fit_score}/100")
            st.write(value(card.fit_status))

        summary_cols = st.columns(3)
        summary_fields = [
            ("Category", card.category),
            ("Country / Region", card.country_region),
            ("Target Audience", card.target_audience),
            ("Contact Email", card.contact_email or "Not detected"),
            ("Contact URL", card.contact_url),
            ("Confidence", card.confidence),
        ]
        for index, (label, text) in enumerate(summary_fields):
            with summary_cols[index % 3]:
                write_field(label, text)

        detail_cols = st.columns(2)
        detail_fields = [
            ("What They Do", card.what_they_do),
            ("Services / Products", card.services_products),
            ("Reason", card.reason),
            ("Why Relevant", card.why_relevant),
            ("Pain Point", card.pain_point),
            ("Suggested Offer", card.suggested_offer),
            ("Recommended Contact", card.recommended_contact_type),
            (
                "Outreach Angle",
                card.outreach_angle or card.possible_collaboration_angle,
            ),
            ("Next Step", card.next_step or card.recommended_action),
            ("AI Reasoning Summary", card.ai_reasoning_summary or card.ai_summary),
        ]
        for index, (label, text) in enumerate(detail_fields):
            with detail_cols[index % 2]:
                write_field(label, text)

    list_cols = st.columns(3)
    list_fields = [
        ("Signals of Fit", card.signals_of_fit),
        ("Missing Information", card.missing_information),
        ("Uncertainty Flags", card.uncertainty_flags),
    ]
    for index, (label, items) in enumerate(list_fields):
        with list_cols[index]:
            st.caption(label)
            cleaned = [
                str(item or "").strip() for item in items if str(item or "").strip()
            ]
            st.write("\n".join(f"- {item}" for item in cleaned) or "None listed")

    with st.expander("Pages scraped", expanded=False):
        st.write("\n".join(f"- {page}" for page in card.pages_scraped) or "None listed")


def _snippet_value(snippet: Any, field_name: str, default: Any = "") -> Any:
    if isinstance(snippet, dict):
        return snippet.get(field_name, default)
    return getattr(snippet, field_name, default)


def _evidence_snippets_by_id(card: Any) -> dict[str, Any]:
    snippets = getattr(card, "evidence_snippets", []) or []
    return {
        str(_snippet_value(snippet, "evidence_id") or "").strip(): snippet
        for snippet in snippets
        if str(_snippet_value(snippet, "evidence_id") or "").strip()
    }


def _format_confidence_percent(value: Any) -> str:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence <= 1:
        confidence *= 100
    return f"{max(0, min(confidence, 100)):.0f}%"


def _field_claim_text(card: Any, field_name: str) -> str:
    if field_name == "signals_of_fit":
        return (
            "; ".join(getattr(card, "signals_of_fit", []) or []) or "No signal listed"
        )
    if field_name == "risk_uncertainty_flags":
        return (
            "; ".join(getattr(card, "risk_uncertainty_flags", []) or [])
            or "No risk listed"
        )
    return str(getattr(card, field_name, "") or "").strip() or "N/A"


def _field_label(field_name: str) -> str:
    labels = {
        "category": "Category",
        "target_audience": "Audience",
        "what_they_do": "What They Do",
        "services_products": "Services / Products",
        "signals_of_fit": "Signals of Fit",
        "risk_uncertainty_flags": "Risk / Uncertainty",
        "suggested_offer": "Suggested Offer",
        "outreach_angle": "Outreach Angle",
        "contact_email": "Contact Email",
        "only_homepage_available": "Only Homepage Available",
        "some_pages_blocked": "Some Pages Blocked",
    }
    return labels.get(field_name, str(field_name).replace("_", " ").title())


def _render_evidence_reasoning(card: Any, state: ProspectAnalysisState | None) -> None:
    st.subheader("Evidence & Reasoning")
    snippets_by_id = _evidence_snippets_by_id(card)
    field_map = getattr(card, "field_evidence_map", {}) or {}
    weak_fields = list(getattr(card, "weak_evidence_fields", []) or [])
    warnings: list[str] = []
    if weak_fields:
        warnings.append("Weak evidence")
        warnings.append("Needs human review")
    if not str(getattr(card, "contact_email", "") or "").strip():
        warnings.append("No contact email found")
    if state is not None and len(getattr(state, "scraped_pages", []) or []) <= 1:
        warnings.append("Only homepage was available")
    if state is not None and any(
        getattr(page, "blocked", False)
        for page in getattr(state, "skipped_pages", []) or []
    ):
        warnings.append("Some pages were blocked")
    if warnings:
        st.warning(" | ".join(dict.fromkeys(warnings)))

    fields = [
        "category",
        "target_audience",
        "what_they_do",
        "services_products",
        "signals_of_fit",
        "risk_uncertainty_flags",
        "suggested_offer",
        "outreach_angle",
    ]
    cols = st.columns(2)
    for index, field_name in enumerate(fields):
        evidence_ids = list(field_map.get(field_name, []) or [])
        evidence = [
            snippets_by_id[evidence_id]
            for evidence_id in evidence_ids
            if evidence_id in snippets_by_id
        ]
        with cols[index % 2]:
            with st.container(border=True):
                st.caption(_field_label(field_name))
                st.write(_field_claim_text(card, field_name))
                if evidence:
                    snippet = evidence[0]
                    st.markdown(f"**Evidence:** \"{_snippet_value(snippet, 'quote')}\"")
                    source_url = str(
                        _snippet_value(snippet, "source_url") or ""
                    ).strip()
                    if source_url:
                        st.markdown(f"Source: [{source_url}]({source_url})")
                    st.caption(
                        f"Confidence: {_format_confidence_percent(_snippet_value(snippet, 'confidence'))} | Direct evidence"
                    )
                else:
                    st.caption(
                        "No direct evidence found. Inference based on available content."
                    )

    all_rows = [
        {
            "Quote": _snippet_value(snippet, "quote"),
            "Source URL": _snippet_value(snippet, "source_url"),
            "Supports Field": _field_label(
                str(_snippet_value(snippet, "supports_field") or "")
            ),
            "Confidence": _format_confidence_percent(
                _snippet_value(snippet, "confidence")
            ),
        }
        for snippet in snippets_by_id.values()
    ]
    with st.expander("View all evidence snippets", expanded=False):
        st.dataframe(
            all_rows
            or [
                {
                    "Quote": "No direct evidence found",
                    "Source URL": "N/A",
                    "Supports Field": "N/A",
                    "Confidence": "N/A",
                }
            ],
            use_container_width=True,
            hide_index=True,
        )


def _render_unavailable_step(title: str, message: str) -> None:
    st.subheader(title)
    st.warning(message)
    cols = st.columns(2)
    with cols[0]:
        _step_button("Go to Criteria", 1, key=f"{title}_inputs")
    with cols[1]:
        _step_button("Go to processing", 2, key=f"{title}_processing")


def _render_input_step(settings: ProspectSettings) -> None:
    st.subheader("Step 1 - Criteria")
    _render_input(settings)
    st.info(
        "Enter one website, define the matching criteria, then start the AI analysis."
    )
    if st.button(
        RUN_BUTTON_LABEL,
        type="primary",
        use_container_width=True,
        disabled=bool(st.session_state.get("prospect_analysis_running", False)),
    ):
        _start_analysis(settings)


def _render_processing_step(
    settings: ProspectSettings, state: ProspectAnalysisState | None
) -> None:
    st.subheader("Step 2 - Processing")
    is_running = bool(st.session_state.get("prospect_analysis_running", False))
    error = str(st.session_state.get("prospect_run_error") or "").strip()
    blocked_error = _latest_blocked_error(state)
    if is_running:
        st.info(
            "AI is scraping, reading, and building the prospect card. Live logs appear below."
        )
        _render_live_log_refresh(settings)
        return
    if isinstance(state, ProspectAnalysisState) and state.prospect_cards:
        st.success("Analysis is done. The prospect output is ready.")
        cols = st.columns(2)
        with cols[0]:
            _step_button(
                "Go to results", 3, key="processing_to_results", button_type="primary"
            )
        with cols[1]:
            _step_button("Go back", 1, key="processing_back")
        _render_progress_snapshot(state, settings)
        return
    if isinstance(state, ProspectAnalysisState) and blocked_error is not None:
        _show_blocked_public_cloud_message(state, blocked_error)
        cols = st.columns(2)
        with cols[0]:
            if st.button(
                "Run with cached demo content",
                type="primary",
                use_container_width=True,
                key="blocked_cached_demo",
            ):
                _start_cached_demo_analysis(settings, state)
        with cols[1]:
            _step_button("Go back to Criteria", 1, key="blocked_back_to_inputs")
        _render_scrape_explainability(state)
        if state.ai_step_logs:
            _render_step_log_table(state.ai_step_logs, auto_scroll=True, height=360)
        return
    if error:
        st.error(f"Analysis stopped: {error}")
        cols = st.columns(2)
        with cols[0]:
            _step_button("Go back to Criteria", 1, key="processing_error_back")
        with cols[1]:
            if st.button(
                "Run with cached demo content",
                use_container_width=True,
                key="processing_error_cached_demo",
            ):
                _start_cached_demo_analysis(settings, state)
        if isinstance(state, ProspectAnalysisState):
            _render_scrape_explainability(state)
        if isinstance(state, ProspectAnalysisState) and state.ai_step_logs:
            _render_step_log_table(state.ai_step_logs, auto_scroll=True, height=360)
        return
    if isinstance(state, ProspectAnalysisState) and state.ai_step_logs:
        st.warning("The latest run has logs, but no prospect output was created yet.")
        _render_scrape_explainability(state)
        _render_step_log_table(state.ai_step_logs, auto_scroll=True, height=360)
        return
    st.warning("No analysis is running yet. Start from the Criteria step.")
    _render_empty_processing_structure()
    _step_button("Go to Criteria", 1, key="processing_empty_inputs")


def _render_output_step(state: ProspectAnalysisState | None) -> None:
    if not isinstance(state, ProspectAnalysisState) or not state.prospect_cards:
        st.subheader("Step 3 - Output")
        _render_empty_output_structure()
        return
    st.subheader("Step 3 - Output")
    _, card = _render_new_card_selector(state, key_suffix="output")
    if card is None:
        return
    _render_output_card_details(card)
    _render_evidence_reasoning(card, state)
    _render_scrape_explainability(state)
    cols = st.columns(3)
    with cols[0]:
        _step_button("Draft an email", 4, key="output_to_draft", button_type="primary")
    with cols[1]:
        if st.button(
            "Skip email and decide", use_container_width=True, key="output_skip_email"
        ):
            state.outreach_draft = {}
            st.session_state["prospect_state"] = state
            _go_to_step(5)
    with cols[2]:
        _step_button("Back to Criteria", 1, key="output_back_inputs")


def _generate_draft(
    settings: ProspectSettings,
    state: ProspectAnalysisState,
    card: Any,
    *,
    instruction: str = "",
) -> None:
    analyzer = ProspectAIAnalyzer(
        settings, get_prospect_logger(settings.prospect_log_dir)
    )
    state.outreach_draft = analyzer.generate_outreach_draft(
        state,
        card=card,
        regenerate_instruction=instruction,
    )
    _record_draft_generated(settings, state, card.website)
    st.session_state["prospect_state"] = state


def _render_draft_fields(state: ProspectAnalysisState, *, selected_index: int) -> None:
    draft = state.outreach_draft or {}
    st.text_input(
        "Subject",
        value=draft.get("subject", ""),
        disabled=True,
    )
    st.text_area(
        "Body",
        value=draft.get("body", ""),
        height=230,
        disabled=True,
    )
    st.text_input(
        "CTA",
        value=draft.get("cta", ""),
        disabled=True,
    )


def _draft_recipient_key(state: ProspectAnalysisState, selected_index: int) -> str:
    return f"draft_recipient_{state.session_id}_{selected_index}"


def _send_draft_and_move_forward(
    settings: ProspectSettings,
    state: ProspectAnalysisState,
    card: Any,
    *,
    recipient_key: str,
) -> None:
    recipient_email = str(st.session_state.get(recipient_key) or "").strip()
    if not recipient_email:
        st.warning("Enter the email address you want to send this draft to.")
        return
    handler = ProspectDecisionHandler(settings)
    with st.spinner("Sending email..."):
        result = handler.send_draft_only(
            state, card=card, recipient_email=recipient_email
        )
    st.session_state["prospect_state"] = state
    st.session_state["prospect_flash"] = (
        "success" if result.success else "error",
        result.message,
    )
    if result.success:
        _go_to_step(5)
        return
    st.rerun()


def _render_draft_step(
    settings: ProspectSettings, state: ProspectAnalysisState | None
) -> None:
    if not isinstance(state, ProspectAnalysisState) or not state.prospect_cards:
        st.subheader("Step 4 - Generate draft")
        _render_empty_draft_structure()
        return
    st.subheader("Step 4 - Generate draft")
    selected_index, card = _render_new_card_selector(state, key_suffix="draft")
    if card is None or selected_index is None:
        return
    if not _has_draft(state):
        st.info("Generating the first outreach draft for this prospect.")
        with st.spinner("Generating one-time email draft..."):
            _generate_draft(settings, state, card)
        st.rerun()

    _render_draft_fields(state, selected_index=selected_index)
    draft = state.outreach_draft or {}
    recipient_key = _draft_recipient_key(state, selected_index)
    if recipient_key not in st.session_state:
        st.session_state[recipient_key] = str(
            draft.get("recipient_email") or card.contact_email or ""
        ).strip()
    st.text_input(
        "Email to send to",
        key=recipient_key,
        placeholder="recipient@example.com",
    )
    if draft.get("sent_at") and draft.get("recipient_email"):
        st.success(f"Email already sent to {draft.get('recipient_email')}.")
    action_cols = st.columns(2)
    with action_cols[0]:
        if st.button(
            "Send email and move forward",
            type="primary",
            use_container_width=True,
            key=f"draft_send_{state.session_id}_{selected_index}",
        ):
            _send_draft_and_move_forward(
                settings, state, card, recipient_key=recipient_key
            )
    with action_cols[1]:
        if st.button(
            "Skip and decide",
            use_container_width=True,
            key=f"draft_skip_{state.session_id}",
        ):
            state.outreach_draft = {}
            st.session_state["prospect_state"] = state
            _go_to_step(5)

    st.divider()
    st.subheader("Regenerate draft")
    regenerate_instruction = st.text_area(
        "Regeneration note",
        key=f"regen_instruction_{state.session_id}_{selected_index}",
        placeholder="Tell the AI what to change in the next draft.",
        height=90,
    )
    if st.button(
        "Regenerate with note",
        use_container_width=True,
        key=f"regen_{state.session_id}_{selected_index}",
    ):
        with st.spinner("Regenerating draft..."):
            _generate_draft(settings, state, card, instruction=regenerate_instruction)
        st.rerun()


def _decision_preview_html(state: ProspectAnalysisState, card: Any) -> str:
    draft = state.outreach_draft or {}
    draft_html = (
        f"""
        {_info_tile("Subject", draft.get("subject", ""))}
        {_info_tile("Body", _html_text(draft.get("body", "No email draft was generated."), default="No email draft was generated."), raw_html=True)}
        {_info_tile("CTA", draft.get("cta", "No CTA generated."))}
        {_info_tile("Recipient Email", draft.get("recipient_email", "Not sent yet"))}
        {_info_tile("Email Status", draft.get("sent_status", "Not sent yet"))}

        """
        if _has_draft(state)
        else _info_tile("Email Draft", "No email draft was generated.")
    )
    return f"""
    <style>
        body {{
            margin: 0;
            font-family: Manrope, Arial, sans-serif;
            color: #2c628a;
            background: transparent;
        }}
        .preview-scrollbox {{
            max-height: 430px;
            overflow-y: auto;
            border: 1px solid #dbe3ef;
            border-radius: 8px;
            background: #ffffff;
            padding: 14px;
            box-sizing: border-box;
        }}
        .preview-section {{
            border-bottom: 1px solid #edf0f5;
            padding-bottom: 12px;
            margin-bottom: 12px;
        }}
        .preview-section:last-child {{
            border-bottom: 0;
            margin-bottom: 0;
            padding-bottom: 0;
        }}
        .prospect-company {{
            font-size: 20px;
            line-height: 1.2;
            font-weight: 800;
        }}
        .prospect-url {{
            color: #64748b;
            font-size: 13px;
            margin-top: 3px;
            word-break: break-word;
        }}
        .prospect-url a {{
            color: #2563eb;
        }}
        .output-summary-grid,
        .output-detail-grid {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px;
        }}
        .output-summary-grid {{
            grid-template-columns: repeat(3, minmax(0, 1fr));
        }}
        .info-tile {{
            border: 1px solid #e3e7ee;
            border-radius: 8px;
            background: #ffffff;
            padding: 12px;
            box-sizing: border-box;
        }}
        .info-tile.accent {{
            border-color: #bfdbfe;
            background: #f8fbff;
        }}
        .info-label {{
            color: #64748b;
            font-size: 11px;
            font-weight: 800;
            text-transform: uppercase;
        }}
        .info-value {{
            color: #2c628a;
            font-size: 13px;
            line-height: 1.45;
            margin-top: 4px;
            word-break: break-word;
            white-space: normal;
        }}
        .info-value.large {{
            font-size: 18px;
            font-weight: 800;
        }}
        @media (max-width: 680px) {{
            .output-summary-grid,
            .output-detail-grid {{
                grid-template-columns: 1fr;
            }}
        }}
    </style>
    <div class="preview-scrollbox">
        <div class="preview-section">
            <div class="prospect-company">{_html_text(card.company)}</div>
            <div class="prospect-url">{_html_link(card.website)}</div>
        </div>
        <div class="preview-section">
            <div class="output-summary-grid">
                {_info_tile("Fit Score", f"{card.fit_score}/100", large=True, accent=True)}
                {_info_tile("Fit Status", card.fit_status)}
                {_info_tile("Category", card.category)}
                {_info_tile("Contact Email", card.contact_email or "Not detected")}
                {_info_tile("Recommended Contact", card.recommended_contact_type)}
                {_info_tile("Next Step", card.next_step or card.recommended_action)}
            </div>
        </div>
        <div class="preview-section">
            <div class="output-detail-grid">
                {_info_tile("Reason", card.reason)}
                {_info_tile("Why Relevant", card.why_relevant)}
                {_info_tile("Suggested Offer", card.suggested_offer)}
                {_info_tile("Risk / Uncertainty", _html_list(card.uncertainty_flags), raw_html=True)}
            </div>
        </div>
        <div class="preview-section">
            <div class="output-detail-grid">{draft_html}</div>
        </div>
    </div>
    """


def _decrement_last_decision_metric() -> None:
    metric_key = str(st.session_state.get("prospect_last_decision_metric_key") or "")
    local = st.session_state.get("prospect_local_metrics", {})
    if metric_key in local and local[metric_key] > 0:
        local[metric_key] -= 1
    st.session_state["prospect_last_decision_metric_key"] = ""


def _clear_decision_state(state: ProspectAnalysisState) -> None:
    state.completed_at = ""
    state.human_decision = ""
    state.crm_stage = ""
    state.google_sheet_status = ""


def _undo_decision(settings: ProspectSettings, state: ProspectAnalysisState) -> None:
    _decrement_last_decision_metric()
    _clear_decision_state(state)
    step = state.log_step(
        "Decision reopened in dashboard", stage="decision", url=state.current_url
    )
    logger = StateJSONLLogger(settings.prospect_log_dir)
    logger.write_step(state, step)
    logger.write_state_snapshot(state)
    st.session_state["prospect_state"] = state
    st.session_state["prospect_flash"] = (
        "warning",
        "Decision reopened locally. Existing Google Sheets rows are not removed automatically.",
    )
    _set_workflow_step(5)
    st.rerun()


def _run_reanalysis(
    settings: ProspectSettings,
    state: ProspectAnalysisState,
    card: Any,
    instruction: str,
) -> None:
    instruction = instruction.strip()
    if not instruction:
        st.warning("Add a re-analysis note before running it.")
        return
    if state.completed_at:
        _decrement_last_decision_metric()
        _clear_decision_state(state)
    flow = ProspectFlow(settings, get_prospect_logger(settings.prospect_log_dir))
    st.session_state["prospect_analysis_running"] = True

    def on_progress(updated_state: ProspectAnalysisState) -> None:
        st.session_state["prospect_state"] = updated_state

    try:
        with st.spinner("Re-analyzing prospect..."):
            updated = flow.reanalyze(
                state=state,
                website=card.website,
                reanalysis_instruction=instruction,
                progress_callback=on_progress,
                timeout_seconds=settings.analysis_timeout_seconds,
            )
    except TimeoutError:
        timeout_seconds = int(settings.analysis_timeout_seconds)
        message = (
            f"The re-analysis took longer than {timeout_seconds} seconds, so I stopped it cleanly. "
            "Please try a shorter note or start a new process."
        )
        error = state.add_error(
            stage="Processing",
            message=message,
            details={
                "error_type": "Timeout",
                "error_message": message,
                "resolved": "No",
            },
        )
        logger = StateJSONLLogger(settings.prospect_log_dir)
        logger.write_error(error)
        if state.ai_step_logs:
            logger.write_step(state, state.ai_step_logs[-1])
        logger.write_state_snapshot(state)
        st.session_state["prospect_state"] = state
        st.session_state["prospect_flash"] = ("error", message)
        _set_workflow_step(5)
        st.rerun()
        return
    finally:
        st.session_state["prospect_analysis_running"] = False
    st.session_state["prospect_state"] = updated
    st.session_state["prospect_flash"] = (
        "success",
        "Re-analysis completed. Review the updated output.",
    )
    _set_workflow_step(3)
    st.rerun()


def _render_post_decision_actions(
    settings: ProspectSettings,
    state: ProspectAnalysisState,
    card: Any,
    selected_index: int,
) -> None:
    decision = state.human_decision or "Decision recorded"
    st.success(f"{decision}. The workflow is complete.")
    cols = st.columns(3)
    with cols[0]:
        if st.button(
            "Undo decision step",
            use_container_width=True,
            key=f"undo_decision_{state.session_id}_{selected_index}",
        ):
            _undo_decision(settings, state)
    with cols[1]:
        if settings.google_sheet_id:
            sheet_url = f"https://docs.google.com/spreadsheets/d/{settings.google_sheet_id}/edit"
            st.link_button("Open CRM Sheet", sheet_url, use_container_width=True)
        else:
            st.button(
                "CRM Sheet not configured", use_container_width=True, disabled=True
            )
    with cols[2]:
        if st.button(
            ANOTHER_RUN_LABEL,
            use_container_width=True,
            key=f"new_process_{state.session_id}_{selected_index}",
        ):
            _prepare_another_run(state)
            st.rerun()
    reanalysis_instruction = st.text_area(
        "Re-analysis note",
        key=f"post_decision_reanalysis_{state.session_id}_{selected_index}",
        placeholder="Tell the AI what to reconsider before producing a new output.",
        height=90,
    )
    if st.button(
        "Re-analyze with note",
        use_container_width=True,
        key=f"post_decision_reanalyze_{state.session_id}_{selected_index}",
    ):
        _run_reanalysis(settings, state, card, reanalysis_instruction)


def _render_decision_step(
    settings: ProspectSettings, state: ProspectAnalysisState | None
) -> None:
    if not isinstance(state, ProspectAnalysisState) or not state.prospect_cards:
        st.subheader("Step 5 - Decision")
        _render_empty_decision_structure()
        return
    st.subheader("Step 5 - Decision")
    selected_index, card = _render_new_card_selector(state, key_suffix="decision")
    if card is None or selected_index is None:
        return
    components.html(_decision_preview_html(state, card), height=462, scrolling=False)
    if state.completed_at:
        _render_post_decision_actions(settings, state, card, selected_index)
        return

    handler = ProspectDecisionHandler(settings)
    notes = st.text_area(
        "Decision note",
        key=f"decision_note_{state.session_id}_{selected_index}",
        height=90,
    )
    cols = st.columns(3)
    if cols[0].button(
        "Approve CRM",
        type="primary",
        use_container_width=True,
        key=f"approve_{state.session_id}_{selected_index}",
    ):
        result = handler.approve_to_crm(state, card=card, notes=notes)
        _finish_clicked_action(state, result, metric_key="approved_prospects")
    if cols[1].button(
        "Move to further review",
        use_container_width=True,
        key=f"review_{state.session_id}_{selected_index}",
    ):
        result = handler.move_to_further_review(state, card=card, notes=notes)
        _finish_clicked_action(state, result, metric_key="further_review")
    if cols[2].button(
        "Move to reject",
        use_container_width=True,
        key=f"reject_{state.session_id}_{selected_index}",
    ):
        result = handler.reject(state, card=card, notes=notes)
        _finish_clicked_action(
            state, result, metric_key="rejected_prospects", success_level="warning"
        )


def _render_current_step(
    settings: ProspectSettings, state: ProspectAnalysisState | None
) -> None:
    current = _current_step()
    if current == 1:
        _render_input_step(settings)
    elif current == 2:
        _render_processing_step(settings, state)
    elif current == 3:
        _render_output_step(state)
    elif current == 4:
        _render_draft_step(settings, state)
    elif current == 5:
        _render_decision_step(settings, state)


def main() -> None:
    settings = load_prospect_settings(_repo_root())
    st.set_page_config(page_title=PAGE_TITLE, layout="wide")
    if hasattr(st, "html"):
        st.html(CSS)
    else:
        st.markdown(CSS, unsafe_allow_html=True)
    _init_session()
    _sync_step_from_query_params()
    _sync_active_run_from_registry()
    _render_header()
    state = st.session_state.get("prospect_state")
    state_for_metrics = state if isinstance(state, ProspectAnalysisState) else None
    _render_workflow_progress(state_for_metrics)
    _render_env_status(settings)
    _render_flash_message()
    state = st.session_state.get("prospect_state")
    _render_current_step(
        settings, state if isinstance(state, ProspectAnalysisState) else None
    )
    st.divider()
    state = st.session_state.get("prospect_state")
    _render_metrics(
        settings, state if isinstance(state, ProspectAnalysisState) else None
    )


if __name__ == "__main__":
    main()
