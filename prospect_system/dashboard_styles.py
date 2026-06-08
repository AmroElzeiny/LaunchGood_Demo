from __future__ import annotations

import html
from typing import Any

PAGE_TITLE = "AI Prospect Discovery CRM"
RUN_BUTTON_LABEL = "Start Prospect Analysis"
DEMO_BUTTON_LABEL = "Please Insert a Demo Link"
ANOTHER_RUN_LABEL = "Run Another Analysis"

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&display=swap');
html, body, [class*="css"] {
    font-family: 'Manrope', sans-serif;
    color: #1f2937;
}
[data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] li,
[data-testid="stText"],
[data-testid="stCaptionContainer"],
textarea,
input {
    color: #2c628a !important;
}
textarea:disabled,
input:disabled {
    -webkit-text-fill-color: #2c628a !important;
    opacity: 1 !important;
}
.stApp {
    background: #f7f8fb;
}
.block-container {
    padding-top: 1.4rem;
    padding-bottom: 2rem;
    max-width: 1320px;
}
div.stButton > button,
div.stDownloadButton > button,
[data-testid="stLinkButton"] > a {
    background-color: rgba(44, 98, 138, 0.10) !important;
    border-color: rgba(44, 98, 138, 0.34) !important;
    color: #2c628a !important;
    box-shadow: none !important;
}
div.stButton > button:hover,
div.stDownloadButton > button:hover,
[data-testid="stLinkButton"] > a:hover {
    background-color: rgba(37, 99, 235, 0.15) !important;
    border-color: rgba(37, 99, 235, 0.48) !important;
    color: #1d4ed8 !important;
}
div.stButton > button[kind="primary"] {
    background-color: rgba(37, 99, 235, 0.78) !important;
    border-color: rgba(37, 99, 235, 0.82) !important;
    color: #ffffff !important;
}
div.stButton > button:disabled,
div.stDownloadButton > button:disabled {
    background-color: rgba(148, 163, 184, 0.16) !important;
    border-color: rgba(148, 163, 184, 0.24) !important;
    color: rgba(44, 98, 138, 0.62) !important;
}
h1, h2, h3 {
    letter-spacing: 0;
}
.prospect-topline {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    gap: 16px;
    margin-bottom: 10px;
}
.prospect-kicker {
    color: #0f766e;
    font-size: 0.82rem;
    font-weight: 800;
    text-transform: uppercase;
}
.prospect-title {
    color: #111827;
    font-size: 1.86rem;
    line-height: 1.15;
    font-weight: 800;
    margin: 0;
}
.prospect-subtitle {
    color: #526071;
    font-size: 0.95rem;
    margin-top: 4px;
}
.workflow-rail {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 8px;
    margin: 16px 0 12px;
}
.workflow-step {
    display: flex;
    align-items: center;
    gap: 9px;
    min-height: 54px;
    padding: 10px 11px;
    border: 1px solid #dbe3ef;
    border-radius: 8px;
    background: #ffffff;
    color: #64748b;
    text-decoration: none;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}
.workflow-step:hover {
    border-color: #94a3b8;
    background: #f8fafc;
}
.workflow-step.active {
    border-color: #2563eb;
    background: #eef5ff;
    color: #1d4ed8;
}
.workflow-step.complete {
    border-color: #99f6e4;
    background: #f0fdfa;
}
.workflow-marker {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex: 0 0 28px;
    width: 28px;
    height: 28px;
    border-radius: 999px;
    background: #e2e8f0;
    color: #475569;
    font-size: 0.82rem;
    font-weight: 800;
}
.workflow-step.active .workflow-marker {
    background: #2563eb;
    color: #ffffff;
}
.workflow-step.complete .workflow-marker {
    background: #0f766e;
    color: #ffffff;
}
.workflow-text {
    color: inherit;
    font-size: 0.86rem;
    font-weight: 800;
    line-height: 1.2;
}
.step-panel {
    background: #ffffff;
    border: 1px solid #e3e7ee;
    border-radius: 8px;
    padding: 16px;
    margin-top: 10px;
}
.step-banner {
    border: 1px solid #bfdbfe;
    border-left: 4px solid #2563eb;
    border-radius: 8px;
    background: #eff6ff;
    color: #1e3a8a;
    padding: 13px 14px;
    margin: 10px 0 14px;
    font-weight: 700;
}
.step-banner.success {
    border-color: #99f6e4;
    border-left-color: #0f766e;
    background: #f0fdfa;
    color: #115e59;
}
.step-banner.warning {
    border-color: #fde68a;
    border-left-color: #d97706;
    background: #fffbeb;
    color: #92400e;
}
.step-banner.error {
    border-color: #fecdd3;
    border-left-color: #e11d48;
    background: #fff1f2;
    color: #9f1239;
}
.metric-strip {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 10px;
    margin: 12px 0 18px;
}
.metric-card {
    background: #ffffff;
    border: 1px solid #e3e7ee;
    border-left: 4px solid var(--accent);
    border-radius: 8px;
    padding: 12px 14px;
}
.metric-label {
    color: #64748b;
    font-size: 0.78rem;
    font-weight: 700;
}
.metric-value {
    color: #111827;
    font-size: 1.45rem;
    font-weight: 800;
    margin-top: 3px;
}
.panel {
    background: #ffffff;
    border: 1px solid #e3e7ee;
    border-radius: 8px;
    padding: 14px;
    margin-bottom: 12px;
}
.prospect-card {
    background: #ffffff;
    border: 1px solid #d8dee8;
    border-radius: 8px;
    padding: 16px;
    margin: 12px 0;
}
.prospect-card-header {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: flex-start;
}
.prospect-company {
    color: #111827;
    font-size: 1.28rem;
    line-height: 1.2;
    font-weight: 800;
}
.prospect-url {
    color: #64748b;
    font-size: 0.84rem;
    word-break: break-word;
}
.fit-pill {
    border-radius: 999px;
    padding: 5px 9px;
    font-size: 0.78rem;
    font-weight: 800;
    white-space: nowrap;
}
.fit-strong { background: #dff6ed; color: #047857; }
.fit-medium { background: #fff4cf; color: #92400e; }
.fit-weak { background: #e8f0ff; color: #1d4ed8; }
.fit-none { background: #ffe4e6; color: #be123c; }
.card-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 10px;
    margin-top: 14px;
}
.field-block {
    border-top: 1px solid #edf0f5;
    padding-top: 8px;
}
.field-label {
    color: #64748b;
    font-size: 0.76rem;
    font-weight: 800;
    text-transform: uppercase;
}
.field-value {
    color: #2c628a;
    font-size: 0.92rem;
    line-height: 1.45;
    margin-top: 3px;
}
.output-summary-grid {
    display: grid;
    grid-template-columns: 1.2fr 0.9fr 0.9fr;
    gap: 10px;
    margin: 12px 0;
}
.output-detail-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
    margin: 12px 0;
}
.output-list-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 10px;
    margin-top: 12px;
}
.info-tile {
    border: 1px solid #e3e7ee;
    border-radius: 8px;
    background: #ffffff;
    padding: 12px;
}
.info-tile.accent {
    border-color: #bfdbfe;
    background: #f8fbff;
}
.info-label {
    color: #64748b;
    font-size: 0.74rem;
    font-weight: 800;
    text-transform: uppercase;
}
.info-value {
    color: #2c628a;
    font-size: 0.93rem;
    line-height: 1.45;
    margin-top: 4px;
    word-break: break-word;
}
.info-value.large {
    font-size: 1.1rem;
    font-weight: 800;
}
.preview-scrollbox {
    max-height: 430px;
    overflow-y: auto;
    border: 1px solid #dbe3ef;
    border-radius: 8px;
    background: #ffffff;
    padding: 14px;
}
.preview-section {
    border-bottom: 1px solid #edf0f5;
    padding-bottom: 12px;
    margin-bottom: 12px;
}
.preview-section:last-child {
    border-bottom: 0;
    margin-bottom: 0;
    padding-bottom: 0;
}
.log-row {
    display: grid;
    grid-template-columns: 155px 110px 1fr;
    gap: 10px;
    padding: 7px 0;
    border-bottom: 1px solid #edf0f5;
    font-size: 0.86rem;
}
.log-time { color: #64748b; }
.log-stage { color: #0f766e; font-weight: 800; }
.log-message { color: #2c628a; }
@media (max-width: 900px) {
    .workflow-rail,
    .metric-strip,
    .card-grid,
    .output-summary-grid,
    .output-detail-grid,
    .output-list-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .prospect-card-header {
        display: block;
    }
}
@media (max-width: 620px) {
    .workflow-rail,
    .metric-strip,
    .card-grid,
    .output-summary-grid,
    .output-detail-grid,
    .output-list-grid {
        grid-template-columns: 1fr;
    }
    .log-row {
        grid-template-columns: 1fr;
        gap: 2px;
    }
}
</style>
"""


def metric_card(label: str, value: Any, accent: str = "#0f766e") -> str:
    return f"""
    <div class="metric-card" style="--accent: {html.escape(accent)};">
        <div class="metric-label">{html.escape(str(label))}</div>
        <div class="metric-value">{html.escape(str(value))}</div>
    </div>
    """


def fit_class(fit_status: str) -> str:
    normalized = str(fit_status or "").lower()
    if "strong" in normalized:
        return "fit-strong"
    if "medium" in normalized:
        return "fit-medium"
    if "weak" in normalized:
        return "fit-weak"
    return "fit-none"


def prospect_card_html(card: Any) -> str:
    fields = [
        ("Category", card.category),
        ("Fit Score", f"{card.fit_score}/100"),
        ("Confidence", card.confidence),
        ("Reason", card.reason),
        ("Pain Point", card.pain_point),
        ("Suggested Offer", card.suggested_offer),
        ("Recommended Contact", card.recommended_contact_type),
        ("Contact Email", card.contact_email or "Not detected"),
        ("Next Step", card.next_step),
    ]
    field_html = "\n".join(f"""
        <div class="field-block">
            <div class="field-label">{html.escape(label)}</div>
            <div class="field-value">{html.escape(str(value or 'Unknown'))}</div>
        </div>
        """ for label, value in fields)
    return f"""
    <div class="prospect-card">
        <div class="prospect-card-header">
            <div>
                <div class="prospect-company">{html.escape(card.company or 'Unknown')}</div>
                <div class="prospect-url">{html.escape(card.website or '')}</div>
            </div>
            <div class="fit-pill {fit_class(card.fit_status)}">{html.escape(card.fit_status or 'Unknown')}</div>
        </div>
        <div class="card-grid">{field_html}</div>
    </div>
    """


def log_row_html(step: Any) -> str:
    return f"""
    <div class="log-row">
        <div class="log-time">{html.escape(str(step.created_at))}</div>
        <div class="log-stage">{html.escape(str(step.stage or 'stage'))}</div>
        <div class="log-message">{html.escape(str(step.message))}</div>
    </div>
    """
