## `make stats` (dev database, 2026-11-08)

`make stats` reads `agent_runs` in the **dev** database: manual chats, demo runs and `make e2e` turns on the scripted fake model, whose flat 1.4 s and small token counts are visible below. Eval turns never land there, because every scenario runs on a throwaway clone. **The live-model figures are in the table above.** This table shows the command working, per org and agent, under RLS as each org's admin.

| Org | Agent | Turns | p50 | p95 | Avg tokens in / turn | Avg tokens out / turn |
|---|---|---|---|---|---|---|
| himali-tea | admin | 2 | 0.05s | 0.07s | 56 | 20 |
| himali-tea | customer | 92 | 1.4s | 7.6s | 1,205 | 71 |
| kathmandu-spice | customer | 2 | 0.76s | 0.77s | 170 | 25 |
| lotus-wellness | customer | 44 | 1.4s | 1.4s | 1,463 | 96 |
