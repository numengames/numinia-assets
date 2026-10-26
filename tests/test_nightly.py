# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""The machine's nightly copy: one consistent zip per world per day, 14 days, idempotent, harmless on a low disk.

A Hyperfy world's zip is the machine's own; a Numinia Engine world's is the engine's own export (`copia`) with its
order beside it, so the room can bring it back. Docker is the one thing faked: `FakeCopia` answers as the engine
image would, from the world's folder it is handed.
"""

from pathlib import Path
from unittest import mock
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
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
HYPERFY = "ghcr.io/numengames/numinia-hyperfy2:sha-0e2045370989d051e73f81c7c9d53121d5c374b3"
ENGINE = "ghcr.io/numengames/numinia-engine@sha256:" + "a" * 64
LIMITS = {"memory": "4g", "cpus": "1", "maxUploadMb": 50}


class FakeCopia:
    """Answers as the engine image's `copia` would: the world's mundo@2 zip, written from the folder mounted at /mundo."""

    def __init__(self, exit_code=0, body=None, error=None):
        self.exit_code, self.body, self.error, self.calls = exit_code, body, error, []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if self.error:
            raise self.error
        if self.exit_code:
            kwargs["stdout"].write(b"half a zip")
            return subprocess.CompletedProcess(cmd, self.exit_code, None, b"docker: no such image\n")
        if self.body is not None:
            kwargs["stdout"].write(self.body)
            return subprocess.CompletedProcess(cmd, 0, None, b"")
        folder = Path(cmd[cmd.index("-v") + 1].rsplit(":/mundo", 1)[0])
        listed, blobs = [], {}
        for part in ("datos", "aventura", "entorno"):
            for path in sorted((folder / part).rglob("*")) if (folder / part).is_dir() else []:
                if path.is_file() and not path.name.startswith("claves"):
                    blob = path.read_bytes()
                    rel = path.relative_to(folder).as_posix()
                    blobs[rel] = blob
                    listed.append({"ruta": rel, "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()})
        manifest = {"esquema": "numinia-engine/mundo@2", "motor": {"build": "t"}, "creado": "2027-01-15T08:00:00Z", "ficheros": listed}
        with zipfile.ZipFile(kwargs["stdout"], "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("mundo.json", json.dumps(manifest))
            for rel, blob in blobs.items():
                z.writestr(rel, blob)
            z.writestr("LEEME.txt", "run it yourself")
        return subprocess.CompletedProcess(cmd, 0, None, b"")


def time_stamp(now):
    return N.time.strftime("%Y%m%d-%H%M%S", N.time.gmtime(now))


def zip_bytes(files):
    import io
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return out.getvalue()


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

    def world(self, wid, db=True, files=None, image=HYPERFY):
        (self.home / "orders" / f"{wid}.json").write_text(json.dumps(
            {"id": wid, "domain": f"{wid}.example.org", "image": image, "limits": LIMITS}))
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

    def engine_world(self, wid, files=None):
        """A Numinia Engine world's folder: datos/, aventura/, entorno/, and what must never travel."""
        files = files or {
            "datos/objetos.json": b'{"state": []}',
            "datos/misiones.jsonl": b'{"op": "s"}\n',
            "aventura/ejemplo/mision.json": b'{"id": "m1"}',
            "entorno/entorno.json": b'{"cielo": "alba"}',
            "claves.env": b"ADMIN_CODE=secret-sentinel\n",
            "sesiones/2027.jsonl": b'{"quien": "visitor-sentinel"}\n',
        }
        return self.world(wid, db=False, files=files, image=ENGINE)

    def copia(self, fake=None):
        """Put a fake `copia` where the machine runs the engine's tools; the fake, to read what it was asked."""
        fake = fake or FakeCopia()
        patch = mock.patch.object(R, "run_tool", fake)
        patch.start()
        self.addCleanup(patch.stop)
        return fake

    def left(self, wid):
        """Everything in a world's copies folder, hidden files too."""
        folder = self.home / "nightly" / wid
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []

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


class TestEngineWorld(Nightly):
    def test_an_engine_world_gets_the_engines_own_zip_and_its_order_beside_it(self):
        self.engine_world("m")
        fake = self.copia()
        out = self.pass_()
        self.assertTrue(out["worlds"]["m"].startswith("copied: m-"))
        (name,) = self.zips("m")
        folder = self.home / "nightly" / "m"
        stem = name[: -len(".zip")]
        self.assertEqual(self.left("m"), [f"{stem}.order.json", name])
        with zipfile.ZipFile(folder / name) as z:
            names = z.namelist()
            self.assertEqual(names[0], "mundo.json")  # at the root, where the room and the engine look for it
            self.assertNotIn("order.json", names)  # nothing of ours is in the engine's zip: its importer refuses extras
            self.assertNotIn("world.json", names)
            manifest = json.loads(z.read("mundo.json"))
            self.assertEqual(manifest["esquema"], "numinia-engine/mundo@2")
            self.assertEqual({f["ruta"] for f in manifest["ficheros"]},
                             {"datos/objetos.json", "datos/misiones.jsonl", "aventura/ejemplo/mision.json", "entorno/entorno.json"})
            self.assertFalse(any(b"secret-sentinel" in z.read(n) or b"visitor-sentinel" in z.read(n) for n in names))
        side = json.loads((folder / f"{stem}.order.json").read_text())
        self.assertEqual(side, {"format": "numinia-world-nightly-order/1", "world": "m", "copiedAt": R.utc(T0),
                                "order": {"id": "m", "domain": "m.example.org", "image": ENGINE, "limits": LIMITS}})
        # the engine's own tool, in the machine's own sandbox: the folder read-only, no network, the world's limits
        (cmd,) = fake.calls
        self.assertEqual(cmd[-1], "copia")
        self.assertEqual(cmd[cmd.index("--network") + 1], "none")
        self.assertIn(f"{self.home}/data/m:/mundo:ro", cmd)
        self.assertEqual(cmd[cmd.index("--memory") + 1], "4g")

    def test_files_and_names_are_as_before_and_private(self):
        self.engine_world("m")
        self.copia()
        self.pass_()
        folder = self.home / "nightly" / "m"
        (name,) = self.zips("m")
        self.assertRegex(name, r"m-\d{8}-\d{6}\.zip")
        self.assertEqual(name, f"m-{time_stamp(T0)}.zip")
        for path in folder.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, path.name)
        self.assertEqual(os.stat(folder).st_mode & 0o777, 0o700)

    def test_a_sidecar_is_never_taken_for_a_zip(self):
        self.assertIsNone(N.ZIP_NAME.fullmatch("m-20270115-080000.order.json"))
        self.assertIsNotNone(N.ORDER_NAME.fullmatch("m-20270115-080000.order.json"))
        self.assertIsNone(N.ORDER_NAME.fullmatch("m-20270115-080000.zip"))

    def test_the_world_folder_is_only_read(self):
        data = self.engine_world("m")
        self.copia()
        before = {p.relative_to(data).as_posix(): (p.stat().st_mtime_ns, p.read_bytes()) for p in data.rglob("*") if p.is_file()}
        self.pass_()
        after = {p.relative_to(data).as_posix(): (p.stat().st_mtime_ns, p.read_bytes()) for p in data.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_a_hyperfy_world_is_copied_as_ever_and_the_engine_is_never_asked(self):
        self.world("h", files={"assets/x.glb": b"model"})
        self.engine_world("m")
        fake = self.copia()
        self.pass_()
        (name,) = self.zips("h")
        self.assertEqual(self.left("h"), [name])  # no sidecar
        with zipfile.ZipFile(self.home / "nightly" / "h" / name) as z:
            self.assertEqual(set(z.namelist()), {"world/db.sqlite", "world/assets/x.glb", "order.json", "world.json"})
        self.assertEqual([c[-3:] for c in fake.calls], [[f"{self.home}/data/m:/mundo:ro", ENGINE, "copia"]])

    def test_a_second_pass_the_same_day_changes_nothing(self):
        self.engine_world("m")
        fake = self.copia()
        self.pass_()
        first = {p.name: p.stat().st_mtime_ns for p in (self.home / "nightly" / "m").iterdir()}
        self.clock += 3600
        out = self.pass_()
        self.assertEqual(out["worlds"]["m"], "already done today")
        self.assertEqual({p.name: p.stat().st_mtime_ns for p in (self.home / "nightly" / "m").iterdir()}, first)
        self.assertEqual(len(fake.calls), 1)

    def test_a_copy_of_today_in_the_old_shape_is_not_todays_and_a_proper_one_is_made(self):
        self.engine_world("m")
        folder = self.home / "nightly" / "m"
        folder.mkdir(parents=True)
        old = folder / f"m-{N.day_of(T0)}-000100.zip"  # what the machine used to make for every world
        with zipfile.ZipFile(old, "w") as z:
            z.writestr("world/datos/objetos.json", "{}")
            z.writestr("order.json", "{}")
            z.writestr("world.json", "{}")
        self.copia()
        out = self.pass_()
        self.assertTrue(out["worlds"]["m"].startswith("copied: m-"))
        new = [n for n in self.zips("m") if n != old.name]
        self.assertEqual(len(new), 1)
        self.assertTrue(old.exists())  # old ones age out as any; none is deleted early
        with zipfile.ZipFile(folder / new[0]) as z:
            self.assertIn("mundo.json", z.namelist())
        self.assertEqual(self.pass_()["worlds"]["m"], "already done today")

    def test_an_engine_zip_of_today_without_its_order_file_is_not_done_either(self):
        self.engine_world("m")
        self.copia()
        self.pass_()
        (name,) = self.zips("m")
        (self.home / "nightly" / "m" / f"{name[:-4]}.order.json").unlink()
        self.clock += 60
        self.assertTrue(self.pass_()["worlds"]["m"].startswith("copied: m-"))
        self.assertEqual(len(self.zips("m")), 2)

    def test_a_broken_zip_of_today_does_not_count(self):
        self.engine_world("m")
        folder = self.home / "nightly" / "m"
        folder.mkdir(parents=True)
        (folder / f"m-{N.day_of(T0)}-000100.zip").write_bytes(b"not a zip")
        self.copia()
        self.assertTrue(self.pass_()["worlds"]["m"].startswith("copied"))

    def test_when_copia_fails_the_world_fails_nothing_partial_is_left_and_the_others_get_theirs(self):
        for label, fake in (("exit code", FakeCopia(exit_code=1)),
                            ("not a zip", FakeCopia(body=b"this is not a zip")),
                            ("no mundo.json", FakeCopia(body=zip_bytes({"world/x": b"1", "order.json": b"{}"}))),
                            ("no docker", FakeCopia(error=FileNotFoundError("docker"))),
                            ("timeout", FakeCopia(error=subprocess.TimeoutExpired("docker", 1800)))):
            with self.subTest(label):
                self.setUp()
                self.world("a")
                self.engine_world("m")
                self.world("z")
                self.copia(fake)
                out = self.pass_()
                self.assertTrue(out["worlds"]["m"].startswith("failed: "), out["worlds"]["m"])
                self.assertEqual(self.left("m"), [])
                self.assertEqual(len(self.zips("a")), 1)
                self.assertEqual(len(self.zips("z")), 1)
                self.assertTrue(any("nightly m: failed" in line for line in self.logs))
                self.assertEqual(N.main(["--home", str(self.home)]), 1)

    def test_a_failed_pass_the_next_night_tries_again(self):
        self.engine_world("m")
        patch = mock.patch.object(R, "run_tool", FakeCopia(exit_code=1))
        patch.start()
        self.assertTrue(self.pass_()["worlds"]["m"].startswith("failed"))
        patch.stop()
        self.copia()
        self.assertTrue(self.pass_()["worlds"]["m"].startswith("copied"))

    def test_a_low_disk_skips_and_the_engine_is_never_asked(self):
        self.engine_world("m")
        fake = self.copia()
        self.free = N.MIN_FREE_BYTES - 1
        out = self.pass_()
        self.assertTrue(out["worlds"]["m"].startswith("skipped: low disk"))
        self.assertEqual((fake.calls, self.left("m")), ([], []))

    def test_a_world_without_a_folder_is_skipped(self):
        self.engine_world("m")
        (self.home / "data" / "m").rename(self.home / "data" / "m-away")
        self.copia()
        self.assertEqual(self.pass_()["worlds"]["m"], "skipped: no folder")


class TestEngineRetention(Nightly):
    def nights(self, wid, n):
        for _ in range(n):
            self.pass_()
            self.advance(1)

    def test_a_zip_and_its_order_file_age_out_together_and_the_newest_stays(self):
        self.engine_world("m")
        self.copia()
        self.nights("m", 20)
        names = self.left("m")
        zips = [n for n in names if n.endswith(".zip")]
        sides = [n for n in names if n.endswith(".order.json")]
        self.assertGreaterEqual(len(zips), 14)
        self.assertLessEqual(len(zips), 15)
        self.assertEqual(sorted(z[:-4] for z in zips), sorted(s[: -len(".order.json")] for s in sides))
        self.assertEqual(len(names), 2 * len(zips))

    def test_the_newest_copy_and_its_order_file_are_never_deleted_however_old(self):
        self.engine_world("m")
        self.copia()
        self.pass_()
        self.advance(60)
        (self.home / "data" / "m").rename(self.home / "data" / "m-away")
        self.pass_()
        self.assertEqual(len(self.left("m")), 2)

    def test_an_old_copy_goes_with_its_order_file_and_the_newest_stays_with_its_own(self):
        self.engine_world("m")
        self.copia()
        self.pass_()
        old = self.left("m")
        self.advance(1)
        self.pass_()
        self.advance(30)
        self.free = 0
        self.pass_()
        left = self.left("m")
        self.assertEqual(len(left), 2)
        self.assertTrue(set(left).isdisjoint(old))  # the first night's pair went, the second's stayed

    def test_an_order_file_with_no_zip_is_deleted_and_files_that_are_not_ours_are_not(self):
        self.engine_world("m")
        self.copia()
        folder = self.home / "nightly" / "m"
        folder.mkdir(parents=True)
        (folder / "m-20200101-000000.order.json").write_text("{}")
        (folder / "notes.order.json").write_text("mine")
        (folder / "m-20200101-000000.order.json.bak").write_text("mine too")
        self.pass_()
        self.assertFalse((folder / "m-20200101-000000.order.json").exists())
        self.assertEqual((folder / "notes.order.json").read_text(), "mine")
        self.assertTrue((folder / "m-20200101-000000.order.json.bak").exists())

    def test_the_order_file_of_a_removed_world_goes_with_its_zip_the_last_stays(self):
        self.engine_world("m")
        self.copia()
        self.nights("m", 3)
        (self.home / "orders" / "m.json").unlink()
        self.advance(40)
        self.world("other")
        self.pass_()
        zips = self.zips("m")
        self.assertEqual(len(zips), 1)
        self.assertEqual(len(self.left("m")), 2)
        self.assertEqual(zips[0].split("-")[-2], N.day_of(T0 + 2 * DAY))

    def test_a_pass_that_was_killed_leaves_no_harm(self):
        self.engine_world("m")
        folder = self.home / "nightly" / "m"
        folder.mkdir(parents=True)
        (folder / ".m-20270101-000000.zip.part").write_bytes(b"half")
        (folder / ".m-20270101-000000.order.json.tmp").write_bytes(b"half")
        self.copia()
        self.pass_()
        self.assertEqual(len(self.left("m")), 2)
        self.assertEqual([p.name for p in folder.glob(".*")], [])

    def test_old_copies_in_the_old_shape_age_out_as_any(self):
        self.engine_world("m")
        folder = self.home / "nightly" / "m"
        folder.mkdir(parents=True)
        for day in range(1, 4):  # 1, 2 and 3 days before tonight's: all old shape, no order file
            when = N.day_of(T0 - (day + 40) * DAY)
            with zipfile.ZipFile(folder / f"m-{when}-030000.zip", "w") as z:
                z.writestr("world.json", "{}")
        self.copia()
        self.pass_()
        zips = self.zips("m")
        self.assertEqual(len(zips), 1)  # 40+ days old: gone; tonight's proper one stays
        self.assertEqual(zips[0].split("-")[-2], N.day_of(T0))


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

    def test_only_the_published_folder_is_copied_and_versions_and_the_switching_journal_are_left_alone(self):
        self.world("a", files={"assets/x.glb": b"model"})
        other = self.home / "versions" / "a" / "v2"
        other.mkdir(parents=True)
        (other / "db.sqlite").write_bytes(b"the unpublished one")
        (self.home / "versions" / "a.json").write_text("{}")
        (self.home / "orders" / "b.json").write_text(json.dumps(
            {"id": "b", "domain": "b.example.org", "image": "ghcr.io/numengames/numinia-engine@sha256:" + "a" * 64}))
        (self.home / "run").mkdir()
        # b is between the two renames of a publication: its data/ is not there for a moment.
        (self.home / "run" / "switching.json").write_text(json.dumps({"b": {"nonce": "n" * 32, "from": "v1", "to": "v2"}}))
        out = self.pass_()
        (name,) = self.zips("a")
        with zipfile.ZipFile(self.home / "nightly" / "a" / name) as z:
            self.assertEqual(set(z.namelist()), {"world/db.sqlite", "world/assets/x.glb", "order.json", "world.json"})
            self.assertNotIn(b"the unpublished one", z.read("world/db.sqlite"))
        self.assertEqual(out["worlds"]["b"], "skipped: no folder")
        self.assertEqual(self.zips("b"), [])
        self.assertEqual(sorted(os.listdir(self.home / "versions")), ["a", "a.json"])  # not touched


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
        self.assertIn("nightly.py recipe.py condiciones.py signers.py versions.py cleanup.py; do", self.text)
        self.assertIn("wrapper fleet-nightly nightly.py", self.text)
        self.assertIn("ExecStart=/usr/local/sbin/fleet-nightly", self.text)

    def test_the_timer_is_nightly_persistent_and_started_and_checked(self):
        self.assertIn("OnCalendar=*-*-* 03:30:00", self.text)
        self.assertIn("Persistent=true", self.text)
        self.assertIn("enable --now fleet-reconcile.timer fleet-reconcile.path fleet-release.timer fleet-nightly.timer", self.text)
        check = self.text.index("== does the machine obey?")
        self.assertIn("fleet-nightly.timer fleet-cleanup.timer fleet-recipe.timer; do", self.text[check:])

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
