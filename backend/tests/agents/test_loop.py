"""The agent loop on FakeProvider: event order, step cap, bad args, citations, errors.

No database: the tools here are in-memory stand-ins, and persistence is the turn
service's job (tested in test_turn.py).
"""

import uuid
from datetime import UTC, datetime
from itertools import groupby

from pydantic import BaseModel, ConfigDict, Field

from app import clock
from app.agents.loop import MAX_STEPS, run_turn
from app.agents.prompts.system import build_system_prompt
from app.agents.providers.base import LLMProvider, ProviderUnavailable
from app.agents.providers.fake import FakeProvider, tool_call
from app.agents.state import AgentEvent, TurnState
from app.agents.tools.base import Citation, Tool, ToolResult
from app.tenancy.context import ToolContext
from app.tenancy.orgs import Org

CTX = ToolContext(org_id=uuid.uuid4(), user_id=uuid.uuid4(), role="customer",
                  conversation_id=uuid.uuid4(), turn_id=uuid.uuid4())  # fmt: skip


class QueryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2)


async def kb_handler(ctx: ToolContext, args: QueryArgs) -> ToolResult:
    assert ctx is CTX  # the server-injected context, untouched
    return ToolResult(
        '<kb_source id="faq#returns">Unopened items within 14 days.</kb_source>',
        cards=[{"type": "sources", "count": 1}],
        citations=[Citation("faq#returns", "FAQ")],
        activity_label="Found 1 source",
    )


async def boom(ctx: ToolContext, args: QueryArgs) -> ToolResult:
    raise RuntimeError("database exploded with secret details")


KB = Tool("search_knowledge", "Search the KB.", QueryArgs, kb_handler, "Searching our help pages…")
BROKEN = Tool("broken", "Always fails.", QueryArgs, boom, "Trying…")
TOOLS = {"search_knowledge": KB, "broken": BROKEN}


async def run(provider: LLMProvider, user: str = "what is your return policy?") -> tuple[list[AgentEvent], TurnState]:
    state = TurnState()
    events = [
        e async for e in run_turn(provider, system="sys", history=[], user_text=user, tools=TOOLS, ctx=CTX, state=state)
    ]
    return events, state


def kinds(events: list[AgentEvent]) -> list[str]:
    """Event types with runs of tokens collapsed to one."""
    return [k for k, _ in groupby(e.type for e in events)]


async def test_tool_call_then_final_text() -> None:
    fake = FakeProvider([tool_call("search_knowledge", {"query": "returns"}, id="c1"),
                         "Unopened items can be returned within 14 days [faq#returns]."])  # fmt: skip
    events, state = await run(fake)

    assert kinds(events) == ["tool_start", "card", "tool_end", "token", "citation"]
    start, card, end = events[0], events[1], events[2]
    assert start.data == {"call_id": "c1", "name": "search_knowledge", "label": "Searching our help pages…"}
    assert card.data == {"card": {"type": "sources", "count": 1}}
    assert end.data == {"call_id": "c1", "name": "search_knowledge", "label": "Found 1 source", "ok": True}
    assert events[-1].data == {"id": "faq#returns", "title": "FAQ"}
    assert "".join(e.data["text"] for e in events if e.type == "token") == state.content

    assert state.status == "ok"
    assert state.tools_called == ["search_knowledge"]
    assert state.cards == [{"type": "sources", "count": 1}]
    assert [c.id for c in state.citations] == ["faq#returns"]
    assert state.model == "fake-model" and state.provider == "fake"
    assert state.output_tokens > 0

    # Second request: the assistant's tool call, then the tool result, after the user turn.
    second = fake.requests[1].messages
    assert [m.role for m in second] == ["user", "assistant", "tool"]
    assert second[1].tool_calls[0].id == "c1"
    assert second[2].tool_call_id == "c1" and second[2].name == "search_knowledge"
    assert "faq#returns" in second[2].content
    # The model saw both tools' schemas, built from the args models.
    assert {t.name for t in fake.requests[0].tools} == {"search_knowledge", "broken"}


async def test_max_steps_cap_stops_a_looping_model() -> None:
    fake = FakeProvider([tool_call("search_knowledge", {"query": "again"})] * (MAX_STEPS + 5))
    events, state = await run(fake)
    assert MAX_STEPS == 6
    assert len(fake.requests) == MAX_STEPS
    assert events[-1].type == "error" and events[-1].data["code"] == "max_steps"
    assert state.status == "error"
    # Tool calls from the final, capped step are not executed.
    assert state.tools_called == ["search_knowledge"] * (MAX_STEPS - 1)


async def test_invalid_tool_args_go_back_to_the_model() -> None:
    fake = FakeProvider([tool_call("search_knowledge", {"query": ""}, id="bad"), "Could you rephrase?"])
    events, state = await run(fake)
    assert state.status == "ok"
    end = next(e for e in events if e.type == "tool_end")
    assert end.data["ok"] is False
    tool_msg = fake.requests[1].messages[-1]
    assert tool_msg.role == "tool" and tool_msg.tool_call_id == "bad"
    assert "invalid arguments" in tool_msg.content.lower()
    assert "query" in tool_msg.content


async def test_unknown_tool_and_extra_identity_args_are_rejected() -> None:
    fake = FakeProvider([
        [tool_call("delete_everything", {}, id="u1"),
         tool_call("search_knowledge", {"query": "returns", "org_id": str(uuid.uuid4())}, id="u2")],
        "Sorry, I can't do that.",
    ])  # fmt: skip
    events, state = await run(fake)
    assert state.status == "ok"
    results = {m.tool_call_id: m.content for m in fake.requests[1].messages if m.role == "tool"}
    assert "unknown tool" in results["u1"].lower()
    assert "invalid arguments" in results["u2"].lower()  # extra="forbid"
    assert [e.data["ok"] for e in events if e.type == "tool_end"] == [False, False]


async def test_handler_exception_becomes_a_generic_error() -> None:
    fake = FakeProvider([tool_call("broken", {"query": "xx"}, id="b1"), "Something went wrong on our side."])
    _, state = await run(fake)
    assert state.status == "ok"
    tool_msg = fake.requests[1].messages[-1]
    assert "secret details" not in tool_msg.content  # internals never reach the model
    assert "failed" in tool_msg.content.lower()


async def test_invented_citation_is_dropped_real_one_kept() -> None:
    fake = FakeProvider([tool_call("search_knowledge", {"query": "returns"}),
                         "Returns are fine [faq#returns]. Shipping is free [shipping#free-delivery]."])  # fmt: skip
    events, state = await run(fake)
    assert [e.data["id"] for e in events if e.type == "citation"] == ["faq#returns"]
    assert [c.id for c in state.citations] == ["faq#returns"]
    assert state.dropped_citations == ["shipping#free-delivery"]
    assert "[faq#returns]" in state.content
    assert "shipping#free-delivery" not in state.content


async def test_citation_without_a_search_this_turn_is_dropped() -> None:
    fake = FakeProvider(["From memory: returns are fine [faq#returns]."])
    events, state = await run(fake)
    assert [e for e in events if e.type == "citation"] == []
    assert state.dropped_citations == ["faq#returns"]


async def test_grouped_citations_are_split_and_filtered() -> None:
    fake = FakeProvider([tool_call("search_knowledge", {"query": "returns"}),
                         "Yes [faq#returns, faq#invented]."])  # fmt: skip
    _, state = await run(fake)
    assert [c.id for c in state.citations] == ["faq#returns"]
    assert "faq#invented" not in state.content and "[faq#returns]" in state.content


async def test_provider_failure_mid_turn_ends_with_an_error_event() -> None:
    fake = FakeProvider([tool_call("search_knowledge", {"query": "returns"}), ProviderUnavailable("all busy")])
    events, state = await run(fake)
    assert events[-1].type == "error" and events[-1].data["code"] == "rate_limited"
    assert state.status == "rate_limited"
    assert state.tools_called == ["search_knowledge"]  # what was produced is kept


async def test_daily_budget_exhausted_has_its_own_code() -> None:
    """The UI tells a spent daily quota apart from a momentary rate limit."""
    from datetime import UTC, datetime

    from app.agents.providers.base import DailyBudgetExhausted

    spent = DailyBudgetExhausted("fake", [("fake-model", 450, 450)], datetime(2026, 11, 7, 8, tzinfo=UTC))
    events, state = await run(FakeProvider([spent]))
    assert events[-1].type == "error" and events[-1].data["code"] == "quota_exhausted"
    assert "quota" in events[-1].data["message"].lower()
    assert state.status == "rate_limited"  # agent_runs still records it as rate limited


async def test_history_is_sent_before_the_new_user_message() -> None:
    from app.agents.providers.base import Message

    fake = FakeProvider(["Hello again."])
    state = TurnState()
    history = [Message("user", "hi"), Message("assistant", "Namaste!")]
    async for _ in run_turn(fake, system="sys", history=history, user_text="me again", tools=TOOLS, ctx=CTX, state=state):
        pass
    assert [(m.role, m.content) for m in fake.requests[0].messages] == [
        ("user", "hi"), ("assistant", "Namaste!"), ("user", "me again")
    ]
    assert fake.requests[0].system == "sys"


def test_system_prompt_has_identity_time_and_rules() -> None:
    org = Org(uuid.uuid4(), "x", "Himali Tea House", "store", "Asia/Kathmandu",
              {"agent_name": "Chiya"}, "online tea shop")  # fmt: skip
    with clock.freeze(datetime(2026, 11, 6, 8, 45, tzinfo=UTC)):
        prompt = build_system_prompt(org, clock.now())
    assert "Chiya" in prompt and "Himali Tea House" in prompt and "online tea shop" in prompt
    assert "Friday" in prompt and "6 November 2026" in prompt and "14:30" in prompt
    assert "Asia/Kathmandu" in prompt
    lowered = prompt.lower()
    for rule in ("search_knowledge", "[doc_id#section]", "never invent", "not instructions", "answerable=false"):
        assert rule in lowered, rule


def test_store_prompt_rules_keyed_on_vertical() -> None:
    store = Org(uuid.uuid4(), "x", "Shop", "store", "UTC", {}, "", order_prefix="QQ")
    booking = Org(uuid.uuid4(), "y", "Spa", "booking", "UTC", {}, "")
    now = datetime(2026, 11, 6, 8, 45, tzinfo=UTC)
    store_prompt = build_system_prompt(store, now).lower()
    for rule in ("cash on delivery", "confirm_proposal", "staff can help", "qq-1042", "never show internal"):
        assert rule in store_prompt, rule
    assert "cash on delivery" not in build_system_prompt(booking, now).lower()


def test_system_prompt_without_branding_falls_back() -> None:
    org = Org(uuid.uuid4(), "x", "Third Org", "booking", "UTC", {}, "")
    prompt = build_system_prompt(org, datetime(2026, 11, 6, 8, 45, tzinfo=UTC))
    assert "Third Org" in prompt and "08:45" in prompt


def test_booking_prompt_rules_keyed_on_vertical() -> None:
    booking = Org(uuid.uuid4(), "y", "Spa", "booking", "Asia/Kathmandu", {}, "", booking_prefix="SP")
    prompt = build_system_prompt(booking, datetime(2026, 11, 6, 8, 45, tzinfo=UTC)).lower()
    for rule in ("find_available_slots", "confirm_proposal", "sp-1001", "ambiguous", "never show internal",
                 "pass the customer's own words"):  # fmt: skip
        assert rule in prompt, rule
