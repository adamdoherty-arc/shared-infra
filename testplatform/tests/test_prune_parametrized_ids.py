"""selection.prune_stale_node_ids: a parametrized node id widens to its function id.

This gate would pass trivially if the pruner kept bracketed ids verbatim (the first case asserts the bracket is
gone) or widened every id to its whole file (the control asserts a plain id is kept as an id).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tplib import selection  # noqa: E402

SRC = (
    "import pytest\n\n"
    "class TestK:\n"
    "    @pytest.mark.parametrize('c', [1])\n"
    "    def test_p(self, c):\n"
    "        pass\n\n"
    "    def test_plain(self):\n"
    "        pass\n"
)


def _write(tmp_path: Path) -> None:
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "test_k.py").write_text(SRC, encoding="utf-8")


def test_a_removed_parameter_case_becomes_the_function_id(tmp_path):
    _write(tmp_path)
    kept, stale = selection.prune_stale_node_ids(
        tmp_path, {"t/test_k.py::TestK::test_p[gone-case]", "t/test_k.py::TestK::test_p[1]"}
    )
    assert kept == {"t/test_k.py::TestK::test_p"}
    assert stale == []


def test_control_a_plain_id_stays_an_id_and_a_missing_one_widens_to_the_file(tmp_path):
    _write(tmp_path)
    kept, _ = selection.prune_stale_node_ids(tmp_path, {"t/test_k.py::TestK::test_plain"})
    assert kept == {"t/test_k.py::TestK::test_plain"}
    kept, stale = selection.prune_stale_node_ids(tmp_path, {"t/test_k.py::TestK::test_renamed"})
    assert kept == {"t/test_k.py"}
    assert stale == ["t/test_k.py::TestK::test_renamed"]
