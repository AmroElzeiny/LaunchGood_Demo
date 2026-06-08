from __future__ import annotations

import asyncio
from pathlib import Path

from job_bot.logger import setup_logging
from job_bot.telegram_config import load_telegram_settings
from job_bot.zapcareers_telegram_bot import TelegramMenuBot


async def _run() -> None:
    cwd = Path(__file__).resolve().parent
    settings = load_telegram_settings(cwd)
    logger = setup_logging(
        settings.log_level,
        log_file_path=cwd / "state" / "runtime_logs" / "telegram-bot.txt",
    )
    logger.info(
        "Loaded Telegram opportunity bot settings | db=%s | logs=%s",
        settings.telegram_subs_db_path,
        settings.telegram_user_log_dir,
    )

    bot = TelegramMenuBot(settings=settings, logger=logger)
    await bot.run_long_polling()


if __name__ == "__main__":
    asyncio.run(_run())
