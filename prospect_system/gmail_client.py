from __future__ import annotations

import base64
import json
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from prospect_system.errors import ProspectGmailError
from prospect_system.prospect_config import ProspectSettings

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


@dataclass(slots=True)
class GmailSendResult:
    success: bool
    message: str
    provider_message_id: str = ""


def _normalize_email_body(body: str) -> str:
    cleaned = (
        str(body or "")
        .strip()
        .replace("\\n", "\n")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


class GmailClient:
    def __init__(self, settings: ProspectSettings) -> None:
        self.settings = settings
        self._service: Any | None = None

    def send_email(
        self, *, recipient_email: str, subject: str, body: str
    ) -> GmailSendResult:
        recipient_email = recipient_email.strip()
        if not recipient_email:
            return GmailSendResult(False, "Recipient email is required.")
        if self.settings.email_send_method == "smtp":
            return self._send_email_with_smtp(
                recipient_email=recipient_email,
                subject=subject,
                body=_normalize_email_body(body),
            )
        if self.settings.email_send_method != "gmail_api":
            return GmailSendResult(
                False, "PROSPECT_EMAIL_SEND_METHOD must be smtp or gmail_api."
            )
        if not self.settings.gmail_sender_email:
            return GmailSendResult(False, "GMAIL_SENDER_EMAIL is missing from .env.")
        if not self.settings.is_google_configured:
            return GmailSendResult(
                False, "Google service account credentials are not configured in .env."
            )
        try:
            message = MIMEText(_normalize_email_body(body), "plain", "utf-8")
            message["To"] = recipient_email
            message["From"] = self.settings.gmail_sender_email
            message["Subject"] = subject
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
            response = (
                self._get_service()
                .users()
                .messages()
                .send(
                    userId="me",
                    body={"raw": raw},
                )
                .execute()
            )
            return GmailSendResult(
                True,
                "Email sent successfully.",
                provider_message_id=str(response.get("id") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            return GmailSendResult(False, f"Gmail API failure: {exc}")

    def _send_email_with_smtp(
        self, *, recipient_email: str, subject: str, body: str
    ) -> GmailSendResult:
        if not self.settings.is_smtp_configured:
            return GmailSendResult(
                False,
                "SMTP is not configured. Set SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, and SMTP_SENDER_EMAIL in .env.",
            )
        message = EmailMessage()
        message["To"] = recipient_email
        message["From"] = self.settings.email_sender_email
        message["Subject"] = subject
        message.set_content(body)
        context = ssl.create_default_context()
        try:
            if self.settings.smtp_use_ssl:
                with smtplib.SMTP_SSL(
                    self.settings.smtp_host,
                    self.settings.smtp_port,
                    timeout=self.settings.smtp_timeout_seconds,
                    context=context,
                ) as server:
                    server.login(
                        self.settings.smtp_username, self.settings.smtp_password
                    )
                    server.send_message(message)
            else:
                with smtplib.SMTP(
                    self.settings.smtp_host,
                    self.settings.smtp_port,
                    timeout=self.settings.smtp_timeout_seconds,
                ) as server:
                    if self.settings.smtp_use_tls:
                        server.starttls(context=context)
                    server.login(
                        self.settings.smtp_username, self.settings.smtp_password
                    )
                    server.send_message(message)
            return GmailSendResult(True, "Email sent successfully via SMTP.")
        except Exception as exc:  # noqa: BLE001
            return GmailSendResult(False, f"SMTP email failure: {exc}")

    def _get_service(self) -> Any:
        if self._service is not None:
            return self._service
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except Exception as exc:  # noqa: BLE001
            raise ProspectGmailError(
                "Google API packages are missing. Install google-api-python-client and google-auth."
            ) from exc
        credentials = self._load_credentials(service_account, scopes=[GMAIL_SEND_SCOPE])
        if self.settings.gmail_sender_email:
            credentials = credentials.with_subject(self.settings.gmail_sender_email)
        self._service = build(
            "gmail", "v1", credentials=credentials, cache_discovery=False
        )
        return self._service

    def _load_credentials(
        self, service_account_module: Any, *, scopes: list[str]
    ) -> Any:
        env_json_error = ""
        if self.settings.google_service_account_json:
            try:
                info = json.loads(self.settings.google_service_account_json)
            except json.JSONDecodeError as exc:
                env_json_error = f"GOOGLE_SERVICE_ACCOUNT_JSON is invalid JSON: {exc}"
            else:
                return service_account_module.Credentials.from_service_account_info(
                    info, scopes=scopes
                )
        path = Path(self.settings.google_service_account_json_path)
        if not path.is_absolute():
            path = self.settings.base_dir / path
        if not path.exists():
            if env_json_error:
                raise ProspectGmailError(
                    f"{env_json_error}. Also, Google service account file was not found: {path}"
                )
            raise ProspectGmailError(
                f"Google service account file was not found: {path}"
            )
        return service_account_module.Credentials.from_service_account_file(
            str(path), scopes=scopes
        )
