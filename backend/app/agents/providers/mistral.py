"""Mistral adapter: chat completions with stream=true (OpenAI-style SSE), via httpx.

Built and tested against hand-built fixtures of the documented format only: this account
has no Mistral key (the free plan needs an upgrade), so it has never made a live call.
"""

import hashlib
import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.agents.providers.base import (
    TRANSIENT_STATUSES,
    ChatRequest,
    Event,
    Finished,
    ProviderAuthError,
    ProviderError,
    ProviderOverloaded,
    RateLimited,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    Usage,
)
from app.agents.providers.sse import iter_sse_data

URL = "https://api.mistral.ai/v1/chat/completions"
_ID = re.compile(r"[A-Za-z0-9]{9}")
_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_FINISH = {"stop": "stop", "tool_calls": "tool_calls", "length": "length", "model_length": "length"}


def _mistral_id(call_id: str) -> str:
    """Mistral rejects tool-call ids that aren't 9 alphanumerics (e.g. ids made by Gemini
    earlier in the conversation). Map them deterministically so call and result still match."""
    if _ID.fullmatch(call_id):
        return call_id
    digest = hashlib.sha256(call_id.encode()).digest()
    return "".join(_ALNUM[b % len(_ALNUM)] for b in digest[:9])


class MistralAdapter:
    name = "mistral"

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None, url: str = URL) -> None:
        self._key = api_key
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=120.0))
        self._url = url

    def build_body(self, model: str, req: ChatRequest) -> dict[str, Any]:
        messages: list[dict] = [{"role": "system", "content": req.system}] if req.system else []
        for m in req.messages:
            if m.role == "user":
                messages.append({"role": "user", "content": m.content})
            elif m.role == "assistant":
                msg: dict[str, Any] = {"role": "assistant", "content": m.content}
                if m.tool_calls:
                    msg["tool_calls"] = [
                        {"id": _mistral_id(c.id), "type": "function",
                         "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                        for c in m.tool_calls
                    ]  # fmt: skip
                messages.append(msg)
            else:
                messages.append(
                    {
                        "role": "tool",
                        "content": m.content,
                        "tool_call_id": _mistral_id(m.tool_call_id or ""),
                        "name": m.name,
                    }
                )
        body: dict[str, Any] = {
            "model": model, "messages": messages, "stream": True,
            "temperature": req.temperature, "max_tokens": req.max_output_tokens,
        }  # fmt: skip
        if req.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in req.tools
            ]
        return body

    async def stream_once(self, model: str, req: ChatRequest) -> AsyncIterator[Event]:
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "text/event-stream"}
        async with self._client.stream("POST", self._url, json=self.build_body(model, req), headers=headers) as resp:
            if resp.status_code != 200:
                await resp.aread()
                raise self._error(resp)
            usage, finish = Usage(), "other"
            pending: dict[int, dict] = {}  # tool calls being assembled, by index
            async for data in iter_sse_data(resp):
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if u := chunk.get("usage"):
                    usage = Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
                for choice in chunk.get("choices", [])[:1]:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        yield TextDelta(delta["content"])
                    for i, tc in enumerate(delta.get("tool_calls") or []):
                        # Arguments may arrive in fragments; id and name only on the first one.
                        slot = pending.setdefault(tc.get("index", i), {"id": "", "name": "", "args": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] = fn.get("name") or slot["name"]
                        slot["args"] += fn.get("arguments") or ""
                    if reason := choice.get("finish_reason"):
                        finish = _FINISH.get(reason, "other")
            for _, slot in sorted(pending.items()):
                try:
                    args = json.loads(slot["args"] or "{}")
                except json.JSONDecodeError as exc:
                    raise ProviderError(f"mistral returned malformed tool arguments: {slot['args'][:200]!r}") from exc
                yield ToolCallEvent(ToolCall(slot["id"], slot["name"], args))
            yield Finished("tool_calls" if pending else finish, usage, model)

    @staticmethod
    def _error(resp: httpx.Response) -> Exception:
        try:
            detail = resp.json().get("message")
        except ValueError:
            detail = None
        message = f"mistral HTTP {resp.status_code}: {detail or resp.text[:200]}"
        if resp.status_code in TRANSIENT_STATUSES:
            return ProviderOverloaded(message)
        if resp.status_code in (401, 403):
            return ProviderAuthError(message + " (check MISTRAL_API_KEY)")
        if resp.status_code == 429:
            retry = resp.headers.get("retry-after")
            return RateLimited(
                message, float(retry) if retry and retry.replace(".", "", 1).isdigit() else None, daily=False
            )
        return ProviderError(message)
