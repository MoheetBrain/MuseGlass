"""Latency instrumentation for the conversational loop.

Metrics (all in milliseconds):
- speech_end_to_transcript   user stopped talking → transcript available
- speech_end_to_agent_ack    user stopped talking → backend acknowledged the instruction
- agent_event_to_spoken      agent event arrived → TTS started speaking about it
- interrupt_to_ack           "stop" transcript → backend confirmed the turn ended
"""

from __future__ import annotations

import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass
class Sample:
    metric: str
    ms: float
    at: float


class LatencyTracker:
    METRICS = ("speech_end_to_transcript", "speech_end_to_agent_ack", "agent_event_to_spoken", "interrupt_to_ack")

    def __init__(self, keep: int = 200) -> None:
        self._samples: dict[str, deque[Sample]] = defaultdict(lambda: deque(maxlen=keep))
        self._marks: dict[str, float] = {}

    # marks: remember a start time under a key, close it later
    def mark(self, key: str, at: float | None = None) -> None:
        self._marks[key] = at if at is not None else time.monotonic()

    def has_mark(self, key: str) -> bool:
        return key in self._marks

    def close(self, key: str, metric: str, at: float | None = None) -> float | None:
        start = self._marks.pop(key, None)
        if start is None:
            return None
        end = at if at is not None else time.monotonic()
        ms = (end - start) * 1000.0
        self.record(metric, ms)
        return ms

    def record(self, metric: str, ms: float) -> None:
        self._samples[metric].append(Sample(metric, ms, time.monotonic()))

    def summary(self) -> dict[str, dict[str, float | int]]:
        out: dict[str, dict[str, float | int]] = {}
        for metric in self.METRICS:
            samples = [s.ms for s in self._samples.get(metric, [])]
            if not samples:
                out[metric] = {"count": 0}
                continue
            out[metric] = {
                "count": len(samples),
                "last_ms": round(samples[-1], 1),
                "p50_ms": round(statistics.median(samples), 1),
                "max_ms": round(max(samples), 1),
            }
        return out
