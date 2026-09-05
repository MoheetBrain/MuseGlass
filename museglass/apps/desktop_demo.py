"""The V0 desktop app: microphone → coding agent → speakers, on one Mac.

    museglass                       # auto backend, local whisper STT, macOS say TTS
    museglass --stt typed           # type what you would say (still needs "Muse, …")
    museglass --backend scripted    # offline demo agent (real edits, real tests, no LLM)
    museglass --console             # also serve the developer console on http://127.0.0.1:8765
    museglass --resume <session-id> # reconnect to a previous session
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
import threading
from pathlib import Path

from museglass.agent.registry import select_agent
from museglass.host.orchestrator import SessionOrchestrator
from museglass.host.workspace import WorkspaceRegistry
from museglass.protocol.events import Event, EventType
from museglass.speech.base import SpeechToTextProvider, TextToSpeechProvider
from museglass.speech.providers.null_tts import NullTTS
from museglass.speech.providers.typed import TypedTextSTT
from museglass.store.sqlite import SessionStore
from museglass.summariser.summariser import Verbosity

CONFIG_DIR = Path(os.environ.get("MUSEGLASS_HOME", Path.home() / ".museglass"))

_ICON = {
    EventType.MUSE_PROGRESS: "🟢", EventType.MUSE_QUESTION: "❓", EventType.MUSE_APPROVAL_REQUEST: "🔐",
    EventType.MUSE_COMPLETE: "✅", EventType.MUSE_ERROR: "❌", EventType.SESSION_STARTED: "▶️",
    EventType.SESSION_PAUSED: "⏸", EventType.SESSION_RESUMED: "⏯", EventType.SESSION_ENDED: "⏹",
}


def print_event(event: Event) -> None:
    if event.is_user_event:
        print(f"🎤 you: {event.message}", flush=True)
    else:
        print(f"{_ICON.get(event.type, '•')} muse: {event.message}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="museglass", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backend", default=os.environ.get("MUSEGLASS_BACKEND", "auto"), help="auto | muse | claude | scripted")
    p.add_argument("--stt", default=os.environ.get("MUSEGLASS_STT", "whisper"), choices=["whisper", "typed"])
    p.add_argument("--tts", default=os.environ.get("MUSEGLASS_TTS", "say"), choices=["say", "null"])
    p.add_argument("--voice", default=os.environ.get("MUSEGLASS_VOICE", "Daniel"))
    p.add_argument("--project", help="open this registered project immediately")
    p.add_argument("--workspaces", help="workspaces root (default ~/MuseWorkspaces or $MUSEGLASS_WORKSPACES)")
    p.add_argument("--verbosity", default="normal", choices=["quiet", "normal", "talkative"])
    p.add_argument("--resume", metavar="SESSION_ID", help="reconnect to an existing session")
    p.add_argument("--console", action="store_true", help="serve the developer console + WebSocket bridge")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-wake-word", action="store_true", help="do not require 'Muse, …' before commands")
    p.add_argument("--db", help="session store path (default ~/.museglass/museglass.db)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def make_tts(kind: str, voice: str) -> TextToSpeechProvider:
    if kind == "say":
        from museglass.speech.providers.macos_say import MacSayTTS

        return MacSayTTS(voice=voice)
    return NullTTS(echo=True)


def make_stt(kind: str) -> SpeechToTextProvider:
    if kind == "whisper":
        from museglass.speech.providers.local_whisper import LocalWhisperSTT

        return LocalWhisperSTT()
    return TypedTextSTT()


async def run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    registry = WorkspaceRegistry(args.workspaces)
    store = SessionStore(args.db or CONFIG_DIR / "museglass.db")
    agent, report = await select_agent(args.backend)
    for name, health in report.items():
        print(f"backend {name}: {'available' if health.available else 'unavailable'} — {health.reason}", flush=True)
    print(f"using backend: {agent.name}", flush=True)
    tts = make_tts(args.tts, args.voice)
    stt = make_stt(args.stt)
    from museglass.host.router import CommandRouter

    orchestrator = SessionOrchestrator(
        agent=agent, store=store, registry=registry, tts=tts, verbosity=Verbosity.parse(args.verbosity),
        session_id=args.resume, router=CommandRouter(wake_word_required=not args.no_wake_word),
    )
    orchestrator.add_listener(print_event)
    await orchestrator.start(resume=bool(args.resume))
    print(f"session {orchestrator.session_id} (resume later with --resume {orchestrator.session_id})", flush=True)
    server_task = None
    if args.console:
        import uvicorn

        from museglass.bridge.server import create_app, load_or_create_token

        token = load_or_create_token(CONFIG_DIR)
        config = uvicorn.Config(create_app(orchestrator, token), host=args.host, port=args.port, log_level="warning")
        server = uvicorn.Server(config)
        server_task = asyncio.create_task(server.serve())
        print(f"console: http://{args.host}:{args.port}/console?token={token}", flush=True)
        print(f"bridge:  ws://{args.host}:{args.port}/ws?token={token}", flush=True)
    if args.project:
        await orchestrator.open_project(args.project)
    await stt.start()
    if isinstance(stt, TypedTextSTT):
        _start_stdin_reader(stt, asyncio.get_running_loop())
        print("type what you would say (Ctrl-D to quit):", flush=True)
    else:
        print("listening… say: Muse, open the demo project.", flush=True)
    try:
        async for transcript in stt.transcripts():
            await orchestrator.handle_transcript(transcript.text, speech_end_at=transcript.speech_end_at)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            await stt.stop()
        await orchestrator.close()
        if server_task:
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await server_task
        store.close()
    return 0


def _start_stdin_reader(stt: TypedTextSTT, loop: asyncio.AbstractEventLoop) -> None:
    def reader() -> None:
        for line in sys.stdin:
            line = line.strip()
            if line:
                loop.call_soon_threadsafe(stt.push, line)
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(stt.stop()))

    threading.Thread(target=reader, name="stdin-reader", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


def main_console(argv: list[str] | None = None) -> int:
    """`museglass-console`: typed input + console, no microphone, no speech."""
    argv = list(argv if argv is not None else sys.argv[1:])
    return main(["--stt", "typed", "--tts", "null", "--console", *argv])


if __name__ == "__main__":
    sys.exit(main())
