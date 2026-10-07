#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

PATTERN = re.compile(r"\d+~" + r"[A-Za-z0-9]" + r"{20,}")
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "state", "logs"}
FORBIDDEN_ENV_NAMES = {"env", ".env"}


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    findings: list[str] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        base = Path(current)
        for name in files:
            path = base / name
            relative = path.relative_to(root)
            if name in FORBIDDEN_ENV_NAMES:
                findings.append(f"forbidden env file: {relative}")
                continue
            try:
                if PATTERN.search(path.read_text(encoding="utf-8", errors="ignore")):
                    findings.append(f"token-shaped value: {relative}")
            except OSError:
                findings.append(f"unreadable during scan: {relative}")
    if findings:
        print("secret scan failed")
        for finding in findings:
            print(f"- {finding}")
        return 1
    print("secret scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
