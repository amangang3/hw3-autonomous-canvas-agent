# Architecture

## Boundary

Hermes is the decision maker: it reads sanitized forum data and decides whether to reply, start a thread, or skip. `canvas_tool` is the deterministic Canvas boundary. It alone loads the Canvas credential and it exposes no edit or delete path.

## Scheduler

Deployment uses a Hermes cron job every 30 minutes (`*/30 * * * *`) with the prompt: `Run one HW3 forum cycle. Follow ~/hw3-agent/RUNTIME.md exactly and actively find one substantive contribution to make.` The Hermes gateway runs as a long-lived launchd service with `RunAtLoad` and `KeepAlive`, so closing the interactive terminal does not stop scheduled runs.

## Canvas access

`client.py` pins HTTPS requests to `canvas.mit.edu`, applies a 20-second timeout, validates same-origin pagination, retries only eligible GET failures, and sends every POST once. Discovery aborts unless exactly one active-course topic has the exact required title.

## Enforcement

`gate.py` performs target, content, idempotency, rate, STOP, and fresh control-line checks before a durable intent and one POST. Messages are normalized, filtered, HTML-escaped, and scoped to the cached topic. Verification requires the returned entry to appear in a fresh tree as self-authored, at the intended parent, with matching logical text.

## Memory

SQLite runs in WAL mode. `meta` caches Canvas IDs and breaker state; `seen_entries` provides at-most-once delivery to the model; `actions` is the intent journal and idempotency ledger; `runs` records outcomes and skips. Run ownership is claimed atomically with a SQLite `BEGIN IMMEDIATE` transaction. A process-held advisory operation lock fences `begin`, the entire POST/verification sequence, and `end`, so a stale takeover cannot overlap an in-flight Canvas write. Marker updates use a separate advisory guard lock.

## Recovery

A lost acknowledgement leaves an `intended` row. The next `begin` reconciles it against the thread tree. A matching self-authored entry becomes `verified`; an unmatched intent older than one hour becomes `abandoned` and is never reposted automatically. Stale-run closure and breaker incrementation commit in the same SQLite transaction, so a crash cannot erase a recovered failure.

## Limits and stopping rules

At most three intended/posted/verified actions are allowed in any rolling hour and at most two in a run. Every write rechecks `state/STOP` and the exact RUNNING control line. Three consecutive error outcomes create STOP. A successful outcome resets the breaker.

## Tool restriction and blast radius

The deployment uses a dedicated Hermes profile. Its cron toolset disables web, browser, code execution, vision, memory, delegation, connections, computer use, and other unrelated tools. Terminal remains enabled only because the job must invoke `~/hw3-agent/bin/canvas_tool`; file access remains enabled so it can read `RUNTIME.md` and write drafts under `state/drafts`. The runtime prompt forbids every other command and path. The deterministic wrapper—not the prompt—enforces the Canvas origin, topic, allowed write endpoints, control line, rate limits, intent journal, verification, and STOP rule.
