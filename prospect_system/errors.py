from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


@dataclass(slots=True)
class ErrorRecord:
    session_id: str
    stage: str
    message: str
    url: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    def to_row(self) -> list[Any]:
        error_type = str(self.details.get("error_type") or "").strip()
        error_message = str(
            self.details.get("error_message") or self.message or ""
        ).strip()
        resolved = str(self.details.get("resolved") or "").strip()
        return [
            self.created_at,
            self.session_id,
            self.stage,
            self.url,
            error_type,
            error_message,
            resolved,
        ]


class ProspectSystemError(Exception):
    stage = "system"


class ProspectConfigError(ProspectSystemError):
    stage = "config"


class ProspectScrapingError(ProspectSystemError):
    stage = "scraping"


class ProspectBlockedError(ProspectScrapingError):
    stage = "blocked_page"


class ProspectAIError(ProspectSystemError):
    stage = "ai"


class ProspectSheetsError(ProspectSystemError):
    stage = "google_sheets"


class ProspectGmailError(ProspectSystemError):
    stage = "gmail"
