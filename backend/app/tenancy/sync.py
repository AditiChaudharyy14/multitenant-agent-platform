"""`make sync-orgs`: upsert the orgs table from seed/<slug>/org.yaml, as app_owner.

Runs in the one-shot ops container, never in the running backend.
"""

import asyncio

from app.config import get_settings, require_owner_url
from app.tenancy.orgs import load_org_configs, sync_orgs


def main() -> None:
    configs = load_org_configs(get_settings().seed_dir)
    asyncio.run(sync_orgs(require_owner_url(), configs))
    for cfg in configs:
        print(f"synced org {cfg.slug} ({cfg.vertical})")


if __name__ == "__main__":
    main()
