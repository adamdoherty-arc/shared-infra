import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ops_reconcile as r  # noqa: E402

NOW = datetime(2026, 9, 30, 22, 0, 0, tzinfo=timezone.utc)


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
        return datetime(2026, 10, 8, h, m, tzinfo=timezone.utc)

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
