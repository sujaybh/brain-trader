#!/usr/bin/env python3
"""Health check for the scheduled setup. Read-only: renders the unit templates into a temp dir
and verifies them, queries systemd (show / is-enabled / is-active only), GETs broker /health
and Ollama /api/tags, and opens the DB with a read-only connection. Changes nothing.

Prints one JSON document: {"ok", "mode", "summary": {PASS, WARN, FAIL, INFO}, "checks": [...]}
with one {"check", "status", "detail"} per check. Exit 0 when nothing FAILed, 1 otherwise.

  .venv/bin/python scheduler/doctor.py            # auto-detects user vs system units
  .venv/bin/python scheduler/doctor.py --mode system
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

import requests  # noqa: E402

from trader import config  # noqa: E402
from trader.client import BrokerClient, BrokerError  # noqa: E402

ROOT = config.ROOT
TEMPLATES = ROOT / "scheduler" / "systemd"
RENDER = ROOT / "scheduler" / "render-units.sh"
USER_DIR = Path.home() / ".config" / "systemd" / "user"
SYSTEM_DIR = Path("/etc/systemd/system")
ALWAYS_ON = ("thesis-broker.service", "thesis-web.service")
# unit files the systemd user manager would not have / that only make sense system-wide
SYSTEM_ONLY_TARGETS = ("network-online.target",)
STALE_DAYS = {"tick": 4, "nightly": 4}  # a long weekend is fine; longer means something is off
SYSTEMD_PATH = "/usr/local/bin:/usr/bin:/bin"


class Doctor:
    def __init__(self, mode: str | None = None, run=None, db_path: Path | None = None, now: datetime | None = None):
        self.checks: list[dict] = []
        self.run = run or self._run
        self.db_path = Path(db_path or config.DB_PATH)
        self.now = now or datetime.now(timezone.utc)
        detected = self.detect_mode()
        self.mode = mode or detected

    # ------------------------------------------------------------ plumbing
    @staticmethod
    def _run(cmd: list[str], timeout: float = 30) -> tuple[int, str]:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return p.returncode, (p.stdout + p.stderr).strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            return 127, f"{type(e).__name__}: {e}"

    def add(self, check: str, status: str, detail, **extra) -> None:
        self.checks.append({"check": check, "status": status, "detail": detail, **extra})

    def systemctl(self, *args: str) -> list[str]:
        return ["systemctl", *(["--user"] if self.mode == "user" else []), *args]

    def unit_names(self) -> list[str]:
        return sorted(p.name for p in TEMPLATES.iterdir() if p.suffix in (".service", ".timer"))

    def installed_dir(self) -> Path:
        return USER_DIR if self.mode == "user" else SYSTEM_DIR

    def detect_mode(self) -> str:
        u = (USER_DIR / "thesis-broker.service").exists()
        s = (SYSTEM_DIR / "thesis-broker.service").exists()
        self._both = u and s
        self._none = not (u or s)
        return "system" if s and not u else "user"

    # ------------------------------------------------------------ unit files
    def render(self, flavor: str, dest: Path) -> tuple[bool, str]:
        rc, out = self.run(["bash", str(RENDER), flavor, str(dest)])
        return rc == 0, out

    def check_install_mode(self) -> None:
        if getattr(self, "_both", False):
            self.add("install_mode", "FAIL", "both user (~/.config/systemd/user) and system (/etc/systemd/system) "
                     "units are installed: two brokers / two schedulers. Remove one set (scheduler/uninstall.sh).")
        elif getattr(self, "_none", False):
            self.add("install_mode", "FAIL", "no thesis-broker.service installed (run scheduler/install-user.sh or "
                     "sudo scheduler/install.sh)", mode=self.mode)
        else:
            self.add("install_mode", "PASS", f"{self.mode} units in {self.installed_dir()}", mode=self.mode)

    def check_verify(self, flavor: str) -> None:
        name = f"units_verify_{flavor}"
        if not shutil.which("systemd-analyze"):
            self.add(name, "WARN", "systemd-analyze not found")
            return
        with tempfile.TemporaryDirectory(prefix="thesis-units-") as td:
            ok, out = self.render(flavor, Path(td))
            if not ok:
                self.add(name, "FAIL", f"render-units.sh {flavor} failed: {out[-500:]}")
                return
            files = sorted(str(p) for p in Path(td).iterdir())
            bad = [Path(f).name for f in files if flavor == "user"
                   and any(t in Path(f).read_text() for t in SYSTEM_ONLY_TARGETS)]
            rc, out = self.run(["systemd-analyze", *(["--user"] if flavor == "user" else []), "verify", *files], 60)
        out = out.replace(td + "/", "")
        if rc != 0:
            self.add(name, "FAIL", out[-1500:] or f"exit {rc}")
        elif bad:
            self.add(name, "FAIL", f"user units reference system-only targets: {bad}")
        elif out:
            self.add(name, "WARN", out[-1500:])
        else:
            self.add(name, "PASS", f"{len(files)} units verify clean")

    def check_calendars(self) -> None:
        for unit in self.unit_names():
            if not unit.endswith(".timer"):
                continue
            for expr in re.findall(r"^OnCalendar=(.+)$", (TEMPLATES / unit).read_text(), re.M):
                rc, out = self.run(["systemd-analyze", "calendar", expr.strip()])
                m = re.search(r"Next elapse:\s*(.+)", out)
                if rc == 0 and m:
                    self.add(f"calendar {unit}", "PASS", f"{expr.strip()} -> next {m.group(1).strip()}")
                else:
                    self.add(f"calendar {unit}", "FAIL", f"{expr.strip()}: {out[-300:]}")

    def check_installed(self) -> None:
        if getattr(self, "_none", False):
            return
        d = self.installed_dir()
        with tempfile.TemporaryDirectory(prefix="thesis-units-") as td:
            ok, _ = self.render(self.mode, Path(td))
            fresh = {p.name: p.read_text() for p in Path(td).iterdir()} if ok else {}
        for unit in self.unit_names():
            path = d / unit
            if not path.exists():
                self.add(f"installed {unit}", "FAIL", f"not installed in {d}; re-run the installer")
                continue
            text = path.read_text()
            wd = re.search(r"^WorkingDirectory=(.+)$", text, re.M)
            notes = []
            status = "PASS"
            want = fresh.get(unit)
            if wd and Path(wd.group(1).strip()) != ROOT:
                notes.append(f"points at {wd.group(1).strip()}, not this checkout")
                if want is not None:  # compare as if rendered from that checkout
                    want = want.replace(str(ROOT), wd.group(1).strip())
            if want is not None and want != text:
                status = "WARN"
                notes.append("differs from the current template: re-run the installer")
            if unit.endswith(".timer") or unit in ALWAYS_ON:
                _, en = self.run(self.systemctl("is-enabled", unit))
                if en.strip() != "enabled":
                    status = "FAIL"
                    notes.append(f"is-enabled: {en.strip() or '?'}")
            if unit in ALWAYS_ON:
                _, act = self.run(self.systemctl("is-active", unit))
                if act.strip() != "active":
                    status = "FAIL"
                notes.append(f"is-active: {act.strip()}")
            self.add(f"installed {unit}", status, "; ".join(notes) or "installed, enabled")

    def check_timers(self) -> None:
        if getattr(self, "_none", False):
            return
        for unit in self.unit_names():
            if not unit.endswith(".timer"):
                continue
            svc = unit.replace(".timer", ".service")
            _, t = self.run(self.systemctl("show", unit, "-p", "NextElapseUSecRealtime", "-p", "LastTriggerUSec",
                                           "-p", "ActiveState"))
            _, s = self.run(self.systemctl("show", svc, "-p", "Result", "-p", "ExecMainStatus",
                                           "-p", "ExecMainExitTimestamp"))
            props = dict(line.split("=", 1) for line in (t + "\n" + s).splitlines() if "=" in line)
            detail = {"next": props.get("NextElapseUSecRealtime") or None, "last": props.get("LastTriggerUSec") or None,
                      "timer": props.get("ActiveState"), "last_result": props.get("Result"),
                      "last_exit": props.get("ExecMainStatus")}
            status = "PASS"
            if props.get("ActiveState") != "active" or not detail["next"]:
                status = "FAIL"
            elif props.get("Result") not in (None, "", "success") or props.get("ExecMainStatus") not in (None, "", "0"):
                status = "WARN"
            self.add(f"timer {unit}", status, detail)

    def check_linger(self) -> None:
        if self.mode != "user":
            return
        user = os.environ.get("USER") or ""
        _, out = self.run(["loginctl", "show-user", user, "-p", "Linger"])
        if "Linger=yes" in out:
            self.add("linger", "PASS", f"lingering on for {user}: user units run at boot without a login")
        else:
            self.add("linger", "FAIL", f"lingering off ({out or '?'}): run `sudo loginctl enable-linger {user}`")

    # ------------------------------------------------------------ services
    def check_broker(self) -> None:
        try:
            h = BrokerClient(timeout=3, retry_delays=()).health()
        except BrokerError as e:
            self.add("broker_health", "FAIL", e.reason)
            return
        notes = [f"backend {h.get('backend')}", "paper" if h.get("paper") else "LIVE"]
        status = "PASS" if h.get("ok") else "FAIL"
        if h.get("halted"):
            notes.append("halted")
        self.add("broker_health", status, ", ".join(notes), url=config.BROKER_URL)

    def check_ollama(self) -> None:
        found = []
        for label, url, model in (("primary", config.OLLAMA_URL, config.OLLAMA_MODEL),
                                  ("fallback", config.OLLAMA_FALLBACK_URL, config.OLLAMA_FALLBACK_MODEL)):
            try:
                r = requests.get(f"{url.rstrip('/')}/api/tags", timeout=3)
                r.raise_for_status()
                names = {m.get("name") for m in r.json().get("models", [])}
                found.append((label, url, model, model in names))
            except (requests.RequestException, ValueError) as e:
                self.add(f"ollama_{label}", "WARN", f"{url}: {type(e).__name__}")
                continue
            self.add(f"ollama_{label}", "PASS" if model in names else "WARN",
                     f"{url} up; model {model} {'present' if model in names else 'NOT pulled'}")
        if not found:
            self.add("ollama", "FAIL", "no Ollama reachable: the nightly news step will fail (journaled error)")

    def check_claude(self) -> None:
        from web.chat import claude_bin
        here = claude_bin()
        unit_path = f"{Path.home()}/.local/bin:{SYSTEMD_PATH}"
        old = os.environ.get("PATH")
        os.environ["PATH"] = unit_path
        try:
            in_unit = claude_bin()
            resolved = shutil.which(in_unit) if os.sep not in in_unit else in_unit
            node_needed = False
            if resolved and os.access(resolved, os.X_OK):
                with open(resolved, "rb") as f:
                    head = f.readline(200)
                node_needed = head.startswith(b"#!") and b"node" in head
                node_ok = bool(shutil.which("node")) if node_needed else True
            else:
                node_ok = True
        finally:
            if old is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = old
        if not resolved or not os.access(resolved, os.X_OK):
            self.add("claude_bin", "FAIL", f"`claude` not found with a systemd PATH ({unit_path}); interactive "
                     f"shell finds {here!r}. Set CLAUDE_BIN=/full/path/to/claude in config/trader.env")
        elif not node_ok:
            self.add("claude_bin", "WARN", f"{resolved} is a node script but `node` is not on the unit PATH; "
                     "use the native installer or extend Environment=PATH in thesis-review.service")
        else:
            self.add("claude_bin", "PASS", resolved + (" (via CLAUDE_BIN)" if os.environ.get("CLAUDE_BIN") else ""))

    # ------------------------------------------------------------ data
    def check_data_dir(self) -> None:
        d = Path(config.DATA_DIR)
        if not d.is_dir():
            self.add("data_writable", "FAIL", f"{d} missing")
            return
        ok = os.access(d, os.W_OK | os.X_OK) and (not self.db_path.exists() or os.access(self.db_path, os.W_OK))
        st = d.stat()
        self.add("data_writable", "PASS" if ok else "FAIL",
                 f"{d} mode {oct(st.st_mode & 0o7777)} (writable by this user: {ok})")
        halt = Path(config.HALT_PATH)
        if halt.exists():
            self.add("halt", "WARN", f"{halt} present: all orders stop until you remove it (tools/halt.py off)")
        else:
            self.add("halt", "PASS", "no HALT file")

    def _ro(self) -> sqlite3.Connection | None:
        if not self.db_path.exists():
            return None
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def check_db(self) -> None:
        try:
            conn = self._ro()
        except sqlite3.Error as e:
            self.add("db", "FAIL", f"cannot open {self.db_path} read-only: {e}")
            return
        if conn is None:
            self.add("db", "FAIL", f"{self.db_path} does not exist")
            return
        try:
            self._db_checks(conn)
        except sqlite3.Error as e:
            self.add("db", "FAIL", f"{type(e).__name__}: {e}")
        finally:
            conn.close()

    def _age(self, iso: str | None) -> timedelta | None:
        if not iso:
            return None
        try:
            d = datetime.fromisoformat(iso)
        except ValueError:
            return None
        d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        return self.now - d

    def _db_checks(self, conn: sqlite3.Connection) -> None:
        def setting(k):
            r = conn.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
            return r["value"] if r else None

        # last successful tick: job_ok:tick (written on every clean tick); older DBs only have
        # last_tick:<thesis> (written by any tick, successful or not)
        ok_tick = setting("job_ok:tick")
        any_tick = conn.execute("SELECT MAX(value) v FROM settings WHERE key LIKE 'last_tick:%'").fetchone()["v"]
        self._freshness("last_tick", ok_tick, any_tick, "tick", "last_tick:<thesis> (any run)")

        ok_n = setting("job_ok:nightly")
        r = conn.execute("SELECT ts FROM journal WHERE kind='news' ORDER BY id DESC LIMIT 1").fetchone()
        self._freshness("last_nightly", ok_n, r["ts"] if r else None, "nightly", "last kind=news row")

        since7 = (self.now - timedelta(days=7)).isoformat(timespec="seconds")
        errs = conn.execute("SELECT ts, message FROM journal WHERE kind='error' AND ts>=? ORDER BY id DESC",
                            (since7,)).fetchall()
        since1 = (self.now - timedelta(days=1)).isoformat(timespec="seconds")
        recent = [e for e in errs if e["ts"] >= since1]
        self.add("errors_7d", "WARN" if recent else ("INFO" if errs else "PASS"),
                 f"{len(errs)} error rows in 7d ({len(recent)} in 24h)",
                 latest=[f"{e['ts'][:16]} {e['message'][:160]}" for e in errs[:5]])

        since14 = (self.now - timedelta(days=14)).isoformat(timespec="seconds")
        exp = conn.execute("SELECT ts, thesis_id, message FROM journal WHERE kind='expired' AND ts>=? ORDER BY id",
                           (since14,)).fetchall()
        self.add("expired_slots_14d", "WARN" if exp else "PASS",
                 f"{len(exp)} DCA slot(s) expired in 14d" + (" (machine was off/asleep > catch-up window)" if exp else ""),
                 slots=[f"{e['thesis_id']}: {e['message'][:120]}" for e in exp])

    def _freshness(self, name: str, ok_iso: str | None, fallback_iso: str | None, job: str, fb_label: str) -> None:
        limit = timedelta(days=STALE_DAYS[job])
        age = self._age(ok_iso)
        if age is not None:
            self.add(name, "PASS" if age <= limit else "WARN", f"last successful {job}: {ok_iso} "
                     f"({age.total_seconds() / 3600:.1f}h ago)")
            return
        fb = self._age(fallback_iso)
        if fb is None:
            self.add(name, "WARN", f"no record of a {job} run")
        else:
            self.add(name, "WARN" if fb > limit else "INFO",
                     f"no job_ok:{job} yet (written by this version); {fb_label}: {fallback_iso} "
                     f"({fb.total_seconds() / 3600:.1f}h ago)")

    def check_second_machine(self) -> None:
        self.add("second_machine", "INFO",
                 "cannot be detected from here. If another machine has these units enabled with the same "
                 "Alpaca keys, both will try every DCA slot (client_order_id dedup is per DB, not per account). "
                 "Disable the timers + broker on the old machine (see scheduler/README.md, 'Moving to an "
                 "always-on server').")

    # ------------------------------------------------------------
    def run_all(self) -> dict:
        self.check_install_mode()
        self.check_verify("user")
        self.check_verify("system")
        self.check_calendars()
        self.check_installed()
        self.check_timers()
        self.check_linger()
        self.check_broker()
        self.check_ollama()
        self.check_claude()
        self.check_data_dir()
        self.check_db()
        self.check_second_machine()
        summary = {k: sum(1 for c in self.checks if c["status"] == k) for k in ("PASS", "WARN", "FAIL", "INFO")}
        return {"ok": summary["FAIL"] == 0, "mode": self.mode, "root": str(ROOT), "db": str(self.db_path),
                "summary": summary, "checks": self.checks}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("user", "system"), help="default: auto-detect from installed units")
    ap.add_argument("--db", help="DB path (default from config: TRADER_DATA_DIR/trader.db); opened read-only")
    a = ap.parse_args(argv)
    out = Doctor(mode=a.mode, db_path=Path(a.db) if a.db else None).run_all()
    print(json.dumps(out, indent=2, default=str))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
