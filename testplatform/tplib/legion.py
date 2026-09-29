from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode

PREFIX = "/api/test-platform"


class LegionError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class LegionClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, path: str, body: Any = None, query: dict[str, Any] | None = None,
              timeout: float | None = None) -> Any:
        url = f"{self.base}{PREFIX}{path}"
        if query:
            url += "?" + urlencode({k: v for k, v in query.items() if v is not None})
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise LegionError(f"{method} {path} -> HTTP {exc.code}: {detail}", exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LegionError(f"{method} {path} unreachable: {exc}") from exc

    def create_run(self, body: dict) -> int:
        return int(self._call("POST", "/runs", body)["run_id"])

    def post_results(self, run_id: int, body: dict) -> dict:
        return self._call("POST", f"/runs/{run_id}/results", body, timeout=180)

    def runs(self, project_id: int, limit: int = 20) -> Any:
        return self._call("GET", "/runs", query={"project_id": project_id, "limit": limit})

    def run(self, run_id: int) -> Any:
        return self._call("GET", f"/runs/{run_id}")

    def run_failures(self, run_id: int) -> Any:
        return self._call("GET", f"/runs/{run_id}/failures")

    def failure(self, failure_id: int) -> Any:
        return self._call("GET", f"/failures/{failure_id}")

    def history(self, project_id: int, node_id: str) -> Any:
        return self._call("GET", "/cases/history", query={"project_id": project_id, "node_id": node_id})

    def hashes(self, project_id: int) -> dict[str, str]:
        return self._call("GET", "/cases/hashes", query={"project_id": project_id}) or {}

    def flaky(self, project_id: int) -> Any:
        return self._call("GET", "/flaky", query={"project_id": project_id})

    def quarantine(self, project_id: int) -> list[str]:
        return self._call("GET", "/quarantine", query={"project_id": project_id}) or []

    def schedules(self, project_id: int | None = None) -> Any:
        return self._call("GET", "/schedules", query={"project_id": project_id})
