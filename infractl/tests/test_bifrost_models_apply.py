"""infractl as the single writer of bifrost/config.json (2026-09-25):
`bifrost_models_apply`, `bifrost_provider_park`, and the binding restart
ladder they share. Only the edges are mocked (Docker socket, the sync
subprocess, HTTP health/probe, the redacted-snapshot export); planning,
validation, the operator-disabled check (the REAL
bifrost/sync_vk_allowlists.py `operator_violations()`), atomic writes,
snapshot/restore, ledger rows and the rollback decision all run for real."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from infractl.bifrost import config as bifrost_config
from infractl.bifrost import restart as restart_mod
from infractl.bifrost import vk_sync
from infractl.core import actions as actions_mod
from infractl.core import docker as docker_mod
from infractl.core.ledger import Ledger
from infractl.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_SCRIPT = REPO_ROOT / "bifrost" / "sync_vk_allowlists.py"

BASE_CFG = {
    "providers": {
        "vllm-local": {"keys": [{"name": "local", "models": ["qwen3.8-27b", "qwen3-chat"],
                                 "aliases": {"qwen3-chat": "qwen3.8-27b"}}]},
        "nvidia-nim": {"keys": [
            {"name": "nim-1", "models": ["a/one", "b/two"]},
            {"name": "nim-2", "models": ["a/one", "b/two"]},
        ]},
        "groq": {"keys": [{"name": "groq-1", "models": ["openai/gpt-oss-120b"]}]},
    }
}
OPERATOR_DISABLED = {"model_patterns": {"kimi": "operator order"}, "providers": {"aion": "no key"}}


@pytest.fixture
def bifrost_dir(tmp_path: Path) -> Path:
    d = tmp_path / "bifrost"
    d.mkdir()
    (d / "config.json").write_text(json.dumps(BASE_CFG, indent=2) + "\n", encoding="utf-8")
    (d / "disabled-providers.json").write_text(json.dumps({"providers": {}}, indent=2) + "\n", encoding="utf-8")
    (d / "operator-disabled.json").write_text(json.dumps(OPERATOR_DISABLED), encoding="utf-8")
    shutil.copy2(SYNC_SCRIPT, d / "sync_vk_allowlists.py")
    return d


@pytest.fixture
def settings(tmp_path: Path, bifrost_dir: Path) -> Settings:
    return Settings(
        INFRACTL_TOKEN="test-token", INFRACTL_BIFROST_DIR=str(bifrost_dir),
        INFRACTL_STATE_DIR=str(tmp_path / "state"), INFRACTL_WRITE_MODE="apply",
        INFRA_PROBE_VK="sk-bf-test-probe", INFRACTL_PROBE_BASE="http://gw:8080",
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "infractl.db")


class _Resp:
    def __init__(self, status_code: int):
        self.status_code = status_code


@pytest.fixture
def edges(monkeypatch):
    """Records every Docker/sync/probe call in order; each edge's behavior is
    switchable per test via the returned dict."""
    state = {"calls": [], "sync_results": [], "health": 200, "probe": (True, "HTTP 200 in 0.1s"),
             "verify": (True, {"health": "HTTP 200"}), "unimported_once": []}

    def stop(name, timeout_s=10):
        state["calls"].append(f"stop {name}")
        return 204

    def start(name):
        state["calls"].append(f"start {name}")
        return 204

    def run_vk_sync(bifrost_dir, timeout_s=120):
        state["calls"].append("sync")
        if state["sync_results"]:
            outcome = state["sync_results"].pop(0)
            if isinstance(outcome, Exception):
                raise outcome
        return "updated=1 inserted=0\n"

    monkeypatch.setattr(docker_mod, "stop", stop)
    monkeypatch.setattr(docker_mod, "start", start)
    monkeypatch.setattr(restart_mod.vk_sync, "run_vk_sync", run_vk_sync)
    monkeypatch.setattr(restart_mod.httpx, "get", lambda *a, **k: _Resp(state["health"]))
    monkeypatch.setattr(restart_mod, "completion_probe", lambda *a, **k: state["probe"])
    monkeypatch.setattr(restart_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(restart_mod, "_unimported_providers",
                        lambda d: state["unimported_once"] and [state["unimported_once"].pop()] or [])
    monkeypatch.setattr(actions_mod, "verify_gateway", lambda s: state["verify"])
    monkeypatch.setattr(bifrost_config, "write_redacted_snapshot", lambda d: d / "config.snapshot.redacted.json")
    return state


def _cfg(bifrost_dir: Path) -> dict:
    return json.loads((bifrost_dir / "config.json").read_text(encoding="utf-8"))


# ---- pure planning ---------------------------------------------------------

def test_apply_model_changes_touches_every_key_and_reports_noops():
    new, diff = bifrost_config.apply_model_changes(
        BASE_CFG, {"nvidia-nim": {"add": ["c/three", "a/one"], "remove": ["b/two", "z/absent"]}})
    for key in new["providers"]["nvidia-nim"]["keys"]:
        assert key["models"] == ["a/one", "c/three"]
    assert diff["nvidia-nim"] == {"added": ["c/three"], "already_present": ["a/one"],
                                  "removed": ["b/two"], "not_present": ["z/absent"]}
    assert BASE_CFG["providers"]["nvidia-nim"]["keys"][0]["models"] == ["a/one", "b/two"]  # pure


@pytest.mark.parametrize("changes, fragment", [
    ({"nope": {"add": ["x"]}}, "not active"),
    ({"groq": {"add": ["x"], "remove": ["x"]}}, "both add and remove"),
    ({"groq": {"add": "x"}}, "list of non-empty"),
    ({"groq": {"rename": ["x"]}}, "only 'add'/'remove'"),
    ({"vllm-local": {"remove": ["qwen3.8-27b"]}}, "aliased by"),
    ({}, "non-empty"),
])
def test_apply_model_changes_rejects_bad_requests(changes, fragment):
    with pytest.raises(bifrost_config.ConfigError) as exc_info:
        bifrost_config.apply_model_changes(BASE_CFG, changes)
    assert fragment in str(exc_info.value)


def test_cascade_aliases_retires_dead_model_and_its_alias():
    """The dead-model sync passes cascade_aliases: the alias to a removed model goes too,
    and the alias name leaves `models`. Control: without the flag the same request is
    refused (test_apply_model_changes_rejects_bad_requests), so the guard is not vacuous."""
    new_cfg, diff = bifrost_config.apply_model_changes(
        BASE_CFG, {"vllm-local": {"remove": ["qwen3.8-27b"], "cascade_aliases": True}})
    key = new_cfg["providers"]["vllm-local"]["keys"][0]
    assert "qwen3.8-27b" not in key["models"] and "qwen3-chat" not in key["models"]
    assert "aliases" not in key
    assert diff["vllm-local"]["aliases_removed"] == ["qwen3-chat"]
    assert BASE_CFG["providers"]["vllm-local"]["keys"][0]["aliases"] == {"qwen3-chat": "qwen3.8-27b"}  # pure


def test_operator_violations_delegates_to_the_real_sync_script(bifrost_dir):
    cfg = json.loads(json.dumps(BASE_CFG))
    cfg["providers"]["groq"]["keys"][0]["models"].append("moonshotai/Kimi-K3")
    assert bifrost_config.operator_violations(BASE_CFG, bifrost_dir) == []
    assert any("Kimi-K3" in v for v in bifrost_config.operator_violations(cfg, bifrost_dir))


# ---- bifrost_models_apply through execute() ---------------------------------

@pytest.mark.asyncio
async def test_dry_run_returns_diff_and_writes_nothing(ledger, settings, bifrost_dir, edges):
    before = (bifrost_dir / "config.json").read_bytes()
    result = await actions_mod.execute(
        ledger, settings, "bifrost_models_apply", {"changes": {"nvidia-nim": {"remove": ["b/two"]}}},
        requested_by="tester", reason="dry run", dry_run=True,
    )
    assert result["dry_run"] is True
    assert result["diff"]["nvidia-nim"]["removed"] == ["b/two"]
    assert any(ln.startswith("-") and '"b/two"' in ln for ln in result["unified_diff"].splitlines())
    assert (bifrost_dir / "config.json").read_bytes() == before
    assert edges["calls"] == []
    assert ledger.get_action(result["action_id"])["status"] == "succeeded_dry_run"


@pytest.mark.asyncio
async def test_apply_writes_then_runs_the_binding_sequence(ledger, settings, bifrost_dir, edges):
    result = await actions_mod.execute(
        ledger, settings, "bifrost_models_apply", {"changes": {"groq": {"add": ["qwen/qwen3.8-27b"]}}},
        requested_by="ada:bifrost_model_sync", reason="daily sync", dry_run=False,
    )
    assert _cfg(bifrost_dir)["providers"]["groq"]["keys"][0]["models"] == ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"]
    assert edges["calls"] == ["stop bifrost-autoheal", "stop shared-bifrost", "sync",
                              "start shared-bifrost", "start bifrost-autoheal"]
    row = ledger.get_action(result["action_id"])
    assert row["status"] == "succeeded"
    assert row["pre_config_sha256"] != row["post_config_sha256"]
    assert (Path(row["snapshot_dir"]) / "config.json").exists()


@pytest.mark.asyncio
async def test_noop_change_never_restarts(ledger, settings, edges):
    result = await actions_mod.execute(
        ledger, settings, "bifrost_models_apply", {"changes": {"groq": {"add": ["openai/gpt-oss-120b"]}}},
        requested_by="tester", reason="noop", dry_run=False,
    )
    assert result["noop"] is True
    assert edges["calls"] == []
    assert ledger.get_action(result["action_id"])["status"] == "succeeded_noop"


@pytest.mark.asyncio
async def test_operator_disabled_model_is_refused_before_any_write(ledger, settings, bifrost_dir, edges):
    before = (bifrost_dir / "config.json").read_bytes()
    with pytest.raises(actions_mod.ActionError) as exc_info:
        await actions_mod.execute(
            ledger, settings, "bifrost_models_apply", {"changes": {"groq": {"add": ["moonshotai/kimi-k3"]}}},
            requested_by="tester", reason="banned", dry_run=False,
        )
    assert exc_info.value.code == "operator_disabled"
    assert (bifrost_dir / "config.json").read_bytes() == before
    assert edges["calls"] == []
    assert ledger.list_actions()[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_sync_refusal_rolls_back_config_and_restarts_again(ledger, settings, bifrost_dir, edges):
    before = _cfg(bifrost_dir)
    edges["sync_results"] = [vk_sync.VkSyncError("REFUSING", returncode=2, output="REFUSING")]
    with pytest.raises(actions_mod.ActionError) as exc_info:
        await actions_mod.execute(
            ledger, settings, "bifrost_models_apply", {"changes": {"groq": {"add": ["x/new"]}}},
            requested_by="tester", reason="refused", dry_run=False,
        )
    assert exc_info.value.code == "rolled_back"
    assert _cfg(bifrost_dir) == before
    # first ladder: stopped, sync refused, finally restarts both; second
    # ladder (rollback) runs the full sequence against the restored file.
    assert edges["calls"] == [
        "stop bifrost-autoheal", "stop shared-bifrost", "sync", "start shared-bifrost", "start bifrost-autoheal",
        "stop bifrost-autoheal", "stop shared-bifrost", "sync", "start shared-bifrost", "start bifrost-autoheal",
    ]
    row = ledger.list_actions()[0]
    assert row["status"] == "rolled_back"
    assert json.loads(row["verify_json"])["verified_ok"] is True


@pytest.mark.asyncio
async def test_failed_probe_rolls_back(ledger, settings, bifrost_dir, edges):
    before = _cfg(bifrost_dir)
    edges["probe"] = (False, "HTTP 403 in 0.1s")
    with pytest.raises(actions_mod.ActionError) as exc_info:
        await actions_mod.execute(
            ledger, settings, "bifrost_models_apply", {"changes": {"groq": {"add": ["x/new"]}}},
            requested_by="tester", reason="probe fails", dry_run=False,
        )
    assert exc_info.value.code == "rolled_back"
    assert _cfg(bifrost_dir) == before
    assert json.loads(ledger.list_actions()[0]["verify_json"])["verified_ok"] is False


# ---- bifrost_provider_park -------------------------------------------------

@pytest.mark.asyncio
async def test_autoheal_park_moves_block_and_leaves_autoheal_running(ledger, settings, bifrost_dir, edges):
    await actions_mod.execute(
        ledger, settings, "bifrost_provider_park", {"provider": "groq", "reason": "8 auth hits in 300s"},
        requested_by="bifrost-autoheal", reason="auth failures", dry_run=False,
    )
    assert "groq" not in _cfg(bifrost_dir)["providers"]
    parked = json.loads((bifrost_dir / "disabled-providers.json").read_text(encoding="utf-8"))["providers"]["groq"]
    assert "bifrost-autoheal" in parked["_comment"] and parked["keys"][0]["name"] == "groq-1"
    assert "stop bifrost-autoheal" not in edges["calls"]
    assert edges["calls"] == ["stop shared-bifrost", "sync", "start shared-bifrost"]


@pytest.mark.asyncio
async def test_park_protected_lane_is_invalid(ledger, settings, edges):
    with pytest.raises(actions_mod.ActionError) as exc_info:
        await actions_mod.execute(
            ledger, settings, "bifrost_provider_park", {"provider": "vllm-local", "reason": "x"},
            requested_by="tester", reason="x", dry_run=False,
        )
    assert exc_info.value.code == "invalid_request"
    assert edges["calls"] == []


# ---- restart ladder --------------------------------------------------------

def test_restart_second_cycle_for_provider_bifrost_has_not_imported(bifrost_dir, edges):
    edges["unimported_once"] = ["groq"]
    rr = restart_mod.restart_bifrost(bifrost_dir, probe_base="http://gw:8080", probe_vk="sk-bf-x")
    assert rr["ok"] is True
    assert edges["calls"] == ["stop bifrost-autoheal", "stop shared-bifrost", "sync", "start shared-bifrost",
                              "stop shared-bifrost", "sync", "start shared-bifrost", "start bifrost-autoheal"]


def test_restart_unhealthy_gateway_still_restores_autoheal(bifrost_dir, edges):
    edges["health"] = 503
    rr = restart_mod.restart_bifrost(bifrost_dir, probe_base="http://gw:8080", probe_vk="sk-bf-x",
                                     health_timeout_s=6, poll_interval_s=3)
    assert rr["ok"] is False and rr["healthy"] is False
    assert edges["calls"][-1] == "start bifrost-autoheal"


def test_restart_without_probe_vk_fails_closed(bifrost_dir, edges):
    rr = restart_mod.restart_bifrost(bifrost_dir, probe_base="http://gw:8080", probe_vk="")
    assert rr["ok"] is False and "INFRA_PROBE_VK" in rr["probe"]


def test_restart_docker_error_mid_ladder_restarts_both(bifrost_dir, edges, monkeypatch):
    calls = edges["calls"]

    def start(name):
        calls.append(f"start {name}")
        if name == "shared-bifrost" and calls.count("start shared-bifrost") == 1:
            raise docker_mod.DockerError("start shared-bifrost -> HTTP 500")
        return 204

    monkeypatch.setattr(docker_mod, "start", start)
    rr = restart_mod.restart_bifrost(bifrost_dir, probe_base="http://gw:8080", probe_vk="sk-bf-x")
    assert rr["ok"] is False
    assert calls[-2:] == ["start shared-bifrost", "start bifrost-autoheal"]


# ---- heal rules never run a T2 kind for real --------------------------------

@pytest.mark.asyncio
async def test_heal_rule_vk_resync_is_forced_to_dry_run(ledger, settings, edges):
    from infractl.heal import rules

    await rules._fire(ledger, settings, "vk_resync", {}, reason="parity red", probe_name="config_parity")
    row = ledger.list_actions()[0]
    assert row["kind"] == "vk_resync" and row["dry_run"] == 1 and row["status"] == "succeeded_dry_run"
    assert edges["calls"] == []
