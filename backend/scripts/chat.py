"""`make chat ORG=<slug> EMAIL=<email> PASSWORD=<pw> Q="..." [CONV=<id>]`: one chat turn.

Talks to the running backend over HTTP exactly like the browser: logs in (the slug only
picks the org), creates a conversation (or continues CONV), posts the question and prints
the SSE stream. --admin (make chat ADMIN=1) uses the admin analytics chat instead, which
needs an admin account. Q="!confirm <proposal_id>" presses a card's Confirm button instead, and
Q="!select <service>|<start>[|<practitioner>[|<booking number>]]" presses a slot button.
Runs inside the backend container. DEMO ONLY: a password on the command line is visible
in the process list and shell history; use the seeded demo accounts.
"""

import argparse
import json
import sys

import httpx

from app.config import get_settings

BASE_URL = "http://127.0.0.1:8000"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--org", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--conversation", help="continue this conversation instead of starting one")
    p.add_argument("--admin", action="store_true", help="the admin analytics chat (/api/admin/conversations)")
    p.add_argument("question")
    a = p.parse_args()

    # Non-GET /api requests need an allowed Origin (CSRF check), like a browser sends.
    origin = sorted(get_settings().allowed_origin_set)[0]
    with httpx.Client(base_url=BASE_URL, headers={"Origin": origin}, timeout=httpx.Timeout(10, read=120)) as c:
        r = c.post(f"/api/orgs/{a.org}/auth/login", json={"email": a.email, "password": a.password})
        if r.status_code != 200:
            print(f"login failed: {r.status_code} {r.text}", file=sys.stderr)
            return 1
        chat_api = "/api/admin/conversations" if a.admin else "/api/conversations"
        if a.conversation:
            cid = a.conversation
        else:
            r = c.post(chat_api)
            if r.status_code != 201:
                print(f"could not start a chat: {r.status_code} {r.text}", file=sys.stderr)
                return 1
            cid = r.json()["id"]
        print(f"org={a.org} {'admin ' if a.admin else ''}conversation={cid}\n> {a.question}\n")
        status = "?"
        body: dict = {"content": a.question}
        if a.question.startswith("!confirm "):  # the Confirm button: no model decides the commit
            body = {"action": {"type": "confirm_proposal", "proposal_id": a.question.split(maxsplit=1)[1]}}
        elif a.question.startswith("!select "):  # a slot button: service|start[|practitioner[|booking]]
            parts = [x.strip() or None for x in a.question.split(maxsplit=1)[1].split("|")] + [None, None]
            body = {"action": {"type": "select_slot", "service": parts[0], "start": parts[1],
                               "practitioner": parts[2], "booking_number": parts[3]}}  # fmt: skip
        with c.stream("POST", f"{chat_api}/{cid}/messages", json=body) as resp:
            if resp.status_code != 200:
                print(f"error {resp.status_code}: {resp.read().decode()}", file=sys.stderr)
                return 1
            kind = None
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    kind = line[7:]
                elif line.startswith("data: ") and kind:
                    status = _print(kind, json.loads(line[6:])) or status
                elif line.startswith(":"):
                    print("[heartbeat]", file=sys.stderr)
        print(f"\n[status] {status}")
        return 0 if status == "complete" else 2


def _print(kind: str, data: dict) -> str | None:
    if kind == "token":
        print(data["text"], end="", flush=True)
    elif kind == "tool_start":
        print(f"[tool] {data['name']}: {data['label']}")
    elif kind == "tool_end":
        print(f"[tool] {data['name']}: {data['label']} ({'ok' if data['ok'] else 'failed'})")
    elif kind == "card":
        print(f"[card] {_card(data['card'])}")
    elif kind == "citation":
        print(f"\n[cite] {data['id']} ({data['title']})", end="")
    elif kind == "error":
        print(f"\n[error] {data['code']}: {data['message']}")
    elif kind == "done":
        return data["status"]
    return None


def _card(card: dict) -> str:
    """Order cards on a few readable lines; anything else as JSON."""
    kind = card.get("type")
    if kind in {"order_summary", "order_status", "order_confirmed", "order_updated"}:
        head = f"{kind}" + (f" ({card['kind']})" if "kind" in card else "")
        head += f" {card.get('order_number', '')} {card.get('status', '')}".rstrip()
        lines = card.get("lines") or card.get("items") or []
        out = [head] + [
            f"    {x['qty']} x {x['name']} ({x['sku']}) @ {x['unit_price']} = {x['line_total']}" for x in lines
        ]
        if "total_display" in card:
            out.append(f"    total {card['total_display']}, to: {card.get('delivery_address', '')}")
        out += [f"    change: {c}" for c in card.get("changes", [])]
        out += [f"    history: {h['from_status']} -> {h['to_status']} at {h['at']}" for h in card.get("history", [])]
        if card.get("actions"):
            out.append(f"    [button] {card['actions'][0]['label']}  proposal_id={card['proposal_id']}")
        return "\n".join(out)
    if kind in {"slot_list", "slot_alternatives"}:
        head = f"{kind} {card['service']}" + (f" (refused: {card['reason']})" if kind == "slot_alternatives" else "")
        if card.get("resolved_dates"):
            head += f" on {', '.join(card['resolved_dates'])}" + (f", {card['window']}" if card.get("window") else "")
        out = [head] + [f"    note: {n}" for n in card.get("notes", [])]
        for slot in card["slots"]:
            who = slot.get("practitioner") or ", ".join(slot.get("practitioners", []))
            act = slot["action"]
            button = f"!select {act['service']}|{act['start']}|{act.get('practitioner') or ''}"
            button += f"|{act['booking_number']}" if act.get("booking_number") else ""
            out.append(f"    [button] {slot['display']} ({who})  -> {button.rstrip('|')}")
        return "\n".join(out)
    if kind in {"booking_summary", "booking_confirmed", "booking_rescheduled", "booking_cancelled"}:
        head = (
            f"{kind}"
            + (f" ({card['kind']})" if "kind" in card else "")
            + f" {card.get('booking_number') or ''}".rstrip()
        )
        out = [head, f"    {card.get('service', '')} with {card.get('practitioner', '')}: {card.get('when', '')}"
                     + (f", {card['duration_min']} min" if "duration_min" in card else "")
                     + (f", {card['price_display']}" if "price_display" in card else "")]  # fmt: skip
        if card.get("old"):
            out.append(f"    was: {card['old']['when']} with {card['old']['practitioner']}")
        if "fee_display" in card:
            out.append(f"    cancellation fee: {card['fee_display']}")
        out += [f"    change: {c}" for c in card.get("changes", [])]
        if card.get("actions"):
            out.append(f"    [button] {card['actions'][0]['label']}  proposal_id={card['proposal_id']}")
        return "\n".join(out)
    if kind == "metric":
        w = card["window"]
        return (
            f"metric {card['metric']}: {json.dumps(card['values'], ensure_ascii=False)}\n"
            f"    window: {w['label']} [{w['start']} .. {w['end']}) {w['timezone']}\n"
            f"    definition: {card['definition']}"
        )
    if kind == "booking_list":
        out = ["booking_list"]
        for group in ("upcoming", "past"):
            out += [f"    {group}: {b['booking_number']} {b['when']} {b['service']} with {b['practitioner']} [{b['status']}]"
                    + (f" fee {b['cancellation_fee_display']}" if b.get("cancellation_fee_display") else "")
                    for b in card[group]]  # fmt: skip
        return "\n".join(out)
    return json.dumps(card, ensure_ascii=False)


if __name__ == "__main__":
    sys.exit(main())
