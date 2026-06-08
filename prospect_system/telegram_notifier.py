from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from prospect_system.prospect_config import ProspectSettings


@dataclass(slots=True)
class TelegramNotifyResult:
    success: bool
    message: str


def send_prospect_usage_notification(
    settings: ProspectSettings, *, input_urls: list[str]
) -> TelegramNotifyResult:
    token = settings.telegram_bot_token.strip()
    chat_id = settings.prospect_usage_notify_chat_id.strip()
    if not token or not chat_id:
        return TelegramNotifyResult(
            False, "Telegram usage notification is not configured."
        )

    timestamp = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    websites = "\n".join(f"- {url}" for url in input_urls) or "- Unknown"
    text = (
        "Prospect dashboard is being used.\n\n"
        f"Time: {timestamp}\n"
        f"Target pages per site: {settings.max_pages_to_scrape}\n"
        f"Website:\n{websites}"
    )
    payload = urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        method="POST",
    )
    try:
        with urlopen(request, timeout=8) as response:
            if 200 <= int(response.status) < 300:
                return TelegramNotifyResult(True, "Telegram usage notification sent.")
            return TelegramNotifyResult(
                False, f"Telegram API returned HTTP {response.status}."
            )
    except Exception as exc:  # noqa: BLE001
        return TelegramNotifyResult(False, f"Telegram usage notification failed: {exc}")
