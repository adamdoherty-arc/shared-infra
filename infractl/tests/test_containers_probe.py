"""containers probe's compose-derived watch list — the live 2026-09-15 fix
(false positives from ada-init/zero-api/erpnext one-shot containers) is
pinned here so it can't regress."""
from __future__ import annotations

from pathlib import Path

from infractl.probes.containers import watched_container_names


def test_watched_container_names_parses_container_name_fields(tmp_path: Path):
    compose_dir = tmp_path / "compose"
    compose_dir.mkdir()
    (compose_dir / "docker-compose.bifrost.yml").write_text(
        "services:\n"
        "  bifrost:\n"
        "    container_name: shared-bifrost\n"
        "  metrics:\n"
        "    container_name: bifrost-metrics\n",
        encoding="utf-8",
    )
    (compose_dir / "docker-compose.vllm.yml").write_text(
        "services:\n  chat:\n    container_name: qwen38-chat\n", encoding="utf-8",
    )
    names = watched_container_names(compose_dir)
    assert names == {"shared-bifrost", "bifrost-metrics", "qwen38-chat"}


def test_watched_container_names_missing_dir_returns_empty(tmp_path: Path):
    assert watched_container_names(tmp_path / "does-not-exist") == set()


def test_watched_container_names_ignores_service_without_container_name(tmp_path: Path):
    compose_dir = tmp_path / "compose"
    compose_dir.mkdir()
    (compose_dir / "docker-compose.x.yml").write_text(
        "services:\n  noname:\n    image: foo\n  named:\n    container_name: bar\n",
        encoding="utf-8",
    )
    assert watched_container_names(compose_dir) == {"bar"}


def test_watched_container_names_skips_unparseable_file(tmp_path: Path):
    compose_dir = tmp_path / "compose"
    compose_dir.mkdir()
    (compose_dir / "docker-compose.bad.yml").write_text(": not valid yaml: [", encoding="utf-8")
    (compose_dir / "docker-compose.good.yml").write_text(
        "services:\n  a:\n    container_name: ok\n", encoding="utf-8",
    )
    assert watched_container_names(compose_dir) == {"ok"}
