from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def default_websites_refresh_signal_path() -> Path:
    return Path(__file__).resolve().parents[1] / "state" / "websites.refresh.signal"


def request_websites_refresh(signal_path: Path | None = None) -> None:
    path = signal_path or default_websites_refresh_signal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")


def consume_websites_refresh(signal_path: Path | None = None) -> bool:
    path = signal_path or default_websites_refresh_signal_path()
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
