from __future__ import annotations

from datetime import timedelta

import pytest

from canvas_tool.faults import FaultController, InjectedCrash, LostAck


SAFE = "A durable intent record lets the next run reconcile an uncertain acknowledgement safely."


def make_draft(state):
    p = state / "drafts" / "x.txt"
    p.parent.mkdir(exist_ok=True)
    p.write_text(SAFE, encoding="utf-8")
    return p


def test_lost_ack_leaves_intent_then_begin_reconciles(gate):
    g, client, store, state = gate
    g.faults = FaultController("lost_ack")
    run = g.begin()["run_id"]
    result = g.post(run, new_thread=True, message_file=make_draft(state))
    assert not result["ok"] and result["reason"] == "lost_ack"
    assert len(client.post_calls) == 1 and store.actions()[0]["status"] == "intended"
    g.end(run, "error")
    g.faults = FaultController(None)
    g.begin()
    assert store.actions()[0]["status"] == "verified"
    assert len(client.post_calls) == 1


def test_crash_after_intent_never_posts_and_old_intent_abandons(gate):
    g, client, store, state = gate
    g.faults = FaultController("crash_after_intent")
    run = g.begin()["run_id"]
    with pytest.raises(InjectedCrash):
        g.post(run, new_thread=True, message_file=make_draft(state))
    assert client.post_calls == [] and store.actions()[0]["status"] == "intended"
    g.end(run, "error")
    g.faults = FaultController(None)
    old_now = g.now
    g.now = lambda: old_now() + timedelta(hours=2)
    g.begin()
    assert store.actions()[0]["status"] == "abandoned"


def test_stale_crashed_run_lock_is_recovered_without_manual_end(gate):
    g, client, store, state = gate
    g.faults = FaultController("crash_after_intent")
    run = g.begin()["run_id"]
    with pytest.raises(InjectedCrash):
        g.post(run, new_thread=True, message_file=make_draft(state))
    original_now = g.now
    g.now = lambda: original_now() + timedelta(hours=2)
    g.faults = FaultController(None)
    restarted = g.begin()
    assert restarted["ok"] is True
    assert store.actions()[0]["status"] == "abandoned"
    assert store.meta_get("consecutive_failed_runs") == "1"
    assert client.post_calls == []


def test_posted_action_is_reconciled_on_next_begin(gate):
    g, client, store, state = gate
    run = g.begin()["run_id"]
    message = SAFE
    key = "posted-key"
    store.insert_action(key, "new", None, "hash", message, run, g.now())
    entry_id = client.post_entry(11, 22, message)
    store.mark_posted(key, entry_id, g.now())
    g.end(run, "error")
    assert g.begin()["ok"] is True
    assert store.actions()[0]["status"] == "verified"


def test_timeout_get_fires_only_once():
    f = FaultController("timeout_get")
    with pytest.raises(TimeoutError): f.before_get()
    f.before_get()


def test_malformed_fault_is_one_shot():
    f = FaultController("malformed")
    with pytest.raises(ValueError): f.after_view([])
    assert f.after_view([]) == []
