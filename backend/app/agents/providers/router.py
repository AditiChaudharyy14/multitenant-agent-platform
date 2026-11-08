"""ModelRouter: one provider facade over an ordered list of models.

For each request it tries [primary, *fallbacks] in order. Per model:
  1. reserve one request in the daily budget (at the limit → next model, no HTTP call)
  2. wait for the model's RPM limiter
  3. stream from the adapter
A daily-quota 429 marks the model exhausted for today and moves on; other 429s back off
exponentially (honouring the vendor's retry delay) and fall back after max_retries.
Network failures before the first event (connect/read timeouts, resets) are transient too,
and are handled exactly like an overloaded 5xx.
Fallback only happens before the first event is yielded: a half-streamed answer is never
continued by a different model. Across a multi-step turn, the agent loop goes further and
pin()s the router to the model that answered the turn's first step.
"""

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.agents.providers.base import (
    ChatRequest,
    DailyBudgetExhausted,
    Event,
    LLMProvider,
    ProviderOverloaded,
    ProviderUnavailable,
    RateLimited,
)
from app.agents.providers.budget import DailyBudget, next_reset
from app.agents.providers.limiter import RpmLimiter

log = logging.getLogger("app.llm")


class ModelAdapter(Protocol):
    """One vendor's wire format, for a single HTTP call to a single model."""

    name: str

    def stream_once(self, model: str, req: ChatRequest) -> AsyncIterator[Event]: ...


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 3  # per model, for transient 429s
    base_delay: float = 1.0  # seconds; doubles each retry
    max_delay: float = 30.0


DEFAULT_RETRY = RetryPolicy()  # frozen, so one shared default is safe


class ModelRouter(LLMProvider):
    def __init__(
        self,
        adapter: ModelAdapter,
        models: list[str],
        budget: DailyBudget,
        limiters: dict[str, RpmLimiter],
        retry: RetryPolicy = DEFAULT_RETRY,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if not models:
            raise ValueError("at least one model is required")
        self.name = adapter.name
        self.adapter, self.models, self.budget = adapter, models, budget
        self.limiters, self.retry = limiters, retry
        self._sleep, self._jitter = sleep, jitter

    def pin(self, model: str) -> "ModelRouter":
        """A router over `model` only. It shares the adapter, the daily budget, the RPM
        limiters and the retry policy, so it still backs off and counts against the same
        quotas; it just has nothing to fall back to."""
        if model not in self.models:
            raise ValueError(f"{model!r} is not one of {self.models}")
        return ModelRouter(
            self.adapter, [model], self.budget, self.limiters, self.retry,
            sleep=self._sleep, jitter=self._jitter,
        )  # fmt: skip

    def _backoff(self, attempt: int, err: RateLimited) -> float:
        if err.retry_after is not None:
            return err.retry_after  # the vendor said how long: believe it
        # Exponential, with jitter in [0.5x, 1.5x) so concurrent callers don't retry in step.
        return min(self.retry.max_delay, self.retry.base_delay * 2**attempt) * (0.5 + self._jitter())

    async def stream(self, req: ChatRequest) -> AsyncIterator[Event]:
        out_of_budget = 0
        for model in self.models:
            attempt = 0
            while True:
                if await self.budget.reserve(model) is None:
                    out_of_budget += 1
                    log.warning(f"{self.name}/{model}: daily budget used up, trying the next model")
                    break
                await self.limiters[model].acquire()
                started = False
                try:
                    async for event in self.adapter.stream_once(model, req):
                        started = True
                        yield event
                    return  # success
                except (RateLimited, httpx.TransportError) as exc:
                    if started:
                        raise  # can't switch models mid-answer
                    # A transport error (no HTTP response at all) is a transient failure.
                    err = exc if isinstance(exc, RateLimited) else ProviderOverloaded(f"network: {type(exc).__name__}")
                    if err.daily:
                        await self.budget.exhaust(model)
                        out_of_budget += 1
                        break
                    if attempt >= self.retry.max_retries:
                        log.warning(f"{self.name}/{model}: still rate limited after {attempt} retries, falling back")
                        break
                    delay = self._backoff(attempt, err)
                    attempt += 1
                    log.info(f"{self.name}/{model}: rate limited, retry {attempt} in {delay:.1f}s")
                    await self._sleep(delay)
        if out_of_budget == len(self.models):
            usage = [(m, await self.budget.used(m), self.budget.limit) for m in self.models]
            raise DailyBudgetExhausted(self.name, usage, next_reset())
        raise ProviderUnavailable(
            f"{self.name}: every model is rate limited ({', '.join(self.models)}); try again shortly"
        )
