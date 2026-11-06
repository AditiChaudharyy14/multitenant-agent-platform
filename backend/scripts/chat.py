"""`make chat ORG=<slug> EMAIL=<email> PASSWORD=<pw> Q="..."`: one chat turn from the terminal.

Talks to the running backend over HTTP exactly like the browser: logs in (the slug only
picks the org), creates a conversation, posts the question and prints the SSE stream.
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
    p.add_argument("question")
    a = p.parse_args()

    # Non-GET /api requests need an allowed Origin (CSRF check), like a browser sends.
    origin = sorted(get_settings().allowed_origin_set)[0]
    with httpx.Client(base_url=BASE_URL, headers={"Origin": origin}, timeout=httpx.Timeout(10, read=120)) as c:
        r = c.post(f"/api/orgs/{a.org}/auth/login", json={"email": a.email, "password": a.password})
        if r.status_code != 200:
            print(f"login failed: {r.status_code} {r.text}", file=sys.stderr)
            return 1
        cid = c.post("/api/conversations").json()["id"]
        print(f"org={a.org} conversation={cid}\n> {a.question}\n")
        status = "?"
        with c.stream("POST", f"/api/conversations/{cid}/messages", json={"content": a.question}) as resp:
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
        print(f"[card] {json.dumps(data['card'], ensure_ascii=False)}")
    elif kind == "citation":
        print(f"\n[cite] {data['id']} ({data['title']})", end="")
    elif kind == "error":
        print(f"\n[error] {data['code']}: {data['message']}")
    elif kind == "done":
        return data["status"]
    return None


if __name__ == "__main__":
    sys.exit(main())
