"""Structured JSON logs, one object per line, with the request's id, route and org.

- RequestContextMiddleware (pure ASGI, so SSE is never buffered) gives every request an id,
  returned as the X-Request-ID header. A safe incoming id ([A-Za-z0-9-], 8-64 chars) is
  kept so a proxy's id carries through; anything else is replaced. It records the route
  *template* (/api/conversations/{conversation_id}/messages), never a path with ids in
  it, and writes one "request" line per request (method, route, status, duration).
- The org slug is added once the session is known (auth.deps.current_ctx) or from the
  public /api/orgs/{slug}/... path.
- RedactFilter masks secrets (app.redact) and email addresses in every record. Message
  text is never logged by the app; the access line carries no body, query or path.

configure() installs the handler on the root logger and routes uvicorn's loggers through
it (its own access log is replaced by ours).
"""

import json
import logging
import re
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.redact import redact

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_route: ContextVar[str | None] = ContextVar("route", default=None)
_org: ContextVar[str | None] = ContextVar("org", default=None)

_SAFE_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_STD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}

access_log = logging.getLogger("app.access")


def request_context() -> dict[str, str]:
    """The current request's id, route and org (empty outside a request)."""
    found = {"request_id": _request_id.get(), "route": _route.get(), "org": _org.get()}
    return {k: v for k, v in found.items() if v}


def set_org(slug: str | None) -> None:
    if slug:
        _org.set(slug)


class RedactFilter(logging.Filter):
    """Masks secrets and emails in the final message (args included) and string extras."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = _EMAIL.sub(r"***@\1", redact(record.getMessage()))
        record.msg, record.args = message, None
        for key, value in list(vars(record).items()):
            if key not in _STD_ATTRS and isinstance(value, str):
                setattr(record, key, _EMAIL.sub(r"***@\1", redact(value)))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **request_context(),
        }
        out.update({k: v for k, v in vars(record).items() if k not in _STD_ATTRS and not k.startswith("_")})
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(out, default=str, ensure_ascii=False)


def configure(level: int = logging.INFO) -> None:
    """JSON lines on stderr for the app and uvicorn; uvicorn's access log is replaced."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True  # RequestContextMiddleware logs requests


def _leaf_routes(routes: list[Any]) -> list[Any]:
    """FastAPI wraps an included router (APIRouter with a prefix) in an object whose own
    matches() doesn't name the route; its original_router holds the real routes."""
    out: list[Any] = []
    for route in routes:
        inner = getattr(route, "original_router", None)
        out += _leaf_routes(inner.routes) if inner is not None else [route]
    return out


def _route_template(app: ASGIApp, scope: Scope) -> str | None:
    for route in _leaf_routes(getattr(app, "routes", [])):
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return getattr(route, "path", None)
    return None


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, router_app: Any = None) -> None:
        self.app = app
        self.router_app = router_app  # the FastAPI app, whose routes give the template

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        rid = incoming if _SAFE_ID.match(incoming) else uuid.uuid4().hex
        route = _route_template(self.router_app, scope) if self.router_app is not None else None
        tokens = (_request_id.set(rid), _route.set(route), _org.set(None))
        if route and route.startswith("/api/orgs/{slug}"):
            parts = scope["path"].split("/")
            set_org(parts[3] if len(parts) > 3 else None)
        started, status = time.monotonic(), 500

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], (b"x-request-id", rid.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            if route != "/api/health":  # the Docker healthcheck polls every few seconds
                access_log.info("request", extra={"method": scope["method"], "status": status,
                                                  "duration_ms": round((time.monotonic() - started) * 1000, 1)})  # fmt: skip
            for var, token in zip((_request_id, _route, _org), tokens, strict=True):
                var.reset(token)
