#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: AGPL-3.0-only
"""The fleet machine's vitals: what the machine and each world container use, kept locally.

Runs every minute from a systemd timer (fleet-vitals.timer), installed by the
bootstrap and never run from the clone. It runs as its own unprivileged user
(DynamicUser=yes, state in /var/lib/fleet/vitals, no root, no docker socket).
Each pass reads Linux's own counters (/proc, the cgroups, statvfs; nothing is
installed) and appends one sample per metric to a small SQLite file,
<state dir>/vitals.db. What it reads: /proc, /sys/fs/cgroup, /etc/os-release,
/var/run/reboot-required, `apt-get -s upgrade` (a simulation), and
/run/fleet/containers.json, the small file the reconciler (root) writes with
each container's id and world name: this program never asks docker.

- machine, the USE method (use, saturation, errors): cpu busy / steal /
  iowait %, load per core, pressure (PSI) of cpu, memory and io, memory
  available, swap used, OOM kills, disk used and free, network bytes per
  second, packet errors and drops, uptime;
- each running container (a world, Caddy): memory in use and cpu %, from its
  cgroup (never `docker stats`);
- once a day, the facts for the inventory: OS and version, kernel, reboot
  needed, security updates waiting (`apt-get -s`, nothing installed).

Resolutions: 1 minute for 48 hours, 5 minutes for 30 days, 1 hour for 400
days; older rows are deleted, and the file is capped (VITALS_CAP_BYTES): over
the cap the oldest hourly rows go first.

What this file does NOT do: it publishes nothing. The door (fleetd) does not
read it yet. Real-time load and patch state are private by decision; they will
be served only with a short-lived read-only token (a separate change). It keeps
no IP, no name, no wallet and no count of people: machines and containers
only. Python's standard library only; everything outside (files, docker, the
clock) is passed in, so the tests run with fakes.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

STATE_DIR = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/fleet/vitals").split(":")[0])
OUT_DIR = Path("/run/fleet-vitals")  # world-readable copy for the door: numbers only, no names but the worlds'
# (this is what a read token lets fleetd serve; vitals.db itself stays private to this unit)
CONTAINERS_FILE = Path("/run/fleet/containers.json")  # written by reconcile.py, world-readable: names and ids, no secrets
RAW_KEEP_S = 48 * 3600
FIVE_KEEP_S = 30 * 86400
HOUR_KEEP_S = 400 * 86400
VITALS_CAP_BYTES = 50 * 1024 * 1024
FACTS_EVERY_S = 86400
CGROUP = Path("/sys/fs/cgroup")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")


def read(path: Path | str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def parse_cpu(text: str | None) -> dict | None:
    """The first line of /proc/stat as jiffies: user nice system idle iowait irq softirq steal."""
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("cpu "):
            v = [int(x) for x in line.split()[1:9]]
            v += [0] * (8 - len(v))
            return {"user": v[0] + v[1], "system": v[2] + v[5] + v[6], "idle": v[3], "iowait": v[4], "steal": v[7]}
    return None


def cpu_rates(prev: dict | None, cur: dict | None) -> dict:
    if not prev or not cur:
        return {}
    delta = {k: cur[k] - prev[k] for k in cur}
    total = sum(delta.values())
    if total <= 0 or any(d < 0 for d in delta.values()):
        return {}
    return {
        "cpu_busy_pct": 100.0 * (delta["user"] + delta["system"]) / total,
        "cpu_iowait_pct": 100.0 * delta["iowait"] / total,
        "cpu_steal_pct": 100.0 * delta["steal"] / total,
    }


def parse_meminfo(text: str | None) -> dict:
    out: dict[str, float] = {}
    for line in (text or "").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key] = int(parts[0]) * 1024.0
    result = {}
    if "MemAvailable" in out and "MemTotal" in out:
        result["mem_available_bytes"] = out["MemAvailable"]
        result["mem_total_bytes"] = out["MemTotal"]
    if "SwapTotal" in out and "SwapFree" in out:
        result["swap_used_bytes"] = out["SwapTotal"] - out["SwapFree"]
    return result


def parse_psi(text: str | None, prefix: str) -> dict:
    """`some avg10=1.23 ...` of a pressure file: the share of the last 10 s something waited."""
    for line in (text or "").splitlines():
        if line.startswith("some "):
            for part in line.split():
                if part.startswith("avg10="):
                    try:
                        return {f"psi_{prefix}_some_avg10": float(part[6:])}
                    except ValueError:
                        return {}
    return {}


def parse_net(text: str | None) -> dict:
    """Totals over every interface but loopback, docker's bridges and veth pairs (they are the same bytes twice)."""
    rx = tx = errs = drops = 0
    for line in (text or "").splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        if not rest or name == "lo" or name.startswith(("docker", "br-", "veth")):
            continue
        f = rest.split()
        if len(f) < 16:
            continue
        rx += int(f[0]); errs += int(f[2]) + int(f[10]); drops += int(f[3]) + int(f[11]); tx += int(f[8])
    return {"rx": rx, "tx": tx, "errs": errs, "drops": drops}


def net_rates(prev: dict | None, cur: dict, dt: float) -> dict:
    if not prev or dt <= 0 or any(cur[k] < prev.get(k, 0) for k in cur):
        return {}
    return {
        "net_rx_bytes_s": (cur["rx"] - prev["rx"]) / dt,
        "net_tx_bytes_s": (cur["tx"] - prev["tx"]) / dt,
        "net_errors": float(cur["errs"] - prev["errs"]),
        "net_drops": float(cur["drops"] - prev["drops"]),
    }


def parse_vmstat_oom(text: str | None) -> dict:
    for line in (text or "").splitlines():
        if line.startswith("oom_kill "):
            return {"oom_kill_total": float(line.split()[1])}
    return {}


def cgroup_stat_usec(text: str | None) -> int | None:
    for line in (text or "").splitlines():
        if line.startswith("usage_usec "):
            return int(line.split()[1])
    return None


class Env:
    """Everything outside: the files, the clock, docker, the disk. The tests replace these."""

    def __init__(self, dir: Path = STATE_DIR, proc: Path = Path("/proc"), cgroup: Path = CGROUP,
                 now=time.time, run=None, disk_usage=shutil.disk_usage, cores: int | None = None,
                 containers_file: Path = CONTAINERS_FILE, out: Path = OUT_DIR) -> None:
        self.out = out
        self.dir = dir
        self.containers_file = containers_file
        self.proc = proc
        self.cgroup = cgroup
        self.now = now
        self.run = run or self._run
        self.disk_usage = disk_usage
        self.cores = cores or os.cpu_count() or 1

    @staticmethod
    def _run(cmd: list[str], timeout: int = 60) -> str | None:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout if out.returncode == 0 else None


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS m (res INTEGER NOT NULL, ts INTEGER NOT NULL, scope TEXT NOT NULL,"
               " name TEXT NOT NULL, value REAL NOT NULL, PRIMARY KEY (res, ts, scope, name))")
    db.execute("CREATE TABLE IF NOT EXISTS facts (ts INTEGER NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL)")
    return db


def containers(env: Env) -> dict[str, str]:
    """{full container id: name} from the file the reconciler writes; empty if it is missing or odd. Never docker."""
    try:
        data = json.loads(env.containers_file.read_text(encoding="utf-8"))
        listed = data["containers"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    result = {}
    if isinstance(listed, dict):
        for name, cid in listed.items():
            if isinstance(cid, str) and re.fullmatch(r"[0-9a-f]{64}", cid) and NAME.fullmatch(str(name)):
                result[cid] = str(name)
    return result


def cgroup_dir(env: Env, cid: str) -> Path | None:
    for candidate in (env.cgroup / "system.slice" / f"docker-{cid}.scope", env.cgroup / "docker" / cid):
        if candidate.is_dir():
            return candidate
    return None


def collect(env: Env, prev: dict, now: float) -> tuple[dict[str, dict[str, float]], dict]:
    """One pass of readings: {scope: {metric: value}} and the counters to remember for the next pass."""
    machine: dict[str, float] = {}
    keep: dict = {"at": now}
    cpu = parse_cpu(read(env.proc / "stat"))
    if cpu:
        keep["cpu"] = cpu
        machine.update(cpu_rates(prev.get("cpu"), cpu))
    load = read(env.proc / "loadavg")
    if load:
        machine["load1_per_core"] = float(load.split()[0]) / env.cores
    machine.update(parse_meminfo(read(env.proc / "meminfo")))
    for name in ("cpu", "memory", "io"):
        machine.update(parse_psi(read(env.proc / "pressure" / name), name))
    machine.update(parse_vmstat_oom(read(env.proc / "vmstat")))
    up = read(env.proc / "uptime")
    if up:
        machine["uptime_s"] = float(up.split()[0])
    try:
        du = env.disk_usage(env.dir)
        machine["disk_used_pct"] = 100.0 * du.used / du.total if du.total else 0.0
        machine["disk_free_bytes"] = float(du.free)
    except OSError:
        pass
    net = parse_net(read(env.proc / "net" / "dev"))
    keep["net"] = net
    dt = now - prev.get("at", now)
    machine.update(net_rates(prev.get("net"), net, dt))
    scopes: dict[str, dict[str, float]] = {"machine": machine}
    keep["ctr"] = {}
    for cid, name in containers(env).items():
        cg = cgroup_dir(env, cid)
        if cg is None:
            continue
        values: dict[str, float] = {}
        mem = read(cg / "memory.current")
        if mem and mem.strip().isdigit():
            values["mem_bytes"] = float(mem)
        usec = cgroup_stat_usec(read(cg / "cpu.stat"))
        if usec is not None:
            keep["ctr"][cid] = usec
            before = prev.get("ctr", {}).get(cid)
            if before is not None and dt > 0 and usec >= before:
                values["cpu_pct_of_core"] = 100.0 * (usec - before) / 1e6 / dt
        oom = read(cg / "memory.events")
        for line in (oom or "").splitlines():
            if line.startswith("oom_kill "):
                values["oom_kill_total"] = float(line.split()[1])
        if values:
            scopes[f"c:{name}"] = values
    return scopes, keep


def facts(env: Env) -> dict[str, str]:
    """The inventory facts a machine knows about itself. Nothing is installed or changed."""
    out: dict[str, str] = {}
    osr = read("/etc/os-release") or ""
    for line in osr.splitlines():
        if line.startswith("PRETTY_NAME="):
            out["os"] = line.split("=", 1)[1].strip().strip('"')
    kernel = read(env.proc / "sys" / "kernel" / "osrelease")
    if kernel:
        out["kernel"] = kernel.strip()
    out["reboot_required"] = "yes" if Path("/var/run/reboot-required").exists() else "no"
    sim = env.run(["apt-get", "-s", "upgrade"], timeout=120)
    if sim is not None:
        waiting = [ln for ln in sim.splitlines() if ln.startswith("Inst ")]
        out["updates_waiting"] = str(len(waiting))
        out["security_updates_waiting"] = str(sum(1 for ln in waiting if "-security" in ln))
    out["cores"] = str(env.cores)
    return out


def store(db: sqlite3.Connection, now: int, scopes: dict[str, dict[str, float]]) -> None:
    ts = now - now % 60
    db.executemany("INSERT OR REPLACE INTO m VALUES (1, ?, ?, ?, ?)",
                   [(ts, scope, name, float(v)) for scope, vals in scopes.items() for name, v in vals.items()])


def roll_up(db: sqlite3.Connection, now: int) -> None:
    """Average finished 5-minute and 1-hour buckets into the coarser rows, then delete what outlived its resolution."""
    for res, size, source in ((5, 300, 1), (60, 3600, 5)):
        edge = now - now % size
        db.execute(
            "INSERT OR IGNORE INTO m SELECT ?, ts - ts % ?, scope, name, AVG(value) FROM m"
            " WHERE res = ? AND ts < ? AND ts >= ? GROUP BY ts - ts % ?, scope, name",
            (res, size, source, edge, edge - 2 * size, size))
    db.execute("DELETE FROM m WHERE res = 1 AND ts < ?", (now - RAW_KEEP_S,))
    db.execute("DELETE FROM m WHERE res = 5 AND ts < ?", (now - FIVE_KEEP_S,))
    db.execute("DELETE FROM m WHERE res = 60 AND ts < ?", (now - HOUR_KEEP_S,))


EXPORTS = (("minute", 1, 3 * 3600, 60), ("five", 5, 48 * 3600, 300), ("hour", 60, 30 * 86400, 3600))


def export(db: sqlite3.Connection, env: Env, now: int) -> None:
    """Copy the recent rows into world-readable files the door can serve. A coarse file is rewritten only
    when one of its buckets has just closed. No state is lost if the directory is missing: nothing is served."""
    try:
        env.out.mkdir(parents=True, exist_ok=True)
        os.chmod(env.out, 0o755)
    except OSError:
        return
    for label, res, keep_s, size in EXPORTS:
        path = env.out / f"{label}.json"
        if path.exists() and now % size >= 60 and size > 60:
            continue
        rows: dict[str, dict[str, dict[str, float]]] = {}
        for ts, scope, name, value in db.execute(
                "SELECT ts, scope, name, value FROM m WHERE res = ? AND ts >= ? ORDER BY ts", (res, now - keep_s)):
            rows.setdefault(scope, {}).setdefault(name, {})[str(ts)] = round(value, 3)
        body = {"every_s": size, "at": now, "scopes": {s: {n: sorted(p.items(), key=lambda kv: int(kv[0])) for n, p in d.items()}
                                                       for s, d in rows.items()}}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, separators=(",", ":")), encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)


def cap_file(db: sqlite3.Connection, path: Path) -> None:
    """Over the cap the oldest hourly rows go first, then the oldest five-minute rows."""
    for res in (60, 5):
        while path.exists() and path.stat().st_size > VITALS_CAP_BYTES:
            cur = db.execute("DELETE FROM m WHERE rowid IN (SELECT rowid FROM m WHERE res = ? ORDER BY ts LIMIT 5000)", (res,))
            db.commit()
            if cur.rowcount == 0:
                break
            db.execute("VACUUM")


def run_pass(env: Env) -> dict:
    now = env.now()
    env.dir.mkdir(parents=True, exist_ok=True)
    state_file = env.dir / "prev.json"
    try:
        prev = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prev = {}
    scopes, keep = collect(env, prev, now)
    db = open_db(env.dir / "vitals.db")
    try:
        store(db, int(now), scopes)
        last = db.execute("SELECT MAX(ts) FROM facts").fetchone()[0]
        if last is None or now - last >= FACTS_EVERY_S:
            for name, value in facts(env).items():
                db.execute("INSERT INTO facts VALUES (?, ?, ?)", (int(now), name, value))
            db.execute("DELETE FROM facts WHERE ts < ?", (now - HOUR_KEEP_S,))
        roll_up(db, int(now))
        db.commit()
        cap_file(db, env.dir / "vitals.db")
        export(db, env, int(now))
    finally:
        db.close()
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(keep), encoding="utf-8")
    os.replace(tmp, state_file)
    return {"at": int(now), "scopes": len(scopes), "metrics": sum(len(v) for v in scopes.values())}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Keep this machine's own vitals (cpu, memory, disk, network, containers) locally.")
    ap.add_argument("--dir", type=Path, default=STATE_DIR)
    args = ap.parse_args(argv)
    env = Env(args.dir)
    env.dir.mkdir(parents=True, exist_ok=True)
    with open(env.dir / "lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another vitals pass is running", file=sys.stderr)
            return 0
        print(json.dumps(run_pass(env)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
