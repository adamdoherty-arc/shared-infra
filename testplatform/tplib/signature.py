from __future__ import annotations

import re

_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_WIN_PATH = re.compile(r"[A-Za-z]:[\\/][^\s'\"<>|:*?]+")
_POSIX_PATH = re.compile(r"(?<![\w.])/(?:[\w.\-@+]+/)+[\w.\-@+]*")
_HEX0X = re.compile(r"\b0x[0-9a-fA-F]+\b")
_HEXLONG = re.compile(r"\b(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{8,}\b")
_NUM = re.compile(r"(?<![A-Za-z_])-?\d+(?:\.\d+)?")
_WS = re.compile(r"\s+")

MAX_SIGNATURE = 200


def normalize_message(text: str) -> str:
    s = text or ""
    s = _TS.sub("<ts>", s)
    s = _UUID.sub("<uuid>", s)
    s = _WIN_PATH.sub("<path>", s)
    s = _POSIX_PATH.sub("<path>", s)
    s = _HEX0X.sub("<hex>", s)
    s = _HEXLONG.sub("<hex>", s)
    s = _NUM.sub("<n>", s)
    s = _WS.sub(" ", s).strip()
    return s


def signature(failure_type: str, message: str) -> str:
    first_line = (message or "").strip().splitlines()[0] if (message or "").strip() else ""
    if failure_type and first_line.startswith(failure_type + ":"):
        first_line = first_line[len(failure_type) + 1:]
    sig = f"{failure_type}: {normalize_message(first_line)}".strip()
    return sig[:MAX_SIGNATURE]
