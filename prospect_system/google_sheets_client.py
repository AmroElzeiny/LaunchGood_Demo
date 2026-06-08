from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prospect_system.dashboard_state import ProspectAnalysisState, StepLog
from prospect_system.errors import ErrorRecord, ProspectSheetsError
from prospect_system.prospect_config import ProspectSettings, SHEET_TABS


SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


@dataclass(slots=True)
class SheetAppendResult:
    success: bool
    message: str


def _cell(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return value


class GoogleSheetsClient:
    def __init__(self, settings: ProspectSettings) -> None:
        self.settings = settings
        self._service: Any | None = None
        self._known_tabs: set[str] | None = None
        self._sheet_ids: dict[str, int] | None = None

    def append_row(self, tab_name: str, values: list[Any]) -> SheetAppendResult:
        if not self.settings.is_google_configured:
            return SheetAppendResult(False, "Google Sheets is not configured in .env.")
        try:
            service = self._get_service()
            self.ensure_tab_exists(tab_name)
            row_values = [_cell(value) for value in values]
            response = service.spreadsheets().values().append(
                spreadsheetId=self.settings.google_sheet_id,
                range=f"'{tab_name}'!A:Z",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": [row_values]},
            ).execute()
            updated_range = str(response.get("updates", {}).get("updatedRange") or "")
            self._clear_updated_row_background(tab_name, updated_range, column_count=len(row_values))
            return SheetAppendResult(True, f"Saved to Google Sheets tab: {tab_name}")
        except Exception as exc:  # noqa: BLE001
            return SheetAppendResult(False, f"Google Sheets API failure: {exc}")

    def append_step(self, state: ProspectAnalysisState, step: StepLog) -> SheetAppendResult:
        return self.append_row(SHEET_TABS["logs"], step.to_row(state.session_id))

    def append_error(self, error: ErrorRecord) -> SheetAppendResult:
        return self.append_row(SHEET_TABS["errors"], error.to_row())

    def append_dashboard_metric(self, values: list[Any]) -> SheetAppendResult:
        return self.append_row(SHEET_TABS["metrics"], values)

    def ensure_required_tabs(self) -> SheetAppendResult:
        if not self.settings.is_google_configured:
            return SheetAppendResult(False, "Google Sheets is not configured in .env.")
        try:
            for tab_name in SHEET_TABS.values():
                self.ensure_tab_exists(tab_name)
            return SheetAppendResult(True, "Google Sheets tabs are ready.")
        except Exception as exc:  # noqa: BLE001
            return SheetAppendResult(False, f"Google Sheets tab setup failed: {exc}")

    def ensure_tab_exists(self, tab_name: str) -> None:
        tabs = self._sheet_tabs()
        if tab_name in tabs:
            return
        service = self._get_service()
        service.spreadsheets().batchUpdate(
            spreadsheetId=self.settings.google_sheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": tab_name,
                            }
                        }
                    }
                ]
            },
        ).execute()
        tabs.add(tab_name)
        self._sheet_ids = None

    def _sheet_tabs(self) -> set[str]:
        if self._known_tabs is not None:
            return self._known_tabs
        self._load_sheet_metadata()
        return self._known_tabs or set()

    def _sheet_id_for_tab(self, tab_name: str) -> int | None:
        if self._sheet_ids is None:
            self._load_sheet_metadata()
        return (self._sheet_ids or {}).get(tab_name)

    def _load_sheet_metadata(self) -> None:
        service = self._get_service()
        metadata = service.spreadsheets().get(
            spreadsheetId=self.settings.google_sheet_id,
            fields="sheets.properties(sheetId,title)",
        ).execute()
        sheet_ids: dict[str, int] = {}
        for sheet in metadata.get("sheets", []):
            properties = sheet.get("properties", {})
            title = str(properties.get("title") or "")
            sheet_id = properties.get("sheetId")
            if title and isinstance(sheet_id, int):
                sheet_ids[title] = sheet_id
        self._sheet_ids = sheet_ids
        self._known_tabs = set(sheet_ids)

    def _clear_updated_row_background(self, tab_name: str, updated_range: str, *, column_count: int) -> None:
        row_number = self._row_number_from_updated_range(updated_range)
        sheet_id = self._sheet_id_for_tab(tab_name)
        if row_number is None or sheet_id is None:
            return
        try:
            self._get_service().spreadsheets().batchUpdate(
                spreadsheetId=self.settings.google_sheet_id,
                body={
                    "requests": [
                        {
                            "repeatCell": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "startRowIndex": row_number - 1,
                                    "endRowIndex": row_number,
                                    "startColumnIndex": 0,
                                    "endColumnIndex": max(1, column_count),
                                },
                                "cell": {
                                    "userEnteredFormat": {},
                                },
                                "fields": (
                                    "userEnteredFormat.backgroundColor,"
                                    "userEnteredFormat.backgroundColorStyle"
                                ),
                            }
                        }
                    ]
                },
            ).execute()
        except Exception:
            return

    @staticmethod
    def _row_number_from_updated_range(updated_range: str) -> int | None:
        range_part = str(updated_range or "").split("!", 1)[-1]
        match = re.search(r"\d+", range_part)
        if not match:
            return None
        return int(match.group(0))

    def fetch_dashboard_metrics(self) -> dict[str, float]:
        if not self.settings.is_google_configured:
            return {}
        try:
            approved = self._read_tab(SHEET_TABS["approved"])
            rejected = self._read_tab(SHEET_TABS["rejected"])
            review = self._read_tab(SHEET_TABS["review"])
        except Exception:
            return {}

        def is_header_row(row: list[Any]) -> bool:
            first_cell = str(row[0]).strip().lower() if row else ""
            second_cell = str(row[1]).strip().lower() if len(row) > 1 else ""
            return first_cell in {"created_at", "created at"} or second_cell in {"website_url", "website url"}

        approved_rows = [row for row in approved if not is_header_row(row)]
        rejected_rows = [row for row in rejected if not is_header_row(row)]
        review_rows = [row for row in review if not is_header_row(row)]
        rows = approved_rows + rejected_rows + review_rows
        scores: list[float] = []
        uncertainty_count = 0
        draft_count = 0
        for row in rows:
            try:
                scores.append(float(row[6]))
            except (IndexError, TypeError, ValueError):
                pass
            try:
                if str(row[11]).strip():
                    uncertainty_count += len([part for part in str(row[11]).split(";") if part.strip()])
            except IndexError:
                pass
            try:
                if str(row[20]).strip() or str(row[21]).strip():
                    draft_count += 1
            except IndexError:
                pass
        total = len(rows)
        average_score = sum(scores) / len(scores) if scores else 0.0
        return {
            "analyzed_websites": float(total),
            "approved_prospects": float(len(approved_rows)),
            "rejected_prospects": float(len(rejected_rows)),
            "average_fit_score": average_score,
            "uncertainty_flags": float(uncertainty_count),
            "generated_outreach_drafts": float(draft_count),
        }

    def _read_tab(self, tab_name: str) -> list[list[Any]]:
        service = self._get_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=self.settings.google_sheet_id,
            range=f"'{tab_name}'!A:Z",
        ).execute()
        return list(result.get("values", []))

    def _get_service(self) -> Any:
        if self._service is not None:
            return self._service
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except Exception as exc:  # noqa: BLE001
            raise ProspectSheetsError(
                "Google API packages are missing. Install google-api-python-client and google-auth."
            ) from exc
        credentials = self._load_credentials(service_account, scopes=[SHEETS_SCOPE])
        self._service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        return self._service

    def _load_credentials(self, service_account_module: Any, *, scopes: list[str]) -> Any:
        env_json_error = ""
        if self.settings.google_service_account_json:
            try:
                info = json.loads(self.settings.google_service_account_json)
            except json.JSONDecodeError as exc:
                env_json_error = f"GOOGLE_SERVICE_ACCOUNT_JSON is invalid JSON: {exc}"
            else:
                return service_account_module.Credentials.from_service_account_info(info, scopes=scopes)
        path = Path(self.settings.google_service_account_json_path)
        if not path.is_absolute():
            path = self.settings.base_dir / path
        if not path.exists():
            if env_json_error:
                raise ProspectSheetsError(f"{env_json_error}. Also, Google service account file was not found: {path}")
            raise ProspectSheetsError(f"Google service account file was not found: {path}")
        return service_account_module.Credentials.from_service_account_file(str(path), scopes=scopes)
