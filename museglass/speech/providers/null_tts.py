"""TTS that records instead of speaking (tests, console-only runs)."""

from __future__ import annotations

import asyncio

from museglass.speech.base import TextToSpeechProvider


class NullTTS(TextToSpeechProvider):
    name = "null"

    def __init__(self, *, delay: float = 0.0, echo: bool = False) -> None:
        self.spoken: list[str] = []
        self.delay = delay
        self.echo = echo
        self._current: asyncio.Task | None = None

    async def speak(self, text: str) -> bool:
        self.spoken.append(text)
        if self.echo:
            print(f"\n🔈 {text}", flush=True)
        if self.delay:
            self._current = asyncio.current_task()
            try:
                await asyncio.sleep(self.delay)
            except asyncio.CancelledError:
                return False
            finally:
                self._current = None
        return True

    async def stop(self) -> None:
        if self._current and not self._current.done():
            self._current.cancel()
