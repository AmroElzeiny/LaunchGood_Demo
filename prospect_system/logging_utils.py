from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from prospect_system.dashboard_state import ProspectAnalysisState, StepLog
from prospect_system.errors import ErrorRecord


def get_prospect_logger(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("prospect-system")
    logger.setLevel(logging.INFO)
    if not any(
        isinstance(handler, logging.FileHandler)
        and handler.baseFilename.endswith("prospect-system.log")
        for handler in logger.handlers
    ):
        handler = logging.FileHandler(log_dir / "prospect-system.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


class StateJSONLLogger:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def write_step(self, state: ProspectAnalysisState, step: StepLog) -> None:
        self._append(
            "analysis_steps.jsonl",
            {
                "session_id": state.session_id,
                **asdict(step),
            },
        )

    def write_error(self, error: ErrorRecord) -> None:
        self._append("errors.jsonl", asdict(error))

    def write_state_snapshot(self, state: ProspectAnalysisState) -> None:
        self._append("analysis_runs.jsonl", state.to_dict())

    def write_decision(
        self, state: ProspectAnalysisState, payload: dict[str, Any]
    ) -> None:
        self._append(
            "decisions.jsonl",
            {
                "session_id": state.session_id,
                **payload,
            },
        )

    def _append(self, filename: str, payload: dict[str, Any]) -> None:
        path = self.log_dir / filename
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
