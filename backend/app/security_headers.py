"""Security headers on every API response (pure ASGI, so the SSE stream is never buffered).

The API serves JSON and an event stream, never HTML, so its CSP forbids everything and it
may not be framed. The browser-facing pages get their own headers from Next.js
(frontend/next.config.ts).
"""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEADERS = [
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
]


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), *HEADERS]
            await send(message)

        await self.app(scope, receive, send_with_headers)
