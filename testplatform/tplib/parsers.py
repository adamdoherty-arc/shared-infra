from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .bodyhash import body_hash
from .signature import signature

MAX_MESSAGE = 500
TRACE_LINES = 40

_TYPE_RE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Failed|Timeout|Exit|Interrupt|Warning|Exhausted))\b")


def _trace_tail(text: str) -> str:
    lines = (text or "").splitlines()
    return "\n".join(lines[-TRACE_LINES:])


def _failure_type(message: str, longrepr: str) -> str:
    first = (message or "").strip().splitlines()[0] if (message or "").strip() else ""
    match = _TYPE_RE.match(first)
    if match:
        return match.group(1)
    for line in reversed((longrepr or "").splitlines()):
        if line.startswith("E "):
            m2 = _TYPE_RE.match(line[1:].strip())
            if m2:
                return m2.group(1)
    if first.startswith("assert"):
        return "AssertionError"
    if "Timeout" in (longrepr or "")[:400]:
        return "Timeout"
    return "Failure"


def make_failure(ftype: str, message: str, tail: str) -> dict[str, Any]:
    message = (message or "").replace(chr(0), "\\x00").strip()
    tail = (tail or "").replace(chr(0), "\\x00")
    if ftype and message.startswith(ftype + ":"):
        message = message[len(ftype) + 1:].strip()
    return {
        "type": ftype,
        "message": message[:MAX_MESSAGE],
        "signature": signature(ftype, message),
        "trace_tail": _trace_tail(tail),
    }


def _phase_failure(test: dict[str, Any]) -> tuple[str, str, str]:
    for phase in ("setup", "call", "teardown"):
        part = test.get(phase) or {}
        if part.get("outcome") in ("failed", "error"):
            crash = part.get("crash") or {}
            message = crash.get("message") or part.get("longrepr") or ""
            longrepr = part.get("longrepr") or ""
            if isinstance(longrepr, dict):
                longrepr = str(longrepr)
            return message, longrepr, phase
    return "", "", "call"


_REQ_KEY_RE = re.compile(r"\[((?:FR|SC)-\d+(?:\s*,\s*(?:FR|SC)-\d+)*)\]")
_SLUG_TAG_RE = re.compile(r"\[(product_feature:[A-Za-z0-9_.-]+)\]")
_FEATURE_HEADER_RE = re.compile(r"^\s*(?://|#)\s*feature:\s*([A-Za-z0-9_.:-]+)", re.MULTILINE)
_HEADER_SCAN_BYTES = 2048


def normalise_feature_slug(raw: Any) -> str | None:
    """`bitcoin-lab` -> `product_feature:bitcoin-lab`; a full slug passes; anything else is None."""
    if not isinstance(raw, str):
        return None
    slug = raw.strip()
    if not slug:
        return None
    return slug if ":" in slug else f"product_feature:{slug}"


def normalise_requirement_ids(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    seen: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip() and item.strip() not in seen:
            seen.append(item.strip())
    return seen


def requirement_ids_from_title(title: str) -> list[str]:
    """`[FR-012]` / `[FR-012, SC-001]` tags in a test title."""
    keys: list[str] = []
    for group in _REQ_KEY_RE.findall(title or ""):
        for key in re.split(r"\s*,\s*", group):
            if key not in keys:
                keys.append(key)
    return keys


def feature_slug_from_title(title: str) -> str | None:
    m = _SLUG_TAG_RE.search(title or "")
    return m.group(1) if m else None


def feature_slug_from_file_header(repo_root: Path, rel: str) -> str | None:
    """First `// feature: <slug>` (or `# feature:`) line in the file head; unreadable -> None."""
    try:
        with open(repo_root / rel, "rb") as fh:
            head = fh.read(_HEADER_SCAN_BYTES).decode("utf-8", errors="ignore")
    except OSError:
        return None
    m = _FEATURE_HEADER_RE.search(head)
    return normalise_feature_slug(m.group(1)) if m else None


def pytest_trace(test: dict[str, Any]) -> tuple[str | None, list[str]]:
    """(feature_slug, requirement_ids) from a pytest-json-report entry's `metadata` (set by the ADA `req` marker)."""
    meta = test.get("metadata")
    if not isinstance(meta, dict):
        return None, []
    return normalise_feature_slug(meta.get("feature_slug")), normalise_requirement_ids(meta.get("requirement_ids"))


def parse_pytest_json(report: dict[str, Any], repo_root: Path, reruns: int = 0,
                      path_prefix: str = "") -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for test in report.get("tests", []):
        node_id = test["nodeid"]
        trace_slug, trace_reqs = pytest_trace(test)
        if path_prefix and not node_id.startswith(path_prefix):
            node_id = path_prefix + node_id
        outcome = test.get("outcome")
        duration_ms = int(round(sum(float((test.get(p) or {}).get("duration") or 0)
                                    for p in ("setup", "call", "teardown")) * 1000))
        attempts = 1
        failure = None
        if outcome == "passed":
            status = "passed"
        elif outcome == "rerun":
            status, attempts = "rerun_passed", 2
        elif outcome == "skipped":
            status = "skipped"
        elif outcome == "xfailed":
            status = "xfail"
        elif outcome == "xpassed":
            status = "xpass"
        else:
            message, longrepr, phase = _phase_failure(test)
            status = "error" if phase != "call" or outcome == "error" else "failed"
            ftype = _failure_type(message, longrepr)
            failure = make_failure(ftype, message, longrepr or message)
            attempts = 1 + reruns if reruns else 1
        case = {
            "node_id": node_id,
            "file": node_id.split("::", 1)[0],
            "status": status,
            "duration_ms": duration_ms,
            "attempts": attempts,
            "body_hash": body_hash(repo_root, node_id),
            "feature_slug": trace_slug,
            "requirement_ids": trace_reqs,
        }
        if failure:
            case["failure"] = failure
        cases.append(case)
    for collector in report.get("collectors", []):
        if collector.get("outcome") == "failed":
            node_id = collector.get("nodeid") or "<collection>"
            longrepr = collector.get("longrepr") or ""
            if not isinstance(longrepr, str):
                longrepr = str(longrepr)
            ftype = _failure_type("", longrepr)
            if ftype == "Failure":
                ftype = "CollectionError"
            last = [ln for ln in longrepr.splitlines() if ln.startswith("E ")]
            message = last[-1][1:].strip() if last else longrepr[:MAX_MESSAGE]
            cases.append({
                "node_id": node_id, "file": node_id.split("::", 1)[0], "status": "error",
                "duration_ms": 0, "attempts": 1,
                "body_hash": body_hash(repo_root, node_id),
                "feature_slug": None, "requirement_ids": [],
                "failure": make_failure(ftype, message, longrepr),
            })
    return cases


def _file_hash(repo_root: Path, rel: str) -> str:
    try:
        return hashlib.sha1((repo_root / rel).read_bytes()).hexdigest()
    except OSError:
        return hashlib.sha1(rel.encode()).hexdigest()


def parse_vitest_json(report: dict[str, Any], repo_root: Path, container_root: str = "/app") -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    prefix = container_root.rstrip("/") + "/"
    for suite in report.get("testResults", []):
        name = str(suite.get("name", "")).replace("\\", "/")
        rel = name[len(prefix):] if name.startswith(prefix) else name
        rel = f"frontend/{rel}" if (repo_root / "frontend" / rel).exists() else rel
        suite_failed = suite.get("status") == "failed" and not suite.get("assertionResults")
        if suite_failed:
            msg = str(suite.get("message", ""))
            cases.append({
                "node_id": rel, "file": rel, "status": "error", "duration_ms": 0, "attempts": 1,
                "body_hash": _file_hash(repo_root, rel), "feature_slug": None, "requirement_ids": [],
                "failure": make_failure("SuiteError", msg.splitlines()[0] if msg else "suite failed", msg),
            })
            continue
        for assertion in suite.get("assertionResults", []):
            status_raw = assertion.get("status")
            status = {"passed": "passed", "failed": "failed"}.get(status_raw, "skipped")
            full_name = str(assertion.get('fullName') or assertion.get('title') or "")
            node_id = f"{rel}::{full_name}"
            reqs = requirement_ids_from_title(full_name)
            slug = feature_slug_from_title(full_name) or (feature_slug_from_file_header(repo_root, rel) if reqs else None)
            case = {
                "node_id": node_id, "file": rel, "status": status,
                "duration_ms": int(assertion.get("duration") or 0), "attempts": 1,
                "body_hash": _file_hash(repo_root, rel), "feature_slug": slug, "requirement_ids": reqs,
            }
            if status == "failed":
                messages = assertion.get("failureMessages") or [""]
                first = str(messages[0])
                head = first.splitlines()[0] if first else ""
                m = _TYPE_RE.match(head)
                case["failure"] = make_failure(m.group(1) if m else "AssertionError", head, "\n".join(messages))
            cases.append(case)
    return cases


FUZZ_REPRO_KINDS = 3
FUZZ_BODY_EXCERPT = 300


def _fuzz_body_excerpt(content: Any) -> str:
    if isinstance(content, dict) and "$base64" in content:
        try:
            text = base64.b64decode(str(content["$base64"])).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return ""
    elif isinstance(content, str):
        text = content
    else:
        return ""
    return " ".join(text.split())[:FUZZ_BODY_EXCERPT]


def _shell_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def _fuzz_op(ops: dict[str, dict[str, Any]], label: str) -> dict[str, Any]:
    return ops.setdefault(label, {"statuses": set(), "seconds": 0.0, "failures": [], "errors": []})


def _fold_scenario(ops: dict[str, dict[str, Any]], body: dict[str, Any]) -> None:
    recorder = body.get("recorder") if isinstance(body.get("recorder"), dict) else {}
    label = str(recorder.get("label") or "")
    if not label:
        return
    op = _fuzz_op(ops, label)
    op["statuses"].add(str(body.get("status") or ""))
    try:
        op["seconds"] += float(body.get("elapsed_time") or 0)
    except (TypeError, ValueError):
        pass
    interactions = recorder.get("interactions") if isinstance(recorder.get("interactions"), dict) else {}
    checks = recorder.get("checks") if isinstance(recorder.get("checks"), dict) else {}
    for case_id, results in checks.items():
        for check in results or []:
            if not isinstance(check, dict) or check.get("status") != "failure":
                continue
            failure = (check.get("failure_info") or {}).get("failure") or {}
            interaction = interactions.get(case_id) or {}
            request = interaction.get("request") or {}
            response = interaction.get("response") or {}
            op["failures"].append({
                "type": str(failure.get("type") or "CheckFailed"),
                "title": str(failure.get("title") or check.get("name") or "check failed"),
                "message": str(failure.get("message") or ""),
                "status_code": response.get("status_code"),
                "method": str(request.get("method") or label.split(" ", 1)[0]),
                "uri": str(request.get("uri") or ""),
                "body": _fuzz_body_excerpt(response.get("content")),
            })


def _fold_error(ops: dict[str, dict[str, Any]], body: dict[str, Any]) -> None:
    value = body.get("value") if isinstance(body.get("value"), dict) else {}
    _fuzz_op(ops, str(body.get("label") or "-"))["errors"].append(
        {"type": str(value.get("type") or "Error"), "message": str(value.get("message") or "")})


def _fuzz_failure(label: str, failures: list[dict[str, Any]]) -> dict[str, Any]:
    kinds: dict[tuple[str, Any], list[dict[str, Any]]] = {}
    for item in failures:
        kinds.setdefault((item["type"], item["status_code"]), []).append(item)
    ordered = sorted(kinds.items(), key=lambda kv: (kv[0][0] != "ServerError", str(kv[0])))

    def headline(item: dict[str, Any], code: Any) -> str:
        return f"{item['title']}{f': {code}' if code is not None else ''} on {label}"

    (ftype, code), group = ordered[0]
    lines: list[str] = []
    for (_kind, kind_code), items in ordered[:FUZZ_REPRO_KINDS]:
        shortest = min(items, key=lambda f: len(f["uri"]))
        lines.append(f"{headline(shortest, kind_code)} ({len(items)} case{'' if len(items) == 1 else 's'})")
        if shortest["message"].strip():
            lines.append(shortest["message"].strip()[:400])
        if shortest["body"]:
            lines.append(f"Response: {shortest['body']}")
        if shortest["uri"]:
            lines.append(f"Reproduce with: curl -X {shortest['method']} {_shell_quote(shortest['uri'])}")
    if len(ordered) > FUZZ_REPRO_KINDS:
        lines.append(f"+{len(ordered) - FUZZ_REPRO_KINDS} more failure kinds")
    return make_failure(ftype, headline(group[0], code), "\n".join(lines))


def _fuzz_error(label: str, errors: list[dict[str, Any]]) -> dict[str, Any]:
    if not errors:
        return make_failure("FuzzError", f"scenario ended in error on {label}", "")
    first = errors[0]
    text = first["message"].strip()
    line = text.splitlines()[0] if text else first["type"]
    return make_failure(first["type"], line, "\n\n".join(f"{e['type']}: {e['message']}" for e in errors[:3]))


def parse_schemathesis_ndjson(lines: Iterable[str], prefix: str = "fuzz") -> dict[str, Any]:
    """Per-operation cases from a schemathesis `--report ndjson` event stream, whole or cut off by a kill.

    schemathesis flushes one line per engine event, so a run killed at its budget still holds every operation that
    finished; a torn last line is skipped. An operation's verdict folds all its scenarios (examples and fuzzing): a
    failed check is `failed`, a network or schema error `error`, any success `passed`, only skips `skipped`. The
    failure message names the status and the operation template, never the fuzzed values, so one defect keeps one
    signature night after night; the values, the response excerpt and a curl line go to the trace tail.
    `unfinished` counts scenarios started but never finished (in flight when the run was killed).
    """
    ops: dict[str, dict[str, Any]] = {}
    started: set[str] = set()
    finished: set[str] = set()
    stop_reason: str | None = None
    engine_finished = False
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict) or len(event) != 1:
            continue
        kind, body = next(iter(event.items()))
        if not isinstance(body, dict):
            continue
        if kind == "ScenarioStarted":
            started.add(str(body.get("id")))
        elif kind == "ScenarioFinished":
            finished.add(str(body.get("id")))
            _fold_scenario(ops, body)
        elif kind == "NonFatalError":
            _fold_error(ops, body)
        elif kind == "EngineFinished":
            engine_finished = True
            stop_reason = str(body.get("stop_reason") or "") or None
    cases: list[dict[str, Any]] = []
    for label, op in sorted(ops.items()):
        node_id = f"{prefix}::{label}"
        case: dict[str, Any] = {
            "node_id": node_id, "file": prefix, "status": "passed", "duration_ms": int(op["seconds"] * 1000),
            "attempts": 1, "body_hash": hashlib.sha1(node_id.encode()).hexdigest(),
            "feature_slug": None, "requirement_ids": [],
        }
        if op["failures"]:
            case["status"] = "failed"
            case["failure"] = _fuzz_failure(label, op["failures"])
        elif op["errors"] or "error" in op["statuses"]:
            case["status"] = "error"
            case["failure"] = _fuzz_error(label, op["errors"])
        elif "success" not in op["statuses"]:
            case["status"] = "skipped"
        cases.append(case)
    return {"cases": cases, "unfinished": len(started - finished), "stop_reason": stop_reason,
            "engine_finished": engine_finished}


def parse_playwright_smoke(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for item in results:
        url = item.get("url", "")
        path = urlparse(url).path or "/"
        node_id = f"e2e::{path}"
        output = item.get("output") or {}
        ok = output.get("status") == "success" and item.get("returncode", 0) == 0
        case = {
            "node_id": node_id, "file": "e2e", "status": "passed" if ok else "failed",
            "duration_ms": int(item.get("duration_ms", 0)), "attempts": 1,
            "body_hash": hashlib.sha1(node_id.encode()).hexdigest(), "feature_slug": None, "requirement_ids": [],
        }
        if not ok:
            reason = str(output.get("reason") or output.get("error") or item.get("stderr") or "smoke failed")
            errs = output.get("console_errors") or []
            detail = reason + ("\n" + "\n".join(str(e) for e in errs[:10]) if errs else "")
            case["failure"] = make_failure("SmokeFailure", reason, detail)
        cases.append(case)
    return cases


MAX_PAYLOAD_BYTES = 4_000_000


def fit_rows(rows: list[dict[str, Any]], known_hashes: dict[str, str],
             budget: int = MAX_PAYLOAD_BYTES) -> list[dict[str, Any]]:
    """Keep the ingest body under Legion's 5 MB request limit: every failed/error/rerun row, then passed rows
    whose body changed, then the rest in node order until the byte budget is spent."""
    sizes = [len(json.dumps(r)) for r in rows]
    if sum(sizes) <= budget:
        return rows
    bad = {"failed", "error", "rerun_passed"}
    order = sorted(range(len(rows)), key=lambda i: (
        0 if rows[i]["status"] in bad else (1 if known_hashes.get(rows[i]["node_id"]) != rows[i].get("body_hash") else 2),
        rows[i]["node_id"]))
    kept: set[int] = set()
    used = 0
    for i in order:
        if rows[i]["status"] not in bad and used + sizes[i] > budget:
            continue
        kept.add(i)
        used += sizes[i]
    return [r for i, r in enumerate(rows) if i in kept]


def totals_of(cases: list[dict[str, Any]]) -> dict[str, int]:
    def n(*statuses: str) -> int:
        return sum(1 for c in cases if c["status"] in statuses)
    return {
        "total": len(cases),
        "passed": n("passed", "xpass", "xfail"),
        "failed": n("failed"),
        "errors": n("error"),
        "skipped": n("skipped"),
        "rerun_passed": n("rerun_passed"),
    }


def select_rows(cases: list[dict[str, Any]], known_hashes: dict[str, str], sparse: bool) -> list[dict[str, Any]]:
    if not sparse:
        return cases
    kept = []
    for case in cases:
        if case["status"] in ("failed", "error", "rerun_passed"):
            kept.append(case)
        elif case.get("requirement_ids"):
            kept.append(case)
        elif known_hashes.get(case["node_id"]) != case.get("body_hash"):
            kept.append(case)
    return kept
