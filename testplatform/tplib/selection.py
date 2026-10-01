from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

MAX_NODE_IDS = 200
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_TESTMON_QUERY = r"""
import json, os, sqlite3, sys
files = json.loads(sys.stdin.read())
con = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
cols = lambda t: {r[1] for r in con.execute("pragma table_info(%s)" % t)}
need = {"test_execution": {"id", "test_name"}, "file_fp": {"id", "filename"},
        "test_execution_file_fp": {"test_execution_id", "fingerprint_id"}}
if any(not need[t] <= cols(t) for t in need):
    os.write(1, json.dumps({"schema": False}).encode())
    raise SystemExit(0)
marks = ",".join("?" * len(files))
rows = con.execute(
    "select distinct f.filename, te.test_name from test_execution te "
    "join test_execution_file_fp l on l.test_execution_id = te.id "
    "join file_fp f on f.id = l.fingerprint_id where f.filename in (%s)" % marks, files).fetchall()
os.write(1, json.dumps({"schema": True, "pairs": sorted([r[0], r[1]] for r in rows)}).encode())
"""


def _exec(cmd: list[str], stdin: str | None = None, timeout: float = 120) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout,
                              creationflags=NO_WINDOW)
        return proc.returncode, proc.stdout
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def testmon_data_exists(runtime: dict[str, Any], data_file: str) -> bool:
    if runtime.get("kind") != "exec":
        return False
    rc, _ = _exec(["docker", "exec", runtime["container"], "test", "-f", data_file], timeout=30)
    return rc == 0


def testmon_pairs(runtime: dict[str, Any], data_file: str, files: list[str]) -> list[list[str]] | None:
    """(source file, node id) pairs recorded by testmon; None when the database cannot answer."""
    if not files or not testmon_data_exists(runtime, data_file):
        return None
    rc, out = _exec(["docker", "exec", "-i", runtime["container"], "python", "-c", _TESTMON_QUERY, data_file],
                    stdin=json.dumps(files), timeout=120)
    if rc != 0:
        return None
    try:
        data = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return list(data["pairs"]) if data.get("schema") else None


def _dotted(rel: str) -> str:
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _is_test(rel: str, test_globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, g.replace("/**/", "/")) for g in test_globs)


def all_test_files(root: Path, test_globs: list[str]) -> list[str]:
    dirs = sorted({g.replace("\\", "/").split("*", 1)[0].rstrip("/") or "." for g in test_globs})
    found: list[str] = []
    for d in dirs:
        base = root / d
        if base.exists():
            found += [p.relative_to(root).as_posix() for p in base.rglob("test_*.py")]
    return sorted(set(found))


def tests_from_imports(root: Path, files: list[str], test_globs: list[str]) -> list[str]:
    """Import-graph fallback: test files importing a changed module, plus name-matched test files."""
    tests = all_test_files(root, test_globs)
    patterns: list[re.Pattern[str]] = []
    chosen: set[str] = set()
    for f in files:
        rel = f.replace("\\", "/").removeprefix("./")
        if not rel.endswith(".py"):
            continue
        stem = Path(rel).stem
        if stem != "__init__" and len(stem) >= 3:
            for t in tests:
                ts = Path(t).stem
                if ts == f"test_{stem}" or ts.startswith(f"test_{stem}_"):
                    chosen.add(t)
        dotted = _dotted(rel)
        if not dotted:
            continue
        head, _, tail = dotted.rpartition(".")
        pattern = rf"(?:^|[^\w.]){re.escape(dotted)}(?!\w)"
        if head:
            pattern += rf"|from\s+{re.escape(head)}\s+import\s+[^\n]*\b{re.escape(tail)}\b"
        patterns.append(re.compile(pattern, re.M))
    if patterns:
        for t in tests:
            try:
                text = (root / t).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if any(p.search(text) for p in patterns):
                chosen.add(t)
    return sorted(chosen)


def select_for_paths(root: Path, runtime: dict[str, Any], data_file: str, files: list[str],
                     test_globs: list[str]) -> tuple[list[str], dict[str, Any]]:
    """Map caller-supplied paths to tests: testmon dependency data first, import graph otherwise."""
    norm = [f.replace("\\", "/").removeprefix("./") for f in files]
    direct = [f for f in norm if f.endswith(".py") and _is_test(f, test_globs) and (root / f).exists()]
    sources = [f for f in norm if f not in direct]
    selection: dict[str, Any] = {"mode": "paths", "paths": norm}
    pairs = testmon_pairs(runtime, data_file, sources)
    chosen = set(direct)
    if pairs is None:
        selection["source"] = "import-graph"
        chosen |= set(tests_from_imports(root, sources, test_globs))
    else:
        selection["source"] = "testmon"
        chosen |= {test for _, test in pairs}
        known = {f for f, _ in pairs}
        unknown = [f for f in sources if f.endswith(".py") and f not in known]
        if unknown:
            selection["source"] = "testmon+import-graph"
            selection["import_graph_files"] = unknown
            chosen |= set(tests_from_imports(root, unknown, test_globs))
    chosen = sorted(chosen)
    files_only = sorted({t.split("::", 1)[0] for t in chosen})
    ids = chosen if len(chosen) <= MAX_NODE_IDS else files_only
    selection["tests"] = len(ids)
    selection["files"] = len(files_only)
    return ids, selection


def preflight_changed(runtime: dict[str, Any], tier: dict[str, Any], has_paths: bool) -> str | None:
    """A one-line refusal when a `changed` run has neither paths nor testmon data."""
    if tier.get("mode") != "changed" or has_paths:
        return None
    data_file = tier.get("testmon_datafile", "/tmp/testplatform/.testmondata")
    if testmon_data_exists(runtime, data_file):
        return None
    if runtime.get("kind") != "exec":
        return ("plain `changed` needs testmon data, which only an exec runtime records (this profile runs a throwaway "
                "container): pass --paths <files you edited>")
    return ("no testmon data: pass --paths <files you edited> or build the data once with "
            "`testctl run <project>:testmon` (the nightly full tier keeps it fresh)")
