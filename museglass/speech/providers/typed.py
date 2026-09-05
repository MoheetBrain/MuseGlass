"""Typed-text "speech" provider: deterministic input for tests, the console and stdin."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from museglass.speech.base import SpeechToTextProvider, Transcript


class TypedTextSTT(SpeechToTextProvider):
    name = "typed"

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Transcript | None] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._running = True

    async def stop(self) -> None:
        self._running = False
        await self._queue.put(None)

    def push(self, text: str, *, speech_end_at: float | None = None) -> None:
        """Thread-safe: may be called from a reader thread."""
        transcript = Transcript(text=text.strip(), speech_end_at=speech_end_at or time.monotonic())
        if self._loop and self._loop.is_running() and asyncio.get_event_loop_policy() and not self._in_loop_thread():
            self._loop.call_soon_threadsafe(self._queue.put_nowait, transcript)
        else:
            self._queue.put_nowait(transcript)

    def _in_loop_thread(self) -> bool:
        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    async def transcripts(self) -> AsyncIterator[Transcript]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if item.text:
                yield item
