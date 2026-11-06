"""`make record-fixtures`: record real Gemini responses for tests/agents/test_gemini_adapter.py.

Spends 3 requests on the primary model, counted by the daily budget like any other call:
  text.json         a plain streamed answer
  tool_call.json    a streamed functionCall (with Gemini 3's thoughtSignature)
  tool_result.json  the follow-up that sends the functionResponse and the signature back

Only the URL (which carries no key), the request body and the response are saved; the
x-goog-api-key header is never written. tests/agents/test_fixtures_clean.py checks that.
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx

from app import clock
from app.agents.providers.base import ChatRequest, Message, ToolCallEvent, ToolSpec
from app.agents.providers.budget import DailyBudget
from app.agents.providers.gemini import GeminiAdapter
from app.agents.providers.limiter import limiter_for
from app.agents.providers.router import ModelRouter
from app.config import get_settings
from app.db.session import app_engine

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "llm" / "gemini"

SEARCH = ToolSpec(
    name="search_knowledge",
    description="Search the shop's knowledge base for policies and product descriptions.",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "What to search for"}},
        "required": ["query"],
        "additionalProperties": False,
    },
)
SYSTEM = (
    "You are the assistant of a tea shop. Always call search_knowledge before answering a "
    "question about shop policies, then answer briefly using only what it returns."
)


class RecordingTransport(httpx.AsyncBaseTransport):
    """Passes requests through and keeps the last exchange, minus any credentials."""

    def __init__(self) -> None:
        self.inner = httpx.AsyncHTTPTransport()
        self.last: dict = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.headers["accept-encoding"] = "identity"  # store readable SSE, not gzip
        resp = await self.inner.handle_async_request(request)
        raw = b"".join([part async for part in resp.stream])
        await resp.aclose()
        self.last = {
            "url": str(request.url),
            "request_body": json.loads(request.content),
            "status": resp.status_code,
            "content_type": resp.headers.get("content-type", ""),
            "body": raw.decode("utf-8"),
        }
        return httpx.Response(resp.status_code, headers=resp.headers, content=raw)


async def main() -> int:
    s = get_settings()
    key = s.gemini_api_key.get_secret_value()
    if not key:
        print("GEMINI_API_KEY is not set in .env", file=sys.stderr)
        return 2
    model = s.llm_model_gemini
    rec = RecordingTransport()
    adapter = GeminiAdapter(key, httpx.AsyncClient(transport=rec, timeout=httpx.Timeout(10.0, read=120.0)))
    # Primary model only, through the real router: budget counted, RPM respected.
    router = ModelRouter(adapter, [model], DailyBudget("gemini", s.llm_daily_budget_gemini), {model: limiter_for("gemini", model, s.llm_rpm_gemini)})
    OUT.mkdir(parents=True, exist_ok=True)

    async def call(name: str, req: ChatRequest) -> list:
        events = [e async for e in router.stream(req)]
        fixture = {"source": f"RECORDED from {model} on {clock.now():%Y-%m-%d} by make record-fixtures", "model": model, **rec.last}
        (OUT / f"{name}.json").write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"recorded {name}.json ({len(events)} events)")
        return events

    await call("text", ChatRequest(system="", temperature=0.0, messages=[
        Message("user", "Reply with exactly this sentence and nothing else: Namaste from Himali Tea House."),
    ]))  # fmt: skip
    question = Message("user", "What is your return policy for opened tea?")
    events = await call("tool_call", ChatRequest(system=SYSTEM, messages=[question], tools=[SEARCH], temperature=0.0))
    calls = [e.call for e in events if isinstance(e, ToolCallEvent)]
    if not calls:
        print("the model answered without calling search_knowledge; re-run to record a tool call", file=sys.stderr)
        return 1
    result = json.dumps({"answerable": True, "results": [{
        "citation": "returns-and-refunds#opened-tea",
        "content": "Opened tea can't be returned unless it is faulty; unopened pouches can be returned within fourteen days.",
    }]})  # fmt: skip
    await call("tool_result", ChatRequest(system=SYSTEM, tools=[SEARCH], temperature=0.0, messages=[
        question,
        Message("assistant", "", tool_calls=calls),
        *(Message("tool", result, tool_call_id=c.id, name=c.name) for c in calls),
    ]))  # fmt: skip
    await app_engine.dispose()
    return 0


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(main()))
