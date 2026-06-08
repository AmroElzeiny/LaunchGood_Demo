from __future__ import annotations

import asyncio
import html
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv

from job_bot.human_review_queue import (
    approve_review_item,
    build_human_review_presentation,
    neglect_review_item,
)
from job_bot.storage import StateStore
from job_bot.subscriber_notifier import SubscriberNotifier
from job_bot.telegram_subscription_store import SubscriptionStore


def _safe_scalar(conn: sqlite3.Connection, sql: str, params: tuple = (), default: float = 0) -> float:
    try:
        row = conn.execute(sql, params).fetchone()
        if row is None or row[0] is None:
            return default
        return float(row[0])
    except sqlite3.Error:
        return default


def _safe_frame(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> pd.DataFrame:
    try:
        return pd.read_sql_query(sql, conn, params=params)
    except Exception:
        return pd.DataFrame()


def _load_metrics(state_db: Path, subs_db: Path) -> list[tuple[str, str]]:
    now = datetime.now(timezone.utc)
    day_ago = (now - timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
    week_ago = (now - timedelta(days=7)).isoformat(timespec="seconds").replace("+00:00", "Z")
    month_ago = (now - timedelta(days=30)).isoformat(timespec="seconds").replace("+00:00", "Z")

    with sqlite3.connect(state_db) as state_conn, sqlite3.connect(subs_db) as subs_conn:
        metrics = {
            "Users overall": _safe_scalar(subs_conn, "SELECT COUNT(DISTINCT user_id) FROM subscriptions"),
            "Users active": _safe_scalar(subs_conn, "SELECT COUNT(*) FROM subscriptions WHERE is_active = 1"),
            "Users expired": _safe_scalar(subs_conn, "SELECT COUNT(*) FROM subscriptions WHERE is_active = 0"),
            "New users 24h": _safe_scalar(
                subs_conn,
                "SELECT COUNT(*) FROM subscriptions WHERE updated_at_utc >= ?",
                (day_ago,),
            ),
            "New users 7d": _safe_scalar(
                subs_conn,
                "SELECT COUNT(*) FROM subscriptions WHERE updated_at_utc >= ?",
                (week_ago,),
            ),
            "Daily active users": _safe_scalar(
                subs_conn,
                "SELECT COUNT(DISTINCT user_id) FROM filter_audit_logs WHERE created_at_utc >= ?",
                (day_ago,),
            ),
            "Weekly active users": _safe_scalar(
                subs_conn,
                "SELECT COUNT(DISTINCT user_id) FROM filter_audit_logs WHERE created_at_utc >= ?",
                (week_ago,),
            ),
            "Invoices created": _safe_scalar(subs_conn, "SELECT COUNT(*) FROM payment_sessions"),
            "Invoices 24h": _safe_scalar(
                subs_conn,
                "SELECT COUNT(*) FROM payment_sessions WHERE created_at_utc >= ?",
                (day_ago,),
            ),
            "Invoices paid": _safe_scalar(
                subs_conn,
                "SELECT COUNT(*) FROM payment_sessions WHERE status IN ('finished', 'confirmed')",
            ),
            "Invoices pending": _safe_scalar(
                subs_conn,
                "SELECT COUNT(*) FROM payment_sessions WHERE status NOT IN ('finished', 'confirmed', 'failed', 'expired', 'cancelled')",
            ),
            "Invoices failed/expired": _safe_scalar(
                subs_conn,
                "SELECT COUNT(*) FROM payment_sessions WHERE status IN ('failed', 'expired', 'cancelled')",
            ),
            "Notifications sent": _safe_scalar(
                subs_conn,
                "SELECT COUNT(*) FROM notification_queue WHERE status = 'sent'",
            ),
            "Notifications sent 24h": _safe_scalar(
                subs_conn,
                "SELECT COUNT(*) FROM notification_queue WHERE status = 'sent' AND sent_at_utc >= ?",
                (day_ago,),
            ),
            "Notifications queued": _safe_scalar(
                subs_conn,
                "SELECT COUNT(*) FROM notification_queue WHERE status = 'queued'",
            ),
            "Notifications cancelled": _safe_scalar(
                subs_conn,
                "SELECT COUNT(*) FROM notification_queue WHERE status = 'cancelled'",
            ),
            "Match deliveries queued": _safe_scalar(
                subs_conn,
                "SELECT COUNT(*) FROM delivery_job_queue WHERE status IN ('queued', 'processing')",
            ),
            "Avg delivery delay (s)": _safe_scalar(
                subs_conn,
                """
                SELECT AVG(strftime('%s', completed_at_utc) - strftime('%s', created_at_utc))
                FROM delivery_job_queue
                WHERE status = 'completed'
                  AND COALESCE(completed_at_utc, '') != ''
                """,
            ),
            "Avg match time (s)": _safe_scalar(
                subs_conn,
                """
                SELECT AVG(strftime('%s', completed_at_utc) - strftime('%s', started_processing_at_utc))
                FROM delivery_job_queue
                WHERE status = 'completed'
                  AND COALESCE(completed_at_utc, '') != ''
                  AND COALESCE(started_processing_at_utc, '') != ''
                """,
            ),
            "Opportunities saved": _safe_scalar(state_conn, "SELECT COUNT(*) FROM seen_links WHERE status = 'job_saved'"),
            "Posts neglected": _safe_scalar(state_conn, "SELECT COUNT(*) FROM seen_links WHERE neglected = 1"),
            "Posts fetch-failed": _safe_scalar(state_conn, "SELECT COUNT(*) FROM seen_links WHERE status = 'fetch_failed'"),
            "Posts queued review": _safe_scalar(
                state_conn,
                "SELECT COUNT(*) FROM seen_links WHERE status = 'queued_human_review'",
            ),
            "Cycles overall": _safe_scalar(state_conn, "SELECT COUNT(*) FROM cycle_stats"),
            "Cycle errors total": _safe_scalar(state_conn, "SELECT COALESCE(SUM(errors), 0) FROM cycle_stats"),
            "Avg new cards/cycle": _safe_scalar(
                state_conn,
                "SELECT COALESCE(AVG(new_cards), 0) FROM cycle_stats",
            ),
            "Avg fetched pages/cycle": _safe_scalar(
                state_conn,
                "SELECT COALESCE(AVG(fetched_pages), 0) FROM cycle_stats",
            ),
            "Reviews pending": _safe_scalar(
                state_conn,
                "SELECT COUNT(*) FROM human_review_queue WHERE status = 'pending'",
            ),
            "Reviews approved+sent": _safe_scalar(
                state_conn,
                "SELECT COUNT(*) FROM human_review_queue WHERE status = 'approved_sent'",
            ),
            "Reviews approved no-send": _safe_scalar(
                state_conn,
                "SELECT COUNT(*) FROM human_review_queue WHERE status = 'approved_no_send'",
            ),
            "Reviews neglected": _safe_scalar(
                state_conn,
                "SELECT COUNT(*) FROM human_review_queue WHERE status = 'neglected'",
            ),
            "AI extraction timeouts": _safe_scalar(
                state_conn,
                "SELECT metric_value FROM runtime_metrics WHERE metric_key = 'ai.extraction.timeouts'",
            ),
            "AI retries": _safe_scalar(
                state_conn,
                "SELECT COALESCE(SUM(metric_value), 0) FROM runtime_metrics WHERE metric_key LIKE 'ai.%.retries'",
            ),
            "Fetch timeouts": _safe_scalar(
                state_conn,
                "SELECT metric_value FROM runtime_metrics WHERE metric_key = 'fetch.timeouts'",
            ),
            "Fetch retries": _safe_scalar(
                state_conn,
                "SELECT metric_value FROM runtime_metrics WHERE metric_key = 'fetch.retries'",
            ),
            "Fetch rate limits": _safe_scalar(
                state_conn,
                "SELECT metric_value FROM runtime_metrics WHERE metric_key = 'fetch.rate_limits'",
            ),
            "Zero-site cycles": _safe_scalar(
                state_conn,
                "SELECT metric_value FROM runtime_metrics WHERE metric_key = 'zero_site_cycles'",
            ),
            "Zero-site cycles consecutive": _safe_scalar(
                state_conn,
                "SELECT metric_value FROM runtime_metrics WHERE metric_key = 'zero_site_cycles_consecutive'",
            ),
            "Rejected country restriction 24h": _safe_scalar(
                subs_conn,
                "SELECT COUNT(*) FROM filter_mismatch_events WHERE reason_code = 'country_restriction' AND created_at_utc >= ?",
                (day_ago,),
            ),
            "Rejected nationality/citizenship 24h": _safe_scalar(
                subs_conn,
                "SELECT COUNT(*) FROM filter_mismatch_events WHERE reason_code = 'nationality_or_citizenship_restriction' AND created_at_utc >= ?",
                (day_ago,),
            ),
            "Rejected non-remote 24h": _safe_scalar(
                subs_conn,
                "SELECT COUNT(*) FROM filter_mismatch_events WHERE reason_code = 'non_remote' AND created_at_utc >= ?",
                (day_ago,),
            ),
            "Rejected source mismatch 24h": _safe_scalar(
                subs_conn,
                "SELECT COUNT(*) FROM filter_mismatch_events WHERE reason_code = 'source_mismatch' AND created_at_utc >= ?",
                (day_ago,),
            ),
            "Budget visibility mismatches 24h": _safe_scalar(
                subs_conn,
                "SELECT COUNT(*) FROM filter_mismatch_events WHERE reason_code = 'salary_policy' AND created_at_utc >= ?",
                (day_ago,),
            ),
            "Project filter mismatches 24h": _safe_scalar(
                subs_conn,
                "SELECT COUNT(*) FROM filter_mismatch_events WHERE reason_code = 'role_mismatch' AND created_at_utc >= ?",
                (day_ago,),
            ),
            "Watchdog stage timeouts": _safe_scalar(
                state_conn,
                "SELECT metric_value FROM runtime_metrics WHERE metric_key = 'watchdog.stage_timeouts'",
            ),
            "Watchdog hard timeouts": _safe_scalar(
                state_conn,
                "SELECT metric_value FROM runtime_metrics WHERE metric_key = 'watchdog.hard_timeouts'",
            ),
            "Filter updates 30d": _safe_scalar(
                subs_conn,
                "SELECT COUNT(*) FROM filter_audit_logs WHERE created_at_utc >= ?",
                (month_ago,),
            ),
            "Sources selected": _safe_scalar(subs_conn, "SELECT COUNT(*) FROM user_websites"),
            "Keywords saved": _safe_scalar(subs_conn, "SELECT COUNT(*) FROM user_keywords"),
            "Specialties selected": _safe_scalar(subs_conn, "SELECT COUNT(*) FROM user_spheres"),
        }

        invoices_total = metrics["Invoices created"]
        conversion = (metrics["Invoices paid"] / invoices_total * 100.0) if invoices_total > 0 else 0.0
        metrics["Invoice conversion"] = conversion

        ordered = []
        for key, value in metrics.items():
            if "conversion" in key.lower():
                ordered.append((key, f"{value:.1f}%"))
            elif float(value).is_integer():
                ordered.append((key, f"{int(value):,}"))
            else:
                ordered.append((key, f"{value:,.2f}"))
        return ordered


def _load_crawler_health(state_db: Path) -> dict[str, str]:
    with sqlite3.connect(state_db) as conn:
        try:
            row = conn.execute(
                """
                SELECT severity, state, reason, details_json, updated_at_utc
                FROM crawler_health_state
                WHERE health_key = 'crawler'
                LIMIT 1
                """
            ).fetchone()
        except sqlite3.Error:
            row = None
    if row is None:
        return {
            "severity": "info",
            "state": "unknown",
            "reason": "No crawler health state has been recorded yet.",
            "details_json": "{}",
            "updated_at_utc": "",
        }
    return {
        "severity": str(row[0] or "info"),
        "state": str(row[1] or "unknown"),
        "reason": str(row[2] or ""),
        "details_json": str(row[3] or "{}"),
        "updated_at_utc": str(row[4] or ""),
    }


def _load_fetch_strategy_stats(state_db: Path) -> pd.DataFrame:
    with sqlite3.connect(state_db) as conn:
        return _safe_frame(
            conn,
            """
            SELECT
                domain,
                strategy,
                success_count,
                failure_count,
                timeout_count,
                rate_limit_count,
                retry_count,
                ROUND(
                    CASE
                        WHEN (success_count + failure_count) > 0 THEN total_latency_ms / (success_count + failure_count)
                        ELSE 0
                    END,
                    1
                ) AS avg_latency_ms
            FROM fetch_strategy_stats
            ORDER BY (success_count + failure_count) DESC, domain ASC, strategy ASC
            LIMIT 20
            """,
        )


def _load_charts(state_db: Path, subs_db: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    with sqlite3.connect(subs_db) as subs_conn, sqlite3.connect(state_db) as state_conn:
        sent_by_day = _safe_frame(
            subs_conn,
            """
            SELECT substr(sent_at_utc, 1, 10) AS day, COUNT(*) AS sent_count
            FROM notification_queue
            WHERE status = 'sent' AND sent_at_utc IS NOT NULL
            GROUP BY day
            ORDER BY day
            """,
        )
        plan_split = _safe_frame(
            subs_conn,
            """
            SELECT plan, COUNT(*) AS users_count
            FROM subscriptions
            WHERE is_active = 1
            GROUP BY plan
            ORDER BY users_count DESC
            """,
        )
        cycle_cards = _safe_frame(
            state_conn,
            """
            SELECT substr(cycle_utc, 1, 10) AS day, SUM(new_cards) AS cards_count
            FROM cycle_stats
            GROUP BY day
            ORDER BY day
            """,
        )
        return sent_by_day, plan_split, cycle_cards


def _enqueue_action_send(item, state_store: StateStore, subs_db: Path, bot_token: str, reviewer: str, notes: str) -> str:
    logger = logging.getLogger("dashboard-review")
    subs_store = SubscriptionStore(subs_db)
    try:
        subscriber_notifier = None
        if bot_token.strip():
            subscriber_notifier = SubscriberNotifier(
                bot_token=bot_token,
                store=subs_store,
                logger=logger,
                openai_api_key=os.getenv("OPENAI_API_KEY", ""),
                openai_model=os.getenv("OPENAI_MODEL_FILTER_MATCH", os.getenv("OPENAI_MODEL", "gpt-4o-mini")),
                keyword_model=os.getenv("OPENAI_MODEL_KEYWORD_EXPANSION", ""),
                final_match_model=os.getenv("OPENAI_MODEL_FILTER_MATCH", ""),
            )
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                approve_review_item(
                    item,
                    state_store=state_store,
                    subs_store=subs_store,
                    bot_token=bot_token,
                    reviewer=reviewer,
                    notes=notes,
                    logger=logger,
                    send_to_subscribers=True,
                    subscriber_notifier=subscriber_notifier,
                    openai_api_key=os.getenv("OPENAI_API_KEY", ""),
                    openai_model=os.getenv("OPENAI_MODEL_FILTER_MATCH", os.getenv("OPENAI_MODEL", "gpt-4o-mini")),
                    keyword_model=os.getenv("OPENAI_MODEL_KEYWORD_EXPANSION", ""),
                    final_match_model=os.getenv("OPENAI_MODEL_FILTER_MATCH", ""),
                )
            )
        finally:
            loop.close()
        return result.message
    finally:
        subs_store.close()


def main() -> None:
    base_dir = Path(__file__).resolve().parent
    load_dotenv(base_dir / ".env")
    state_db = Path(os.getenv("STATE_DB_PATH", str(base_dir / "state" / "job_bot_state.db")))
    subs_db = Path(os.getenv("TELEGRAM_SUBS_DB_PATH", str(base_dir / "state" / "telegram_subscriptions.db")))
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")

    st.set_page_config(page_title="ZapLance Ops Dashboard", layout="wide")
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&display=swap');
        html, body, [class*="css"] { font-family: 'Manrope', sans-serif; }
        .stApp { background: linear-gradient(120deg, #0f172a 0%, #111827 45%, #1f2937 100%); color: #e5e7eb; }
        .metric-card {
            background: linear-gradient(145deg, rgba(30,41,59,0.95), rgba(15,23,42,0.9));
            border: 1px solid rgba(96,165,250,0.25);
            border-radius: 16px;
            padding: 14px 16px;
            margin-bottom: 10px;
            box-shadow: 0 6px 22px rgba(0,0,0,0.25);
        }
        .metric-label { color: #93c5fd; font-size: 0.86rem; }
        .metric-value { color: #f8fafc; font-size: 1.5rem; font-weight: 800; }
        .review-card {
            background: linear-gradient(180deg, rgba(2,6,23,0.96), rgba(15,23,42,0.94));
            border: 1px solid rgba(251, 191, 36, 0.38);
            border-radius: 16px;
            padding: 16px;
            margin-bottom: 12px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.34);
        }
        .review-card-title { color: #f8fafc; font-size: 1.05rem; font-weight: 800; }
        .review-card-meta { color: #cbd5e1; font-size: 0.92rem; line-height: 1.55; }
        .review-card-accent { color: #fcd34d; font-weight: 700; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.title("ZapLance Analytics + Human Review")
    st.caption("Live operations metrics, conversion analytics, and review workflow controls.")

    if not state_db.exists():
        st.error(f"State DB not found: {state_db}")
        return
    if not subs_db.exists():
        st.error(f"Subscriptions DB not found: {subs_db}")
        return

    crawler_health = _load_crawler_health(state_db)
    health_message = (
        f"State: {crawler_health['state']} | Updated (UTC): {crawler_health['updated_at_utc'] or 'unknown'}\n\n"
        f"{crawler_health['reason'] or 'No reason recorded.'}"
    )
    severity = crawler_health["severity"].strip().lower()
    if severity == "critical":
        st.error(health_message)
    elif severity == "warning":
        st.warning(health_message)
    else:
        st.info(health_message)

    metrics = _load_metrics(state_db, subs_db)
    cols = st.columns(4)
    for idx, (label, value) in enumerate(metrics):
        with cols[idx % 4]:
            st.markdown(
                f"""
                <div class="metric-card">
                    <div class="metric-label">{label}</div>
                    <div class="metric-value">{value}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    sent_by_day, plan_split, cycle_cards = _load_charts(state_db, subs_db)
    chart_col_a, chart_col_b, chart_col_c = st.columns(3)
    with chart_col_a:
        st.subheader("Posts Sent Over Time")
        if not sent_by_day.empty:
            fig = px.line(sent_by_day, x="day", y="sent_count", markers=True, color_discrete_sequence=["#22d3ee"])
            fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), template="plotly_dark")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No sent-notification data yet.")
    with chart_col_b:
        st.subheader("Active Plans Split")
        if not plan_split.empty:
            fig = px.pie(plan_split, names="plan", values="users_count", hole=0.45)
            fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), template="plotly_dark")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No active-plan data yet.")
    with chart_col_c:
        st.subheader("New Cards Per Day")
        if not cycle_cards.empty:
            fig = px.bar(cycle_cards, x="day", y="cards_count", color_discrete_sequence=["#34d399"])
            fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), template="plotly_dark")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No cycle data yet.")

    st.subheader("Fetch Strategy Health")
    fetch_stats = _load_fetch_strategy_stats(state_db)
    if fetch_stats.empty:
        st.info("No fetch-strategy health data yet.")
    else:
        st.dataframe(fetch_stats, use_container_width=True, hide_index=True)

    st.divider()
    st.header("Human Review Queue")
    reviewer = st.text_input("Reviewer Name", value=os.getenv("USERNAME", "ops_reviewer"))

    state_store = StateStore(state_db)
    subs_store = SubscriptionStore(subs_db)
    try:
        review_match_notifier = None
        if bot_token.strip():
            review_match_notifier = SubscriberNotifier(
                bot_token=bot_token,
                store=subs_store,
                logger=logging.getLogger("dashboard-review-match"),
                openai_api_key=os.getenv("OPENAI_API_KEY", ""),
                openai_model=os.getenv("OPENAI_MODEL_FILTER_MATCH", os.getenv("OPENAI_MODEL", "gpt-4o-mini")),
                keyword_model=os.getenv("OPENAI_MODEL_KEYWORD_EXPANSION", ""),
                final_match_model=os.getenv("OPENAI_MODEL_FILTER_MATCH", ""),
            )
        pending = state_store.list_human_review_items(status="pending", limit=200)
        if not pending:
            st.success("No pending human-review items.")
            return

        for item in pending:
            presentation = build_human_review_presentation(
                item,
                subs_store,
                subscriber_notifier=review_match_notifier,
            )
            card = presentation.card
            review_sent = "Yes" if item.admin_message_id else "No"
            counterparty = getattr(card, "counterparty", "") or card.company or "Unknown"
            opportunity_location = getattr(card, "opportunity_location", "") or card.location or "Unknown"
            payment_terms = getattr(card, "payment_terms", "") or card.salary or "Unknown"
            engagement = " / ".join(part for part in (getattr(card, "engagement_type", ""), getattr(card, "commitment_level", "")) if part) or "Unknown"
            timeline = " / ".join(part for part in (getattr(card, "start_timeline", ""), getattr(card, "duration", "")) if part) or "Unknown"
            skills = getattr(card, "skills_required", "") or "Unknown"
            opportunity_url = getattr(card, "proposal_or_contact_url", "") or card.url
            st.markdown(
                f"""
                <div class="review-card">
                    <div class="review-card-title">{html.escape(card.title or "Untitled Post")}</div>
                    <div class="review-card-meta">{html.escape(card.url)}</div>
                    <div class="review-card-meta">
                        <span class="review-card-accent">Confidence:</span> {presentation.confidence:.3f}
                        &nbsp; | &nbsp;
                        <span class="review-card-accent">Reason:</span> {html.escape(item.reason or "No queue reason recorded.")}
                    </div>
                    <div class="review-card-meta">
                        <span class="review-card-accent">Client:</span> {html.escape(counterparty)}
                        &nbsp; | &nbsp;
                        <span class="review-card-accent">Budget / Rate:</span> {html.escape(payment_terms)}
                        &nbsp; | &nbsp;
                        <span class="review-card-accent">Engagement Type:</span> {html.escape(engagement)}
                    </div>
                    <div class="review-card-meta">
                        <span class="review-card-accent">Timeline / Duration:</span> {html.escape(timeline)}
                        &nbsp; | &nbsp;
                        <span class="review-card-accent">Skills Requested:</span> {html.escape(skills)}
                        &nbsp; | &nbsp;
                        <span class="review-card-accent">Remote / Location:</span> {html.escape(opportunity_location)}
                    </div>
                    <div class="review-card-meta">
                        <span class="review-card-accent">Source:</span> {html.escape(card.website or "Unknown")}
                        &nbsp; | &nbsp;
                        <span class="review-card-accent">Language:</span> {html.escape(card.language or "en")}
                        &nbsp; | &nbsp;
                        <span class="review-card-accent">Telegram Review Sent:</span> {review_sent}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.markdown(f"**Summary**  \n{card.description or 'No summary extracted.'}")
            st.markdown(f"**Extractor Notes**  \n{card.notes or 'No extractor notes.'}")
            st.markdown(
                f"**Client Context**  \n"
                f"- Posted at: {card.posted_at_utc or 'Unknown'}\n"
                f"- Extraction source: {card.extraction_method or 'human-review'}"
            )
            st.markdown("**Target User**")
            if presentation.target_recipient is not None:
                target = presentation.target_recipient
                keywords = ", ".join(target.keywords) or "none"
                specialties = ", ".join(target.specialties) or "none"
                project_preferences = " | ".join(target.project_preferences) or "none"
                st.markdown(
                    f"User ID: `{target.user_id}`  \n"
                    f"Username: `{target.username}`  \n"
                    f"Role Filter: {target.role}  \n"
                    f"Location Filter: {target.location}  \n"
                    f"Budget / Payment Filter: {target.payment_preference}  \n"
                    f"Budget / Payment Policy: {target.budget_visibility_policy}  \n"
                    f"Delivery Mode: {target.delivery_mode}  \n"
                    f"Keywords: {keywords}  \n"
                    f"Specialties: {specialties}  \n"
                    f"Project Preferences: {project_preferences}  \n"
                    f"Why It Matches: {target.match_reason}"
                )
            else:
                st.markdown("No matching subscriber is currently eligible for auto-send.")
            notes = st.text_area("Notes", key=f"notes_{item.id}", height=68, placeholder="Add review note...")
            col0, col1, col3 = st.columns(3)
            if hasattr(col0, "link_button"):
                col0.link_button("Open Project", opportunity_url, use_container_width=True)
            else:
                col0.markdown(f"[Open Project]({opportunity_url})")
            if col1.button("Approve & Send", key=f"send_{item.id}", use_container_width=True):
                message = _enqueue_action_send(item, state_store, subs_db, bot_token, reviewer, notes)
                if "Missing TELEGRAM_BOT_TOKEN" in message or "failed" in message.lower():
                    st.error(message)
                else:
                    st.success(message)
                st.rerun()
            if col3.button("Neglect", key=f"neglect_{item.id}", use_container_width=True):
                result = neglect_review_item(
                    item,
                    state_store=state_store,
                    reviewer=reviewer,
                    notes=notes,
                )
                st.warning(result.message)
                st.rerun()
            st.divider()
    finally:
        subs_store.close()
        state_store.close()


if __name__ == "__main__":
    main()
