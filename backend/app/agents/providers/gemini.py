"""Gemini adapter: REST streamGenerateContent over SSE, called directly with httpx.

Translation notes:
- The system prompt goes in systemInstruction; assistant turns use role "model".
- Tool calls are functionCall parts; results are functionResponse parts, and consecutive
  results share one user turn (Gemini expects all answers to a parallel call together).
- Gemini 3 attaches a thoughtSignature to functionCall parts and wants it back on later
  requests. It's kept in ToolCall.provider_meta with the model that made it, and only sent
  back to that same model (a signature is meaningless to another one).
- Call ids are ours and deterministic (call_<turn>_<n>), which keeps eval recordings
  stable. Gemini 3 also returns its own random id; it's kept in provider_meta and echoed
  on the functionCall and its functionResponse (same model only), so parallel calls to the
  same tool pair up correctly.
"""

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.agents.providers.base import (
    ChatRequest,
    Event,
    Finished,
    ProviderAuthError,
    ProviderError,
    RateLimited,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    Usage,
)
from app.agents.providers.sse import iter_sse_data

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# JSON Schema keywords outside Gemini's OpenAPI subset; sending them is a 400.
_UNSUPPORTED_SCHEMA_KEYS = {"additionalProperties", "$schema", "$id", "title"}
_FINISH = {"STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "blocked", "RECITATION": "blocked", "PROHIBITED_CONTENT": "blocked"}  # fmt: skip


def _schema(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _schema(v) for k, v in node.items() if k not in _UNSUPPORTED_SCHEMA_KEYS}
    if isinstance(node, list):
        return [_schema(v) for v in node]
    return node


def _tool_response(content: str) -> dict:
    """functionResponse.response must be an object: wrap anything else as {"result": ...}."""
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        value = content
    return value if isinstance(value, dict) else {"result": value}


def _seconds(delay: str | None) -> float | None:
    m = re.fullmatch(r"([\d.]+)s", delay or "")
    return float(m.group(1)) if m else None


class GeminiAdapter:
    name = "gemini"

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None, base_url: str = BASE_URL) -> None:
        self._key = api_key
        # Long read timeout: a streamed answer can pause while the model thinks.
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=120.0))
        self._base = base_url

    def build_body(self, model: str, req: ChatRequest) -> dict[str, Any]:
        # Our call id → Gemini's own id, for calls this same model made.
        vendor_ids = {
            c.id: c.provider_meta["call_id"]
            for m in req.messages for c in m.tool_calls
            if c.provider_meta and c.provider_meta.get("model") == model and c.provider_meta.get("call_id")
        }  # fmt: skip
        contents: list[dict] = []
        for m in req.messages:
            if m.role == "user":
                contents.append({"role": "user", "parts": [{"text": m.content}]})
            elif m.role == "assistant":
                parts: list[dict] = [{"text": m.content}] if m.content else []
                for c in m.tool_calls:
                    part: dict = {"functionCall": {"name": c.name, "args": c.arguments}}
                    if c.id in vendor_ids:
                        part["functionCall"]["id"] = vendor_ids[c.id]
                    meta = c.provider_meta or {}
                    if meta.get("thought_signature") and meta.get("model") == model:
                        part["thoughtSignature"] = meta["thought_signature"]
                    parts.append(part)
                contents.append({"role": "model", "parts": parts})
            else:  # tool result
                part = {"functionResponse": {"name": m.name, "response": _tool_response(m.content)}}
                if m.tool_call_id in vendor_ids:
                    part["functionResponse"]["id"] = vendor_ids[m.tool_call_id]
                if contents and contents[-1].get("_tool_turn"):
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part], "_tool_turn": True})
        for c in contents:
            c.pop("_tool_turn", None)  # internal marker only
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"temperature": req.temperature, "maxOutputTokens": req.max_output_tokens},
        }
        if req.system:
            body["systemInstruction"] = {"parts": [{"text": req.system}]}
        if req.tools:
            body["tools"] = [{"functionDeclarations": [
                {"name": t.name, "description": t.description, "parameters": _schema(t.parameters)} for t in req.tools
            ]}]  # fmt: skip
        return body

    async def stream_once(self, model: str, req: ChatRequest) -> AsyncIterator[Event]:
        url = f"{self._base}/models/{model}:streamGenerateContent?alt=sse"
        turn = sum(1 for m in req.messages if m.role == "assistant")  # for deterministic call ids
        headers = {"x-goog-api-key": self._key}  # header, so the key never appears in URLs or logs
        async with self._client.stream("POST", url, json=self.build_body(model, req), headers=headers) as resp:
            if resp.status_code != 200:
                await resp.aread()
                raise self._error(resp)
            n_calls, usage, finish = 0, Usage(), "other"
            async for data in iter_sse_data(resp):
                chunk = json.loads(data)
                if chunk.get("promptFeedback", {}).get("blockReason"):  # the prompt itself was refused
                    yield Finished("blocked", usage, model)
                    return
                if um := chunk.get("usageMetadata"):  # cumulative: the last chunk has the totals
                    usage = Usage(
                        um.get("promptTokenCount", 0),
                        um.get("candidatesTokenCount", 0) + um.get("thoughtsTokenCount", 0),
                    )
                for cand in chunk.get("candidates", [])[:1]:
                    for part in cand.get("content", {}).get("parts", []):
                        if "functionCall" in part:
                            fc = part["functionCall"]
                            meta = {"model": model}
                            if sig := part.get("thoughtSignature"):
                                meta["thought_signature"] = sig
                            if fc.get("id"):
                                meta["call_id"] = fc["id"]
                            yield ToolCallEvent(ToolCall(f"call_{turn}_{n_calls}", fc["name"], fc.get("args") or {}, meta))
                            n_calls += 1
                        elif part.get("text") and not part.get("thought"):
                            yield TextDelta(part["text"])
                    if reason := cand.get("finishReason"):
                        finish = _FINISH.get(reason, "other")
            yield Finished("tool_calls" if n_calls else finish, usage, model)

    @staticmethod
    def _error(resp: httpx.Response) -> Exception:
        try:
            err = resp.json().get("error", {})
        except ValueError:
            err = {}
        message = f"gemini HTTP {resp.status_code}: {err.get('message') or resp.text[:200]}"
        if resp.status_code in (401, 403):
            return ProviderAuthError(message + " (check GEMINI_API_KEY)")
        if resp.status_code == 429:
            daily, retry_after = False, None
            for d in err.get("details", []):
                if d.get("@type", "").endswith("QuotaFailure"):
                    daily = any("PerDay" in v.get("quotaId", "") for v in d.get("violations", []))
                if d.get("@type", "").endswith("RetryInfo"):
                    retry_after = _seconds(d.get("retryDelay"))
            return RateLimited(message, retry_after, daily)
        return ProviderError(message)
