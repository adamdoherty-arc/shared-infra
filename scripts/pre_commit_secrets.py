#!/usr/bin/env python3
"""Pre-commit secrets gate for shared-infra. Blocks a commit that stages a
Bifrost virtual key, a provider API key, or a SQLite database.

This gate would pass trivially if it only scanned file NAMES; it scans the
staged CONTENT (git show :path) of every added/modified text file.
Run standalone: python scripts/pre_commit_secrets.py [--all]
"""
from __future__ import annotations

import re
import subprocess
import sys

PATTERNS = [
    (re.compile(r"sk-bf-[0-9a-f]{8}-[0-9a-f-]{20,}"), "Bifrost virtual key"),
    (re.compile(r"\bnvapi-[A-Za-z0-9_-]{20,}"), "NVIDIA NIM key"),
    (re.compile(r"\bgsk_[A-Za-z0-9]{20,}"), "Groq key"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "Google API key"),
    (re.compile(r"\bsk-or-v1-[0-9a-f]{20,}"), "OpenRouter key"),
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}"), "HuggingFace token"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "Anthropic key"),
    (re.compile(r"https://discord(app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]{20,}"), "Discord webhook URL"),
]
BLOCKED_SUFFIXES = (".db", ".db-wal", ".db-shm", ".sqlite")
ALLOW_FILES = {".env.example"}


def staged_files(all_files: bool) -> list[str]:
    cmd = ["git", "ls-files"] if all_files else ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"]
    return [l for l in subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines() if l]


def content(path: str, all_files: bool) -> str:
    if all_files:
        try:
            return open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            return ""
    r = subprocess.run(["git", "show", f":{path}"], capture_output=True)
    return r.stdout.decode("utf-8", errors="ignore")


def main() -> int:
    all_files = "--all" in sys.argv
    hits = 0
    for path in staged_files(all_files):
        if path.endswith(BLOCKED_SUFFIXES):
            print(f"BLOCKED {path}: SQLite database must not be committed")
            hits += 1
            continue
        if path in ALLOW_FILES:
            continue
        text = content(path, all_files)
        for rx, label in PATTERNS:
            for m in rx.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                print(f"BLOCKED {path}:{line}: {label} ({m.group(0)[:10]}...)")
                hits += 1
    if hits:
        print(f"{hits} secret finding(s); commit refused. Move the value to .env and reference it by name.")
        return 1
    print("secrets scan: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
