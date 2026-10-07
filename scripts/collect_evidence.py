#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence" / "scheduled_runs.md"
TOKEN = re.compile(r"\d+~" + r"[A-Za-z0-9]" + r"{20,}")


def main() -> int:
    scan = subprocess.run([sys.executable, str(ROOT / "scripts" / "secret_scan.py"), str(ROOT)])
    if scan.returncode:
        return scan.returncode
    lines = ["# Scheduled-run evidence", "", "Generated from local redacted logs and state.", ""]
    log_path = ROOT / "logs" / "agent.jsonl"
    records = []
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                records.append(json.loads(TOKEN.sub("[REDACTED]", line)))
            except json.JSONDecodeError:
                continue
    lines += ["## Recent runs", ""]
    if records:
        for record in records[-100:]:
            result = record.get("result", {})
            reason = result.get("reason") or result.get("control") or "ok"
            lines.append(f"- {record.get('timestamp')} run={record.get('run_id')} command={record.get('command')} result={reason}")
    else:
        lines.append("No run logs available yet.")
    lines += ["", "## Verified thread URLs", ""]
    db_path = ROOT / "state" / "agent.db"
    urls = []
    if db_path.exists():
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        meta = dict(con.execute("SELECT key,value FROM meta WHERE key IN ('course_id','topic_id')"))
        if "course_id" in meta and "topic_id" in meta:
            for (entry_id,) in con.execute("SELECT canvas_entry_id FROM actions WHERE status='verified' AND canvas_entry_id IS NOT NULL"):
                urls.append(f"https://canvas.mit.edu/courses/{meta['course_id']}/discussion_topics/{meta['topic_id']}#entry-{entry_id}")
        con.close()
    lines.extend(f"- {url}" for url in urls)
    if not urls:
        lines.append("No verified entries available yet.")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
