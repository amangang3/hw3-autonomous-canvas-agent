from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .client import CanvasClient, CanvasError, load_token
from .faults import FaultController
from .gate import CanvasGate, GateError
from .memory import Store
from .safety import redact


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="canvas_tool")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("begin")
    new = sub.add_parser("new")
    new.add_argument("--run", required=True)
    thread = sub.add_parser("thread")
    thread.add_argument("--run", required=True)
    thread.add_argument("--entry", required=True, type=int)
    post = sub.add_parser("post")
    post.add_argument("--run", required=True)
    target = post.add_mutually_exclusive_group(required=True)
    target.add_argument("--reply-to", type=int)
    target.add_argument("--new-thread", action="store_true")
    post.add_argument("--message-file", required=True, type=Path)
    skip = sub.add_parser("skip")
    skip.add_argument("--run", required=True)
    skip.add_argument("--reason", required=True)
    end = sub.add_parser("end")
    end.add_argument("--run", required=True)
    end.add_argument("--outcome", required=True, choices=["ok", "error"])
    sub.add_parser("status")
    return p


def _sanitize(value: Any, token: str | None = None) -> Any:
    if isinstance(value, str):
        return redact(value, token)
    if isinstance(value, dict):
        return {str(k): _sanitize(v, token) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v, token) for v in value]
    return value


def _log(root: Path, command: str, run_id: str | None, result: dict[str, Any], token: str | None = None) -> None:
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(logs, 0o700)
    except OSError:
        pass
    record = _sanitize({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "command": command,
        "result": result,
    }, token)
    path = logs / "agent.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")


def execute(args: argparse.Namespace, root: Path, token: str) -> dict[str, Any]:
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    faults = FaultController()
    client = CanvasClient(token, faults=faults)
    gate = CanvasGate(client, Store(state / "agent.db"), state, faults=faults)
    if args.command == "begin":
        return gate.begin()
    if args.command == "new":
        return gate.new(args.run)
    if args.command == "thread":
        return gate.thread(args.run, args.entry)
    if args.command == "post":
        return gate.post(args.run, reply_to=args.reply_to, new_thread=args.new_thread, message_file=args.message_file)
    if args.command == "skip":
        return gate.skip(args.run, args.reason)
    if args.command == "end":
        return gate.end(args.run, args.outcome)
    if args.command == "status":
        return gate.status()
    raise GateError("unknown_command")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = Path(os.environ.get("CANVAS_TOOL_HOME", str(Path.home() / "hw3-agent"))).expanduser().resolve()
    run_id = getattr(args, "run", None)
    token = None
    try:
        token = load_token()
        result = execute(args, root, token)
    except (CanvasError, GateError) as exc:
        result = {"ok": False, "reason": redact(str(exc), token)}
    except Exception:
        result = {"ok": False, "reason": "internal_error"}
    result = _sanitize(result, token)
    try:
        _log(root, args.command, run_id or result.get("run_id"), result, token)
    except OSError:
        if result.get("ok"):
            result = {"ok": False, "reason": "audit_log_failed"}
    sys.stdout.write(json.dumps(result, sort_keys=True, ensure_ascii=False) + "\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
