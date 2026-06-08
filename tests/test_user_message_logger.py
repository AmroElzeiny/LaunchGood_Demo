from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from job_bot.user_message_logger import UserMessageLogger


class UserMessageLoggerTests(unittest.TestCase):
    def test_append_event_creates_per_user_file_and_appends(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir) / "user_logs"
            logger = UserMessageLogger(log_dir)

            logger.append_event(
                user_id=7001,
                username="alice",
                direction="in",
                text="Hello\nTeam",
                event_type="message",
                created_at_utc="2026-03-07T11:00:00Z",
            )
            logger.append_event(
                user_id=7001,
                username="alice",
                direction="out",
                text="Hi there",
                event_type="bot_message",
                created_at_utc="2026-03-07T11:00:01Z",
            )

            user_file = log_dir / "7001.txt"
            self.assertTrue(user_file.exists())
            lines = user_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn("direction=in", lines[0])
            self.assertIn("event=message", lines[0])
            self.assertIn("Hello\\nTeam", lines[0])
            self.assertIn("direction=out", lines[1])


if __name__ == "__main__":
    unittest.main()
