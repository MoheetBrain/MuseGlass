"""Local streaming STT: ffmpeg microphone capture → WebRTC VAD endpointing → mlx-whisper.

Runs entirely on the Mac (Apple Silicon). Emits one final transcript per utterance; the
endpoint is detected by a silence tail so `speech_end_at` is accurate for latency
measurement. Partial transcripts are not produced by this provider (whisper is not
incremental); a cloud streaming provider can be dropped in for that.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator

from museglass.speech.base import SpeechToTextProvider, Transcript

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
FRAME_MS = 30
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2  # int16 mono
DEFAULT_MODEL = os.environ.get("MUSEGLASS_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")


class LocalWhisperSTT(SpeechToTextProvider):
    name = "local-whisper"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        device_index: int = 0,
        vad_aggressiveness: int = 2,
        silence_tail_ms: int = 700,
        min_speech_ms: int = 300,
        max_utterance_s: float = 30.0,
        language: str = "en",
    ) -> None:
        self.model = model
        self.device_index = device_index
        self.vad_aggressiveness = vad_aggressiveness
        self.silence_tail_frames = max(1, silence_tail_ms // FRAME_MS)
        self.min_speech_frames = max(1, min_speech_ms // FRAME_MS)
        self.max_utterance_frames = int(max_utterance_s * 1000 / FRAME_MS)
        self.language = language
        self._queue: asyncio.Queue[Transcript | None] = asyncio.Queue()
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._transcribe_lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------------------
    async def start(self) -> None:
        import webrtcvad  # noqa: F401  (fail early if missing)

        # Warm the model in a thread so the first utterance is not slow.
        await asyncio.to_thread(self._warm_model)
        self._proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "avfoundation", "-i", f":{self.device_index}",
            "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        self._reader = asyncio.create_task(self._read_loop(), name="whisper-stt-reader")

    async def stop(self) -> None:
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            with contextlib.suppress(ProcessLookupError):
                await self._proc.wait()
        await self._queue.put(None)

    async def transcripts(self) -> AsyncIterator[Transcript]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    # -- internals ---------------------------------------------------------------------
    def _warm_model(self) -> None:
        import mlx_whisper
        import numpy as np

        silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
        mlx_whisper.transcribe(silence, path_or_hf_repo=self.model, language=self.language, fp16=True, verbose=None)

    async def _read_loop(self) -> None:
        import webrtcvad

        assert self._proc and self._proc.stdout
        vad = webrtcvad.Vad(self.vad_aggressiveness)
        stdout = self._proc.stdout
        speech: list[bytes] = []
        in_speech = False
        silent = 0
        voiced_run = 0
        pre_roll: list[bytes] = []
        while True:
            frame = await stdout.readexactly(FRAME_BYTES) if not stdout.at_eof() else b""
            if not frame:
                err = await self._proc.stderr.read() if self._proc.stderr else b""
                log.error("ffmpeg microphone stream ended: %s", err.decode(errors="replace").strip())
                await self._queue.put(None)
                return
            voiced = vad.is_speech(frame, SAMPLE_RATE)
            if not in_speech:
                pre_roll.append(frame)
                if len(pre_roll) > 8:
                    pre_roll.pop(0)
                voiced_run = voiced_run + 1 if voiced else 0
                if voiced_run >= 3:
                    in_speech = True
                    speech = list(pre_roll)
                    silent = 0
                continue
            speech.append(frame)
            if voiced:
                silent = 0
            else:
                silent += 1
            if silent >= self.silence_tail_frames or len(speech) >= self.max_utterance_frames:
                speech_end_at = time.monotonic() - silent * FRAME_MS / 1000.0
                utterance = b"".join(speech)
                in_speech = False
                voiced_run = 0
                pre_roll = []
                speech = []
                if len(utterance) // FRAME_BYTES >= self.min_speech_frames:
                    asyncio.create_task(self._transcribe(utterance, speech_end_at))

    async def _transcribe(self, pcm: bytes, speech_end_at: float) -> None:
        async with self._transcribe_lock:
            try:
                text = await asyncio.to_thread(self._run_whisper, pcm)
            except Exception as exc:  # noqa: BLE001
                log.exception("transcription failed: %s", exc)
                return
        text = text.strip()
        if text and not _is_noise(text):
            await self._queue.put(Transcript(text=text, speech_end_at=speech_end_at))

    def _run_whisper(self, pcm: bytes) -> str:
        import mlx_whisper
        import numpy as np

        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        result = mlx_whisper.transcribe(
            audio, path_or_hf_repo=self.model, language=self.language, fp16=True, verbose=None,
            condition_on_previous_text=False,
        )
        return str(result.get("text", ""))


_NOISE = {"", ".", "you", "thank you.", "thanks for watching.", "bye.", "[blank_audio]", "(silence)"}


def _is_noise(text: str) -> bool:
    return text.strip().lower() in _NOISE
