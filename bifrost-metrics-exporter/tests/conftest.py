"""Shared exporter module loader for bifrost-metrics-exporter/tests.

exporter.py is not a package (no __init__.py in this directory's parent),
so tests load it directly via importlib.util.spec_from_file_location.
prometheus_client's default CollectorRegistry is process-global: loading
exporter.py a second time inside the same pytest process re-registers the
same metric names and raises `ValueError: Duplicated timeseries in
CollectorRegistry` (hit when test_lane_eval_metrics.py was added alongside
test_probe_retry.py, WS7 lane-eval harness, 2026-09-25). This conftest loads
the module exactly once per test session; every test file reuses that same
instance via `sys.modules["bifrost_exporter"]` instead of re-executing it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MODULE_NAME = "bifrost_exporter"

if _MODULE_NAME not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_MODULE_NAME, Path(__file__).resolve().parent.parent / "exporter.py")
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_MODULE_NAME] = _module
    _spec.loader.exec_module(_module)
