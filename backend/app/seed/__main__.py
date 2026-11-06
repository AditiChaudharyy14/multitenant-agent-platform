"""`make seed` entry point: python -m app.seed (ops container, app_owner)."""

import asyncio

from app.config import get_settings, require_owner_url
from app.seed.run import seed_all


def main() -> None:
    settings = get_settings()
    results = asyncio.run(seed_all(require_owner_url(), settings.seed_dir, settings.seed_now))
    anchor = settings.seed_now or "real clock"
    for r in results:
        counts = ", ".join(f"{t}={n}" for t, n in r.counts.items() if n)
        print(f"seeded {r.slug} ({r.vertical}, now={anchor}): {counts}")


if __name__ == "__main__":
    main()
