from __future__ import annotations

from pathlib import Path
from threading import Lock


class UserMessageLogger:
    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def append_event(
        self,
        user_id: int,
        username: str,
        direction: str,
        text: str,
        event_type: str,
        created_at_utc: str,
    ) -> None:
        safe_username = (username or "").strip() or "unknown"
        safe_text = (text or "").replace("\r", "\\r").replace("\n", "\\n")
        line = (
            f"{created_at_utc} | direction={direction} | event={event_type} | "
            f"user={safe_username} | text={safe_text}\n"
        )
        user_log_path = self._log_dir / f"{user_id}.txt"
        with self._lock:
            with user_log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(line)
