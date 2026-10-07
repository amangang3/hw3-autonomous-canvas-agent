from __future__ import annotations

import hashlib
import json
import fcntl
import os
import secrets
import stat
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .faults import FaultController, InjectedCrash, LostAck
from .memory import Store, iso
from .safety import filter_message, html_message, normalize_text, strip_html

RUNNING = "COURSE-TEAM CONTROL: RUNNING"


class GateError(RuntimeError):
    pass


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


class CanvasGate:
    def __init__(self, client, store: Store, state_dir: Path, *,
                 now: Callable[[], datetime] | None = None,
                 wait: Callable[[float], None] | None = None,
                 faults: FaultController | None = None) -> None:
        self.client = client
        self.store = store
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_path = self.state_dir / "lock"
        self.stop_path = self.state_dir / "STOP"
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.wait = wait or time.sleep
        self.faults = faults or FaultController()

    @contextmanager
    def _operation_lock(self, *, blocking: bool = False):
        """Fence begin/post/end across processes, including stale recovery."""
        fd = os.open(self.state_dir / ".operation.guard", os.O_RDWR | os.O_CREAT, 0o600)
        acquired = False
        try:
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(fd, flags)
                acquired = True
            except BlockingIOError:
                pass
            yield acquired
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _create_stop(self) -> None:
        fd = os.open(self.stop_path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)

    def _finish_locked(self, run_id: str, outcome: str, error: str | None = None) -> tuple[bool, int]:
        ended, failures = self.store.finish_run(run_id, outcome, self.now(), error)
        if ended and failures >= 3:
            self._create_stop()
        if ended:
            self._release(run_id)
        return ended, failures

    def _cached_ids(self) -> tuple[int, int, int] | None:
        values = [self.store.meta_get(k) for k in ("course_id", "topic_id", "self_user_id")]
        if any(v is None for v in values):
            return None
        return tuple(int(v) for v in values)  # type: ignore[return-value]

    def _discover(self) -> tuple[int, int, int]:
        cached = self._cached_ids()
        if cached:
            return cached
        data = self.client.discover()
        for key in ("course_id", "topic_id", "self_user_id"):
            if key not in data:
                raise GateError(f"discovery missing {key}")
            self.store.meta_set(key, int(data[key]))
        return int(data["course_id"]), int(data["topic_id"]), int(data["self_user_id"])

    def _write_lock_marker(self, run_id: str) -> None:
        guard_fd = os.open(self.state_dir / ".lock.guard", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_EX)
            temporary = self.state_dir / f".lock.{run_id}.tmp"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(run_id)
            os.replace(temporary, self.lock_path)
        finally:
            fcntl.flock(guard_fd, fcntl.LOCK_UN)
            os.close(guard_fd)

    def _release(self, run_id: str) -> None:
        guard_fd = os.open(self.state_dir / ".lock.guard", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_EX)
            try:
                if self.lock_path.read_text(encoding="utf-8") == run_id:
                    self.lock_path.unlink()
            except FileNotFoundError:
                pass
        finally:
            fcntl.flock(guard_fd, fcntl.LOCK_UN)
            os.close(guard_fd)

    @staticmethod
    def _clean_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        required = {"id", "parent_id", "user_id", "created_at", "text"}
        clean: list[dict[str, Any]] = []
        seen: set[int] = set()
        for raw in entries:
            if not isinstance(raw, dict) or not required.issubset(raw):
                raise GateError("malformed entry")
            if type(raw["id"]) is not int:
                raise GateError("malformed entry id")
            entry_id = raw["id"]
            if raw.get("deleted"):
                continue
            if type(raw["user_id"]) is not int:
                raise GateError("malformed entry user")
            if raw["parent_id"] is not None and type(raw["parent_id"]) is not int:
                raise GateError("malformed entry parent")
            if not isinstance(raw["text"], str) or _parse_time(raw["created_at"]) is None:
                raise GateError("malformed entry content")
            if entry_id in seen:
                continue
            seen.add(entry_id)
            clean.append({**raw, "id": entry_id, "text": strip_html(raw["text"]) if "<" in str(raw["text"]) else normalize_text(str(raw["text"]))})
        return clean

    def _view(self, course: int, topic: int) -> list[dict[str, Any]]:
        entries = self.client.get_entries(course, topic)
        # Real client applies view fault itself; fakes used by tests do not.
        if getattr(self.client, "faults", None) is None:
            entries = self.faults.after_view(entries)
        if not isinstance(entries, list):
            raise GateError("malformed view")
        return self._clean_entries(entries)

    def _reconcile(self, course: int, topic: int, self_id: int) -> None:
        pending = self.store.unresolved_actions()
        if not pending:
            return
        entries = self._view(course, topic)
        now = self.now()
        for action in pending:
            intended = _parse_time(action["intended_at"])
            match = None
            for entry in entries:
                created = _parse_time(entry.get("created_at"))
                same_parent = entry.get("parent_id") == action["target"]
                if action["kind"] == "new":
                    same_parent = entry.get("parent_id") is None
                id_matches = action["status"] != "posted" or action["canvas_entry_id"] is None or entry["id"] == action["canvas_entry_id"]
                time_matches = (
                    created is not None
                    and intended is not None
                    and created.replace(microsecond=0) >= intended.replace(microsecond=0)
                )
                if (
                    entry.get("user_id") == self_id
                    and same_parent
                    and id_matches
                    and time_matches
                    and normalize_text(entry["text"]) == normalize_text(action["message"])
                ):
                    match = entry
                    break
            if match:
                self.store.mark_verified(action["idem_key"], match["id"], now)
            elif action["status"] == "intended" and intended is not None and (now - intended).total_seconds() > 3600:
                self.store.mark_abandoned(action["idem_key"], now)

    def begin(self) -> dict[str, Any]:
        with self._operation_lock() as acquired:
            if not acquired:
                return {"ok": False, "reason": "locked"}
            return self._begin_locked()

    def _begin_locked(self) -> dict[str, Any]:
        if self.stop_path.exists():
            return {"ok": False, "reason": "stopped"}
        if int(self.store.meta_get("consecutive_failed_runs") or 0) >= 3:
            self._create_stop()
            return {"ok": False, "reason": "stopped"}
        run_id = secrets.token_hex(12)
        claimed, recovered, failures = self.store.claim_run(run_id, self.now())
        if not claimed:
            return {"ok": False, "reason": "locked"}
        if recovered:
            if failures >= 3:
                self._create_stop()
        try:
            self._write_lock_marker(run_id)
            if self.stop_path.exists():
                self._finish_locked(run_id, "error", "circuit breaker stopped run")
                return {"ok": False, "reason": "stopped"}
            course, topic, self_id = self._discover()
            self._reconcile(course, topic, self_id)
            control = self.client.get_control_line(course, topic)
            return {
                "ok": True,
                "run_id": run_id,
                "control": control,
                "remaining_hourly_budget": max(0, 3 - self.store.count_hour(self.now())),
            }
        except Exception:
            self._finish_locked(run_id, "error", "begin failed")
            raise

    def _require_run(self, run_id: str) -> tuple[int, int, int]:
        if not self.store.active_run(run_id):
            raise GateError("inactive_run")
        ids = self._cached_ids()
        if not ids:
            raise GateError("target_not_cached")
        return ids

    def new(self, run_id: str) -> dict[str, Any]:
        course, topic, self_id = self._require_run(run_id)
        entries = self._view(course, topic)
        seen = self.store.seen_ids()
        unseen = [e for e in entries if e.get("user_id") != self_id and e["id"] not in seen]
        replied = self.store.reply_targets()
        candidates = sorted(
            (e for e in entries if e.get("user_id") != self_id and e["id"] not in replied),
            key=lambda e: _parse_time(e["created_at"]),
            reverse=True,
        )[:30]

        def payload(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [{
                "id": e["id"], "parent_id": e.get("parent_id"),
                "author_name": e.get("author_name", "Unknown"), "created_at": e.get("created_at"),
                "text": e["text"][:1500], "untrusted": True,
            } for e in items]

        self.store.mark_seen(unseen, run_id)
        return {
            "ok": True,
            "entries": payload(unseen),
            "candidates": payload(candidates),
            "own_recent": self.store.own_actions(10),
        }

    def thread(self, run_id: str, entry_id: int) -> dict[str, Any]:
        course, topic, _ = self._require_run(run_id)
        entries = self._view(course, topic)
        by_id = {e["id"]: e for e in entries}
        if entry_id not in by_id:
            return {"ok": False, "reason": "target_missing"}
        root = entry_id
        while by_id[root].get("parent_id") in by_id:
            root = by_id[root]["parent_id"]
        members = []
        frontier = [root]
        while frontier:
            current = frontier.pop(0)
            item = by_id[current]
            members.append({**item, "untrusted": True})
            frontier.extend(e["id"] for e in entries if e.get("parent_id") == current)
        return {"ok": True, "entries": members}

    def _read_draft(self, path: Path) -> str:
        drafts = self.state_dir / "drafts"
        try:
            if path.parent.resolve() != drafts.resolve() or not path.name:
                raise GateError("unsafe_message_file")
            state_fd = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                drafts_fd = os.open("drafts", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=state_fd)
            finally:
                os.close(state_fd)
            try:
                file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=drafts_fd)
            finally:
                os.close(drafts_fd)
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode):
                os.close(file_fd)
                raise GateError("unsafe_message_file")
            if info.st_size > 8192:
                os.close(file_fd)
                raise GateError("message_file_too_large")
            with os.fdopen(file_fd, "r", encoding="utf-8") as handle:
                return handle.read()
        except UnicodeError as exc:
            raise GateError("message_not_utf8") from exc
        except OSError as exc:
            raise GateError("unsafe_message_file") from exc

    def post(self, run_id: str, *, reply_to: int | None = None, new_thread: bool = False,
             message_file: Path) -> dict[str, Any]:
        with self._operation_lock() as acquired:
            if not acquired:
                return {"ok": False, "reason": "locked"}
            return self._post_locked(run_id, reply_to=reply_to, new_thread=new_thread, message_file=message_file)

    def _post_locked(self, run_id: str, *, reply_to: int | None = None, new_thread: bool = False,
                     message_file: Path) -> dict[str, Any]:
        try:
            course, topic, self_id = self._require_run(run_id)
            if (reply_to is None) == (not new_thread):
                return {"ok": False, "reason": "invalid_target_mode"}
            entries = self._view(course, topic)
            target = None
            kind = "new"
            if reply_to is not None:
                target = next((e for e in entries if e["id"] == int(reply_to)), None)
                if not target:
                    return {"ok": False, "reason": "target_missing"}
                if target.get("user_id") == self_id:
                    return {"ok": False, "reason": "target_is_self"}
                if any(e.get("parent_id") == int(reply_to) and e.get("user_id") == self_id for e in entries):
                    return {"ok": False, "reason": "already_replied"}
                kind = "reply"
            message = normalize_text(self._read_draft(Path(message_file)))
            allowed, why = filter_message(message)
            if not allowed:
                return {"ok": False, "reason": f"content:{why}"}
            target_key = "" if reply_to is None else str(int(reply_to))
            digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
            idem_key = hashlib.sha256(f"{kind}|{target_key}|{message}".encode("utf-8")).hexdigest()
            if self.store.action_exists(idem_key):
                return {"ok": False, "reason": "duplicate"}
            if self.store.count_run(run_id) >= 2:
                return {"ok": False, "reason": "run_limit"}
            if self.store.count_hour(self.now()) >= 3:
                return {"ok": False, "reason": "hourly_limit"}
            if self.stop_path.exists():
                return {"ok": False, "reason": "stopped"}
            if self.client.get_control_line(course, topic) != RUNNING:
                return {"ok": False, "reason": "control_not_running"}
            if not self.store.insert_action(idem_key, kind, reply_to, digest, message, run_id, self.now()):
                return {"ok": False, "reason": "duplicate"}
            self.faults.after_intent()
            entry_id = self.client.post_entry(course, topic, html_message(message), reply_to)
            self.faults.after_post()
            self.store.mark_posted(idem_key, entry_id, self.now())
            expected_parent = int(reply_to) if reply_to is not None else None
            found = None
            for delay in (0, 1, 2, 4, 8):
                if delay:
                    self.wait(delay)
                fresh = self._view(course, topic)
                candidate = next((e for e in fresh if e["id"] == entry_id), None)
                if candidate and candidate.get("user_id") == self_id \
                        and candidate.get("parent_id") == expected_parent \
                        and normalize_text(candidate["text"]) == message:
                    found = candidate
                    break
            if not found:
                return {"ok": False, "reason": "verification_failed", "entry_id": entry_id}
            self.store.mark_verified(idem_key, entry_id, self.now())
            return {
                "ok": True, "entry_id": entry_id, "verified": True,
                "url": f"https://canvas.mit.edu/courses/{course}/discussion_topics/{topic}#entry-{entry_id}",
            }
        except LostAck:
            return {"ok": False, "reason": "lost_ack", "uncertain": True}
        except InjectedCrash:
            raise
        except GateError as exc:
            return {"ok": False, "reason": str(exc)}
        except Exception:
            return {"ok": False, "reason": "post_failed", "uncertain": True}

    def skip(self, run_id: str, reason: str) -> dict[str, Any]:
        self._require_run(run_id)
        self.store.record_skip(run_id, normalize_text(reason)[:500])
        return {"ok": True}

    def end(self, run_id: str, outcome: str, error: str | None = None) -> dict[str, Any]:
        if outcome not in {"ok", "error"}:
            return {"ok": False, "reason": "invalid_outcome"}
        with self._operation_lock() as acquired:
            if not acquired:
                return {"ok": False, "reason": "locked"}
            ended, failures = self._finish_locked(run_id, outcome, error)
            if not ended:
                return {"ok": False, "reason": "inactive_run"}
            return {"ok": True, "breaker": failures, "stopped": self.stop_path.exists()}

    def status(self) -> dict[str, Any]:
        course, topic, _ = self._discover()
        control = self.client.get_control_line(course, topic)
        return {
            "ok": True,
            "control": control,
            "remaining_hourly_budget": max(0, 3 - self.store.count_hour(self.now())),
            "breaker": int(self.store.meta_get("consecutive_failed_runs") or 0),
            "stopped": self.stop_path.exists(),
            "pending_intents": len(self.store.actions("intended")),
        }
