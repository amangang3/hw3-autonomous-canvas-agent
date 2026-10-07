from __future__ import annotations

import subprocess
import sys


def test_secret_scan_passes_clean_tree_and_detects_seed(tmp_path):
    clean = subprocess.run([sys.executable, "scripts/secret_scan.py", "."], text=True, capture_output=True)
    assert clean.returncode == 0, clean.stdout + clean.stderr
    seeded = tmp_path / "seed.txt"
    seeded.write_text("123~" + "A" * 24, encoding="utf-8")
    bad = subprocess.run([sys.executable, "scripts/secret_scan.py", str(tmp_path)], text=True, capture_output=True)
    assert bad.returncode == 1
    assert "seed.txt" in bad.stdout
    assert "123~" + "A" * 24 not in bad.stdout
