import socket
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ops_reconcile as r  # noqa: E402

NOW = datetime(2026, 9, 30, 22, 0, 0, tzinfo=UTC)


def ins(up_s=600, running=True, ports=None, restarting=False):
    started = (NOW - timedelta(seconds=up_s)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    ports = ports if ports is not None else {"8006/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8006"}]}
    return {"State": {"Running": running, "Restarting": restarting, "Paused": False, "StartedAt": started,
                      "Health": {"Status": "healthy"}},
            "NetworkSettings": {"Ports": ports}}


def plan(inspects, probe, last=None):
    return r.plan_port_heals(inspects, probe, last or {}, NOW.timestamp(), NOW, sleep=lambda s: None)


def test_dead_publish_on_running_healthy_container_is_flagged():
    # sabotage: a State.Running/health-only check would return [] here
    assert plan({"ada-backend": ins()}, lambda p: False) == [("ada-backend", 8006)]


def test_alive_port_not_flagged():
    assert plan({"ada-backend": ins()}, lambda p: True) == []


def test_transient_failure_needs_two_failures():
    calls = iter([False, True])
    assert plan({"ada-backend": ins()}, lambda p: next(calls)) == []


def test_boot_race_floor_and_not_running_skipped():
    assert plan({"a": ins(up_s=30)}, lambda p: False) == []
    assert plan({"a": ins(running=False)}, lambda p: False) == []
    assert plan({"a": ins(restarting=True)}, lambda p: False) == []


def test_cooldown_and_unpublished_and_filters():
    assert plan({"ada-backend": ins()}, lambda p: False, {"port:ada-backend": NOW.timestamp() - 60}) == []
    udp = {"9/tcp": None, "5/udp": [{"HostIp": "0.0.0.0", "HostPort": "5"}]}
    assert plan({"x": ins(ports=udp)}, lambda p: False) == []
    assert r.published_tcp_ports(ins(ports={"1/tcp": [{"HostIp": "192.168.1.5", "HostPort": "1"}]})) == []


def test_container_inside_health_start_period_is_not_healed():
    starting = ins(up_s=600)
    starting["State"]["Health"]["Status"] = "starting"
    assert plan({"qwen38-chat": starting}, lambda p: False) == []
    unhealthy = ins(up_s=600)
    unhealthy["State"]["Health"]["Status"] = "unhealthy"
    assert plan({"qwen38-chat": unhealthy}, lambda p: False) == [("qwen38-chat", 8006)]


def test_heal_commands():
    assert r.port_heal_cmd("shared-bifrost", 4445) is None
    c = r.port_heal_cmd("ada-frontend", 5420)
    assert "--recreate" in c and "--frontend-config-changed" in c and "ada-frontend" in c
    assert "--frontend-config-changed" not in r.port_heal_cmd("ada-backend", 8006)
    assert r.port_heal_cmd("shared-grafana", 3050) == ["docker", "restart", "shared-grafana"]


def _server(behaviour):
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def run():
        try:
            while True:
                c, _ = srv.accept()
                behaviour(c)
        except OSError:
            pass
    threading.Thread(target=run, daemon=True).start()
    return srv, srv.getsockname()[1]


def test_probe_port_classification():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    refused = s.getsockname()[1]
    s.close()
    assert r.probe_port(refused, 1) is False  # refused

    srv, p = _server(lambda c: c.close())  # dead Docker publish: accept then close
    assert r.probe_port(p, 1) is False
    srv.close()

    def http(c):
        c.settimeout(2)
        c.recv(100)
        c.sendall(b"HTTP/1.0 200 OK\r\n\r\n")
        c.close()
    srv, p = _server(http)
    assert r.probe_port(p, 2) is True
    srv.close()

    def nonhttp(c):  # silent, closes after the garbage request (postgres/redis style)
        c.settimeout(2)
        c.recv(100)
        c.close()
    srv, p = _server(nonhttp)
    assert r.probe_port(p, 2) is True
    srv.close()


def test_stopped_bitcoin_engine_is_standby_while_its_sibling_runs():
    assert r.is_standby_engine("ada-bitcoin", {"ada-bitcoin": "exited", "ada-bitcoin-prod": "running"})
    assert r.is_standby_engine("ada-bitcoin-prod", {"ada-bitcoin": "running"})


def test_both_bitcoin_engines_down_is_healed_and_other_containers_unaffected():
    assert not r.is_standby_engine("ada-bitcoin", {"ada-bitcoin": "exited", "ada-bitcoin-prod": "exited"})
    assert not r.is_standby_engine("ada-backend", {"ada-backend": "exited", "ada-bitcoin": "running"})


def _fleet(n, port0=9000):
    return {f"svc-{i}": ins(ports={f"{port0 + i}/tcp": [{"HostIp": "0.0.0.0", "HostPort": str(port0 + i)}]})
            for i in range(n)}


def test_forwarder_outage_suppresses_every_per_container_heal():
    fleet = _fleet(10)
    probed, dead = r.scan_ports(fleet, lambda p: False, NOW, sleep=lambda s: None)
    assert (probed, len(dead)) == (10, 10)
    assert r.is_forwarder_outage(probed, dead)
    assert plan(fleet, lambda p: False) == []


def test_single_dead_container_among_healthy_still_heals_per_container():
    fleet = _fleet(10)
    probed, dead = r.scan_ports(fleet, lambda p: p != 9003, NOW, sleep=lambda s: None)
    assert not r.is_forwarder_outage(probed, dead)
    assert plan(fleet, lambda p: p != 9003) == [("svc-3", 9003)]


def test_outage_thresholds_need_both_count_and_fraction():
    assert not r.is_forwarder_outage(4, {"a": 1, "b": 2, "c": 3})
    assert r.is_forwarder_outage(8, {"a": 1, "b": 2, "c": 3, "d": 4})
    assert not r.is_forwarder_outage(20, {"a": 1, "b": 2, "c": 3, "d": 4})


def test_outage_counts_containers_in_heal_cooldown():
    fleet = _fleet(6)
    cooling = {f"port:{n}": NOW.timestamp() - 60 for n in fleet}
    probed, dead = r.scan_ports(fleet, lambda p: False, NOW, sleep=lambda s: None)
    assert r.is_forwarder_outage(probed, dead)
    assert plan(fleet, lambda p: False, cooling) == []


def test_forwarder_restart_refusals():
    def at(h, m):
        return datetime(2026, 10, 8, h, m, tzinfo=UTC)

    def flat():
        return []

    t = at(12, 15).timestamp()
    assert r.forwarder_restart_refusal(at(12, 15), {}, t, flat) is None
    assert "6h" in r.forwarder_restart_refusal(at(12, 15), {"forwarder_restart": t - 3600}, t, flat)
    for minute in (0, 5, 30, 36, 55, 59):
        assert "window" in r.forwarder_restart_refusal(at(12, minute), {}, at(12, minute).timestamp(), flat)
    assert "exposure" in r.forwarder_restart_refusal(at(12, 15), {}, t, lambda: ["live leg 7 open"])


def test_ada_heals_run_from_the_ada_root():
    assert r.port_heal_cwd("ada-backend") == r.ADA_ROOT
    assert r.port_heal_cwd("shared-grafana") is None


MIRRORED = "[wsl2]\nmemory=32GB\n# networkingMode=nat\nNetworkingMode = Mirrored  # broke ports\n"
NAT = "[wsl2]\nmemory=32GB\nnetworkingMode=nat\n"


def test_mirrored_wslconfig_is_flagged():
    mode = r.wsl_networking_mode(MIRRORED)
    assert mode == "mirrored"
    assert "mirrored" in r.wslconfig_finding(mode) and "nat" in r.wslconfig_finding(mode)


def test_nat_missing_key_and_wrong_section_are_not_flagged():
    assert r.wslconfig_finding(r.wsl_networking_mode(NAT)) is None
    assert r.wsl_networking_mode("[wsl2]\nmemory=32GB\n") is None
    assert r.wslconfig_finding(r.wsl_networking_mode("")) is None
    assert r.wsl_networking_mode("[experimental]\nnetworkingMode=mirrored\n") is None
    assert r.wsl_networking_mode("[wsl2]\n# networkingMode=mirrored\n") is None


def test_missing_wslconfig_file_is_not_flagged(tmp_path):
    assert r.read_wsl_networking_mode(str(tmp_path / "absent")) is None
    f = tmp_path / ".wslconfig"
    f.write_text(MIRRORED, encoding="utf8")
    assert r.read_wsl_networking_mode(str(f)) == "mirrored"


def test_outage_with_mirrored_skips_docker_desktop_restart_and_names_cause():
    t = datetime(2026, 10, 8, 12, 15, tzinfo=UTC)

    def flat():
        return []

    refusal = r.forwarder_restart_refusal(t, {}, t.timestamp(), flat, networking_mode="mirrored")
    assert refusal and "mirrored" in refusal and "does not fix" in refusal
    assert r.forwarder_restart_refusal(t, {}, t.timestamp(), flat, networking_mode="nat") is None
    assert r.forwarder_restart_refusal(t, {}, t.timestamp(), flat, networking_mode=None) is None


INCIDENT_NOW = datetime(2026, 10, 9, 12, 40, 0, tzinfo=UTC)
ENGINE_STOP = "2026-10-09T12:35:11.240276753Z"


def cont(project, running, policy="unless-stopped", started="2026-10-09T12:35:50.000000000Z",
         finished=ENGINE_STOP, name=""):
    labels = {"com.docker.compose.project": project} if project else {}
    return {"Name": "/" + name, "Config": {"Labels": labels},
            "HostConfig": {"RestartPolicy": {"Name": policy}},
            "State": {"Running": running, "Status": "running" if running else "exited",
                      "StartedAt": started, "FinishedAt": finished}}


def incident_fleet():
    """The 2026-10-09 12:40Z fleet: compose re-upped ada/legion/shared-infra; the rest stayed Exited."""
    fleet = {}
    for n, proj in [("ada-backend", "ada"), ("ada-redis", "ada"), ("legion-redis", "legion"),
                    ("shared-grafana", "shared-infra"), ("shared-searxng", "shared-infra")]:
        fleet[n] = cont(proj, True, name=n)
    fleet["vllm-autoheal"] = cont("shared-infra", True, policy="always", name="vllm-autoheal")
    old_start = "2026-10-09T12:31:28.000000000Z"
    for n, proj in [("ada-bitcoin-prod", "ada"), ("ada-nginx", None), ("erpnext-db-1", "erpnext"),
                    ("erpnext-frontend-1", "erpnext"), ("customer-ops-personal-1", "customer-ops"),
                    ("fortressos-postgres-1", "fortressos")]:
        fleet[n] = cont(proj, False, started=old_start, name=n)
    fleet["ada-bitcoin"] = cont("ada", False, started="2026-10-09T00:45:32.000000000Z",
                                finished="2026-10-09T02:10:23.000000000Z", name="ada-bitcoin")
    fleet["erpnext-create-site-1"] = cont("erpnext", False, policy="no", name="erpnext-create-site-1")
    return fleet


def test_engine_stop_restores_every_exited_restart_policy_container_of_the_incident():
    got = r.plan_engine_stop_restores(incident_fleet(), INCIDENT_NOW)
    assert got == sorted(["ada-bitcoin-prod", "ada-nginx", "customer-ops-personal-1", "erpnext-db-1",
                          "erpnext-frontend-1", "fortressos-postgres-1"])


def test_engine_stop_restore_skips_deliberate_stop_standby_and_no_policy():
    got = r.plan_engine_stop_restores(incident_fleet(), INCIDENT_NOW)
    assert "ada-bitcoin" not in got
    assert "erpnext-create-site-1" not in got


def test_engine_stop_restore_honours_never_restore():
    fleet = incident_fleet()
    fleet["ada-nginx-candidate"] = cont(None, False, name="ada-nginx-candidate")
    got = r.plan_engine_stop_restores(fleet, INCIDENT_NOW, {"ada-nginx-candidate"})
    assert "ada-nginx-candidate" not in got and "ada-nginx" in got


def test_one_project_compose_stop_is_not_an_engine_stop():
    # sabotage: a cluster counted without the project floor would restart a deliberately stopped stack
    fleet = {f"erpnext-{i}": cont("erpnext", False, name=f"erpnext-{i}") for i in range(8)}
    fleet["ada-backend"] = cont("ada", True, finished="2026-10-09T09:00:00.000000000Z", name="ada-backend")
    assert r.plan_engine_stop_restores(fleet, INCIDENT_NOW) == []


def test_single_deliberate_stop_is_not_restored():
    # sabotage: deciding from Exited state alone would restart this
    fleet = {n: cont(p, True, finished="2026-10-09T09:00:00.000000000Z", name=n)
             for n, p in [("a1", "ada"), ("l1", "legion"), ("s1", "shared-infra")]}
    fleet["fortressos-postgres-1"] = cont("fortressos", False, finished="2026-10-09T12:38:00.000000000Z",
                                          name="fortressos-postgres-1")
    assert r.plan_engine_stop_restores(fleet, INCIDENT_NOW) == []


def test_engine_stop_needs_the_engine_back_and_a_recent_stop():
    fleet = {n: cont(p, False, name=n) for n, p in [("a", "ada"), ("b", "ada"), ("c", "legion"),
                                                    ("d", "erpnext"), ("e", "fortressos")]}
    assert r.plan_engine_stop_restores(fleet, INCIDENT_NOW) == []
    fleet["f"] = cont("shared-infra", True, name="f")
    assert r.plan_engine_stop_restores(fleet, INCIDENT_NOW) == ["a", "b", "c", "d", "e"]
    assert r.plan_engine_stop_restores(fleet, INCIDENT_NOW + timedelta(days=2)) == []


def test_bitcoin_standby_never_restored_while_prod_runs():
    fleet = incident_fleet()
    fleet["ada-bitcoin-prod"]["State"].update(Running=True, Status="running")
    fleet["ada-bitcoin"] = cont("ada", False, name="ada-bitcoin")
    assert "ada-bitcoin" not in r.plan_engine_stop_restores(fleet, INCIDENT_NOW)


def test_must_run_heal_cmd_falls_back_to_docker_start_without_compose_mapping():
    assert r.must_run_heal_cmd("ada-nginx", "exited") == ["docker", "start", "ada-nginx"]
    assert r.must_run_heal_cmd("ada-nginx", None) is None
    cmd = r.must_run_heal_cmd("erpnext-frontend-1", "exited")
    assert cmd[:3] == ["docker", "compose", "-p"] and cmd[-1] == "frontend"


def test_must_run_covers_the_incident_containers():
    must = set(r.EXPECTED["must_run"])
    for n in ["ada-bitcoin-prod", "ada-nginx", "customer-ops-personal-1", "fortressos-postgres-1",
              "erpnext-db-1", "erpnext-backend-1", "erpnext-frontend-1"]:
        assert n in must
        assert n == "ada-nginx" or n in r.EXPECTED["compose"]


def test_lock_is_exclusive_and_stale_lock_is_taken(tmp_path, monkeypatch):
    lock = tmp_path / "ops-reconcile.lock"
    monkeypatch.setattr(r, "LOCK", lock)
    now = 1_000_000.0
    assert r.acquire_lock(now) is True
    assert r.acquire_lock(now) is False
    import os
    os.utime(lock, (now - r.LOCK_STALE_S - 5, now - r.LOCK_STALE_S - 5))
    assert r.acquire_lock(now) is True
    r.release_lock()
    assert not lock.exists()


def test_port_probe_skip_ignores_idle_by_design_publishes(monkeypatch):
    monkeypatch.setattr(r, "PORT_PROBE_SKIP", {"customer-ops-personal-1": [80, 443]})
    ports = {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "80"}],
             "8443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "443"}],
             "8765/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8765"}]}
    fleet = {"customer-ops-personal-1": ins(ports=ports)}
    assert plan(fleet, lambda p: p not in (80, 443)) == []
    assert plan(fleet, lambda p: False) == [("customer-ops-personal-1", 8765)]


def test_port_probe_skip_config_names_customer_ops_idle_edge():
    assert set(r.PORT_PROBE_SKIP.get("customer-ops-personal-1", [])) == {80, 443}


def _reply(table):
    return lambda host, port: table[(host, port)]


def test_port_replying_on_v4_and_silent_on_v6_is_a_wedged_relay():
    # sabotage: probe_port() counts a connected-but-silent port alive, so reusing it would return [] here
    table = {("127.0.0.1", 5421): "reply", ("::1", 5421): "silent"}
    assert r.loopback6_wedged([5421], _reply(table), sleep=lambda s: None) == [5421]


def test_healthy_absent_and_non_http_loopback6_are_not_wedged():
    table = {("127.0.0.1", 5421): "reply", ("::1", 5421): "reply",
             ("127.0.0.1", 8006): "reply", ("::1", 8006): "refused",
             ("127.0.0.1", 6379): "silent", ("::1", 6379): "silent"}
    assert r.loopback6_wedged([5421, 8006, 6379], _reply(table), sleep=lambda s: None) == []


def test_loopback6_wedge_needs_two_failures_and_is_capped():
    calls = iter(["reply", "silent", "reply", "reply"])
    assert r.loopback6_wedged([5421], lambda h, p: next(calls), sleep=lambda s: None) == []
    table = {(h, p): v for p in range(1, 9) for h, v in (("127.0.0.1", "reply"), ("::1", "silent"))}
    assert r.loopback6_wedged(list(range(1, 9)), _reply(table), sleep=lambda s: None) == [1, 2, 3]


def test_loopback6_finding_names_ports_and_drops_the_hint_once_forwarding_is_off():
    assert r.loopback6_finding([], None) is None
    on = r.loopback6_finding([5421, 8006], None)
    assert "5421, 8006" in on and "127.0.0.1" in on and "localhostForwarding=false" in on
    assert "localhostForwarding=false" not in r.loopback6_finding([5421], "false")
    assert r.wsl2_value("[wsl2]\nlocalhostForwarding = False\n", "localhostforwarding") == "false"
    assert r.wsl2_value("[wsl2]\n# localhostForwarding=false\n", "localhostForwarding") is None


def test_http_reply_classification_on_a_real_socket():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    refused = s.getsockname()[1]
    s.close()
    assert r.http_reply("127.0.0.1", refused, 1) == "refused"

    def http(c):
        c.settimeout(2)
        c.recv(100)
        c.sendall(b"HTTP/1.0 200 OK\r\n\r\n")
        c.close()
    srv, p = _server(http)
    assert r.http_reply("127.0.0.1", p, 2) == "reply"
    srv.close()

    def wedged(c):
        c.settimeout(3)
        try:
            c.recv(100)
            c.recv(100)
        except OSError:
            pass
        c.close()
    srv, p = _server(wedged)
    assert r.http_reply("127.0.0.1", p, 1) == "silent"
    srv.close()
