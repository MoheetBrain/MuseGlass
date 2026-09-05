"""WebSocket bridge (phone ↔ host) and the developer console.

Wire: every frame is either a protocol `Event` (see museglass/protocol/events.py) or a small
control message `{"type": "hello" | "welcome" | "transcript" | "ping" | "pong", ...}`.

Client → host:  hello {protocol_version, last_seq}   then USER_COMMAND / USER_INTERRUPT /
                USER_RESPONSE events, or {"type":"transcript","text":...,"speech_end_at":...}
Host → client:  welcome {protocol_version, session_id, state}, then replayed events with
                seq > last_seq, then live MUSE_* / SESSION_* events.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from museglass.host.orchestrator import SessionOrchestrator
from museglass.protocol.events import PROTOCOL_VERSION, Event

log = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get("MUSEGLASS_HOME", Path.home() / ".museglass"))


def load_or_create_token(config_dir: Path = CONFIG_DIR) -> str:
    env = os.environ.get("MUSEGLASS_TOKEN")
    if env:
        return env
    config_dir.mkdir(parents=True, exist_ok=True)
    token_file = config_dir / "token"
    if token_file.exists():
        return token_file.read_text().strip()
    token = secrets.token_urlsafe(24)
    token_file.write_text(token)
    token_file.chmod(0o600)
    return token


def create_app(orchestrator: SessionOrchestrator, token: str) -> FastAPI:
    app = FastAPI(title="MuseGlass bridge", version="0.1.0")

    def check(candidate: str | None) -> None:
        if not candidate or not secrets.compare_digest(candidate, token):
            raise HTTPException(status_code=401, detail="invalid token")

    @app.get("/api/state")
    async def state(token_param: str | None = Query(default=None, alias="token")) -> JSONResponse:
        check(token_param)
        return JSONResponse(orchestrator.snapshot())

    @app.post("/api/say")
    async def say(body: dict[str, Any], token_param: str | None = Query(default=None, alias="token")) -> JSONResponse:
        check(token_param)
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text required")
        intent = await orchestrator.handle_transcript(text, speech_end_at=body.get("speech_end_at"))
        return JSONResponse({"intent": intent.kind.value})

    @app.get("/console", response_class=HTMLResponse)
    async def console(token_param: str | None = Query(default=None, alias="token")) -> str:
        check(token_param)
        return CONSOLE_HTML.replace("__TOKEN__", token_param or "")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket, token_param: str | None = Query(default=None, alias="token")) -> None:
        if not token_param or not secrets.compare_digest(token_param, token):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        try:
            hello = json.loads(await asyncio.wait_for(websocket.receive_text(), timeout=15))
        except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
            await websocket.close(code=4400)
            return
        if hello.get("type") != "hello" or hello.get("protocol_version") != PROTOCOL_VERSION:
            await websocket.send_text(json.dumps({"type": "error", "message": f"expected hello with protocol_version {PROTOCOL_VERSION}"}))
            await websocket.close(code=4400)
            return
        last_seq = int(hello.get("last_seq") or 0)
        queue = orchestrator.subscribe()
        try:
            await websocket.send_text(json.dumps({"type": "welcome", "protocol_version": PROTOCOL_VERSION,
                                                  "session_id": orchestrator.session_id, "state": orchestrator.snapshot()}))
            for event in orchestrator.store.events_since(orchestrator.session_id, last_seq):
                if not event.is_user_event:
                    await websocket.send_text(event.to_json())
            if orchestrator.state.value in ("running", "waiting_approval", "waiting_answer") and last_seq:
                await websocket.send_text(json.dumps({"type": "status", "message": orchestrator.status_text(short=True)}))

            async def sender() -> None:
                while True:
                    event = await queue.get()
                    if not event.is_user_event:
                        await websocket.send_text(event.to_json())

            async def receiver() -> None:
                while True:
                    raw = await websocket.receive_text()
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(data, dict):
                        continue
                    if data.get("type") == "ping":
                        await websocket.send_text(json.dumps({"type": "pong"}))
                    elif data.get("type") == "transcript":
                        await orchestrator.handle_transcript(str(data.get("text") or ""), speech_end_at=data.get("speech_end_at"))
                    elif "type" in data and data.get("session_id") is not None:
                        with contextlib.suppress(ValueError):
                            await orchestrator.handle_event(Event.from_dict(data))

            send_task = asyncio.create_task(sender())
            recv_task = asyncio.create_task(receiver())
            done, pending = await asyncio.wait({send_task, recv_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                with contextlib.suppress(WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
                    task.result()
        except WebSocketDisconnect:
            pass
        finally:
            orchestrator.unsubscribe(queue)

    return app


CONSOLE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>MuseGlass console</title>
<style>
body{font-family:ui-monospace,Menlo,monospace;background:#0f1115;color:#e6e6e6;margin:0;padding:16px}
h1{font-size:16px;margin:0 0 12px} .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.card{background:#181b22;border:1px solid #262a33;border-radius:8px;padding:12px} .k{color:#8a93a6;font-size:11px;text-transform:uppercase}
.v{font-size:14px;margin:2px 0 10px;word-break:break-word} .ev{font-size:12px;padding:4px 0;border-bottom:1px solid #22262f}
.ev .t{color:#8a93a6} .MUSE_APPROVAL_REQUEST{color:#ffb454}.MUSE_QUESTION{color:#ffd166}.MUSE_COMPLETE{color:#7ee787}.MUSE_ERROR{color:#ff7b72}
.USER_COMMAND,.USER_RESPONSE,.USER_INTERRUPT{color:#79c0ff} input{width:70%;padding:8px;background:#0f1115;color:#fff;border:1px solid #333;border-radius:6px}
button{padding:8px 12px;border-radius:6px;border:0;background:#3b82f6;color:#fff} table{font-size:12px;border-collapse:collapse} td{padding:2px 8px 2px 0}
</style></head><body>
<h1>MuseGlass developer console</h1>
<div class="card" style="margin-bottom:12px"><input id="say" placeholder="Type what you would say (e.g. Muse, open the demo project)"> <button onclick="say()">Say</button></div>
<div class="grid">
<div class="card"><div class="k">Session</div><div class="v" id="session"></div><div class="k">Project</div><div class="v" id="project"></div>
<div class="k">Backend</div><div class="v" id="backend"></div><div class="k">Agent state</div><div class="v" id="state"></div><div class="k">Phase</div><div class="v" id="phase"></div><div class="k">Verbosity</div><div class="v" id="verbosity"></div></div>
<div class="card"><div class="k">Current task</div><div class="v" id="task"></div><div class="k">Last user command</div><div class="v" id="last"></div>
<div class="k">Pending approval / question</div><div class="v" id="pending"></div><div class="k">Files touched</div><div class="v" id="files"></div><div class="k">Tests</div><div class="v" id="tests"></div></div>
<div class="card"><div class="k">Latency (ms)</div><table id="latency"></table><div class="k" style="margin-top:8px">Tokens / cost</div><div class="v" id="usage"></div></div>
<div class="card" style="grid-column:1/-1"><div class="k">Recent events</div><div id="events"></div></div>
</div>
<script>
const token="__TOKEN__";
async function refresh(){try{const r=await fetch('/api/state?token='+token);const s=await r.json();
for(const k of ['session_id','project','backend','state','phase','verbosity','current_task','last_user_command']){const el=document.getElementById({session_id:'session',current_task:'task',last_user_command:'last'}[k]||k);if(el)el.textContent=s[k]??'—';}
document.getElementById('pending').textContent=s.pending?JSON.stringify(s.pending):'—';
document.getElementById('files').textContent=(s.files_touched||[]).join(', ')||'—';
document.getElementById('tests').textContent=s.tests?JSON.stringify(s.tests):'—';
document.getElementById('usage').textContent=(s.token_usage&&Object.keys(s.token_usage).length?JSON.stringify(s.token_usage):'n/a')+(s.cost_usd!=null?'  $'+s.cost_usd.toFixed(4):'');
let lat='';for(const [k,v] of Object.entries(s.latency||{})){lat+=`<tr><td>${k}</td><td>${v.count?`last ${v.last_ms} · p50 ${v.p50_ms} · max ${v.max_ms} (n=${v.count})`:'—'}</td></tr>`;}document.getElementById('latency').innerHTML=lat;
document.getElementById('events').innerHTML=(s.recent_events||[]).map(e=>`<div class="ev ${e.type}"><span class="t">${(e.timestamp||'').slice(11,19)} #${e.seq} ${e.type}</span> ${e.message}${e.requires_response?' ⏳':''}</div>`).join('');
}catch(e){console.error(e)}}
async function say(){const i=document.getElementById('say');const t=i.value.trim();if(!t)return;i.value='';await fetch('/api/say?token='+token,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({text:t})});refresh();}
document.getElementById('say').addEventListener('keydown',e=>{if(e.key==='Enter')say();});
setInterval(refresh,1000);refresh();
</script></body></html>"""
