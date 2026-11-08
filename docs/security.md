# Supply chain and secrets

Checked on 2026-11-08. Re-run with the `make` targets below.

| Check | Command | Result |
|---|---|---|
| Secrets in the **full git history** (gitleaks v8.21.2, every commit on every ref) | `make secrets-scan` | 0 findings, 113 commits |
| Python dependencies (pip-audit 2.7.3 on `backend/requirements.lock`) | `make audit` | No known vulnerabilities |
| npm dependencies | `make audit` | 1 high, 1 moderate in production dependencies (accepted, see below); 6 moderate in dev-only tooling |
| Containers run as a non-root user | `backend/tests/security/test_containers.py` | backend `app` (UID 1000), frontend `node` (UID 1000) |
| Lockfiles committed | | `backend/requirements.lock` (exact versions, installed by the Dockerfile), `frontend/package-lock.json` (`npm ci`) |

## Accepted: PostCSS advisories inside Next.js

`npm audit --omit=dev` reports PostCSS `<=8.5.22`, vendored by `next@15.5.x` (`node_modules/next/node_modules/postcss`). The advisories are GHSA-r28c-9q8g-f849 and related ones: arbitrary `.map` file reads through an attacker-controlled `sourceMappingURL` in CSS, and an XSS through `</style>` in CSS stringify output.

- **Why it is not exploitable here:** PostCSS runs only at build time (`next build`), on this repository's own stylesheets. No user-supplied CSS is ever processed, and nothing calls PostCSS at runtime.
- **Why not fixed now:** the only fix npm offers is `next@16`, a major upgrade, which is too risky the day before submission.
- **Next step:** upgrade to Next 16 with a full `make e2e` run.

The dev-only moderates (ESLint plugin chain, Tailwind's PostCSS) are not shipped to browsers and run only on developer machines.
