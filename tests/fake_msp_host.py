"""A fake `muse serve` that replays a recorded MSP conformance transcript.

Usage: python tests/fake_msp_host.py <transcript.ndjson>

For every client line in the transcript it reads one frame from stdin and checks the method
(or, for responses to server requests, that a result was returned). It then writes the
server lines that follow, rewriting response ids to the ids the real client used. Params
are not compared byte-for-byte: the transcripts were hand-authored with different ids and
choices; the method sequence and the request/response discipline are what conformance means
for this client.
"""

from __future__ import annotations

import json
import sys


def main(path: str) -> int:
    lines = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    frames = [(entry["dir"], json.loads(entry["raw"])) for entry in lines]
    id_map: dict[str, object] = {}  # transcript request id -> real client id
    i = 0
    out = sys.stdout
    while i < len(frames):
        direction, frame = frames[i]
        if direction == "client":
            raw = sys.stdin.readline()
            if not raw:
                print(f"fake host: client hung up before frame {i}", file=sys.stderr)
                return 3
            got = json.loads(raw)
            expected_method = frame.get("method")
            if expected_method is not None:
                if got.get("method") != expected_method:
                    print(f"fake host: expected {expected_method} got {got.get('method')}", file=sys.stderr)
                    return 4
                if "id" in frame:
                    id_map[json.dumps(frame["id"])] = got.get("id")
            else:  # client response to a server request
                if "result" not in got and "error" not in got:
                    print("fake host: expected a response frame", file=sys.stderr)
                    return 5
            i += 1
            continue
        # server frame: rewrite response ids
        if "method" not in frame and "id" in frame:
            key = json.dumps(frame["id"])
            if key in id_map:
                frame = dict(frame, id=id_map[key])
        out.write(json.dumps(frame, separators=(",", ":")) + "\n")
        out.flush()
        i += 1
    # keep stdin open until the client closes it, like a real host waiting for more commands
    while sys.stdin.readline():
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
