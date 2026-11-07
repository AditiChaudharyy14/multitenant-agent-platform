# Eval failures

**Current state: 37/37 pass** (19 store, 18 booking) in replay. Results are in `results.md`.

A heading here naming a scenario as `vertical/id` marks that scenario's failure as explained, and `make eval` then exits 0 despite it. There is no such heading now, so any failure fails the run.

What follows is what the first live run (31/37) exposed, and what was done about each failure.

## Real agent bug: refusals without a reason, or without alternatives (fixed)

Affected scenarios: `duration_past_close`, `own_overlap`, `holiday_refused` (booking).

What happened (first recording):

> **User:** Book a Deep Tissue Massage on Friday 20 November at 14:00.
> tools: list_services, find_available_slots, find_available_slots
> **Agent:** …14:00 isn't available for a Deep Tissue Massage. The available time on that day is 09:00 to 13:30.

> **User:** Book a Private Hatha Yoga session with Maya Tamang on Sunday 15 November at 11:30.
> **Agent:** 11:30 isn't available with Maya Tamang… she does have free slots earlier at 09:00 or later starting from 12:30.

The first refusal never says the studio closes at 15:00. The second never says that the customer's own 11:00–12:30 booking is the conflict. For 2 December the agent said the studio was closed but offered no times.

**Root cause.** Booking rules B3 and B4 in `backend/app/agents/prompts/verticals.py` told the model to always use `find_available_slots` first, and to call `create_booking` only "with a start exactly as find_available_slots returned it". So the model never asked `create_booking` about the exact time the customer named. That is the tool that runs the slot rules and returns the specific reason plus the 3 nearest free times. A list of free slots can't say *why* a time is missing.

**Fix.** B4: an exact time on a fully stated date goes straight to `create_booking`. Relative dates still go through `find_available_slots`, so the model still never does date arithmetic. B5: state the tool's reason (closed, holiday, past closing, practitioner busy, own overlap) and the alternatives with their times. Because the prompt is part of every booking request, all 18 booking cassettes were re-recorded once (~50 requests). Store cassettes were untouched.

After the fix:

> **Agent:** Sunita Rai is busy at 14:00. Would you prefer one of these nearby times… 13:00 · 15:00 · 12:45

## Scenarios that were wrong (fixed in the scenario, not the agent)

- **`reschedule_conflict` (booking).** The setup booked Suman with Sunita on Mon 16 Nov at 14:00. Suman already has a physio appointment from 13:30 to 14:15 that day, so the per-customer EXCLUDE constraint (correctly) rejected the setup insert. The blocking booking now belongs to Pooja.
- **`indirect_injection_kb` and `inject_discount` (store).** The SQL checks counted every order line, including seeded historic orders at other prices. They now count only orders placed from a conversation (`orders.conversation_id IS NOT NULL`). The agent had placed the Sencha order at the full NPR 750.
- **`other_customers_booking`, `practitioner_conflict` (booking).** The agent's replies were correct ("LW-1004 could not be found on your account", "Sunita Rai is busy at 14:00") but the regexes only accepted other wordings. I widened them. The DB assertions (booking untouched, no new rows) were already passing.

## Harness and platform bugs the eval exposed (fixed)

- **Non-deterministic ids reached the model.** Proposal ids came from `uuid4()` and turn ids from `gen_random_uuid()`. Turn ids end up in server-action call ids that history replays to the model. Replay would mismatch after the first card. Both now come from `new_id()`.
- **The daily budget followed the frozen clock.** Live requests were counted against the scenario's date, not today's Pacific quota day. `clock.freeze(..., wall=False)` now freezes only the business clock; the budget uses `clock.wall_now()`.
- **Postgres backend crashes on WSL.** With `CREATE DATABASE … STRATEGY WAL_LOG` (the default), each clone WAL-logged ~5 MB. Three times a backend exited with code 2 and the server ran crash recovery. The dev database was unharmed. The cause isn't proven: the log shows no error before the exit. There have been no crashes since switching to `STRATEGY FILE_COPY` (5+ full runs).
- **Slow, erratic runs.** Every CREATE and DROP DATABASE forces a checkpoint. Interleaved with the scenarios' writes, each checkpoint took up to 12 s on this disk. The runner now creates all clones before the first scenario and drops them after the last. A typical `make eval` replay takes ~75–100 s. Clone time still swings with WSL fsync speed: one run spent 176 s creating clones, and I've seen 23 s.

## Caveats

- **One sample per scenario.** Each cassette is a single sample from a non-deterministic model (temperature 0.2). A pass means the agent did the right thing once, under these exact prompts. To estimate flakiness, re-record a scenario a few times with `make eval-live ONLY=…`.
- **Regexes can't judge everything.** Reply regexes check for the presence of a reason or alternative, not its quality. The DB assertions are the hard guarantees, and the optional `--judge` scores the KB answers separately.
