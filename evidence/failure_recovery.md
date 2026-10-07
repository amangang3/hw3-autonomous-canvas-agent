# Failure-and-recovery evidence

Snapshot: 2026-10-07 10:13 EDT.

## Injected lost acknowledgement

The deterministic test `test_lost_ack_leaves_intent_then_begin_reconciles` injects `HW3_FAULT=lost_ack` after the fake Canvas POST has taken effect but before the acknowledgement is accepted locally.

Expected and observed sequence:

1. `canvas_tool` writes an `intended` action row before the POST.
2. The fake Canvas endpoint receives exactly one POST.
3. The injected lost acknowledgement makes the first run return `lost_ack`; the action remains unresolved.
4. The failed run ends without retrying the POST.
5. The next `begin` performs a fresh read, matches the self-authored entry by parent and normalized text, and changes the same action to `verified`.
6. The fake client's POST count remains exactly one, proving recovery did not duplicate the external effect.

Verification command and result:

```text
.venv/bin/pytest -q tests/test_faults_reconcile.py tests/test_memory.py::test_stale_claim_counts_recovered_failure_atomically
....... [100%]
```

Relevant source:

- `canvas_tool/faults.py`: one-shot `lost_ack`, `crash_after_intent`, `timeout_get`, and `malformed` injections.
- `tests/test_faults_reconcile.py`: lost-ack reconciliation, crash-after-intent abandonment, stale-run recovery, posted-action reconciliation, and one-shot read/view failures.
- `tests/test_memory.py`: atomic stale-run closure and breaker update.

## Injected crash after durable intent

`test_crash_after_intent_never_posts_and_old_intent_abandons` injects a crash after the durable intent is committed but before Canvas is called. It verifies that Canvas receives zero POSTs. After advancing time by two hours, the next run marks the unmatched intent `abandoned`; it is never automatically reposted.

`test_stale_crashed_run_lock_is_recovered_without_manual_end` leaves the crashed run open. The next `begin` atomically closes the stale run, increments the consecutive-failure counter, claims a new run, and still makes zero Canvas writes.

## Live delayed-read recovery

The scheduled 9:00 PM EDT run posted reply `230741`, but Canvas did not expose it in the immediate read-back window. The wrapper returned `verification_failed` and did not retry the POST. A later read found exactly one matching entry; the existing `posted` action was reconciled to `verified` without another write.

The same conservative behavior occurred for entry `231014`: the scheduled run recorded the single POST as unverified, accepted the refusal without retrying, and a later read reconciled that same entry to `verified`.

After observing this propagation delay, bounded read-back retries (0, 1, 2, 4, and 8 seconds) were added. The regression test `test_post_retries_verification_during_canvas_propagation` withholds the posted entry for two reads and verifies that the third read succeeds with only one POST.

## Complete verification

```text
.venv/bin/pytest -q
............................................... [100%]
.venv/bin/python scripts/secret_scan.py .
secret scan passed
```

No credential or raw private Canvas response is included in this evidence.
