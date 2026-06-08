from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from prospect_system.dashboard_state import ProspectAnalysisState, utc_now_iso
from prospect_system.errors import ErrorRecord
from prospect_system.gmail_client import GmailClient
from prospect_system.google_sheets_client import GoogleSheetsClient, SheetAppendResult
from prospect_system.logging_utils import StateJSONLLogger
from prospect_system.prospect_card import ProspectCard
from prospect_system.prospect_config import ProspectSettings, SHEET_TABS


@dataclass(slots=True)
class DecisionResult:
    success: bool
    message: str


class ProspectDecisionHandler:
    def __init__(self, settings: ProspectSettings) -> None:
        self.settings = settings
        self.sheets = GoogleSheetsClient(settings)
        self.gmail = GmailClient(settings)
        self.state_logger = StateJSONLLogger(settings.prospect_log_dir)

    def approve_to_crm(
        self,
        state: ProspectAnalysisState,
        *,
        card: ProspectCard,
        notes: str = "",
        recipient_email: str = "",
    ) -> DecisionResult:
        state.human_decision = "Approved"
        state.crm_stage = "Ready for Outreach"
        state.prospect_card = card
        result = self._append_card(
            state,
            card=card,
            tab_name=SHEET_TABS["approved"],
            notes=notes,
            recipient_email=recipient_email,
        )
        if result.success:
            self._log(
                state, "Saved to Google Sheets", stage="decision", url=card.website
            )
            self._complete(state)
        else:
            self._error(
                state, stage="google_sheets", message=result.message, url=card.website
            )
        return DecisionResult(result.success, result.message)

    def move_to_further_review(
        self,
        state: ProspectAnalysisState,
        *,
        card: ProspectCard,
        notes: str,
    ) -> DecisionResult:
        state.human_decision = "Needs Review"
        state.crm_stage = "Needs Human Review"
        state.prospect_card = card
        result = self._append_card(
            state,
            card=card,
            tab_name=SHEET_TABS["review"],
            notes=notes,
        )
        if result.success:
            self._log(
                state, "Saved to Google Sheets", stage="decision", url=card.website
            )
            self._complete(state)
        else:
            self._error(
                state, stage="google_sheets", message=result.message, url=card.website
            )
        return DecisionResult(result.success, result.message)

    def reject(
        self,
        state: ProspectAnalysisState,
        *,
        card: ProspectCard,
        notes: str,
    ) -> DecisionResult:
        state.human_decision = "Rejected"
        state.crm_stage = "Not Relevant"
        state.prospect_card = card
        result = self._append_card(
            state,
            card=card,
            tab_name=SHEET_TABS["rejected"],
            notes=notes,
        )
        if result.success:
            self._log(
                state, "Saved to Google Sheets", stage="decision", url=card.website
            )
            self._complete(state)
        else:
            self._error(
                state, stage="google_sheets", message=result.message, url=card.website
            )
        return DecisionResult(result.success, result.message)

    def send_outreach(
        self,
        state: ProspectAnalysisState,
        *,
        card: ProspectCard,
        recipient_email: str,
    ) -> DecisionResult:
        draft = state.outreach_draft or {}
        subject = str(draft.get("subject") or "").strip()
        body = str(draft.get("body") or "").strip()
        if not subject or not body:
            return DecisionResult(False, "Generate an outreach draft before sending.")
        send_result = self.gmail.send_email(
            recipient_email=recipient_email,
            subject=subject,
            body=body,
        )
        if not send_result.success:
            self._error(
                state, stage="gmail", message=send_result.message, url=card.website
            )
            return DecisionResult(False, send_result.message)
        self._store_sent_email(
            state, recipient_email=recipient_email, message=send_result.message
        )
        self._log(state, "Email sent successfully", stage="gmail", url=card.website)
        return self.approve_to_crm(
            state,
            card=card,
            notes=f"Outreach sent to {recipient_email}",
            recipient_email=recipient_email,
        )

    def send_draft_only(
        self,
        state: ProspectAnalysisState,
        *,
        card: ProspectCard,
        recipient_email: str,
    ) -> DecisionResult:
        draft = state.outreach_draft or {}
        subject = str(draft.get("subject") or "").strip()
        body = str(draft.get("body") or "").strip()
        if not subject or not body:
            return DecisionResult(False, "Generate an outreach draft before sending.")
        send_result = self.gmail.send_email(
            recipient_email=recipient_email,
            subject=subject,
            body=body,
        )
        if not send_result.success:
            self._error(
                state, stage="gmail", message=send_result.message, url=card.website
            )
            return DecisionResult(False, send_result.message)
        self._store_sent_email(
            state, recipient_email=recipient_email, message=send_result.message
        )
        self._log(state, "Email sent successfully", stage="gmail", url=card.website)
        return DecisionResult(True, send_result.message)

    def _append_card(
        self,
        state: ProspectAnalysisState,
        *,
        card: ProspectCard,
        tab_name: str,
        notes: str,
        recipient_email: str = "",
    ) -> SheetAppendResult:
        draft = state.outreach_draft or {}
        resolved_recipient_email = (
            recipient_email or str(draft.get("recipient_email") or "").strip()
        )

        row = card.to_sheet_row(
            created_at=utc_now_iso(),
            human_decision=state.human_decision,
            crm_stage=state.crm_stage,
            outreach_goal=state.outreach_goal,
            target_criteria=state.target_criteria,
            telegram_or_slack_alert_sent=str(
                getattr(state, "telegram_or_slack_alert_sent", "") or ""
            ),
            notes=notes,
            outreach_subject=str(draft.get("subject") or ""),
            outreach_body=str(draft.get("body") or ""),
            recipient_email=resolved_recipient_email,
        )
        result = self.sheets.append_row(tab_name, row)
        self._log_evidence_summary(state, card=card)
        state.google_sheet_status = result.message
        self.state_logger.write_decision(
            state,
            {
                "website": card.website,
                "company": card.company,
                "human_decision": state.human_decision,
                "crm_stage": state.crm_stage,
                "notes": notes,
                "google_sheet_status": result.message,
            },
        )
        return result

    def _log_evidence_summary(
        self, state: ProspectAnalysisState, *, card: ProspectCard
    ) -> None:
        evidence_summary = card.compact_evidence_summary(limit=1800)
        if not evidence_summary:
            return
        step = state.log_step(
            f"Evidence summary: {evidence_summary}", stage="evidence", url=card.website
        )
        self.state_logger.write_step(state, step)
        self.sheets.append_step(state, step)

    def _store_sent_email(
        self, state: ProspectAnalysisState, *, recipient_email: str, message: str
    ) -> None:
        draft = dict(state.outreach_draft or {})
        draft["recipient_email"] = recipient_email.strip()
        draft["sent_status"] = message
        draft["sent_at"] = utc_now_iso()
        state.outreach_draft = draft

    def _log(
        self, state: ProspectAnalysisState, message: str, *, stage: str, url: str = ""
    ) -> None:
        step = state.log_step(message, stage=stage, url=url)
        self.state_logger.write_step(state, step)
        self.state_logger.write_state_snapshot(state)
        self.sheets.append_step(state, step)

    def _error(
        self, state: ProspectAnalysisState, *, stage: str, message: str, url: str = ""
    ) -> ErrorRecord:
        error = state.add_error(stage=stage, message=message, url=url)
        self.state_logger.write_error(error)
        self.state_logger.write_state_snapshot(state)
        self.sheets.append_error(error)
        return error

    def _complete(self, state: ProspectAnalysisState) -> None:
        state.mark_completed()
        self._log(state, "Process completed", stage="completed", url=state.current_url)
        self.sheets.append_dashboard_metric(
            [
                state.completed_at,
                state.session_id,
                len(state.input_urls),
                len(state.prospect_cards),
                state.human_decision,
                state.processing_time_seconds,
            ]
        )
