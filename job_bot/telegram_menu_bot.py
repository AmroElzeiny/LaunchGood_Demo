from __future__ import annotations

"""Compatibility shim for the canonical Telegram bot UI module.

`job_bot.zapcareers_telegram_bot` is now the single maintained bot/UI implementation.
This module only re-exports the canonical symbols so older imports keep working
without maintaining a second drifting codepath.
"""

from job_bot.zapcareers_telegram_bot import (
    CALLBACK_MONTHLY_PLAN,
    CALLBACK_QUARTERLY_PLAN,
    CALLBACK_WEEKLY_PLAN,
    CALLBACK_YEARLY_PLAN,
    PLAN_DEFINITIONS,
    PlanDefinition,
    TelegramMenuBot,
)

__all__ = [
    "CALLBACK_MONTHLY_PLAN",
    "CALLBACK_QUARTERLY_PLAN",
    "CALLBACK_WEEKLY_PLAN",
    "CALLBACK_YEARLY_PLAN",
    "PLAN_DEFINITIONS",
    "PlanDefinition",
    "TelegramMenuBot",
]
