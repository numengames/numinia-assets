# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""The machine's vitals: counters become rates, containers come from their cgroups, history is rolled up and capped, nothing is published."""

from pathlib import Path
import collections
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MACHINE = ROOT / "open-worlds" / "machine"


def load(name):
    if str(MACHINE) not in sys.path:
        sys.path.insert(0, str(MACHINE))
    spec = importlib.util.spec_from_file_location(name, MACHINE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


V = load("vitals")
CID = "a" * 64
Usage = collections.namedtuple("Usage", "total used free")


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class Fake:
    def __init__(self, tmp, cpu_line, net_line, usec, mem):
        self.tmp = Path(tmp)
        self.proc = self.tmp / "proc"
        self.cg = self.tmp / "cg"
        self.set(cpu_line, net_line, usec, mem)
        write(self.proc / "loadavg", "1.00 0.5 0.2 1/100 5\n")
        write(self.proc / "meminfo", "MemTotal: 4000000 kB\nMemAvailable: 1000000 kB\nSwapTotal: 100 kB\nSwapFree: 40 kB\n")
        write(self.proc / "pressure" / "cpu", "some avg10=3.50 avg60=1.00 avg300=0.50 total=1\n")
        write(self.proc / "vmstat", "nr_free 1\noom_kill 2\n")
        write(self.proc / "uptime", "1000.5 900.0\n")

    def set(self, cpu_line, net_line, usec, mem):
        write(self.proc / "stat", cpu_line)
        write(self.proc / "net" / "dev", "h\nh\n" + net_line)
        scope = self.cg / "system.slice" / f"docker-{CID}.scope"
        write(scope / "cpu.stat", f"usage_usec {usec}\n")
        write(scope / "memory.current", f"{mem}\n")
        write(scope / "memory.events", "oom_kill 1\n")

    def env(self, now):
        def run(cmd, timeout=60):
            if cmd[:2] == ["docker", "ps"]:
                return f"{CID} city-hall\n{'b' * 64} bad name\n"
            return None

        return V.Env(self.tmp / "home", self.proc, self.cg, now=lambda: now, run=run,
                     disk_usage=lambda p: Usage(1000, 400, 600), cores=2)


NET = "lo: 5 0 0 0 0 0 0 0 5 0 0 0 0 0 0 0\neth0: %d 1 2 3 0 0 0 0 %d 1 4 5 0 0 0 0\nveth1: 99 0 0 0 0 0 0 0 99 0 0 0 0 0 0 0\n"


class Vitals(unittest.TestCase):
    def test_rates_containers_and_privacy(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Fake(d, "cpu  100 0 100 800 0 0 0 0\n", NET % (1000, 2000), 1_000_000, 500)
            first = V.run_pass(fake.env(1_000_000))
            self.assertEqual(first["scopes"], 2)
            fake.set("cpu  150 0 150 880 10 0 0 10\n", NET % (7000, 5000), 31_000_000, 700)
            V.run_pass(fake.env(1_000_060))
            db = sqlite3.connect(fake.tmp / "home" / "vitals" / "vitals.db")
            got = {(s, n): v for s, n, v in db.execute("SELECT scope, name, value FROM m WHERE ts = ?", (1_000_060 - 1_000_060 % 60,))}
            self.assertAlmostEqual(got[("machine", "cpu_busy_pct")], 100 * 100 / 200)
            self.assertAlmostEqual(got[("machine", "cpu_steal_pct")], 100 * 10 / 200)
            self.assertAlmostEqual(got[("machine", "net_rx_bytes_s")], 100)
            self.assertAlmostEqual(got[("machine", "net_tx_bytes_s")], 50)
            self.assertEqual(got[("machine", "net_errors")], 0)
            self.assertEqual(got[("machine", "mem_available_bytes")], 1_000_000 * 1024)
            self.assertEqual(got[("machine", "swap_used_bytes")], 60 * 1024)
            self.assertEqual(got[("machine", "psi_cpu_some_avg10")], 3.5)
            self.assertEqual(got[("machine", "load1_per_core")], 0.5)
            self.assertEqual(got[("machine", "disk_used_pct")], 40.0)
            self.assertAlmostEqual(got[("c:city-hall", "cpu_pct_of_core")], 50.0)
            self.assertEqual(got[("c:city-hall", "mem_bytes")], 700)
            self.assertNotIn("c:bad name", {s for s, _ in got})  # a name that is not a name is skipped
            names = {n for _, n in got}
            self.assertFalse({n for n in names if "ip" in n.split("_") or "wallet" in n or "player" in n or "people" in n})

    def test_counter_reset_gives_no_rate(self):
        self.assertEqual(V.cpu_rates({"user": 5, "system": 5, "idle": 5, "iowait": 0, "steal": 0},
                                     {"user": 1, "system": 1, "idle": 1, "iowait": 0, "steal": 0}), {})
        self.assertEqual(V.net_rates({"rx": 10, "tx": 10, "errs": 0, "drops": 0}, {"rx": 1, "tx": 1, "errs": 0, "drops": 0}, 60), {})

    def test_roll_up_and_retention(self):
        with tempfile.TemporaryDirectory() as d:
            db = V.open_db(Path(d) / "v.db")
            base = 10_000_000 - 10_000_000 % 3600
            for i in range(125):  # two hours of one-minute samples, the pass rolling up each minute
                V.store(db, base + i * 60, {"machine": {"x": float(i)}})
                V.roll_up(db, base + i * 60)
            fives = db.execute("SELECT COUNT(*) FROM m WHERE res = 5").fetchone()[0]
            hours = dict(db.execute("SELECT ts, value FROM m WHERE res = 60"))
            self.assertEqual(fives, 24)  # the bucket still filling is not rolled up yet
            self.assertAlmostEqual(hours[base], 29.5)
            V.roll_up(db, base + 49 * 3600)  # raw rows older than 48 h go; the hour rows stay
            self.assertEqual(db.execute("SELECT COUNT(*) FROM m WHERE res = 1 AND ts < ?", (base + 3600,)).fetchone()[0], 0)
            self.assertGreater(db.execute("SELECT COUNT(*) FROM m WHERE res = 60").fetchone()[0], 0)

    def test_cap_drops_oldest_hours_first(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "v.db"
            db = V.open_db(path)
            db.executemany("INSERT INTO m VALUES (60, ?, 'machine', 'x', 1.0)", [(i * 3600,) for i in range(20000)])
            db.execute("INSERT INTO m VALUES (1, 5, 'machine', 'x', 1.0)")
            db.commit()
            old, before = V.VITALS_CAP_BYTES, path.stat().st_size
            V.VITALS_CAP_BYTES = before // 2
            try:
                V.cap_file(db, path)
            finally:
                V.VITALS_CAP_BYTES = old
            self.assertLess(path.stat().st_size, before)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM m WHERE res = 1").fetchone()[0], 1)
            self.assertLess(db.execute("SELECT COUNT(*) FROM m WHERE res = 60").fetchone()[0], 20000)

    def test_facts_once_a_day_and_only_reads(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Fake(d, "cpu  1 0 1 8 0 0 0 0\n", NET % (1, 1), 1, 1)
            write(fake.proc / "sys" / "kernel" / "osrelease", "6.8.0-1\n")
            calls = []
            env = fake.env(2_000_000)
            env.run = lambda cmd, timeout=60: (calls.append(cmd), "Inst a [1] (2 Ubuntu:24.04/noble-security)\nInst b [1] (2 noble-updates)\n" if cmd[0] == "apt-get" else None)[1]
            V.run_pass(env)
            env.now = lambda: 2_000_060
            V.run_pass(env)
            db = sqlite3.connect(fake.tmp / "home" / "vitals" / "vitals.db")
            facts = dict(db.execute("SELECT name, value FROM facts"))
            self.assertEqual(facts["security_updates_waiting"], "1")
            self.assertEqual(facts["updates_waiting"], "2")
            self.assertEqual(facts["kernel"], "6.8.0-1")
            self.assertEqual(sum(1 for c in calls if c[0] == "apt-get"), 1)
            self.assertEqual([c for c in calls if c[0] == "apt-get"][0][:2], ["apt-get", "-s"])  # a simulation, never an install

    def test_door_does_not_serve_it_yet(self):
        text = (MACHINE / "fleetd.py").read_text(encoding="utf-8")
        self.assertNotIn("vitals", text)  # real-time load is private: served only with a token, in a separate change


if __name__ == "__main__":
    unittest.main()
