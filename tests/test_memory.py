from datetime import datetime, timedelta, timezone

from canvas_tool.memory import Store


def test_store_uses_wal_and_unique_idempotency(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    assert s.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    now = datetime.now(timezone.utc)
    s.create_run("r", now)
    assert s.insert_action("key", "new", None, "hash", "message", "r", now)
    assert not s.insert_action("key", "new", None, "hash", "message", "r", now)


def test_finish_run_updates_breaker_atomically(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    now = datetime.now(timezone.utc)
    s.create_run("r", now)
    ended, failures = s.finish_run("r", "error", now)
    assert ended and failures == 1
    assert s.meta_get("consecutive_failed_runs") == "1"


def test_stale_claim_counts_recovered_failure_atomically(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    now = datetime.now(timezone.utc)
    s.create_run("stale", now - timedelta(hours=2))
    claimed, recovered, failures = s.claim_run("next", now)
    assert claimed and recovered == 1 and failures == 1
    assert s.meta_get("consecutive_failed_runs") == "1"
