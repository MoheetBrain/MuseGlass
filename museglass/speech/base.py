"""Speech provider interfaces. Providers are swappable; the host only sees these."""

from __future__ import annotations

import abc
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field


@dataclass
class Transcript:
    text: str
    is_final: bool = True
    speech_end_at: float | None = None  # time.monotonic() when the user stopped speaking
    received_at: float = field(default_factory=time.monotonic)
    confidence: float | None = None


class SpeechToTextProvider(abc.ABC):
    name: str = "abstract"

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    def transcripts(self) -> AsyncIterator[Transcript]:
        """Yield transcripts as they become available (partials have is_final=False)."""


class TextToSpeechProvider(abc.ABC):
    name: str = "abstract"

    @abc.abstractmethod
    async def speak(self, text: str) -> bool:
        """Speak `text`; return True if it finished, False if it was stopped early."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Cut current playback short (barge-in)."""
