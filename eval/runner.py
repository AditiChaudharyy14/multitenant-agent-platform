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
from sqlalchemy.engine import make_url  # noqa: E402

import checks  # noqa: E402
import evaldb  # noqa: E402
import judge  # noqa: E402
import quota  # noqa: E402
import report  # noqa: E402
import staleness  # noqa: E402
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
from app.redact import install_output_redaction, redact, redact_obj  # noqa: E402
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
        """Requests left today across the primary and fallback models together."""
        used = {m: await self.budget.used(m) for m in self.settings.llm_models(self.settings.llm_provider)}
        return quota.left_combined(used, self.budget.limit)

    async def print_budget(self, when: str) -> str:
        """What `make llm-budget` shows, printed in-process (no extra container per run)."""
        parts = []
        for model in self.settings.llm_models(self.settings.llm_provider):
            used = await self.budget.used(model)
            parts.append(f"{model} {used}/{self.budget.limit} used ({self.budget.limit - used} left)")
        line = f"llm budget {when} (quota day {quota_day()} Pacific): " + "; ".join(parts)
        print(line, flush=True)
        return line


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
                              "user": sc.user, "category": sc.category}  # fmt: skip
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
            # The admin analytics chat has its own API; the server decides who may use it.
            chat_api = "/api/admin/conversations" if sc.chat == "admin" else "/api/conversations"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://eval.local",
                                         headers={"Origin": ORIGIN}, cookies={COOKIE_NAME: token}, timeout=300) as c:  # fmt: skip
                if sc.expect_http is not None:
                    http_fails = await _expect_refused(c, chat_api, sc.expect_http, texts[0] or "")
                    async with evaldb.owner_tx(owner, org.org_id) as conn:
                        fails = http_fails + await checks.check_all(conn, org, sc, me, [], before)
                    result.update(status="pass" if not fails else "fail", failures=fails)
                    return result
                r = await c.post(chat_api)
                r.raise_for_status()
                cid = r.json()["id"]
                cids = [cid]
                for turn, utext in zip(sc.turns, texts, strict=True):
                    if turn.new_conversation:  # no transcript carries over: only long-term memory can
                        r = await c.post(chat_api)
                        r.raise_for_status()
                        cid = r.json()["id"]
                        cids.append(cid)
                    if turn.setup:  # a change *between* turns, e.g. a price update
                        async with evaldb.owner_tx(owner, org.org_id) as conn:
                            for step in turn.setup:
                                await checks.apply_setup(conn, org, step)
                    rec = checks.TurnRecord(request=utext or f"[{turn.action}]")
                    if utext and len(utext) > 300:  # e.g. {{chars:2001}}: keep the report readable
                        rec.request = f"{utext[:80]}… ({len(utext)} characters)"
                    try:
                        body = {"content": utext} if utext else {"action": _action_payload(turn.action or "", turns)}
                    except ScenarioError:
                        if not turn.optional:
                            raise
                        rec.status, rec.reply = "skipped", ""  # nothing to press; still one record per turn
                        turns.append(rec)
                        continue
                    resp = await c.post(f"{chat_api}/{cid}/messages", json=body)
                    if turn.expect_http is not None and resp.status_code != 200:
                        # Refused before any turn started; check_all compares the status.
                        rec.http_status, rec.status = resp.status_code, "refused"
                        rec.reply = _refusal_text(resp)
                        turns.append(rec)
                        continue
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
                    if staleness.is_stale(rec.run_error):  # recorded against an older request
                        raise StaleCassette(rec.run_error)
                    if rec.run_error and rec.run_error.startswith("CassetteMissing"):
                        raise ScenarioError(rec.run_error)
                    if rec.run_error:  # the provider failed (after its retries): not a valid sample
                        raise ScenarioError(f"provider failed during turn {len(turns)}: {rec.run_error}; "
                                            f"re-record: make eval-live ONLY={sc.key}")  # fmt: skip
            async with evaldb.owner_tx(owner, org.org_id) as conn:
                fails = await checks.check_all(conn, org, sc, me, turns, before)
                # Every tool result the agent received: the judge's evidence for its scores.
                result["tool_calls"] = await _tool_calls(conn, cids)
        result.update(status="pass" if not fails else "fail", failures=fails)
    except StaleCassette as exc:
        result.update(status="stale", failures=["cassette stale: the request changed since it was recorded"], detail=str(exc))
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


def _refusal_text(resp: httpx.Response) -> str:
    """The refusal's message(s), without the echoed input (a 2001-character message)."""
    try:
        detail = resp.json().get("detail")
    except ValueError:
        return resp.text[:300]
    if isinstance(detail, list):  # FastAPI validation errors
        return "; ".join(str(d.get("msg", "")) for d in detail if isinstance(d, dict))
    return str(detail)


async def _expect_refused(c: httpx.AsyncClient, chat_api: str, status: int, text_: str) -> list[str]:
    """Starting the chat, listing it and posting into it must all be refused with `status`."""
    fails = []
    for method, path, body in (("POST", chat_api, None), ("GET", chat_api, None),
                               ("POST", f"{chat_api}/{uuid.uuid4()}/messages", {"content": text_})):  # fmt: skip
        r = await c.request(method, path, json=body)
        if r.status_code != status:
            fails.append(f"{method} {path.split('/messages')[0][:40]}…: HTTP {r.status_code}, expected {status}")
    return fails


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


async def _tool_calls(conn: Any, conversation_ids: list[str]) -> list[dict]:
    """name, arguments and result of every tool call (and card button) in these conversations."""
    out = []
    for cid in conversation_ids:
        steps = (await conn.execute(
            text("SELECT tool_calls FROM messages WHERE conversation_id = :c AND role = 'assistant' ORDER BY created_at"),
            {"c": uuid.UUID(cid)},
        )).scalars().all()  # fmt: skip
        out += [{"name": c["name"], "arguments": c.get("arguments"), "result": c.get("result", "")}
                for st in steps for step in (st or []) for c in step["calls"]]  # fmt: skip
    return out


def _orgs() -> dict[str, checks.OrgInfo]:
    out = {}
    for cfg in load_org_configs(get_settings().seed_dir):
        out[cfg.slug] = checks.OrgInfo(evaldb.org_id_for(cfg.slug), cfg.slug, cfg.vertical, cfg.timezone,
                                       cfg.order_prefix or "", cfg.booking_prefix or "")  # fmt: skip
    return out


def _selected(all_: list[Scenario], only: str | None, categories: str | None = None) -> list[Scenario]:
    if categories:
        cats = {c.strip() for c in categories.split(",") if c.strip()}
        all_ = [s for s in all_ if s.category in cats]
    if not only:
        return all_
    pats = [p.strip() for p in only.split(",") if p.strip()]
    picked = [s for s in all_ if any(fnmatch.fnmatch(s.key, p) or s.id == p for p in pats)]
    if not picked:
        raise SystemExit(f"--only {only!r} matches no scenario")
    return picked


KEEP = False


class StaleCassette(ScenarioError):
    """The cassette was recorded against a different request (see eval/staleness.py)."""


def _repeat_run_dir(run_dir: Path, mode: str) -> Path:
    """A pass^k repeat run records into its own new directory, never over the main cassettes."""
    global CASSETTES
    if mode != "record":
        raise SystemExit("--run-dir is for repeat live runs: use --mode record (make eval-live)")
    run_dir = run_dir.resolve()
    if run_dir == EVAL_DIR or run_dir == CASSETTES.resolve() or CASSETTES.resolve() in (run_dir, *run_dir.parents):
        raise SystemExit(f"--run-dir {run_dir} would overwrite the main cassettes or results")
    if (run_dir / "cassettes").exists() and any((run_dir / "cassettes").rglob("*.json")):
        raise SystemExit(f"--run-dir {run_dir} already holds cassettes; pick a new directory")
    (run_dir / "cassettes").mkdir(parents=True, exist_ok=True)
    CASSETTES = run_dir / "cassettes"
    return run_dir


def _mode(sc: Scenario, requested: str) -> str:
    if not sc.needs_model:
        return "replay"  # refused before any model call: needs no cassette, key or budget
    if requested != "auto":
        return requested
    return "replay" if (CASSETTES / f"{sc.key}.json").exists() else "record"


async def main() -> int:
    global KEEP
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=("auto", "record", "replay"), default="auto")
    ap.add_argument("--only", help="comma-separated scenario keys or globs, e.g. store/edit_*,booking/cancel_over_24h")
    ap.add_argument("--judge", action="store_true", help="LLM-as-judge scores for every scenario with a reply")
    ap.add_argument("--calibrate-judge", action="store_true",
                    help="score eval/judge_calibration.yaml's deliberately bad replies; each must get <= 2")
    ap.add_argument("--category", help="comma-separated categories, e.g. rule,safety")
    ap.add_argument("--run-dir", help="a repeat live run (pass^k): cassettes and results go to this new, empty "
                                      "directory, e.g. /eval/runs/run2; the main cassettes are never touched")
    ap.add_argument("--reserve", type=int, default=0,
                    help="requests to keep on top of EVAL_MIN_BUDGET_LEFT (e.g. for a judge run afterwards)")
    ap.add_argument("--keep", action="store_true", help="keep each scenario's clone database for inspection")
    args = ap.parse_args()
    KEEP = args.keep
    out_dir = EVAL_DIR
    if args.run_dir:
        out_dir = _repeat_run_dir(Path(args.run_dir), args.mode)
    logging.getLogger().setLevel(logging.WARNING)  # app.main configured INFO on import
    install_output_redaction()  # no DB URL password or API key in logs or tracebacks
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # A stale cassette is reported once per scenario (STALE) and in the closing banner; the
    # agent's own per-turn warning would add the whole request diff for each one.
    logging.getLogger("app.agent").addFilter(lambda r: staleness.STALE_PREFIX not in r.getMessage())
    logging.getLogger("app.llm").setLevel(logging.INFO if args.mode != "replay" else logging.WARNING)

    s = get_settings()
    # Template and clones live on db-ephemeral only; the dev db holds just the quota ledger.
    ephemeral = os.environ.get("EVAL_DB_HOST", "db-ephemeral")
    for var in ("DATABASE_URL_OWNER", "DATABASE_URL_APP", "DATABASE_URL_READONLY", "DATABASE_URL_EVAL"):
        if make_url(os.environ.get(var, "")).host != ephemeral:
            raise SystemExit(f"{var} must point at {ephemeral}; run evals through `make eval`")
    seed_now = os.environ.get("EVAL_SEED_NOW", "2026-11-12T10:00")
    floor = int(os.environ.get("EVAL_MIN_BUDGET_LEFT", str(quota.DEFAULT_FLOOR)))
    everything = load_scenarios()
    scenarios = _selected(everything, args.only, args.category)
    orgs = _orgs()
    # The quota ledger (llm_usage) lives in the dev database, shared with the running
    # backend; everything else this run touches is on db-ephemeral.
    budget_url = os.environ.get("DATABASE_URL_BUDGET") or s.database_url_app
    main_engine = evaldb.engine(budget_url)
    providers = Providers(main_engine)

    budget_before = await providers.print_budget("before")
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
    stale: list[str] = []
    modes = {sc.key: _mode(sc, args.mode) for sc in scenarios}
    by_key = {sc.key: sc for sc in scenarios}
    for key in staleness.order_for_run(modes):  # replays first: see staleness before recording
        sc, mode = by_key[key], modes[key]
        if mode == "record" and args.mode == "auto" and not staleness.may_record(stale):
            results.append({"key": sc.key, "id": sc.id, "vertical": sc.vertical, "title": sc.title, "rule": sc.rule,
                            "status": "skipped", "failures": ["not recorded: other cassettes are stale"],
                            "turns": [], "mode": mode})  # fmt: skip
            print(f"SKIP {sc.key:45s} not recorded: other cassettes are stale", flush=True)
            continue
        if stopped:
            results.append({"key": sc.key, "id": sc.id, "vertical": sc.vertical, "title": sc.title, "rule": sc.rule,
                            "status": "skipped", "failures": [stopped], "turns": [], "mode": mode})  # fmt: skip
            continue
        if mode == "record":
            left = await providers.left()
            if not quota.may_record(left, floor, args.reserve):
                stopped = (f"not run: {left} requests left today across all models; one more scenario "
                           f"(up to {quota.WORST_CASE_PER_SCENARIO}) could go below the floor of {floor}"
                           f"{f' + reserve {args.reserve}' if args.reserve else ''}")
                print(redact(f"STOP: {stopped}"), flush=True)
                results.append({"key": sc.key, "id": sc.id, "vertical": sc.vertical, "title": sc.title, "rule": sc.rule,
                                "status": "skipped", "failures": [stopped], "turns": [], "mode": mode})  # fmt: skip
                continue
            provider = providers.record()
        else:
            provider = providers.replay
        r = await run_scenario(sc, orgs[sc.org], provider, seed_now)
        r["mode"] = mode
        if args.judge and judge.judgeable(r):
            judged = (CASSETTES / "judge" / f"{sc.key}.json").exists()
            jmode = args.mode if args.mode != "auto" else ("replay" if judged else "record")
            if jmode == "record" and not quota.may_record(await providers.left(), floor, args.reserve, worst=2):
                r["judge_scores"] = {"error": f"not judged: the quota floor of {floor} would be crossed"}
            else:
                r["judge_scores"] = await judge.score(sc, r, providers, jmode)
        results.append(r)
        if r["status"] == "stale":
            stale.append(sc.key)
        mark = {"pass": "PASS", "fail": "FAIL", "error": "ERR ", "stale": "STALE"}[r["status"]]
        print(redact(f"{mark} {sc.key:45s} {mode:6s} {r['wall_ms']:>6d} ms  {'; '.join(r['failures'])[:160]}"), flush=True)

    calibration: list[dict] = []
    if args.calibrate_judge:
        cal_dir = CASSETTES / "judge" / "calibration"
        cmode = args.mode if args.mode != "auto" else ("replay" if cal_dir.exists() and any(cal_dir.glob("*.json")) else "record")
        if cmode == "record" and not quota.may_record(await providers.left(), floor, args.reserve, worst=10):
            print(f"judge calibration not run: the quota floor of {floor} would be crossed", flush=True)
        else:
            calibration = await judge.calibrate(providers, cmode)
            for c in calibration:
                print(f"CALIBRATION {c['id']:28s} target {c['target']:8s} {'caught' if c['caught'] else 'MISSED'}  "
                      f"{ {k: c['scores'].get(k) for k in judge.CRITERIA} }", flush=True)  # fmt: skip

    if not KEEP:  # the first drop's checkpoint flushes everything, so the rest are quick
        for sc in scenarios:
            try:
                await evaldb.drop_clone(evaldb.clone_name(sc.key))
            except Exception as exc:  # results are already in hand; the next run drops leftovers
                print(f"warning: could not drop {evaldb.clone_name(sc.key)}: {type(exc).__name__}", flush=True)
                break
    elapsed = time.monotonic() - t0
    if stale:  # a stale run says nothing about the agent: keep the last real report
        await providers.print_budget("after")
        await main_engine.dispose()
        missing = [r["key"] for r in results if any(f.startswith("CassetteMissing") for f in r["failures"])
                   or r["failures"] == ["not recorded: other cassettes are stale"]]  # fmt: skip
        print(staleness.banner(stale, missing), flush=True)
        return 4
    position = {sc.key: i for i, sc in enumerate(scenarios)}
    results.sort(key=lambda r: position[r["key"]])  # report in file order, not run order
    if args.only and not args.run_dir:  # a partial run updates those scenarios in the last full report
        results = report.merge_previous(EVAL_DIR / "results.json", results)
    results = redact_obj(results)  # results.json/.md are committed: no credentials in them
    explained = report.explained_ids(EVAL_DIR / "FAILURES.md")
    budget_after = await providers.print_budget("after")
    if not calibration and not args.run_dir:  # keep the last calibration when this run made none
        calibration = report.previous_calibration(EVAL_DIR / "results.json")
    report.write(out_dir, results, providers.primary, s.llm_models(s.llm_provider), elapsed, explained,
                 budget=(budget_before, budget_after), calibration=calibration)  # fmt: skip
    await main_engine.dispose()
    bad = [r["key"] for r in results if r["status"] != "pass" and r["key"] not in explained]
    print(f"\n{sum(r['status'] == 'pass' for r in results)}/{len(results)} passed in {elapsed:.1f}s"
          f"; unexplained failures: {', '.join(bad) or 'none'} → eval/results.md")  # fmt: skip
    if stopped:
        return 3
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
