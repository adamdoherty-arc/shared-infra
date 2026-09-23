"""Legion note + sprint-task helpers. Non-fatal if Legion is down — every
call here is best-effort (infractl's own ledger is the source of truth for
actions; Legion is a mirror for human visibility)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from infractl.settings import get_settings


def _post(path: str, payload: dict, base: str | None = None, timeout: float = 5.0) -> dict | None:
    base = base or get_settings().legion_api_base
    url = f"{base.rstrip('/')}{path}"
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body) if body else {}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def _patch(path: str, payload: dict, base: str | None = None, timeout: float = 5.0) -> dict | None:
    base = base or get_settings().legion_api_base
    url = f"{base.rstrip('/')}{path}"
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="PATCH",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body) if body else {}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def post_feature_note(slug: str, kind: str, title: str, body: str,
                       source_ref: str, author: str = "infractl") -> dict | None:
    """kind in design_decision|constraint|workaround|open_question.
    source_ref makes this idempotent — re-posting the same
    (feature, kind, source_ref) updates rather than duplicates."""
    return _post(
        f"/api/features/{slug}/issues",
        {"kind": kind, "title": title, "body": body, "author": author, "source_ref": source_ref},
    )


def create_sprint_task(sprint_id: int, title: str, category: str = "feature",
                        priority: int = 5, story_points: int = 1,
                        files_affected: list[str] | None = None,
                        acceptance_criteria: list[str] | None = None) -> dict | None:
    return _post(
        f"/api/sprints/{sprint_id}/tasks",
        {
            "title": title, "category": category, "priority": priority,
            "story_points": story_points,
            "files_affected": files_affected or [],
            "acceptance_criteria": acceptance_criteria or [],
        },
    )


def move_task(task_id: int, status: str) -> dict | None:
    """status is an UPPERCASE enum: PENDING|QUEUED|RUNNING|COMPLETED|FAILED|SKIPPED|RATE_LIMITED."""
    return _patch(f"/api/sprints/tasks/{task_id}", {"status": status})


def complete_sprint(sprint_id: int, retrospective_data: dict) -> dict | None:
    patched = _patch(f"/api/sprints/{sprint_id}", {"retrospective_data": retrospective_data})
    if patched is None:
        return None
    return _post(f"/api/sprints/{sprint_id}/complete", {})
