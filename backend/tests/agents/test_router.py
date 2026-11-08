"""Model routing: per-model RPM limiter, 429 backoff, daily budget and fallback order."""

import uuid
from collections.abc import AsyncIterator

import pytest

from app.agents.providers.base import (
    ChatRequest,
    DailyBudgetExhausted,
    Event,
    Finished,
    Message,
    ProviderUnavailable,
    RateLimited,
    TextDelta,
    Usage,
)
from app.agents.providers.budget import DailyBudget
from app.agents.providers.limiter import RpmLimiter
from app.agents.providers.router import ModelRouter, RetryPolicy

REQ = ChatRequest(system="s", messages=[Message(role="user", content="hi")])


class ScriptedAdapter:
    """Per model, a queue of outcomes: 'ok', 'ok-then-429', or an exception to raise."""

    name = "scripted"

    def __init__(self, outcomes: dict[str, list]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    async def stream_once(self, model: str, req: ChatRequest) -> AsyncIterator[Event]:
        self.calls.append(model)
        outcome = self.outcomes[model].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        yield TextDelta(f"from {model}")
        if outcome == "ok-then-429":
            raise RateLimited("mid-stream", retry_after=None, daily=False)
        yield Finished("stop", Usage(1, 1), model)


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def router(
    adapter: ScriptedAdapter, models: list[str], limit: int = 100, t: FakeTime | None = None
) -> tuple[ModelRouter, DailyBudget]:
    t = t or FakeTime()
    budget = DailyBudget(f"test-{uuid.uuid4().hex[:8]}", limit=limit)
    limiters = {m: RpmLimiter(1000, clock=t.monotonic, sleep=t.sleep) for m in models}
    r = ModelRouter(
        adapter, models, budget, limiters, RetryPolicy(max_retries=3, base_delay=1.0), sleep=t.sleep, jitter=lambda: 0.5
    )
    return r, budget


async def collect(r: ModelRouter) -> list[Event]:
    return [e async for e in r.stream(REQ)]


async def test_rpm_limiter_spaces_out_the_thirteenth_call() -> None:
    t = FakeTime()
    lim = RpmLimiter(12, clock=t.monotonic, sleep=t.sleep)
    for _ in range(12):
        await lim.acquire()
    assert t.sleeps == []
    await lim.acquire()
    assert t.sleeps == [60.0]  # waits until the first call leaves the one-minute window


async def test_transient_429s_back_off_then_succeed() -> None:
    t = FakeTime()
    a = ScriptedAdapter({"p": [RateLimited("busy", None, False), RateLimited("busy", 7.0, False), "ok"]})
    r, budget = router(a, ["p"], t=t)
    events = await collect(r)
    assert events[-1] == Finished("stop", Usage(1, 1), "p")
    assert t.sleeps == [1.0, 7.0]  # exponential (jitter fixed at 1.0x), then Retry-After honoured
    assert await budget.used("p") == 3  # every HTTP attempt counts against the daily quota


async def test_repeated_429s_fall_back_to_next_model() -> None:
    a = ScriptedAdapter({"p": [RateLimited("busy", None, False)] * 4, "f": ["ok"]})
    r, _ = router(a, ["p", "f"])
    events = await collect(r)
    assert a.calls == ["p", "p", "p", "p", "f"]
    assert events[0] == TextDelta("from f")


async def test_daily_quota_429_falls_back_immediately_and_marks_model_exhausted() -> None:
    a = ScriptedAdapter({"p": [RateLimited("per day", None, daily=True)], "f": ["ok"]})
    r, budget = router(a, ["p", "f"], limit=50)
    await collect(r)
    assert a.calls == ["p", "f"]
    assert await budget.used("p") == 50


async def test_model_at_budget_is_skipped_without_an_http_call() -> None:
    a = ScriptedAdapter({"p": ["ok"], "f": ["ok"]})
    r, budget = router(a, ["p", "f"], limit=5)
    await budget.exhaust("p")
    await collect(r)
    assert a.calls == ["f"]


async def test_all_models_out_of_budget_fails_fast_with_a_clear_message() -> None:
    a = ScriptedAdapter({"p": [], "f": []})
    r, budget = router(a, ["p", "f"], limit=2)
    await budget.exhaust("p")
    await budget.exhaust("f")
    with pytest.raises(DailyBudgetExhausted) as ei:
        await collect(r)
    msg = str(ei.value)
    assert a.calls == []
    assert "p 2/2" in msg and "f 2/2" in msg
    assert "PT" in msg and "NPT" in msg and "EVAL_MODE=replay" in msg


async def test_all_models_rate_limited_is_not_reported_as_budget() -> None:
    a = ScriptedAdapter({"p": [RateLimited("busy", None, False)] * 4})
    r, _ = router(a, ["p"])
    with pytest.raises(ProviderUnavailable):
        await collect(r)


async def test_no_fallback_once_streaming_has_started() -> None:
    a = ScriptedAdapter({"p": ["ok-then-429"], "f": ["ok"]})
    r, _ = router(a, ["p", "f"])
    with pytest.raises(RateLimited):
        await collect(r)
    assert a.calls == ["p"]  # a half-streamed answer is never stitched to another model's


async def test_overloaded_5xx_backs_off_then_succeeds() -> None:
    """A 503 is retried like a transient 429, on the same model, before any fallback."""
    from app.agents.providers.base import ProviderOverloaded

    t = FakeTime()
    a = ScriptedAdapter({"p": [ProviderOverloaded("gemini HTTP 503: high demand"), "ok"], "f": ["ok"]})
    r, _ = router(a, ["p", "f"], t=t)
    events = await collect(r)
    assert a.calls == ["p", "p"]
    assert events[-1] == Finished("stop", Usage(1, 1), "p")
    assert t.sleeps == [1.0]


async def test_persistent_overload_falls_back_to_next_model() -> None:
    from app.agents.providers.base import ProviderOverloaded

    a = ScriptedAdapter({"p": [ProviderOverloaded("503")] * 4, "f": ["ok"]})
    r, _ = router(a, ["p", "f"])
    events = await collect(r)
    assert a.calls == ["p", "p", "p", "p", "f"]
    assert events[0] == TextDelta("from f")
