"""`make eval`: run every scenario against the agent and check the database afterwards.

Per scenario:
  1. use its own clone of the seeded template database (eval_s_<vertical>_<id>; all clones
     are made before the first scenario and dropped after the last), apply `setup`;
  2. freeze the clock at seed_now, make server ids deterministic, open its cassette;
  3. drive the real FastAPI app in-process (httpx ASGITransport): a server-side session for
     the seeded customer, a conversation, then each turn through POST .../messages, card
     buttons included, exactly as the UI sends them. The app talks to the clone as app_user;
  4. assert on replies, tools, citations and, above all, database state (as app_owner);
  5. move on; the clones are dropped together at the end.

Modes: auto (replay a scenario's cassette if it exists, else record it live), record
(always live, overwrites), replay (never touches the network). Live calls go through the
normal router: RPM limiter, daily budget counted in the main database, fallback model.
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))  # scenario, checks, evaldb, report

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

import checks  # noqa: E402
import evaldb  # noqa: E402
import report  # noqa: E402
from app import clock  # noqa: E402
from app.agents.ids import deterministic_ids  # noqa: E402
from app.agents.providers import _live  # noqa: E402
from app.agents.providers.base import LLMProvider  # noqa: E402
from app.agents.providers.budget import DailyBudget, quota_day  # noqa: E402
from app.agents.providers.recording import RecordReplayProvider, cassette_scope  # noqa: E402
from app.api.chat import chat_limiter, get_chat_provider  # noqa: E402
from app.auth.sessions import COOKIE_NAME, create_session  # noqa: E402
from app.config import get_settings, require_owner_url  # noqa: E402
from app.db import session as db  # noqa: E402
from app.main import app  # noqa: E402
from app.seed.run import resolve_seed_now  # noqa: E402
from app.tenancy.orgs import load_org_configs  # noqa: E402
from scenario import Scenario, load_scenarios  # noqa: E402

log = logging.getLogger("eval")
EVAL_DIR = Path(__file__).resolve().parent
CASSETTES = Path(os.environ.get("EVAL_CASSETTE_DIR", EVAL_DIR / "cassettes"))
ORIGIN = "http://localhost:3000"  # an allowed origin, so the CSRF check passes as for the UI
CARD_CONFIRM = ("order_summary", "booking_summary")
CARD_SLOTS = ("slot_list", "slot_alternatives")


class BudgetLow(Exception):
    pass


class Providers:
    """Replay needs nothing; record needs a key and shares one live router for the run."""

    def __init__(self, main_engine: Any) -> None:
        self.settings = get_settings()
        self.main_engine = main_engine
        self._live: LLMProvider | None = None
        self.replay = RecordReplayProvider("replay", CASSETTES)
        self.primary = self.settings.llm_models(self.settings.llm_provider)[0]
        self.budget = DailyBudget(self.settings.llm_provider, getattr(self.settings, f"llm_daily_budget_{self.settings.llm_provider}"), main_engine)

    def live(self) -> LLMProvider:
        if self._live is None:  # raises ProviderAuthError with a clear message if no key
            self._live = _live(self.settings, self.settings.llm_provider, self.main_engine)
        return self._live

    def record(self) -> LLMProvider:
        return RecordReplayProvider("record", CASSETTES, self.live())

    async def left(self) -> int:
        return self.budget.limit - await self.budget.used(self.primary)

    async def print_budget(self, when: str) -> None:
        """What `make llm-budget` shows, printed in-process (no extra container per run)."""
        parts = []
        for model in self.settings.llm_models(self.settings.llm_provider):
            used = await self.budget.used(model)
            parts.append(f"{model} {used}/{self.budget.limit} used ({self.budget.limit - used} left)")
        print(f"llm budget {when} (quota day {quota_day()} Pacific): " + "; ".join(parts), flush=True)


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln and not ln.startswith(":")]
        kind = next((ln[7:] for ln in lines if ln.startswith("event: ")), None)
        if kind:
            events.append((kind, json.loads("\n".join(ln[6:] for ln in lines if ln.startswith("data: ")))))
    return events


def _last_card(turns: list[checks.TurnRecord], types: tuple[str, ...]) -> dict | None:
    for t in reversed(turns):
        for card in reversed(t.cards):
            if card.get("type") in types:
                return card
    return None


def _action_payload(action: str, turns: list[checks.TurnRecord]) -> dict:
    if action == "confirm_last_proposal":
        card = _last_card(turns, CARD_CONFIRM)
        if not card:
            raise ScenarioError("confirm_last_proposal: no proposal card was shown in an earlier turn")
        return {"type": "confirm_proposal", "proposal_id": card["proposal_id"]}
    card = _last_card(turns, CARD_SLOTS)
    if not card or not card.get("slots"):
        raise ScenarioError("select_first_slot: no slot card with free times was shown in an earlier turn")
    return {k: v for k, v in card["slots"][0]["action"].items() if v is not None}


class ScenarioError(Exception):
    """The scenario could not be carried out (harness or cassette problem, missing card)."""


async def run_scenario(sc: Scenario, org: checks.OrgInfo, provider: LLMProvider, default_now: str) -> dict:
    """One scenario on its clone, which main() created beforehand and drops afterwards."""
    s = get_settings()
    name = evaldb.clone_name(sc.key)
    owner = evaldb.engine(evaldb.url_for(require_owner_url(), name))
    # The app's own engines now point at the clone (as app_user / app_readonly).
    saved = db.app_engine, db.readonly_engine
    db.app_engine = evaldb.engine(evaldb.url_for(s.database_url_app, name))
    db.readonly_engine = evaldb.engine(evaldb.url_for(s.database_url_readonly, name))
    chat_limiter.reset()  # org and user ids repeat across clones; the limit is per process
    app.dependency_overrides[get_chat_provider] = lambda: provider
    turns: list[checks.TurnRecord] = []
    result: dict[str, Any] = {"key": sc.key, "id": sc.id, "vertical": sc.vertical, "title": sc.title, "rule": sc.rule,
                              "user": sc.user, "judge": sc.judge}  # fmt: skip
    started = time.monotonic()
    try:
        now = resolve_seed_now(sc.seed_now or default_now, org.timezone)
        assert now is not None
        with clock.freeze(now, wall=False), deterministic_ids(sc.key), cassette_scope(sc.key):
            async with evaldb.owner_tx(owner, org.org_id) as conn:
                for step in sc.setup:
                    await checks.apply_setup(conn, org, step)
                me = await checks.user_id(conn, sc.user)
                role = await conn.scalar(text("SELECT role FROM users WHERE id = :u"), {"u": me})
                texts = [await checks.render(conn, org, t.user) if t.user else None for t in sc.turns]
                before = await checks.table_counts(conn)
            async with db.app_engine.begin() as conn:  # a real server-side session, as login makes
                token = await create_session(conn, org.org_id, me, role)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://eval.local",
                                         headers={"Origin": ORIGIN}, cookies={COOKIE_NAME: token}, timeout=300) as c:  # fmt: skip
                r = await c.post("/api/conversations")
                r.raise_for_status()
                cid = r.json()["id"]
                for turn, utext in zip(sc.turns, texts, strict=True):
                    rec = checks.TurnRecord(request=utext or f"[{turn.action}]")
                    try:
                        body = {"content": utext} if utext else {"action": _action_payload(turn.action or "", turns)}
                    except ScenarioError:
                        if not turn.optional:
                            raise
                        rec.status, rec.reply = "skipped", ""  # nothing to press; still one record per turn
                        turns.append(rec)
                        continue
                    resp = await c.post(f"/api/conversations/{cid}/messages", json=body)
                    if resp.status_code != 200:
                        raise ScenarioError(f"POST message: HTTP {resp.status_code} {resp.text[:200]}")
                    for kind, data in _parse_sse(resp.text):
                        if kind == "card":
                            rec.cards.append(data["card"])
                        elif kind == "error":
                            rec.errors.append(data)
                        elif kind == "done":
                            rec.reply, rec.status, rec.turn_id = data["content"], data["status"], data["turn_id"]
                            rec.citations = [x["id"] for x in data.get("citations", [])]
                    turns.append(rec)
                    await _fill_run(owner, org, rec)
                    if rec.run_error and rec.run_error.startswith(("CassetteMissing", "CassetteMismatch")):
                        raise ScenarioError(rec.run_error)
            async with evaldb.owner_tx(owner, org.org_id) as conn:
                fails = await checks.check_all(conn, org, sc, me, turns, before)
                if sc.judge:  # what the agent actually retrieved, for the judge's groundedness score
                    result["kb_passages"] = await _kb_passages(conn, cid)
        result.update(status="pass" if not fails else "fail", failures=fails)
    except ScenarioError as exc:
        result.update(status="error", failures=[str(exc).splitlines()[0]], detail=str(exc))
    except Exception as exc:  # a harness bug: report it on this scenario, keep going
        log.exception("scenario %s crashed", sc.key)
        result.update(status="error", failures=[f"{type(exc).__name__}: {exc}"])
    finally:
        app.dependency_overrides.pop(get_chat_provider, None)
        await db.app_engine.dispose()
        await db.readonly_engine.dispose()
        db.app_engine, db.readonly_engine = saved
        await owner.dispose()
        result["turns"] = [t.__dict__ for t in turns]
        result["wall_ms"] = int((time.monotonic() - started) * 1000)
    return result


async def _fill_run(owner: Any, org: checks.OrgInfo, rec: checks.TurnRecord) -> None:
    """Tools, model, tokens and latency of the turn, from its agent_runs row."""
    if not rec.turn_id:
        return
    async with evaldb.owner_tx(owner, org.org_id) as conn:
        row = (await conn.execute(
            text("SELECT model, input_tokens, output_tokens, latency_ms, tools_called, error FROM agent_runs WHERE turn_id = :t"),
            {"t": uuid.UUID(rec.turn_id)},
        )).one_or_none()  # fmt: skip
    if row:
        rec.model, rec.input_tokens, rec.output_tokens = row.model, row.input_tokens, row.output_tokens
        rec.latency_ms, rec.tools, rec.run_error = row.latency_ms or 0, list(row.tools_called or []), row.error


async def _kb_passages(conn: Any, conversation_id: str) -> str:
    steps = (await conn.execute(
        text("SELECT tool_calls FROM messages WHERE conversation_id = :c AND role = 'assistant' ORDER BY created_at"),
        {"c": uuid.UUID(conversation_id)},
    )).scalars().all()  # fmt: skip
    return "\n\n".join(c["result"] for st in steps for step in (st or []) for c in step["calls"]
                       if c["name"] == "search_knowledge")  # fmt: skip


def _orgs() -> dict[str, checks.OrgInfo]:
    out = {}
    for cfg in load_org_configs(get_settings().seed_dir):
        out[cfg.slug] = checks.OrgInfo(evaldb.org_id_for(cfg.slug), cfg.slug, cfg.vertical, cfg.timezone,
                                       cfg.order_prefix or "", cfg.booking_prefix or "")  # fmt: skip
    return out


def _selected(all_: list[Scenario], only: str | None) -> list[Scenario]:
    if not only:
        return all_
    pats = [p.strip() for p in only.split(",") if p.strip()]
    picked = [s for s in all_ if any(fnmatch.fnmatch(s.key, p) or s.id == p for p in pats)]
    if not picked:
        raise SystemExit(f"--only {only!r} matches no scenario")
    return picked


KEEP = False


async def main() -> int:
    global KEEP
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=("auto", "record", "replay"), default="auto")
    ap.add_argument("--only", help="comma-separated scenario keys or globs, e.g. store/edit_*,booking/cancel_over_24h")
    ap.add_argument("--judge", action="store_true", help="LLM-as-judge scores for judge: true scenarios")
    ap.add_argument("--keep", action="store_true", help="keep each scenario's clone database for inspection")
    args = ap.parse_args()
    KEEP = args.keep
    logging.getLogger().setLevel(logging.WARNING)  # app.main configured INFO on import
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("app.llm").setLevel(logging.INFO if args.mode != "replay" else logging.WARNING)

    s = get_settings()
    seed_now = os.environ.get("EVAL_SEED_NOW", "2026-11-12T10:00")
    min_left = int(os.environ.get("EVAL_MIN_BUDGET_LEFT", "100"))
    everything = load_scenarios()
    scenarios = _selected(everything, args.only)
    orgs = _orgs()
    main_engine = db.app_engine  # the main database: llm_usage is counted here
    providers = Providers(main_engine)

    await providers.print_budget("before")
    t0 = time.monotonic()
    rebuilt, fp = await evaldb.ensure_template(seed_now)
    print(f"template {evaldb.TEMPLATE}: {'rebuilt' if rebuilt else 'up to date'} ({fp}), seed now {seed_now}", flush=True)
    for name in await evaldb.drop_leftover_clones():
        print(f"dropped leftover clone {name}")

    # Warm the embedding model once (search_knowledge loads it lazily, which would bill the
    # first KB scenario with several seconds).
    from app.domain.kb.embeddings import embed_query

    await embed_query("warm up")

    # Every clone up front, one at a time (Postgres allows one copy of a template at once).
    # Each CREATE/DROP DATABASE forces a checkpoint; done back to back, before any scenario
    # has written anything, those checkpoints have nothing to flush. Interleaved with the
    # scenarios' writes they took seconds each on a slow disk.
    t_clone = time.monotonic()
    for sc in scenarios:
        await evaldb.create_clone(evaldb.clone_name(sc.key))
    print(f"{len(scenarios)} clones ready in {time.monotonic() - t_clone:.1f}s", flush=True)

    results = []
    stopped: str | None = None
    for sc in scenarios:
        has_cassette = (CASSETTES / f"{sc.key}.json").exists()
        mode = args.mode if args.mode != "auto" else ("replay" if has_cassette else "record")
        if stopped:
            results.append({"key": sc.key, "id": sc.id, "vertical": sc.vertical, "title": sc.title, "rule": sc.rule,
                            "status": "skipped", "failures": [stopped], "turns": [], "mode": mode})  # fmt: skip
            continue
        if mode == "record":
            left = await providers.left()
            if left < min_left:
                stopped = f"not run: {providers.primary} has {left} requests left today (< {min_left})"
                print(f"STOP: {stopped}", flush=True)
                results.append({"key": sc.key, "id": sc.id, "vertical": sc.vertical, "title": sc.title, "rule": sc.rule,
                                "status": "skipped", "failures": [stopped], "turns": [], "mode": mode})  # fmt: skip
                continue
            provider = providers.record()
        else:
            provider = providers.replay
        r = await run_scenario(sc, orgs[sc.org], provider, seed_now)
        r["mode"] = mode
        if args.judge and sc.judge and r["turns"]:
            import judge

            judged = (CASSETTES / "judge" / f"{sc.key}.json").exists()
            jmode = args.mode if args.mode != "auto" else ("replay" if judged else "record")
            r["judge_scores"] = await judge.score(sc, r, providers, jmode)
        results.append(r)
        mark = {"pass": "PASS", "fail": "FAIL", "error": "ERR "}[r["status"]]
        print(f"{mark} {sc.key:45s} {mode:6s} {r['wall_ms']:>6d} ms  {'; '.join(r['failures'])[:160]}", flush=True)

    if not KEEP:  # the first drop's checkpoint flushes everything, so the rest are quick
        for sc in scenarios:
            try:
                await evaldb.drop_clone(evaldb.clone_name(sc.key))
            except Exception as exc:  # results are already in hand; the next run drops leftovers
                print(f"warning: could not drop {evaldb.clone_name(sc.key)}: {type(exc).__name__}", flush=True)
                break
    elapsed = time.monotonic() - t0
    if args.only:  # a partial run updates those scenarios in the last full report
        results = report.merge_previous(EVAL_DIR / "results.json", results)
    explained = report.explained_ids(EVAL_DIR / "FAILURES.md")
    report.write(EVAL_DIR, results, providers.primary, s.llm_models(s.llm_provider), elapsed, explained)
    await providers.print_budget("after")
    await main_engine.dispose()
    bad = [r["key"] for r in results if r["status"] != "pass" and r["key"] not in explained]
    print(f"\n{sum(r['status'] == 'pass' for r in results)}/{len(results)} passed in {elapsed:.1f}s"
          f"; unexplained failures: {', '.join(bad) or 'none'} → eval/results.md")  # fmt: skip
    if stopped:
        return 3
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
