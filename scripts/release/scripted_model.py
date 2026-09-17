"""A chat-completions server that answers the release drill, and nothing else.

The drill needs a task that plans, asks to overwrite a file, is approved, writes
it and is verified - on an installed build, on a clean machine, with no provider
key. This answers each kind of request by its shape rather than by a script of
turns, so a retry or a resumed run still gets a sensible reply:

* a request offering tools, with no tool result yet: call `fs.write`;
* a request offering tools, after a tool result: finish;
* a request whose prompt asks for `"passed"`: pass;
* a request whose prompt asks for `"steps"`: one step;
* anything else: a short sentence.

Standard library only, so it runs wherever the drill runs.
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TARGET = "report.md"
CONTENT = "# Drill report\n\nWritten by the release drill after approval.\n"


def answer(payload: dict) -> dict:
    messages = payload.get("messages", [])
    text = json.dumps(messages)
    tools = payload.get("tools") or []
    if tools:
        if any(message.get("role") == "tool" for message in messages):
            return {"role": "assistant", "content": "The report was written."}
        name = next(
            (tool["function"]["name"] for tool in tools if tool["function"]["name"] == "fs_write"),
            tools[0]["function"]["name"],
        )
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "drill-write",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps({"path": TARGET, "content": CONTENT}),
                    },
                }
            ],
        }
    if '\\"passed\\"' in text or '"passed"' in text:
        reply = {"passed": True, "reason": "The report exists with the requested content."}
        return {"role": "assistant", "content": json.dumps(reply)}
    if '\\"steps\\"' in text or '"steps"' in text:
        reply = {"steps": [{"description": "Overwrite report.md with the drill report"}]}
        return {"role": "assistant", "content": json.dumps(reply)}
    return {"role": "assistant", "content": "Understood."}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/models"):
            self._send({"object": "list", "data": [{"id": "drill", "object": "model"}]})
            return
        self._send({"status": "ok"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        message = answer(payload)
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        self._send(
            {
                "id": "drill",
                "object": "chat.completion",
                "model": payload.get("model", "drill"),
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    def _send(self, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


def serve(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=11500)
    arguments = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", arguments.port), Handler).serve_forever()
