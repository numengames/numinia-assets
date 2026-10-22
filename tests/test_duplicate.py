# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""DuplicateVersion: a world's version copied into a new one, for both engines.

A Hyperfy world is copied by SQLite's backup and hardlinks; a Numinia Engine
world by its own image's tools (`copia`, `comprobar`, `importar`), faked here
as the engine's tests fake them. The rig is test_versions' Machine: a
temporary fleet home, a throwaway Oracle, requests dropped in the inbox and
taken by the reconciler's own take_requests.

`versions` imports its own copy of `reconcile` (`V.R`), so the stand-ins go on
that module object, and `take_requests` is given this `V` through sys.modules.
"""

from pathlib import Path
import errno
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import unittest
import zipfile
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MACHINE = ROOT / "open-worlds" / "machine"
HERE = Path(__file__).resolve().parent


def load(name, folder=MACHINE):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    spec = importlib.util.spec_from_file_location(name, folder / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


V = load("versions")
Z = load("fleet_zips", HERE)
TV = load("test_versions", HERE)  # its Machine rig and message builders
R = V.R
ENGINE = "ghcr.io/numengames/numinia-engine:sha-0000000000000000000000000000000000000001"
TABLES = ("config", "users", "blueprints", "entities")


def rig(test, engine=False, state="running"):
    """A Machine with a world `plaza` (a real database, assets, a collection) and `versions` as this V."""
    m = TV.Machine(test)
    order = m.put_world()
    data = m.home / "data" / "plaza"
    (data / "db.sqlite").unlink()
    if engine:
        order["image"] = ENGINE
        (data / "datos").mkdir()
        (data / "datos" / "objetos.jsonl").write_bytes(b'{"id":1}\n')
        (data / "aventura").mkdir()
        (data / "aventura" / "una.json").write_bytes(b"{}")
    else:
        Z.world_db(data / "db.sqlite")
        (data / "storage.json").write_bytes(b'{"seen": true}')
        (data / "assets").mkdir()
        (data / "assets" / ("a" * 64 + ".glb")).write_bytes(b"model bytes")
        (data / "collections" / "deep").mkdir(parents=True)
        (data / "collections" / "deep" / "c.json").write_bytes(b"{}")
    order["state"] = state
    (m.orders / "plaza.json").write_text(json.dumps(order), encoding="utf-8")
    patcher = mock.patch.dict(sys.modules, {"versions": V})
    patcher.start()
    test.addCleanup(patcher.stop)
    return m


def ask(m, version="v1", world="plaza"):
    """Sign and take one DuplicateVersion through the reconciler; its result."""
    nonce = m.drop("DuplicateVersion", TV.message("DuplicateVersion", world=world, version=version))
    m.take()
    return m.result(nonce)


def entry_for(m, nonce=None):
    return {"nonce": nonce or os.urandom(16).hex(), "type": "DuplicateVersion", "world": "plaza", "by": m.oracle.address}


def call(m, version="v1", sleep=None, now=TV.NOW):
    """duplicate_version straight: (the rule, the entry)."""
    entry = entry_for(m)
    return V.duplicate_version(m.home, m.orders, entry, {"version": version}, now, sleep), entry


def usage(free):
    """What shutil.disk_usage says of a disk with this much free."""
    return shutil._ntuple_diskusage(100 * R.RESERVE, 0, free)


def rows(path):
    db = sqlite3.connect(str(path))
    try:
        return {t: db.execute(f"select * from {t} order by 1").fetchall() for t in TABLES}
    finally:
        db.close()


def staging_left(m):
    folder = m.home / "staging"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


class FakeEngine:
    """Answers as the engine image's tools would: copia zips the mounted folder, comprobar and importar read it."""

    def __init__(self, copia_code=0, copia=None, check=0, unpack=0, slow=None):
        self.copia_code, self.copia, self.check, self.unpack, self.slow = copia_code, copia, check, unpack, slow
        self.calls = []

    def __call__(self, cmd, **kwargs):
        tool = cmd[-1]
        self.calls.append(cmd)
        mount = cmd[cmd.index("-v") + 1] if "-v" in cmd else None
        if tool == "copia":
            if self.slow:
                raise subprocess.TimeoutExpired(cmd, 1)
            if self.copia_code:
                return subprocess.CompletedProcess(cmd, self.copia_code, None, b"copia: no\n")
            if self.copia is not None:
                kwargs["stdout"].write(self.copia)
            else:
                source = Path(mount.rsplit(":/mundo", 1)[0])
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w") as z:
                    z.writestr("mundo.json", json.dumps({"esquema": "numinia-engine/mundo@2"}))
                    for path in sorted(source.rglob("*")):
                        if path.is_file():
                            z.writestr(path.relative_to(source).as_posix(), path.read_bytes())
                kwargs["stdout"].write(buffer.getvalue())
            return subprocess.CompletedProcess(cmd, 0, None, b"")
        if tool == "comprobar":
            if self.check:
                return subprocess.CompletedProcess(cmd, self.check, b"", b"starting\nesquema roto\n")
            return subprocess.CompletedProcess(cmd, 0, b"ok\n", b"")
        if tool == "importar":
            if self.unpack:
                return subprocess.CompletedProcess(cmd, self.unpack, b"", b"no se puede\n")
            folder = Path(mount.rsplit(":/mundo", 1)[0])
            assert list(folder.iterdir()) == []  # importar unpacks into an EMPTY /mundo
            with zipfile.ZipFile(kwargs["stdin"]) as z:
                z.extractall(folder)
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        raise AssertionError(tool)


class HyperfyWorld(unittest.TestCase):
    def test_a_copy_has_the_database_the_files_and_the_linked_assets_and_names_its_slot(self):
        m = rig(self)
        data = m.home / "data" / "plaza"
        (data / "db.sqlite-wal").write_bytes(b"half written")
        (data / "db.sqlite-shm").write_bytes(b"index")
        (data / "db.sqlite-journal").write_bytes(b"journal")
        (data / "assets" / "out.glb").symlink_to("/etc/hostname")
        (data / "assets" / "folder-link").symlink_to(data / "collections")
        (data / "junk.txt").write_bytes(b"not a world file")
        (data / "collections" / "empty").mkdir()
        slept = []
        with mock.patch.object(V.time, "sleep", slept.append):
            result = ask(m)
        self.assertEqual((result["state"], result["rule"], result["version"], result["made"]), ("done", None, "v1", "v2"))
        new = m.home / "versions" / "plaza" / "v2"
        self.assertEqual(rows(new / "db.sqlite"), rows(data / "db.sqlite"))
        self.assertEqual(sorted(p.name for p in new.iterdir()), ["assets", "collections", "db.sqlite", "storage.json"])
        # Assets and collections: the same inode; the storage file: a copy.
        glb = "a" * 64 + ".glb"
        self.assertEqual((new / "assets" / glb).stat().st_ino, (data / "assets" / glb).stat().st_ino)
        self.assertEqual((new / "collections" / "deep" / "c.json").stat().st_ino,
                         (data / "collections" / "deep" / "c.json").stat().st_ino)
        self.assertTrue((new / "collections" / "empty").is_dir())
        self.assertNotEqual((new / "storage.json").stat().st_ino, (data / "storage.json").stat().st_ino)
        self.assertEqual((new / "storage.json").read_bytes(), b'{"seen": true}')
        # No journal file of SQLite's, no link, nothing outside what an import keeps.
        self.assertEqual(sorted(p.name for p in (new / "assets").iterdir()), [glb])
        self.assertFalse(any(p.is_symlink() for p in new.rglob("*")))
        self.assertEqual(stat.S_IMODE(new.stat().st_mode), 0o755)
        self.assertEqual(slept, [R.save_pause(R.SAVE_INTERVAL)])  # running and published: it waited for a save
        # Metadata: written last, the implicit v1 kept, `next` grown, the new one measured.
        meta = m.meta()
        self.assertEqual((meta["published"], meta["next"], sorted(meta["versions"])), ("v1", 3, ["v1", "v2"]))
        self.assertEqual(meta["versions"]["v1"]["origin"], "created")
        self.assertEqual(meta["versions"]["v1"]["createdAt"], "2026-10-01T00:00:00Z")
        self.assertIsInstance(meta["versions"]["v1"]["bytes"], int)
        v2 = meta["versions"]["v2"]
        self.assertEqual((v2["origin"], v2["createdBy"], v2["createdAt"], v2["bytes"]),
                         ("copy:v1", m.oracle.address, R.utc(TV.NOW), V.measure(new)))
        self.assertEqual(staging_left(m), [])
        self.assertEqual(m.log()[-1]["state"], "done")
        # The source keeps its data (opening a database may fold a leftover -wal into it, as a download's does).
        self.assertEqual((data / "storage.json").read_bytes(), b'{"seen": true}')
        self.assertEqual((data / "assets" / ("a" * 64 + ".glb")).read_bytes(), b"model bytes")

    def test_the_save_pause_is_for_a_running_published_hyperfy_source_only(self):
        slept = []
        stopped = rig(self, state="stopped")
        self.assertEqual(call(stopped, sleep=slept.append)[0], None)
        self.assertEqual(slept, [])  # a stopped world writes nothing
        running = rig(self)
        self.assertEqual(call(running, sleep=slept.append)[0], None)
        self.assertEqual(slept, [R.save_pause(R.SAVE_INTERVAL)])
        # The unpublished version of a running world: nothing writes it, so no wait (room for three, for the test).
        three = rig(self)
        three.write_meta(TV.two())
        shutil.copytree(three.home / "data" / "plaza", three.home / "versions" / "plaza" / "v2")
        slept.clear()
        with mock.patch.object(V, "MAX_VERSIONS", 3):
            rule, entry = call(three, version="v2", sleep=slept.append)
            self.assertEqual((rule, entry["made"]), (None, "v3"))
            self.assertEqual(slept, [])
            meta = three.meta()
        self.assertEqual((meta["next"], meta["versions"]["v3"]["origin"]), (4, "copy:v2"))
        self.assertEqual(rows(three.home / "versions" / "plaza" / "v3" / "db.sqlite"),
                         rows(three.home / "versions" / "plaza" / "v2" / "db.sqlite"))
        self.assertEqual(meta["versions"]["v1"]["bytes"], None)  # the file was there: its entries are as they were

    def test_a_filesystem_that_will_not_link_gets_copies(self):
        for code in (errno.EXDEV, errno.EPERM, errno.EMLINK):
            with self.subTest(errno=code):
                m = rig(self)
                real = os.link

                def refuse(*args, **kwargs):
                    raise OSError(code, "no link")

                with mock.patch.object(V.os, "link", refuse):
                    self.assertIsNone(call(m, sleep=lambda _: None)[0])
                glb = "a" * 64 + ".glb"
                new, old = m.home / "versions" / "plaza" / "v2" / "assets" / glb, m.home / "data" / "plaza" / "assets" / glb
                self.assertEqual(new.read_bytes(), b"model bytes")
                self.assertNotEqual(new.stat().st_ino, old.stat().st_ino)
                self.assertIs(os.link, real)

    def test_any_other_error_while_linking_fails_and_leaves_nothing(self):
        m = rig(self)
        with mock.patch.object(V.os, "link", side_effect=OSError(errno.EACCES, "no")):
            rule, entry = call(m, sleep=lambda _: None)
        self.assertEqual(rule, V.NOT_WRITTEN)
        self.assertNotIn("made", entry)
        self.assertEqual(staging_left(m), [])
        self.assertFalse((m.home / "versions").exists())  # no metadata, no folder: nothing was named

    def test_a_database_that_is_not_one_fails_whole(self):
        m = rig(self)
        (m.home / "data" / "plaza" / "db.sqlite").write_bytes(b"this is not sqlite at all, but long enough" * 40)
        self.assertEqual(call(m, sleep=lambda _: None)[0], V.NOT_WRITTEN)
        self.assertEqual(staging_left(m), [])
        self.assertFalse((m.home / "versions").exists())

    def test_a_world_without_a_database_or_with_a_linked_one_is_not_copied(self):
        for name in ("missing", "link"):
            with self.subTest(case=name):
                m = rig(self)
                database = m.home / "data" / "plaza" / "db.sqlite"
                real = m.home / "elsewhere.sqlite"
                database.rename(real)
                if name == "link":
                    database.symlink_to(real)
                self.assertEqual(call(m, sleep=lambda _: None)[0], V.NOT_WRITTEN)
                self.assertEqual(staging_left(m), [])

    def test_a_disk_that_fills_midway_is_no_room_and_leaves_nothing(self):
        m = rig(self)
        with mock.patch.object(V.R, "snapshot_sqlite", side_effect=OSError(errno.ENOSPC, "full")):
            self.assertEqual(call(m, sleep=lambda _: None)[0], "no-room")
        self.assertEqual(staging_left(m), [])
        self.assertFalse((m.home / "versions").exists())

    def test_a_folder_that_will_not_take_the_new_metadata_is_taken_back(self):
        m = rig(self)
        with mock.patch.object(V, "write_versions", side_effect=OSError(errno.EIO, "io")):
            rule, entry = call(m, sleep=lambda _: None)
        self.assertEqual(rule, "versions-unwritable")
        self.assertNotIn("made", entry)
        self.assertFalse((m.home / "versions" / "plaza" / "v2").exists())
        self.assertEqual(staging_left(m), [])

    def test_a_leftover_under_the_new_name_that_no_metadata_lists_is_replaced(self):
        m = rig(self)
        m.write_meta({**V.implicit("plaza"), "next": 2})
        left = m.put_version("v2", content=b"left by a run that died")
        (left / "stray").write_bytes(b"x")
        self.assertEqual(call(m, sleep=lambda _: None)[0], None)
        new = m.home / "versions" / "plaza" / "v2"
        self.assertFalse((new / "stray").exists())
        self.assertEqual(rows(new / "db.sqlite"), rows(m.home / "data" / "plaza" / "db.sqlite"))

    def test_the_source_must_be_a_real_folder(self):
        m = rig(self)
        data = m.home / "data" / "plaza"
        data.rename(m.home / "elsewhere")
        data.symlink_to(m.home / "elsewhere")
        self.assertEqual(call(m, sleep=lambda _: None)[0], V.NOT_WRITTEN)
        self.assertEqual(staging_left(m), [])


class NuminiaEngineWorld(unittest.TestCase):
    def duplicate(self, m, fake, **kw):
        with mock.patch.object(V.R, "run_tool", fake):
            return ask(m, **kw)

    def test_the_engine_copies_its_own_world_through_its_own_tools(self):
        m = rig(self, engine=True)
        fake = FakeEngine()
        slept = []
        with mock.patch.object(V.time, "sleep", slept.append):
            result = self.duplicate(m, fake)
        self.assertEqual((result["state"], result["version"], result["made"]), ("done", "v1", "v2"), result)
        self.assertEqual([c[-1] for c in fake.calls], ["copia", "comprobar", "importar"])
        self.assertIn(f"{m.home}/data/plaza:/mundo:ro", fake.calls[0])  # the SOURCE, read-only
        self.assertNotIn("-v", fake.calls[1])
        self.assertTrue(fake.calls[2][fake.calls[2].index("-v") + 1].endswith(":/mundo"))
        self.assertIn("--network", fake.calls[0])
        self.assertEqual(slept, [])  # the engine writes each snapshot whole: no wait
        new = m.home / "versions" / "plaza" / "v2"
        self.assertEqual((new / "datos" / "objetos.jsonl").read_bytes(), b'{"id":1}\n')
        self.assertEqual((new / "aventura" / "una.json").read_bytes(), b"{}")
        self.assertEqual(stat.S_IMODE(new.stat().st_mode), 0o700)
        self.assertEqual(staging_left(m), [])  # no folder, no zip
        meta = m.meta()
        self.assertEqual((meta["next"], meta["versions"]["v2"]["origin"], meta["versions"]["v2"]["bytes"]),
                         (3, "copy:v1", V.measure(new)))
        self.assertEqual((m.home / "data" / "plaza" / "datos" / "objetos.jsonl").read_bytes(), b'{"id":1}\n')

    def test_the_engine_zip_is_made_in_staging_in_a_file_only_this_run_opens(self):
        m = rig(self, engine=True)
        seen = {}
        inner = FakeEngine()

        def fake(cmd, **kwargs):
            if cmd[-1] == "copia":
                seen["staging"] = staging_left(m)
                seen["mode"] = stat.S_IMODE(os.fstat(kwargs["stdout"].fileno()).st_mode)
            return inner(cmd, **kwargs)

        self.duplicate(m, fake)
        self.assertEqual(len(seen["staging"]), 2)  # <nonce> folder and <nonce>.zip
        self.assertEqual(seen["mode"], 0o600)
        self.assertEqual(staging_left(m), [])

    def test_a_tool_that_refuses_is_engine_refused_and_nothing_is_left(self):
        cases = {
            "importar says no": FakeEngine(unpack=1),
            "comprobar says no": FakeEngine(check=1),
            "copia says no": FakeEngine(copia_code=1),
            "copia gives no zip": FakeEngine(copia=b"not a zip"),
            "copia takes too long": FakeEngine(slow=True),
        }
        for name, fake in cases.items():
            with self.subTest(case=name):
                m = rig(self, engine=True)
                result = self.duplicate(m, fake)
                self.assertEqual((result["state"], result["rule"]), ("failed", "engine-refused"))
                self.assertNotIn("made", result)
                self.assertEqual(staging_left(m), [])
                self.assertFalse((m.home / "versions").exists())  # no metadata change: it never existed
                self.assertEqual(m.log()[-1]["rule"], "engine-refused")
                self.assertNotIn("esquema roto", (m.home / "log.jsonl").read_text())  # the rule, never the line

    def test_refusal_leaves_existing_metadata_as_it_was(self):
        m = rig(self, engine=True)
        meta = {**V.implicit("plaza"), "next": 5}
        m.write_meta(meta)
        before = (m.home / "versions" / "plaza.json").read_bytes()
        self.duplicate(m, FakeEngine(unpack=1))
        self.assertEqual((m.home / "versions" / "plaza.json").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in (m.home / "versions" / "plaza").iterdir()), [])

    def test_tools_that_cannot_run_are_not_called_a_bad_world(self):
        m = rig(self, engine=True)
        result = self.duplicate(m, FakeEngine(copia_code=125))
        self.assertEqual(result["rule"], "the engine's tools could not run here")
        m = rig(self, engine=True)
        result = self.duplicate(m, FakeEngine(unpack=125))
        self.assertEqual(result["rule"], "the engine's tools could not run here")
        self.assertEqual(staging_left(m), [])

    def test_docker_missing_is_a_plain_failure(self):
        m = rig(self, engine=True)
        with mock.patch.object(V.R, "run_tool", side_effect=FileNotFoundError("docker")):
            rule, _ = call(m)
        self.assertEqual(rule, V.NOT_WRITTEN)
        self.assertEqual(staging_left(m), [])

    def test_an_engine_world_needs_twice_the_room_and_a_hyperfy_one_once(self):
        for engine, times in ((True, 2), (False, 1)):
            for delta, rule in ((-1, "no-room"), (0, None)):
                with self.subTest(engine=engine, delta=delta):
                    m = rig(self, engine=engine)
                    free = R.RESERVE + times * V.measure(m.home / "data" / "plaza") + delta
                    with mock.patch.object(shutil, "disk_usage", return_value=usage(free)), \
                            mock.patch.object(V.R, "run_tool", FakeEngine()):
                        self.assertEqual(call(m, sleep=lambda _: None)[0], rule)
                    self.assertEqual(staging_left(m), [])

    def test_a_disk_that_fills_in_the_engine_import_is_no_room(self):
        m = rig(self, engine=True)
        with mock.patch.object(V.R, "run_tool", FakeEngine()), \
                mock.patch.object(V.R, "engine_import", side_effect=OSError(errno.ENOSPC, "full")):
            self.assertEqual(call(m)[0], "no-room")
        self.assertEqual(staging_left(m), [])


class Rules(unittest.TestCase):
    def test_every_refusal_by_name_and_in_order(self):
        # missing: no order.
        m = rig(self)
        self.assertEqual(ask(m, world="nowhere")["rule"], "missing")
        # versions-unreadable: a file that cannot be trusted.
        (m.home / "versions").mkdir()
        (m.home / "versions" / "plaza.json").write_text("{", encoding="utf-8")
        self.assertEqual(ask(m)["rule"], "versions-unreadable")
        # no-version: the source is not one of the world's.
        (m.home / "versions" / "plaza.json").unlink()
        self.assertEqual(ask(m, version="v7")["rule"], "no-version")
        # full: already two (and `full` comes before the disk is asked).
        m.write_meta(TV.two())
        m.put_version("v2")
        with mock.patch.object(shutil, "disk_usage", side_effect=AssertionError("asked the disk")):
            result = ask(m)
        self.assertEqual((result["state"], result["rule"]), ("failed", "full"))
        self.assertEqual(m.log()[-1]["rule"], "full")
        self.assertEqual(sorted(m.meta()["versions"]), ["v1", "v2"])
        self.assertEqual(staging_left(m), [])

    def test_a_world_that_has_used_every_name_is_full(self):
        m = rig(self)
        m.write_meta({**V.implicit("plaza"), "next": 10_000})
        self.assertEqual(ask(m)["rule"], "full")

    def test_no_room_before_anything_is_written(self):
        m = rig(self)
        with mock.patch.object(shutil, "disk_usage", return_value=usage(R.RESERVE)):
            result = ask(m)
        self.assertEqual((result["state"], result["rule"]), ("failed", "no-room"))
        self.assertEqual(staging_left(m), [])
        self.assertFalse((m.home / "versions").exists())

    def test_a_name_is_not_reused_after_a_delete(self):
        m = rig(self)
        self.assertEqual(ask(m)["made"], "v2")
        self.assertEqual(m.delete("v2")["state"], "done")
        self.assertEqual(sorted(m.meta()["versions"]), ["v1"])
        self.assertEqual(m.meta()["next"], 3)
        result = ask(m)
        self.assertEqual((result["state"], result["made"]), ("done", "v3"))
        self.assertTrue((m.home / "versions" / "plaza" / "v3").is_dir())
        self.assertFalse((m.home / "versions" / "plaza" / "v2").exists())
        self.assertEqual(m.meta()["next"], 4)

    def test_a_result_that_names_no_new_version_has_no_made(self):
        m = rig(self)
        m.write_meta(TV.two())
        m.put_version("v2")
        self.assertNotIn("made", ask(m))
        self.assertNotIn("made", m.delete("v2"))


class Leftovers(unittest.TestCase):
    def test_a_staging_folder_or_zip_a_dead_duplicate_left_goes_on_the_next_run(self):
        m = rig(self, engine=True)
        staging = m.home / "staging"
        staging.mkdir(mode=0o700)
        folder, zipped = "a" * 32, "b" * 32 + ".zip"
        (staging / folder).mkdir()
        (staging / folder / "datos").write_bytes(b"half")
        (staging / zipped).write_bytes(b"half a zip")
        outside = m.home / "outside.zip"
        outside.write_bytes(b"not ours")
        (staging / ("c" * 32 + ".zip")).symlink_to(outside)  # a link is unlinked, never followed
        (staging / "notes.zip").write_bytes(b"not a nonce")
        (staging / ("d" * 32 + ".txt")).write_bytes(b"not a zip name")
        m.take()
        self.assertEqual(staging_left(m), ["d" * 32 + ".txt", "notes.zip"])
        self.assertEqual(outside.read_bytes(), b"not ours")


if __name__ == "__main__":
    unittest.main()
