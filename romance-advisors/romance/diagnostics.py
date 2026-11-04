"""Bounded timing facts for one local workbench, never prompts or access URLs."""
from __future__ import annotations

from collections import deque
import copy
import math
import re
import time


class PerformanceMetrics:
    """Use monotonic time for durations and wall time only to locate recordings."""

    IDENTIFIERS = {"session_id", "episode_id", "round_id", "job_id", "run_id"}

    def __init__(self, limit=200):
        self.events = deque(maxlen=limit)
        self.sequence = 0

    def record(self, stage, *, started=None, outcome=None, code=None, **ids):
        if not isinstance(stage, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", stage):
            return
        now = time.monotonic()
        self.sequence += 1
        event = {"seq": self.sequence, "stage": stage, "time": time.time(), "monotonic": now}
        if isinstance(started, (int, float)) and math.isfinite(started):
            event["elapsed_seconds"] = round(max(0, now - started), 6)
        for key, value in {"outcome": outcome, "code": code, **ids}.items():
            if key not in self.IDENTIFIERS | {"outcome", "code"}:
                continue
            # Callers cannot accidentally log an exception, body or bearer URL.
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
                event[key] = value
        self.events.append(event)

    def snapshot(self):
        return copy.deepcopy(list(self.events))
