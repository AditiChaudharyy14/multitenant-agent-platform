"""CSRF defence: every state-changing request under /api must come from an allowed Origin.

Pure ASGI middleware (not BaseHTTPMiddleware), so it never buffers streamed responses.
A missing Origin is rejected too: browsers send it on every cross-site and same-site POST.
"""

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class OriginCheckMiddleware:
    def __init__(self, app: ASGIApp, allowed_origins: frozenset[str]) -> None:
        self.app = app
        self.allowed_origins = allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] not in SAFE_METHODS
            and (scope["path"] == "/api" or scope["path"].startswith("/api/"))
            and Headers(scope=scope).get("origin") not in self.allowed_origins
        ):
            response = JSONResponse({"detail": "Forbidden origin"}, status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
