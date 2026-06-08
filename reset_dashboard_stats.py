from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from job_bot.storage import StateStore
from job_bot.telegram_subscription_store import SubscriptionStore


def main() -> None:
    cwd = Path(__file__).resolve().parent
    load_dotenv(cwd / ".env")

    state_db = Path(os.getenv("STATE_DB_PATH", str(cwd / "state" / "job_bot_state.db")))
    subs_db = Path(os.getenv("TELEGRAM_SUBS_DB_PATH", str(cwd / "state" / "telegram_subscriptions.db")))

    state_store = StateStore(state_db)
    subs_store = SubscriptionStore(subs_db)
    try:
        state_store.reset_dashboard_stats()
        subs_store.reset_dashboard_stats()
    finally:
        state_store.close()
        subs_store.close()

    print(f"Reset dashboard analytics in {state_db} and {subs_db}.")
    print("Preserved active subscriptions, user preferences, selected websites, keywords, spheres, and trial claims.")


if __name__ == "__main__":
    main()
