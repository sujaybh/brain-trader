"""deploy/: compose files validate (`docker compose config`, skipped without docker), the broker
publishes no ports, the Traefik overlay only touches `web`, switch.sh parses, and doctor's docker mode."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scheduler"))
DEPLOY = ROOT / "deploy"
BASE = DEPLOY / "docker-compose.yml"
TRAEFIK = DEPLOY / "docker-compose.traefik.yml"

needs_docker = pytest.mark.skipif(
    shutil.which("docker") is None or subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0,
    reason="docker compose not available")

ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp",
       "TRADER_ENV_FILE": "/dev/null", "ALPACA_ENV_FILE": "/dev/null",
       "TRADER_HOST": "trader.example.ts.net", "TRAEFIK_NETWORK": "proxy", "TRAEFIK_CERTRESOLVER": "le"}


def compose_config(*files: Path, extra_env: dict | None = None) -> dict:
    cmd = ["docker", "compose"]
    for f in files:
        cmd += ["-f", str(f)]
    r = subprocess.run([*cmd, "config", "--format", "json"], capture_output=True, text=True,
                       env={**ENV, **(extra_env or {})}, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@needs_docker
def test_base_compose_validates_and_broker_publishes_nothing():
    cfg = compose_config(BASE)
    assert set(cfg["services"]) == {"broker", "web", "scheduler"}
    broker = cfg["services"]["broker"]
    assert not broker.get("ports")
    assert not broker.get("expose")
    assert broker["environment"]["BROKER_HOST"] == "0.0.0.0"   # inside the container only
    assert not cfg["services"]["scheduler"].get("ports")
    for svc in ("web", "scheduler"):
        assert cfg["services"][svc]["environment"]["BROKER_URL"] == "http://broker:8787"
    # only the broker gets the Alpaca keys: the alpaca env file is referenced exactly once, in broker's block
    text = BASE.read_text()
    assert text.count("ALPACA_ENV_FILE") == 1
    assert text.index("ALPACA_ENV_FILE") < text.index("  web:")


@needs_docker
def test_base_compose_web_port_is_loopback_by_default_and_configurable():
    ports = compose_config(BASE)["services"]["web"]["ports"]
    assert len(ports) == 1 and ports[0]["host_ip"] == "127.0.0.1" and ports[0]["target"] == 8010
    ports = compose_config(BASE, extra_env={"WEB_BIND": "100.64.0.19", "WEB_PORT": "18010"})["services"]["web"]["ports"]
    assert ports[0]["host_ip"] == "100.64.0.19" and str(ports[0]["published"]) == "18010"


@needs_docker
def test_services_depend_on_healthy_broker_and_restart():
    cfg = compose_config(BASE)["services"]
    for svc in ("web", "scheduler"):
        assert cfg[svc]["depends_on"]["broker"]["condition"] == "service_healthy"
    for svc in cfg.values():
        assert svc["restart"] == "unless-stopped" and svc.get("healthcheck")
        assert any(v["target"] == "/app/data" for v in svc["volumes"])
        lim = [v for v in svc["volumes"] if v["target"] == "/app/config/limits.json"]
        assert lim and lim[0].get("read_only") is True
        assert any("host.docker.internal" in h for h in svc["extra_hosts"])


@needs_docker
def test_traefik_overlay_validates_and_only_touches_web():
    base, over = compose_config(BASE), compose_config(BASE, TRAEFIK)
    web = over["services"]["web"]
    assert not web.get("ports")                                   # host publish removed
    assert "traefik" in web["networks"] and "default" in web["networks"]
    lab = web["labels"]
    assert lab["traefik.enable"] == "true"
    assert lab["traefik.http.routers.thesis.rule"] == "Host(`trader.example.ts.net`)"
    assert lab["traefik.http.routers.thesis.entrypoints"] == "websecure"
    assert "traefik.http.routers.thesis.tls.certresolver" not in lab          # opt-in (commented line)
    assert lab["traefik.http.services.thesis.loadbalancer.server.port"] == "8010"
    assert lab["traefik.docker.network"] == "proxy"
    assert over["networks"]["traefik"]["external"] is True and over["networks"]["traefik"]["name"] == "proxy"
    for svc in ("broker", "scheduler"):                           # untouched
        assert over["services"][svc] == base["services"][svc]
        assert not over["services"][svc].get("labels")
    assert "traefik" not in over["services"]["broker"]["networks"]


@needs_docker
def test_traefik_overlay_entrypoint_is_parameterized():
    web = compose_config(BASE, TRAEFIK, extra_env={"TRAEFIK_ENTRYPOINT": "web", "TRAEFIK_TLS": "false"})["services"]["web"]
    assert web["labels"]["traefik.http.routers.thesis.entrypoints"] == "web"
    assert web["labels"]["traefik.http.routers.thesis.tls"] == "false"


@needs_docker
def test_traefik_overlay_requires_trader_host():
    env = {k: v for k, v in ENV.items() if k != "TRADER_HOST"}
    r = subprocess.run(["docker", "compose", "-f", str(BASE), "-f", str(TRAEFIK), "config"], capture_output=True,
                       text=True, env=env, cwd=ROOT)
    assert r.returncode != 0 and "TRADER_HOST" in r.stderr


def test_env_example_covers_every_variable_the_compose_files_use():
    import re
    used = set(re.findall(r"\$\{([A-Z_]+)", BASE.read_text() + TRAEFIK.read_text()))
    example = (DEPLOY / ".env.example").read_text()
    missing = [v for v in sorted(used) if v not in example]
    assert not missing, missing


def test_switch_script_parses_and_status_is_read_only():
    sh = DEPLOY / "switch.sh"
    assert subprocess.run(["bash", "-n", str(sh)]).returncode == 0
    text = sh.read_text()
    body = text[text.index("do_status()"):text.index("prepare_docker()")]
    for bad in ("disable", "enable", " stop", " start", " rm ", "mkdir", " up", "down"):
        assert bad not in body, bad
    assert subprocess.run([str(sh), "bogus"], capture_output=True).returncode == 2


def test_dockerfile_basics():
    df = (DEPLOY / "Dockerfile").read_text()
    assert df.startswith("# One image") and "FROM python:3.12-slim" in df
    assert "TZ=America/New_York" in df and "tzdata" in df and "USER thesis" in df
    assert "claude" not in df.lower().replace("claude.md", "").replace("no claude cli", "")
    ign = (ROOT / ".dockerignore").read_text().split()
    for p in (".venv", "data", "config/*.env", ".git"):
        assert p in ign


# ---------------------------------------------------------------- doctor, docker mode

def _doctor(tmp_path, monkeypatch, docker_out, units_active, alive_age_s):
    import doctor as dr
    from trader import db
    monkeypatch.setattr(dr, "USER_DIR", tmp_path / "u")
    monkeypatch.setattr(dr, "SYSTEM_DIR", tmp_path / "s")
    (tmp_path / "u").mkdir()
    (tmp_path / "u" / "thesis-broker.service").write_text("x")
    conn = db.connect()
    if alive_age_s is not None:
        db.set_setting(conn, "cron_alive", (datetime.now(timezone.utc) - timedelta(seconds=alive_age_s)).isoformat())
    conn.close()

    def run(cmd, timeout=30):
        if cmd[0] == "docker":
            return 0, docker_out
        if "is-active" in cmd:
            return (0, "active") if cmd[-1] in units_active else (3, "inactive")
        return 1, ""
    monkeypatch.setattr(dr.Doctor, "_run", staticmethod(run))
    from trader import config
    return dr.Doctor(db_path=config.DB_PATH), dr


HEALTHY = "broker\tUp 2 minutes (healthy)\nweb\tUp 2 minutes (healthy)\nscheduler\tUp 2 minutes (healthy)"


def test_doctor_autodetects_docker_and_passes(tmp_path, monkeypatch):
    d, dr = _doctor(tmp_path, monkeypatch, HEALTHY, [], 20)
    assert d.mode == "docker"
    out = d.run_all()
    st = {c["check"]: c["status"] for c in out["checks"]}
    assert st["exclusive"] == "PASS"
    assert st["container_broker"] == st["container_web"] == st["container_scheduler"] == "PASS"
    assert st["scheduler_loop"] == "PASS"


def test_doctor_fails_when_systemd_also_active(tmp_path, monkeypatch):
    d, dr = _doctor(tmp_path, monkeypatch, HEALTHY, ["thesis-broker.service"], 20)
    out = d.run_all()
    st = {c["check"]: c for c in out["checks"]}
    assert st["exclusive"]["status"] == "FAIL" and "BOTH" in st["exclusive"]["detail"] and not out["ok"]


def test_doctor_container_and_loop_failures(tmp_path, monkeypatch):
    d, dr = _doctor(tmp_path, monkeypatch, "broker\tUp 1 minute (healthy)\nweb\tUp 1 minute (unhealthy)", [], 3600)
    st = {c["check"]: c["status"] for c in d.run_all()["checks"]}
    assert st["container_web"] == "FAIL" and st["container_scheduler"] == "FAIL"
    assert st["scheduler_loop"] == "FAIL"


def test_doctor_no_heartbeat_fails(tmp_path, monkeypatch):
    d, dr = _doctor(tmp_path, monkeypatch, HEALTHY, [], None)
    assert {c["check"]: c["status"] for c in d.run_all()["checks"]}["scheduler_loop"] == "FAIL"


def test_doctor_systemd_mode_flags_running_containers(tmp_path, monkeypatch):
    d, dr = _doctor(tmp_path, monkeypatch, HEALTHY, ["thesis-web.service"], 20)
    d.mode = "user"
    st = {c["check"]: c for c in d.run_all()["checks"]}
    assert st["exclusive"]["status"] == "FAIL"


def test_doctor_without_docker_keeps_systemd_mode(tmp_path, monkeypatch):
    d, dr = _doctor(tmp_path, monkeypatch, "", [], 20)
    assert d.mode == "user"
