from __future__ import annotations

import sys

DEFAULT_MAX_LINES = 15
DETAIL_MAX_LINES = 60
MAX_LINE_CHARS = 220


def budget(detail: bool) -> int:
    return DETAIL_MAX_LINES if detail else DEFAULT_MAX_LINES


def fit(lines: list[str], detail: bool = False) -> list[str]:
    limit = budget(detail)
    flat: list[str] = []
    for line in lines:
        for part in str(line).splitlines() or [""]:
            flat.append(part if len(part) <= MAX_LINE_CHARS else part[: MAX_LINE_CHARS - 3] + "...")
    if len(flat) <= limit:
        return flat
    hint = "" if detail else " (use --detail)"
    kept = flat[: limit - 1]
    kept.append(f"... +{len(flat) - limit + 1} more lines{hint}")
    return kept


def emit(lines: list[str] | str, detail: bool = False, stream=None) -> int:
    if isinstance(lines, str):
        lines = lines.splitlines()
    out = fit(list(lines), detail)
    target = stream if stream is not None else sys.stdout
    target.write("\n".join(out) + "\n")
    target.flush()
    return len(out)
