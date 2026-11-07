"""Eval scenario files (eval/scenarios/<vertical>/<id>.yaml): schema and loader.

A scenario is one conversation by one seeded customer, on its own clone of the seeded
template database, with the clock frozen. Its assertions check the reply and, above all,
the database state afterwards. Every model forbids unknown keys, so a typo in an
assertion name fails loudly instead of silently checking nothing.

Placeholders in turn text and assertions: {{order:<seed key>}} and {{booking:<seed key>}}
become the seeded row's number (HT-1004, LW-1003); "me" as a user means the scenario's
own customer. In SQL (setup, db checks, reply_contains_sql), {{today}}, {{tomorrow}},
{{yesterday}}, {{week_start}}, {{next_week}}, {{month_start}} and {{next_month}} become
the frozen clock's local midnights (timestamptz literals), computed by the harness itself.

Admin scenarios (eval/scenarios/admin/, `chat: admin`) talk to the analytics chat as the
org's admin. `expect_http` instead asserts that the admin chat refuses the user outright
(e.g. 403 for a customer) without any model call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ───────────── setup: small mutations applied to the clone before the first turn ─────────────


class SetPrice(_Strict):
    sku: str
    price: str  # NPR, e.g. "1350"


class SetStock(_Strict):
    sku: str
    stock: int


class AdvanceOrder(_Strict):
    order: str  # seed key, e.g. c2-pending


class AddBooking(_Strict):
    customer: str  # email of a seeded customer
    service: str  # service name
    practitioner: str  # practitioner name
    start: str  # org-local "YYYY-MM-DDTHH:MM"


class SetupStep(_Strict):
    set_price: SetPrice | None = None
    set_stock: SetStock | None = None
    advance_order: AdvanceOrder | None = None
    add_booking: AddBooking | None = None
    sql: str | None = None  # runs as app_owner with app.org_id set and app.role = admin
    # Add seed/<slug>/analytics.yaml's history to this clone (the template leaves it out).
    analytics_history: bool | None = None

    @model_validator(mode="after")
    def _one(self) -> SetupStep:
        if sum(v is not None for v in self.model_dump().values()) != 1:
            raise ValueError("each setup step has exactly one of set_price, set_stock, advance_order, add_booking, sql, "
                             "analytics_history")
        return self


# ───────────── assertions ─────────────


class ToolsCalled(_Strict):
    includes: list[str] = Field(default_factory=list)  # server button actions: "server:<tool>"
    excludes: list[str] = Field(default_factory=list)


class ReplyChecks(_Strict):
    reply_matches: list[str] = Field(default_factory=list)  # regexes, case-insensitive
    reply_not_matches: list[str] = Field(default_factory=list)
    tools_called: ToolsCalled | None = None


class OrderCheck(_Strict):
    ref: str | None = None  # seed key
    latest_for: str | None = None  # "me" or an email: that customer's newest order
    status: str | None = None
    items: dict[str, int] | None = None  # sku → qty, exactly these lines
    unit_prices: dict[str, str] | None = None  # sku → price, e.g. "1450.00"
    total: str | None = None
    history_to: list[str] | None = None  # to_status sequence in order_status_history

    @model_validator(mode="after")
    def _which(self) -> OrderCheck:
        if (self.ref is None) == (self.latest_for is None):
            raise ValueError("order check needs exactly one of ref, latest_for")
        return self


class BookingCheck(_Strict):
    ref: str | None = None
    latest_for: str | None = None  # newest booking (by number) of that customer
    status: str | None = None
    start_local: str | None = None  # "YYYY-MM-DDTHH:MM" in the org timezone
    start_local_between: tuple[str, str] | None = None  # [from, to) local
    practitioner: str | None = None
    service: str | None = None
    cancellation_fee: str | None = None  # "0.00", "2600.00"; "null" for none

    @model_validator(mode="after")
    def _which(self) -> BookingCheck:
        if (self.ref is None) == (self.latest_for is None):
            raise ValueError("booking check needs exactly one of ref, latest_for")
        return self


class ProposalCheck(_Strict):
    status: str | None = None  # open | confirmed | superseded | rejected | expired
    count: int


class SqlCheck(_Strict):
    query: str  # one scalar; runs as app_owner with app.org_id set
    equals: Any


class DbCheck(_Strict):
    order: OrderCheck | None = None
    booking: BookingCheck | None = None
    stock: dict[str, int] | None = None
    proposals: ProposalCheck | None = None
    sql: SqlCheck | None = None

    @model_validator(mode="after")
    def _one(self) -> DbCheck:
        if sum(v is not None for v in self.model_dump().values()) != 1:
            raise ValueError("each db check has exactly one of order, booking, stock, proposals, sql")
        return self


class Assertions(ReplyChecks):
    """Checked once the conversation is over. reply_* look at the final reply."""

    any_reply_not_matches: list[str] = Field(default_factory=list)  # checked on every reply
    citations_include: list[str] = Field(default_factory=list)  # doc_id (prefix of doc_id#section)
    no_citations: bool = False
    no_new_rows: list[str] = Field(default_factory=list)  # tables whose org row count must not grow
    new_rows: dict[str, int] = Field(default_factory=dict)  # table → exact number of new rows
    db: list[DbCheck] = Field(default_factory=list)
    # Ground truth for numbers: each query returns one scalar, computed by the harness from
    # the database, and the final reply must contain that value (commas and .00 ignored).
    reply_contains_sql: list[str] = Field(default_factory=list)

    def checks_state(self) -> bool:
        return bool(self.db or self.no_new_rows or self.new_rows or self.reply_contains_sql)


# ───────────── turns ─────────────


class Turn(_Strict):
    user: str | None = None
    action: Literal["confirm_last_proposal", "select_first_slot"] | None = None
    expect: ReplyChecks | None = None  # checks on this turn's reply only
    # An action turn marked optional is skipped when there is no card to press (e.g. the
    # agent rightly refused to prepare an order); otherwise a missing card is an error.
    optional: bool = False

    @model_validator(mode="after")
    def _one(self) -> Turn:
        if (self.user is None) == (self.action is None):
            raise ValueError("a turn is either `user: <text>` or `action: <button>`")
        return self


class Scenario(_Strict):
    id: str
    title: str
    org: str  # slug: selects the org at "login", exactly like the URL slug
    user: str  # seeded email
    seed_now: str | None = None  # agent clock; default EVAL_SEED_NOW (the template's)
    chat: Literal["customer", "admin"] = "customer"  # admin: the analytics chat API
    expect_http: int | None = None  # the admin chat must refuse with this status; no turns run
    judge: bool = False  # LLM-as-judge scores this KB answer (reported, never pass/fail)
    rule: str = ""  # the business rule this scenario exercises, for the report
    setup: list[SetupStep] = Field(default_factory=list)
    turns: list[Turn] = Field(min_length=1)
    assertions: Assertions

    vertical: str = ""  # filled by the loader from the directory name

    @model_validator(mode="after")
    def _http(self) -> Scenario:
        if self.expect_http is not None and self.chat != "admin":
            raise ValueError("expect_http is for admin-chat scenarios")
        return self

    @property
    def key(self) -> str:
        return f"{self.vertical}/{self.id}"


def load_scenarios(root: Path = SCENARIO_DIR) -> list[Scenario]:
    out = []
    for path in sorted(root.glob("*/*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        sc = Scenario.model_validate({**raw, "vertical": path.parent.name})
        if sc.id != path.stem:
            raise ValueError(f"{path}: id {sc.id!r} must match the file name")
        out.append(sc)
    return out
