"""Minimal Server-Sent Events reader for streamed HTTP responses (Gemini and Mistral)."""

from collections.abc import AsyncIterator

import httpx


async def iter_sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """Yield each event's `data:` payload (multi-line data joined with newlines)."""
    data: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif line == "" and data:  # a blank line ends the event
            yield "\n".join(data)
            data = []
        # Other fields (event:, id:, retry:) and comments (:) are not used by these APIs.
    if data:
        yield "\n".join(data)
