from __future__ import annotations

import json
import os
import subprocess
import sys

from canvas_tool.cli import _sanitize


def test_cli_failure_is_single_json_and_logged(tmp_path):
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["CANVAS_TOOL_HOME"] = str(tmp_path / "agent")
    proc = subprocess.run(
        [sys.executable, "-m", "canvas_tool", "status"],
        text=True, capture_output=True, env=env, check=False,
    )
    payload = json.loads(proc.stdout)
    assert proc.stdout.count("\n") == 1
    assert payload == {"ok": False, "reason": "credential file unavailable"}
    log = tmp_path / "agent" / "logs" / "agent.jsonl"
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert records[-1]["command"] == "status"
    assert records[-1]["run_id"] is None


def test_module_help_is_json_error_free():
    proc = subprocess.run([sys.executable, "-m", "canvas_tool", "--help"], text=True, capture_output=True)
    assert proc.returncode == 0
    assert "canvas_tool" in proc.stdout


def test_cli_sanitizer_redacts_actual_nonpattern_token():
    payload = _sanitize({"entries": [{"text": "opaque-secret-value"}]}, "opaque-secret-value")
    assert payload["entries"][0]["text"] == "[REDACTED]"
