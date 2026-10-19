# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""The machine's nightly copy: one consistent zip per world per day, 14 days, idempotent, harmless on a low disk."""

from pathlib import Path
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile


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


R = load("reconcile")
N = load("nightly")

DAY = 86400
T0 = 1_800_000_000.0  # a fixed instant


class Nightly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "orders").mkdir()
        self.clock = T0
        self.free = 100 * 1024**3
        self.logs = []
        env = N.Env(self.home)
        env.now = lambda: self.clock
        env.free = lambda _path: self.free
        env.log = self.logs.append
        self.env = env

    def world(self, wid, db=True, files=None):
        (self.home / "orders" / f"{wid}.json").write_text(json.dumps(
            {"id": wid, "domain": f"{wid}.example.org", "image": "ghcr.io/numengames/numinia-engine@sha256:" + "a" * 64}))
        data = self.home / "data" / wid
        data.mkdir(parents=True)
        if db:
            con = sqlite3.connect(data / "db.sqlite")
            con.execute("create table t (v text)")
            con.execute("insert into t values ('hello')")
            con.commit()
            con.close()
        for name, body in (files or {}).items():
            (data / name).parent.mkdir(parents=True, exist_ok=True)
            (data / name).write_bytes(body)
        return data

    def zips(self, wid):
        folder = self.home / "nightly" / wid
        return sorted(p.name for p in folder.glob("*.zip")) if folder.is_dir() else []

    def pass_(self):
        return N.run_pass(self.env)

    def advance(self, days):
        self.clock += days * DAY


class TestCopy(Nightly):
    def test_a_world_gets_one_zip_with_its_files_order_and_manifest(self):
        self.world("a", files={"assets/x.glb": b"model"})
        out = self.pass_()
        self.assertTrue(out["worlds"]["a"].startswith("copied: a-"))
        (name,) = self.zips("a")
        with zipfile.ZipFile(self.home / "nightly" / "a" / name) as z:
            names = set(z.namelist())
            self.assertEqual(names, {"world/db.sqlite", "world/assets/x.glb", "order.json", "world.json"})
            manifest = json.loads(z.read("world.json"))
            self.assertEqual(manifest["format"], "numinia-world-nightly/1")
            self.assertEqual({f["path"] for f in manifest["files"]}, {"world/db.sqlite", "world/assets/x.glb"})
            self.assertEqual(json.loads(z.read("order.json"))["id"], "a")

    def test_the_database_in_the_zip_is_a_consistent_sqlite(self):
        self.world("a")
        self.pass_()
        (name,) = self.zips("a")
        out = Path(self.tmp.name) / "restored.sqlite"
        with zipfile.ZipFile(self.home / "nightly" / "a" / name) as z:
            out.write_bytes(z.read("world/db.sqlite"))
        con = sqlite3.connect(out)
        self.assertEqual(con.execute("pragma integrity_check").fetchone(), ("ok",))
        self.assertEqual(con.execute("select v from t").fetchall(), [("hello",)])
        con.close()

    def test_a_database_with_an_open_writer_still_copies_whole(self):
        data = self.world("a")
        writer = sqlite3.connect(data / "db.sqlite")
        writer.execute("pragma journal_mode=wal")
        writer.execute("insert into t values ('in the wal')")
        writer.commit()
        writer.execute("begin")
        writer.execute("insert into t values ('never committed')")  # a half-done write
        self.pass_()
        writer.rollback()
        writer.close()
        (name,) = self.zips("a")
        out = Path(self.tmp.name) / "r.sqlite"
        with zipfile.ZipFile(self.home / "nightly" / "a" / name) as z:
            out.write_bytes(z.read("world/db.sqlite"))
        con = sqlite3.connect(out)
        self.assertEqual(sorted(v for (v,) in con.execute("select v from t")), ["hello", "in the wal"])
        con.close()

    def test_the_world_folder_is_only_read(self):
        data = self.world("a", files={"assets/x.glb": b"model"})
        before = {p.relative_to(data).as_posix(): (p.stat().st_mtime_ns, p.read_bytes())
                  for p in data.rglob("*") if p.is_file()}
        self.pass_()
        after = {p.relative_to(data).as_posix(): (p.stat().st_mtime_ns, p.read_bytes())
                 for p in data.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(sorted(p.name for p in data.iterdir()), ["assets", "db.sqlite"])

    def test_no_keys_in_the_zip(self):
        self.world("a")
        env_dir = self.home / "env"
        env_dir.mkdir()
        (env_dir / "a.env").write_text("ADMIN_CODE=secret-sentinel\n")
        self.pass_()
        (name,) = self.zips("a")
        with zipfile.ZipFile(self.home / "nightly" / "a" / name) as z:
            self.assertFalse(any(b"secret-sentinel" in z.read(n) for n in z.namelist()))

    def test_symlinks_in_a_world_are_not_followed(self):
        data = self.world("a")
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("not the world's")
        os.symlink(outside, data / "link.txt")
        self.pass_()
        (name,) = self.zips("a")
        with zipfile.ZipFile(self.home / "nightly" / "a" / name) as z:
            self.assertNotIn("world/link.txt", z.namelist())

    def test_every_world_is_copied_a_world_without_database_too(self):
        self.world("a")
        self.world("b", db=False, files={"x.txt": b"1"})
        out = self.pass_()
        self.assertEqual(self.zips("a")[0][:2], "a-")
        self.assertEqual(self.zips("b")[0][:2], "b-")
        self.assertEqual(len(out["worlds"]), 2)

    def test_files_and_folders_are_private(self):
        self.world("a")
        self.pass_()
        folder = self.home / "nightly" / "a"
        self.assertEqual(os.stat(self.home / "nightly").st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(folder).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(folder / self.zips("a")[0]).st_mode & 0o777, 0o600)


class TestIdempotent(Nightly):
    def test_a_second_pass_the_same_day_changes_nothing(self):
        self.world("a")
        self.pass_()
        first = {p: p.stat().st_mtime_ns for p in (self.home / "nightly" / "a").glob("*.zip")}
        self.clock += 3600
        out = self.pass_()
        self.assertEqual(out["worlds"]["a"], "already done today")
        self.assertEqual({p: p.stat().st_mtime_ns for p in (self.home / "nightly" / "a").glob("*.zip")}, first)

    def test_the_next_day_makes_a_new_copy(self):
        self.world("a")
        self.pass_()
        self.advance(1)
        self.pass_()
        self.assertEqual(len(self.zips("a")), 2)

    def test_a_pass_that_was_killed_leaves_no_harm(self):
        self.world("a")
        folder = self.home / "nightly" / "a"
        folder.mkdir(parents=True)
        (folder / ".a-20270101-000000.zip.part").write_bytes(b"half")
        (folder / ".a-20270101-000000.sqlite.tmp").write_bytes(b"half")
        self.pass_()
        self.assertEqual(len(self.zips("a")), 1)
        self.assertEqual([p.name for p in folder.glob(".*")], [])

    def test_a_broken_world_does_not_stop_the_others(self):
        self.world("a")
        data = self.world("b")
        (data / "db.sqlite").write_bytes(b"this is not a database at all" * 100)
        self.world("c")
        out = self.pass_()
        self.assertTrue(out["worlds"]["b"].startswith("failed"))
        self.assertEqual(len(self.zips("a")), 1)
        self.assertEqual(len(self.zips("c")), 1)
        self.assertEqual(self.zips("b"), [])
        self.assertEqual([p.name for p in (self.home / "nightly" / "b").glob(".*")], [])

    def test_an_order_without_a_folder_is_skipped(self):
        self.world("a")
        (self.home / "orders" / "ghost.json").write_text(json.dumps({"id": "ghost", "domain": "g.example.org", "image": "x"}))
        out = self.pass_()
        self.assertEqual(out["worlds"]["ghost"], "skipped: no folder")
        self.assertEqual(len(self.zips("a")), 1)


class TestRetention(Nightly):
    def test_copies_are_kept_14_days_and_older_ones_go(self):
        self.world("a")
        for _ in range(20):
            self.pass_()
            self.advance(1)
        names = self.zips("a")
        days = {n.split("-")[-2] for n in names}
        self.assertEqual(len(names), len(days))
        # 20 nights were made; the oldest kept is no older than 14 full days
        oldest = N.day_start(min(days))
        self.assertLessEqual(len(names), 15)
        self.assertGreaterEqual(len(names), 14)
        self.assertGreaterEqual(oldest, (self.clock - DAY) - 15 * DAY)

    def test_the_newest_copy_is_never_deleted_however_old(self):
        self.world("a")
        self.pass_()
        self.advance(60)
        # the world's folder vanished, so no new copy; the old one must stay
        os_rename = self.home / "data" / "a"
        os_rename.rename(self.home / "data" / "a-away")
        self.pass_()
        self.assertEqual(len(self.zips("a")), 1)

    def test_a_removed_worlds_copies_age_out_but_the_last_stays(self):
        self.world("a")
        for _ in range(3):
            self.pass_()
            self.advance(1)
        (self.home / "orders" / "a.json").unlink()
        self.advance(40)
        self.world("b")
        self.pass_()
        self.assertEqual(len(self.zips("a")), 1)
        self.assertEqual(self.zips("a")[0].split("-")[-2], N.day_of(T0 + 2 * DAY))

    def test_files_that_are_not_ours_are_left_alone(self):
        self.world("a")
        folder = self.home / "nightly" / "a"
        folder.mkdir(parents=True)
        (folder / "notes.txt").write_text("mine")
        (folder / "a-20200101-000000.zip.bak").write_text("mine too")
        self.advance(1)
        self.pass_()
        self.assertEqual((folder / "notes.txt").read_text(), "mine")
        self.assertTrue((folder / "a-20200101-000000.zip.bak").exists())


class TestLowDisk(Nightly):
    def test_a_low_disk_skips_logs_and_deletes_nothing(self):
        self.world("a", files={"big.bin": b"x" * 10_000})
        self.pass_()
        before = self.zips("a")
        self.advance(1)
        self.free = N.MIN_FREE_BYTES - 1
        out = self.pass_()
        self.assertTrue(out["worlds"]["a"].startswith("skipped: low disk"))
        self.assertEqual(self.zips("a"), before)  # nothing made, nothing taken
        self.assertTrue(any("low disk" in line for line in self.logs))
        self.assertFalse((self.home / "data" / "a").stat().st_size == 0)

    def test_the_size_of_the_world_counts(self):
        self.world("a", files={"big.bin": b"x" * 5_000})
        self.free = N.MIN_FREE_BYTES + 100  # fine for nothing, not for 5 kB
        out = self.pass_()
        self.assertTrue(out["worlds"]["a"].startswith("skipped: low disk"))
        self.free = N.MIN_FREE_BYTES + 1_000_000
        self.logs.clear()
        out = self.pass_()
        self.assertTrue(out["worlds"]["a"].startswith("copied"))

    def test_pruning_still_runs_on_a_low_disk(self):
        self.world("a")
        self.pass_()
        self.advance(1)
        self.pass_()
        self.advance(30)
        self.free = 0
        self.pass_()
        self.assertEqual(len(self.zips("a")), 1)  # the old one went, the newest stays


class TestMain(Nightly):
    def test_main_runs_a_pass_and_exits_0_then_a_second_run_is_a_noop(self):
        self.world("a")
        self.assertEqual(N.main(["--home", str(self.home)]), 0)
        self.assertEqual(len(self.zips("a")), 1)
        self.assertEqual(N.main(["--home", str(self.home)]), 0)
        self.assertEqual(len(self.zips("a")), 1)
        state = json.loads((self.home / "nightly" / "state.json").read_text())
        self.assertEqual(state["worlds"]["a"], "already done today")

    def test_main_exits_1_when_a_world_failed(self):
        data = self.world("a")
        (data / "db.sqlite").write_bytes(b"garbage" * 1000)
        self.assertEqual(N.main(["--home", str(self.home)]), 1)

    def test_a_second_pass_running_at_once_yields(self):
        import fcntl
        self.world("a")
        (self.home / "nightly").mkdir()
        with open(self.home / "nightly" / "lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(N.main(["--home", str(self.home)]), 0)
        self.assertEqual(self.zips("a"), [])


class TestBootstrapWiring(unittest.TestCase):
    text = (MACHINE / "bootstrap.sh").read_text()

    def test_installed_not_run_from_the_clone(self):
        self.assertIn("nightly.py recipe.py; do", self.text)
        self.assertIn("wrapper fleet-nightly nightly.py", self.text)
        self.assertIn("ExecStart=/usr/local/sbin/fleet-nightly", self.text)

    def test_the_timer_is_nightly_persistent_and_started_and_checked(self):
        self.assertIn("OnCalendar=*-*-* 03:30:00", self.text)
        self.assertIn("Persistent=true", self.text)
        self.assertIn("enable --now fleet-reconcile.timer fleet-reconcile.path fleet-release.timer fleet-nightly.timer", self.text)
        check = self.text.index("== does the machine obey?")
        self.assertIn("fleet-nightly.timer fleet-recipe.timer; do", self.text[check:])

    def test_the_timer_is_paused_while_programs_change(self):
        self.assertIn("systemctl stop fleet-reconcile.timer fleet-reconcile.path fleet-release.timer fleet-nightly.timer", self.text)

    def test_the_service_is_gentle_with_the_machine(self):
        service = self.text[self.text.index("fleet-nightly.service <<EOF"):]
        service = service[:service.index("\nEOF\n")]
        self.assertIn("Type=oneshot", service)
        self.assertIn("Nice=19", service)
        self.assertIn("IOSchedulingClass=idle", service)

    def test_the_readme_no_longer_says_there_is_no_nightly_copy(self):
        readme = (MACHINE / "README.md").read_text()
        self.assertNotIn("No nightly copy yet", readme)
        self.assertIn("## Nightly copy (`nightly.py`)", readme)


if __name__ == "__main__":
    unittest.main()
