"""Replay recorded (or documented) HTTP responses through httpx.MockTransport."""

import json
from pathlib import Path

import httpx

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "llm"


def load(provider: str, name: str) -> dict:
    return json.loads((FIXTURES / provider / f"{name}.json").read_text(encoding="utf-8"))


class Replay:
    """An httpx client whose every request gets the next queued response; requests are kept."""

    def __init__(self, *responses: dict) -> None:
        self.queue = list(responses)
        self.requests: list[httpx.Request] = []
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.queue.pop(0)
        headers = {"content-type": r.get("content_type", "text/event-stream"), **r.get("headers", {})}
        return httpx.Response(r["status"], headers=headers, content=r["body"].encode())

    def body(self, n: int = -1) -> dict:
        return json.loads(self.requests[n].content)


def error(status: int, body: dict, headers: dict | None = None) -> dict:
    return {"status": status, "body": json.dumps(body), "content_type": "application/json", "headers": headers or {}}
