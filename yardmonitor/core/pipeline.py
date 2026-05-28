"""Abstract base class for all sensor processing pipelines."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class BasePipeline(ABC):
    """
    Base for every sensor pipeline in YardMonitor.

    Subclasses implement `run(**kwargs) -> dict` and can use the built-in
    state-persistence helpers to make long pipelines resumable.
    """

    sensor_type: str = "base"

    def __init__(self, config: dict):
        self.config = config
        self.state: dict = {}

    @abstractmethod
    def run(self, **kwargs) -> dict:
        """Execute the full pipeline. Returns a summary dict."""

    # ── State persistence ─────────────────────────────────────────────────

    def load_state(self, state_path: Path) -> bool:
        """Load persisted pipeline state. Returns True if state was found."""
        if state_path.exists():
            with open(state_path, encoding="utf-8") as f:
                self.state = json.load(f)
            logger.info("Resumed pipeline state from %s", state_path)
            return True
        return False

    def save_state(self, state_path: Path) -> None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, default=str)

    def mark_step_complete(
        self, step: str, state_path: Path, result: Any = None
    ) -> None:
        self.state.setdefault("completed_steps", {})[step] = {
            "timestamp": datetime.now().isoformat(),
            "result": result,
        }
        self.save_state(state_path)
        logger.info("Step '%s' complete", step)

    def is_step_complete(self, step: str) -> bool:
        return step in self.state.get("completed_steps", {})
