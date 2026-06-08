from __future__ import annotations

from pathlib import Path

from prospect_system.dashboard_state import ProspectAnalysisState, ScrapedPage
from prospect_system.prospect_card import ProspectCard
from prospect_system.prospect_config import load_prospect_settings
from prospect_system.prospect_scraper import normalize_input_urls


def _set_required_env(monkeypatch) -> None:
    values = {
        "PROSPECT_DEMO_WEBSITE_URL": "https://demo.test",
        "PROSPECT_DEFAULT_TARGET_CRITERIA": "nonprofit education programs",
        "PROSPECT_DEFAULT_OUTREACH_GOAL": "partnership intro",
        "PROSPECT_MAX_PAGES_TO_SCRAPE": "4",
        "PROSPECT_MANUAL_REVIEW_MINUTES": "10",
        "PROSPECT_LOG_REFRESH_SECONDS": "3",
        "PROSPECT_USAGE_NOTIFY_CHAT_ID": "100000001",
        "GOOGLE_SHEET_ID": "sheet-id",
        "GOOGLE_SERVICE_ACCOUNT_JSON": "{}",
        "GOOGLE_SERVICE_ACCOUNT_EMAIL": "service-account@example.iam.gserviceaccount.com",
        "GMAIL_SENDER_EMAIL": "sender@example.com",
        "OPENAI_API_KEY": "test-key",
        "AI_MODEL": "gpt-test",
        "SCRAPING_TIMEOUT_SECONDS": "20",
        "CAPTCHA_END_MESSAGE": "captcha stop",
        "PROSPECT_FIT_STATUS_RULES_JSON": (
            '[{"min":80,"max":100,"label":"Strong Fit"},'
            '{"min":60,"max":79,"label":"Medium Fit"},'
            '{"min":40,"max":59,"label":"Weak Fit"},'
            '{"min":0,"max":39,"label":"Not Relevant"}]'
        ),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_normalize_input_urls_adds_scheme_and_dedupes() -> None:
    urls, errors = normalize_input_urls("example.com, https://example.com, not a url")

    assert urls == ["https://example.com/"]
    assert errors == ["Invalid URL: not a url"]
    assert len(urls) == len(set(urls))


def test_fit_status_mapping_is_env_driven(monkeypatch, tmp_path: Path) -> None:
    _set_required_env(monkeypatch)

    settings = load_prospect_settings(tmp_path)

    assert settings.missing_env_values == []
    assert settings.fit_status_for_score(85) == "Strong Fit"
    assert settings.fit_status_for_score(62) == "Medium Fit"
    assert settings.fit_status_for_score(45) == "Weak Fit"
    assert settings.fit_status_for_score(15) == "Not Relevant"
    assert settings.log_refresh_seconds == 3
    assert settings.prospect_usage_notify_chat_id == "100000001"


def test_smtp_settings_require_real_password(monkeypatch, tmp_path: Path) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("PROSPECT_EMAIL_SEND_METHOD", "smtp")
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USERNAME", "sender@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "YOUR_SMTP_APP_PASSWORD_HERE")
    monkeypatch.setenv("SMTP_SENDER_EMAIL", "sender@example.com")
    monkeypatch.setenv("SMTP_USE_TLS", "true")
    monkeypatch.setenv("SMTP_USE_SSL", "false")

    settings = load_prospect_settings(tmp_path)

    assert settings.email_send_method == "smtp"
    assert settings.email_sender_email == "sender@example.com"
    assert not settings.is_smtp_configured
    assert "SMTP_PASSWORD" in settings.missing_env_values

    monkeypatch.setenv("SMTP_PASSWORD", "real-test-password")
    settings = load_prospect_settings(tmp_path)

    assert settings.is_smtp_configured


def test_invalid_google_json_is_not_treated_as_configured(monkeypatch, tmp_path: Path) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "not-json")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH", "missing-service-account.json")

    settings = load_prospect_settings(tmp_path)

    assert not settings.is_google_configured
    assert "GOOGLE_SERVICE_ACCOUNT_JSON is invalid JSON" in settings.missing_env_values
    assert "GOOGLE_SERVICE_ACCOUNT_JSON_PATH file not found: missing-service-account.json" in settings.missing_env_values


def test_state_preserves_logs_pages_and_eta() -> None:
    state = ProspectAnalysisState.create(
        input_urls=["https://example.com/"],
        target_criteria="criteria",
        outreach_goal="goal",
    )
    state.log_step("Input received", stage="input")
    state.add_scraped_page(
        ScrapedPage(
            url="https://example.com/",
            final_url="https://example.com/",
            title="Example",
            text="Example text",
        )
    )

    assert state.session_id.startswith("prospect-")
    assert state.ai_step_logs[0].message == "Input received"
    assert state.extracted_text == "Example text"
    assert state.estimate_remaining_seconds(max_pages_to_scrape=4) >= 0


def test_prospect_card_sheet_row_contains_decision_and_draft() -> None:
    card = ProspectCard(
        company="Acme",
        website="https://acme.test/",
        category="Education",
        fit_score=88,
        fit_status="Strong Fit",
        reason="Good evidence",
        pain_point="Manual outreach",
        suggested_offer="Automation",
        recommended_contact_type="Partnership lead",
        confidence="High",
        next_step="Approve",
        risk_uncertainty_flags=["No email"],
        pages_scraped=["https://acme.test/"],
        source_session_id="prospect-test",
    )

    row = card.to_sheet_row(
        created_at="2026-06-08T00:00:00Z",
        human_decision="Approved",
        crm_stage="Ready for Outreach",
        outreach_goal="partnership",
        target_criteria="education",
        telegram_or_slack_alert_sent="Yes",
        notes="ok",
        outreach_subject="Hello",
        outreach_body="Body",
        recipient_email="me@example.com",
    )

    assert row[0] == "2026-06-08T00:00:00Z"
    assert row[1] == "https://acme.test/"
    assert row[6] == 88
    assert row[8] == "Ready for Outreach"
    assert row[17] == "Approved"
    assert row[18] == "Yes"
    assert row[20] == "Hello"
    assert row[22] == "me@example.com"
