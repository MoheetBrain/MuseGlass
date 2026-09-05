"""macOS `say` text-to-speech: offline, zero setup, killable for barge-in."""

from __future__ import annotations

import asyncio
import shutil

from museglass.speech.base import TextToSpeechProvider


class MacSayTTS(TextToSpeechProvider):
    name = "macos-say"

    def __init__(self, voice: str | None = "Daniel", rate: int = 190) -> None:
        if shutil.which("say") is None:
            raise RuntimeError("macOS `say` not found; use another TTS provider")
        self.voice = voice
        self.rate = rate
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def speak(self, text: str) -> bool:
        text = text.strip()
        if not text:
            return True
        args = ["say", "-r", str(self.rate)]
        if self.voice:
            args += ["-v", self.voice]
        args.append(text)
        async with self._lock:
            self._proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            proc = self._proc
        try:
            code = await proc.wait()
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.terminate()
            raise
        finally:
            if self._proc is proc:
                self._proc = None
        return code == 0

    async def stop(self) -> None:
        proc = self._proc
        if proc and proc.returncode is None:
            proc.terminate()
