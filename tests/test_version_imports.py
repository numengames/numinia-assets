# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""DownloadVersion and ImportVersion: a copy of any version out, a zip in as a new, unpublished version.

The rig is test_duplicate's: a temporary fleet home with a world `plaza`
(Hyperfy, or Numinia Engine with `engine=True`), a throwaway Oracle, requests
dropped in the inbox and taken by the reconciler's own take_requests. The
Numinia Engine's tools are faked as the other engine tests fake them.

`versions` imports its own copy of `reconcile` (`V.R`): the import's stand-ins
go on that module object; a copy is built by the copy of reconcile that
test_versions loads (`TV.R`, which runs make_copies), so a copy's tool is
faked there.
"""

from pathlib import Path
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
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


Z = load("fleet_zips", HERE)
TV = load("test_versions", HERE)  # its Machine rig and message builders
TD = load("test_duplicate", HERE)  # the rig of a world with data, and the engine's fake tools
V = TD.V  # the `versions` its rig puts in sys.modules, the one take_requests reaches
R = V.R  # what the import runs on
TR = TV.R  # what take_requests and make_copies run on
NOW = TV.NOW
ENGINE = TD.ENGINE
STATUE = b"glTF" + bytes(range(256)) * 20


def rows(path):
    return TD.rows(path)


def tree(folder):
    """Every file under a folder with its bytes, and the folders: the whole picture of a version's data."""
    found = {}
    for path in sorted(Path(folder).rglob("*")):
        found[path.relative_to(folder).as_posix()] = path.read_bytes() if path.is_file() else "folder"
    return found


def hyperfy_zip(db=None, extra=(), prefix="world/"):
    """A Hyperfy world's folder zipped: a database (a small one of its own) and two assets."""
    files = [(f"{prefix}db.sqlite", db if db is not None else Z.db_bytes(blueprints=1, entities=2)),
             (f"{prefix}assets/statue.glb", STATUE), (f"{prefix}storage.json", b'{"from": "zip"}')]
    return Z.make_zip(files + list(extra))


def engine_zip(extra=()):
    """A Numinia Engine world's zip (numinia-engine/mundo@2): mundo.json, datos/, aventura/."""
    manifest = {"esquema": "numinia-engine/mundo@2", "ficheros": []}
    return Z.make_zip([("mundo.json", json.dumps(manifest).encode()), ("datos/objetos.json", b'[{"from":"zip"}]'),
                       ("aventura/misiones/una.json", b"{}"), *extra])


class Tools:
    """The engine image's tools as an import sees them: comprobar says yes or no, importar writes the world."""

    def __init__(self, check=None):
        self.check, self.calls = check, []

    def __call__(self, cmd, **kwargs):
        tool = cmd[-1]
        self.calls.append(cmd)
        if tool == "comprobar":
            with zipfile.ZipFile(io.BytesIO(kwargs["stdin"].read())) as z:
                names = z.namelist()
            if self.check or "mundo.json" not in names:
                line = self.check or "mundo.json is not here"
                return subprocess.CompletedProcess(cmd, 1, b"", f"starting\n{line}\n".encode())
            return subprocess.CompletedProcess(cmd, 0, b"ok\n", b"")
        if tool == "importar":
            folder = Path(cmd[cmd.index("-v") + 1].rsplit(":/mundo", 1)[0])
            assert list(folder.iterdir()) == []  # importar unpacks into an EMPTY /mundo
            with zipfile.ZipFile(io.BytesIO(kwargs["stdin"].read())) as z:
                z.extractall(folder)
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        raise AssertionError(tool)


def rig(test, engine=False, state="running", versions=None):
    """test_duplicate's world `plaza` with the upload and staging folders; `versions` is its metadata, if any."""
    m = TD.rig(test, engine=engine, state=state)
    for folder in ("uploads", "staging"):
        (m.home / folder).mkdir(mode=0o700)
    if versions is not None:
        m.write_meta(versions)
        # The unpublished version v2 of the metadata, with data of its own.
        folder = m.home / "versions" / "plaza" / "v2"
        folder.mkdir(parents=True)
        if engine:
            (folder / "datos").mkdir()
            (folder / "datos" / "objetos.jsonl").write_bytes(b'{"id":"v2"}\n')
        else:
            Z.world_db(folder / "db.sqlite", blueprints=1, entities=1)
            (folder / "storage.json").write_bytes(b'{"only": "v2"}')
    return m


def bring(m, data, replace="", world="plaza", sha=None, door=None, size=None, **over):
    """An ImportVersion whose zip has arrived, as the door leaves it: the zip in uploads/, the request in the inbox."""
    message = TV.message("ImportVersion", world=world, zipBytes=len(data) if size is None else size,
                         zipSha256=sha or hashlib.sha256(data).hexdigest(), replace=replace, **over)
    nonce = message["nonce"]
    (m.home / "uploads" / f"{nonce}.zip").write_bytes(data)
    fields = {"zip": f"{nonce}.zip", "expiresAt": R.utc(NOW + R.SLOT_SECONDS), "bytes": len(data), **(door or {})}
    m.drop("ImportVersion", message, **fields)
    return nonce


def bring_and_take(m, data, tools=None, **kwargs):
    """Drop an ImportVersion and take it (with the engine's tools faked); its result."""
    nonce = bring(m, data, **kwargs)
    with mock.patch.object(R, "run_tool", tools or Tools()):
        m.take()
    return m.result(nonce)


def left(m, folder):
    path = m.home / folder
    return sorted(p.name for p in path.iterdir()) if path.is_dir() else []


def nothing_left(test, m):
    for folder in ("uploads", "staging"):
        test.assertEqual(left(m, folder), [], folder)
    test.assertFalse((m.home / "run" / R.IMPORTING).exists())


class Snapshot:
    """What an import that fails may not change: the metadata file, the version folders, the live world's data."""

    def __init__(self, m):
        self.m = m
        self.picture = self.take()

    def take(self):
        meta = self.m.home / "versions" / "plaza.json"
        data = self.m.home / "data" / "plaza"
        return {
            "meta": meta.read_bytes() if meta.exists() else None,
            "versions": tree(self.m.home / "versions" / "plaza") if (self.m.home / "versions" / "plaza").exists() else None,
            "data": tree(data),
            "inode": data.stat().st_ino,
            "order": (self.m.orders / "plaza.json").read_bytes(),
        }

    def same(self, test):
        test.assertEqual(self.take(), self.picture)


class DownloadHyperfy(unittest.TestCase):
    def queue(self, m, version="v2", copy=TV.COPY):
        msg = TV.message("DownloadVersion", version=version)
        m.drop("DownloadVersion", msg, copy=copy)
        m.take()
        return msg["nonce"]

    def make(self, m, copy=TV.COPY, now=NOW):
        with contextlib.redirect_stderr(io.StringIO()):
            return TR.make_copies(m.home, m.orders, "open-1", now)

    def test_the_published_version_of_a_running_world_waits_for_a_save_and_is_the_worlds_folder(self):
        m = rig(self, versions=TV.two())
        nonce = self.queue(m, "v1")
        self.assertEqual(m.result(nonce)["state"], "working")
        self.assertEqual(m.result(nonce)["version"], "v1")
        queued = json.loads((m.home / "run" / "copy-queue.json").read_text())
        self.assertEqual(queued, [{"copy": TV.COPY, "world": "plaza", "nonce": nonce, "by": m.oracle.address,
                                   "version": "v1"}])
        preparing = json.loads((m.home / "copies" / f"{TV.COPY}.json").read_text())
        self.assertEqual({k: preparing[k] for k in ("copy", "world", "version", "state")},
                         {"copy": TV.COPY, "world": "plaza", "version": "v1", "state": "preparing"})
        self.assertEqual(TR.copies_wait(m.home, m.orders), TR.save_pause(TR.SAVE_INTERVAL))
        self.assertEqual(self.make(m), [TV.COPY])
        status = json.loads((m.home / "copies" / f"{TV.COPY}.json").read_text())
        self.assertEqual((status["state"], status["version"], status["world"]), ("ready", "v1", "plaza"))
        self.assertRegex(status["filename"], r"\Aplaza-v1-[0-9]{8}-[0-9]{4}\.zip\Z")
        self.assertEqual(status["sha256"], TR.file_digest(m.home / "copies" / f"{TV.COPY}.zip"))
        with zipfile.ZipFile(m.home / "copies" / f"{TV.COPY}.zip") as z:
            self.assertEqual(json.loads(z.read("world.json"))["version"], "v1")
            self.assertIn("world/assets/" + "a" * 64 + ".glb", z.namelist())
        done = m.result(nonce)
        self.assertEqual((done["state"], done["type"], done["version"]), ("done", "DownloadVersion", "v1"))

    def test_an_unpublished_version_is_built_from_its_own_folder_with_no_wait(self):
        m = rig(self, versions=TV.two())
        nonce = self.queue(m, "v2")
        self.assertEqual(TR.copies_wait(m.home, m.orders), 0)  # nobody writes versions/<id>/v2
        self.assertEqual(self.make(m), [TV.COPY])
        status = json.loads((m.home / "copies" / f"{TV.COPY}.json").read_text())
        self.assertEqual((status["state"], status["version"]), ("ready", "v2"))
        self.assertRegex(status["filename"], r"\Aplaza-v2-[0-9]{8}-[0-9]{4}\.zip\Z")
        # The zip holds v2's data (one blueprint, one entity, its own storage), not the published folder's.
        with zipfile.ZipFile(m.home / "copies" / f"{TV.COPY}.zip") as z:
            self.assertEqual(sorted(n for n in z.namelist() if n.startswith("world/")),
                             ["world/db.sqlite", "world/storage.json"])
            self.assertEqual(z.read("world/storage.json"), b'{"only": "v2"}')
            db = m.home / "extracted.sqlite"
            db.write_bytes(z.read("world/db.sqlite"))
        self.assertEqual(len(rows(db)["blueprints"]), 1)
        self.assertEqual(len(rows(m.home / "data" / "plaza" / "db.sqlite")["blueprints"]), 3)
        self.assertEqual(m.result(nonce)["state"], "done")
        self.assertEqual(m.log()[-1]["state"], "done")

    def test_after_a_switch_the_old_published_folder_is_the_one_without_a_wait(self):
        m = rig(self, versions=TV.two(published="v2"))
        # v2 is now the world's folder (data/plaza); v1 waits in versions/plaza/v1.
        shutil.copytree(m.home / "versions" / "plaza" / "v2", m.home / "versions" / "plaza" / "v1")
        self.queue(m, "v1")
        self.assertEqual(TR.copies_wait(m.home, m.orders), 0)
        (m.home / "run" / "copy-queue.json").unlink()
        self.queue(m, "v2", copy="beef" + "0" * 28)
        self.assertEqual(TR.copies_wait(m.home, m.orders), TR.save_pause(TR.SAVE_INTERVAL))

    def test_a_stopped_world_waits_for_nothing(self):
        m = rig(self, versions=TV.two(), state="stopped")
        self.queue(m, "v1")
        self.assertEqual(TR.copies_wait(m.home, m.orders), 0)

    def test_a_world_with_no_versions_file_has_its_implicit_v1_to_download(self):
        m = rig(self)
        nonce = self.queue(m, "v1")
        self.assertEqual(self.make(m), [TV.COPY])
        self.assertEqual(m.result(nonce)["state"], "done")
        self.assertFalse((m.home / "versions").exists())  # a download writes no metadata

    def test_a_request_for_what_is_not_there_is_refused_by_name_and_queues_nothing(self):
        m = rig(self, versions=TV.two())
        nonce = self.queue(m, "v7")
        self.assertEqual((m.result(nonce)["state"], m.result(nonce)["rule"]), ("failed", "no-version"))
        self.assertEqual(m.log()[-1]["rule"], "no-version")
        # No order.
        gone = TV.message("DownloadVersion", world="ghost")
        m.drop("DownloadVersion", gone, copy="beef" + "0" * 28)
        m.take()
        self.assertEqual(m.result(gone["nonce"])["rule"], "missing")
        # A file that cannot be trusted is never taken for the implicit one.
        (m.home / "versions" / "plaza.json").write_text("{not json")
        nonce = self.queue(m, "v1", copy="cafe" + "0" * 28)
        self.assertEqual(m.result(nonce)["rule"], "versions-unreadable")
        # A listed version whose folder is not there.
        (m.home / "versions" / "plaza.json").unlink()
        m.write_meta(TV.two())
        shutil.rmtree(m.home / "versions" / "plaza" / "v2")
        nonce = self.queue(m, "v2", copy="f00d" + "0" * 28)
        self.assertEqual(m.result(nonce)["rule"], "not-a-folder")
        self.assertFalse((m.home / "run" / "copy-queue.json").exists())
        self.assertEqual(left(m, "copies"), [])

    def test_a_version_gone_by_the_time_the_copy_is_made_fails_with_the_rule(self):
        m = rig(self, versions=TV.two())
        nonce = self.queue(m, "v2")
        shutil.rmtree(m.home / "versions" / "plaza" / "v2")
        (m.home / "versions" / "plaza.json").unlink()
        m.write_meta({**TV.two(), "versions": {"v1": TV.info()}})
        self.assertEqual(self.make(m), [])
        failed = json.loads((m.home / "copies" / f"{TV.COPY}.json").read_text())
        self.assertEqual((failed["state"], failed["version"]), ("failed", "v2"))
        result = m.result(nonce)
        self.assertEqual((result["state"], result["rule"], result["version"]), ("failed", "no-version", "v2"))
        self.assertEqual(sorted(left(m, "copies")), [f"{TV.COPY}.json"])
        # And a world deleted in between.
        (m.orders / "plaza.json").unlink()
        (m.home / "run" / "copy-queue.json").write_text(json.dumps(
            [{"copy": "beef" + "0" * 28, "world": "plaza", "nonce": "e" * 32, "by": m.oracle.address, "version": "v1"}]))
        self.assertEqual(self.make(m), [])
        self.assertEqual(m.result("e" * 32)["rule"], "missing")
        # A job whose version is not a version is dropped, as one with a bad copy number is.
        (m.home / "run" / "copy-queue.json").write_text(json.dumps(
            [{"copy": "cafe" + "0" * 28, "world": "plaza", "nonce": "d" * 32, "by": None, "version": "../x"}]))
        self.assertEqual(self.make(m), [])
        self.assertFalse((m.home / "requests" / ("d" * 32 + ".json")).exists())

    def test_a_copy_of_a_version_and_a_copy_of_the_world_never_stand_in_for_each_other(self):
        m = rig(self, versions=TV.two())
        world_copy = "a1" * 16
        m.drop("DownloadWorld", {"machine": "open-1", "world": "plaza", "nonce": os.urandom(16).hex(),
                                 "expires": TV.EXPIRES}, copy=world_copy)
        m.take()
        self.make(m)
        world_status = json.loads((m.home / "copies" / f"{world_copy}.json").read_text())
        self.assertNotIn("version", world_status)  # a world's record is what it was
        self.assertRegex(world_status["filename"], r"\Aplaza-[0-9]{8}-[0-9]{4}\.zip\Z")
        # A minute later a version's copy is its own; the same version asked again shares it.
        self.queue(m, "v2", copy="b2" * 16)
        self.make(m, now=NOW + 60)
        first = json.loads((m.home / "copies" / f"{'b2' * 16}.json").read_text())
        self.assertNotEqual(first["sha256"], world_status["sha256"])
        self.assertEqual(first["version"], "v2")
        self.queue(m, "v2", copy="c3" * 16)
        self.make(m, now=NOW + 120)
        again = json.loads((m.home / "copies" / f"{'c3' * 16}.json").read_text())
        self.assertEqual((again["copy"], again["sha256"], again["version"]), ("c3" * 16, first["sha256"], "v2"))
        self.assertEqual(os.stat(m.home / "copies" / f"{'b2' * 16}.zip").st_ino,
                         os.stat(m.home / "copies" / f"{'c3' * 16}.zip").st_ino)
        # Another version of the same world is not served the first.
        self.queue(m, "v1", copy="d4" * 16)
        self.make(m, now=NOW + 180)
        other = json.loads((m.home / "copies" / f"{'d4' * 16}.json").read_text())
        self.assertEqual(other["version"], "v1")
        self.assertNotEqual(other["sha256"], first["sha256"])
        # And a download of the world right after is not served a version's copy.
        again_world = "e5" * 16
        m.drop("DownloadWorld", {"machine": "open-1", "world": "plaza", "nonce": os.urandom(16).hex(),
                                 "expires": TV.EXPIRES}, copy=again_world)
        m.take()
        self.make(m, now=NOW + 240)
        served = json.loads((m.home / "copies" / f"{again_world}.json").read_text())
        self.assertNotIn("version", served)
        self.assertEqual(served["sha256"], world_status["sha256"])  # the world's own copy, from 4 minutes ago


class DownloadEngine(unittest.TestCase):
    def test_a_numinia_engine_version_is_copied_by_the_engine_from_its_own_folder_read_only(self):
        m = rig(self, engine=True, versions=TV.two())
        msg = TV.message("DownloadVersion", version="v2")
        m.drop("DownloadVersion", msg, copy=TV.COPY)
        m.take()
        self.assertEqual(TR.copies_wait(m.home, m.orders), 0)
        tools = TD.FakeEngine()
        with mock.patch.object(TR, "run_tool", tools), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(TR.make_copies(m.home, m.orders, "open-1", NOW), [TV.COPY])
        self.assertEqual([c[-1] for c in tools.calls], ["copia"])
        self.assertIn(f"{m.home}/versions/plaza/v2:/mundo:ro", tools.calls[0])
        status = json.loads((m.home / "copies" / f"{TV.COPY}.json").read_text())
        self.assertEqual((status["state"], status["version"]), ("ready", "v2"))
        self.assertRegex(status["filename"], r"\Aplaza-v2-[0-9]{8}-[0-9]{4}\.zip\Z")
        with zipfile.ZipFile(m.home / "copies" / f"{TV.COPY}.zip") as z:
            self.assertEqual(z.read("datos/objetos.jsonl"), b'{"id":"v2"}\n')
        self.assertEqual(m.result(msg["nonce"])["state"], "done")

    def test_the_published_one_is_the_worlds_folder_and_still_needs_no_wait(self):
        m = rig(self, engine=True, versions=TV.two())
        msg = TV.message("DownloadVersion", version="v1")
        m.drop("DownloadVersion", msg, copy=TV.COPY)
        m.take()
        self.assertEqual(TR.copies_wait(m.home, m.orders), 0)  # the engine writes each snapshot whole
        tools = TD.FakeEngine()
        with mock.patch.object(TR, "run_tool", tools), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(TR.make_copies(m.home, m.orders, "open-1", NOW), [TV.COPY])
        self.assertIn(f"{m.home}/data/plaza:/mundo:ro", tools.calls[0])

    def test_a_copy_the_engine_could_not_make_fails_and_leaves_no_zip(self):
        m = rig(self, engine=True, versions=TV.two())
        msg = TV.message("DownloadVersion", version="v2")
        m.drop("DownloadVersion", msg, copy=TV.COPY)
        m.take()
        with mock.patch.object(TR, "run_tool", TD.FakeEngine(copia_code=1)), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(TR.make_copies(m.home, m.orders, "open-1", NOW), [])
        self.assertEqual(sorted(left(m, "copies")), [f"{TV.COPY}.json"])
        self.assertEqual(json.loads((m.home / "copies" / f"{TV.COPY}.json").read_text())["state"], "failed")
        self.assertEqual(m.result(msg["nonce"])["rule"], "the copy could not be made")


class ImportIntoOne(unittest.TestCase):
    def test_a_zip_becomes_v2_unpublished_and_the_live_world_is_not_touched(self):
        m = rig(self)
        before = Snapshot(m)
        data = m.home / "data" / "plaza"
        live = {p: os.stat(p).st_ino for p in sorted(data.rglob("*"))}
        seen = {}
        real = R.stage_import

        def watching(home, nonce, message, image, limits, target):
            seen["marker"] = json.loads((home / "run" / R.IMPORTING).read_text())
            seen["working"] = json.loads((home / "requests" / f"{nonce}.json").read_text())
            seen["upload"] = (home / "uploads" / f"{nonce}.zip").exists()
            real(home, nonce, message, image, limits, target)
            seen["staged"] = target.is_dir()
            seen["upload after"] = (home / "uploads" / f"{nonce}.zip").exists()

        zipped = hyperfy_zip()
        with mock.patch.object(R, "stage_import", watching):
            result = bring_and_take(m, zipped)
        self.assertEqual((result["state"], result["rule"], result["type"]), ("done", None, "ImportVersion"))
        self.assertEqual((result["version"], result["made"]), ("", "v2"))
        # While it ran: the marker names the world, the type and the vid; the request said working; the zip was there.
        nonce = result["request"]
        marker = seen["marker"]["plaza"]
        self.assertEqual((marker["type"], marker["made"], marker["nonce"], marker["by"]),
                         ("ImportVersion", "v2", nonce, m.oracle.address))
        self.assertEqual(marker["message"]["replace"], "")
        self.assertEqual(seen["working"]["state"], "working")
        self.assertEqual((seen["upload"], seen["staged"], seen["upload after"]), (True, True, False))
        # The new version: the zip's database, assets and storage, as a created world's folder is.
        new = m.home / "versions" / "plaza" / "v2"
        self.assertEqual(sorted(p.relative_to(new).as_posix() for p in new.rglob("*")),
                         ["assets", "assets/statue.glb", "db.sqlite", "storage.json"])
        self.assertEqual(len(rows(new / "db.sqlite")["entities"]), 2)
        self.assertEqual((new / "assets" / "statue.glb").read_bytes(), STATUE)
        self.assertEqual(oct(new.stat().st_mode & 0o777), "0o755")
        # The live world: same folder, same inodes, same bytes, same order.
        self.assertEqual(before.picture["inode"], data.stat().st_ino)
        self.assertEqual({p: os.stat(p).st_ino for p in sorted(data.rglob("*"))}, live)
        self.assertEqual(tree(data), before.picture["data"])
        self.assertEqual((m.orders / "plaza.json").read_bytes(), before.picture["order"])
        # The metadata: written whole, v1 kept as the implicit one, the new one a zip nobody has published.
        meta = m.meta()
        self.assertEqual((meta["published"], meta["next"], sorted(meta["versions"])), ("v1", 3, ["v1", "v2"]))
        self.assertEqual(meta["versions"]["v1"]["origin"], "created")
        self.assertEqual(meta["versions"]["v1"]["createdAt"], "2026-10-01T00:00:00Z")
        self.assertEqual(meta["versions"]["v1"]["bytes"], V.measure(data))
        self.assertEqual(meta["versions"]["v2"], {"createdAt": R.utc(NOW), "createdBy": m.oracle.address,
                                                  "origin": "zip", "bytes": V.measure(new)})
        self.assertEqual(oct((m.home / "versions" / "plaza.json").stat().st_mode & 0o777), "0o640")
        nothing_left(self, m)
        self.assertEqual((m.log()[-1]["state"], m.log()[-1]["type"]), ("done", "ImportVersion"))
        # Nothing was queued for the containers: no switch, no update, no restart.
        for name in ("switching.json", "updating.json", "restart.json"):
            self.assertFalse((m.home / "run" / name).exists(), name)

    def test_a_stopped_world_takes_a_version_too_and_stays_stopped(self):
        m = rig(self, state="stopped")
        result = bring_and_take(m, hyperfy_zip())
        self.assertEqual((result["state"], result["made"]), ("done", "v2"))
        self.assertEqual(json.loads((m.orders / "plaza.json").read_text())["state"], "stopped")

    def test_a_metadata_file_that_was_there_keeps_its_v1_entry_as_it_was(self):
        m = rig(self, versions={**TV.two(), "versions": {"v1": TV.info(size=77)}, "next": 2})
        shutil.rmtree(m.home / "versions" / "plaza" / "v2")
        result = bring_and_take(m, hyperfy_zip())
        self.assertEqual((result["state"], result["made"]), ("done", "v2"))
        self.assertEqual(m.meta()["versions"]["v1"]["bytes"], 77)  # not measured again: it was already written

    def test_a_numinia_engine_zip_goes_through_the_engines_own_tools(self):
        m = rig(self, engine=True)
        tools = Tools()
        before = Snapshot(m)
        result = bring_and_take(m, engine_zip(), tools=tools)
        self.assertEqual((result["state"], result["version"], result["made"]), ("done", "", "v2"))
        self.assertEqual([c[-1] for c in tools.calls], ["comprobar", "importar"])
        self.assertIn("2g", tools.calls[0])  # the world's own limits, from its order
        self.assertIn(TD.ENGINE, tools.calls[0])  # the world's own image
        new = m.home / "versions" / "plaza" / "v2"
        self.assertEqual((new / "datos" / "objetos.json").read_bytes(), b'[{"from":"zip"}]')
        self.assertEqual(oct(new.stat().st_mode & 0o777), "0o700")  # the engine's folder stays private
        self.assertEqual(m.meta()["versions"]["v2"]["origin"], "zip")
        self.assertEqual(tree(m.home / "data" / "plaza"), before.picture["data"])
        nothing_left(self, m)


class ImportIntoTwo(unittest.TestCase):
    def test_replacing_the_unpublished_one_makes_v3_and_never_reuses_v2(self):
        m = rig(self, versions=TV.two())
        before = Snapshot(m)
        order_of_events = []
        real_remove, real_write = V.remove_folder, V.write_versions

        def removing(home, wid, vid):
            order_of_events.append(("remove", vid, (home / "versions" / wid / "v3").is_dir()))
            return real_remove(home, wid, vid)

        def writing(home, wid, meta):
            order_of_events.append(("write", sorted(meta["versions"]), (home / "versions" / wid / "v2").is_dir()))
            return real_write(home, wid, meta)

        with mock.patch.object(V, "remove_folder", removing), mock.patch.object(V, "write_versions", writing):
            result = bring_and_take(m, hyperfy_zip(), replace="v2")
        self.assertEqual((result["state"], result["version"], result["made"]), ("done", "v2", "v3"))
        # The new one was whole in versions/ and the metadata written before the old one went.
        self.assertEqual(order_of_events, [("write", ["v1", "v3"], True), ("remove", "v2", True)])
        self.assertFalse((m.home / "versions" / "plaza" / "v2").exists())
        self.assertEqual(len(rows(m.home / "versions" / "plaza" / "v3" / "db.sqlite")["entities"]), 2)
        meta = m.meta()
        self.assertEqual((meta["published"], meta["next"], sorted(meta["versions"])), ("v1", 4, ["v1", "v3"]))
        self.assertEqual(meta["versions"]["v3"]["origin"], "zip")
        self.assertEqual(meta["versions"]["v1"], TV.info())  # the published one's entry is untouched
        self.assertEqual(tree(m.home / "data" / "plaza"), before.picture["data"])
        self.assertEqual(before.picture["inode"], (m.home / "data" / "plaza").stat().st_ino)
        nothing_left(self, m)
        # A later duplicate-free import after deleting: the name keeps growing.
        self.assertEqual(m.delete("v3")["state"], "done")
        again = bring_and_take(m, hyperfy_zip())
        self.assertEqual(again["made"], "v4")

    def test_when_the_published_one_is_v2_the_replaced_one_is_v1(self):
        m = rig(self, versions=TV.two(published="v2"))
        shutil.copytree(m.home / "versions" / "plaza" / "v2", m.home / "versions" / "plaza" / "v1")
        result = bring_and_take(m, hyperfy_zip(), replace="v1")
        self.assertEqual((result["state"], result["version"], result["made"]), ("done", "v1", "v3"))
        meta = m.meta()
        self.assertEqual((meta["published"], sorted(meta["versions"])), ("v2", ["v2", "v3"]))

    def test_the_rules_of_replace_in_order(self):
        for versions, replace, rule in (
            (TV.two(), "", "full"),
            (TV.two(), "v1", "published"),
            (TV.two(), "v9", "no-version"),
            (TV.two(published="v2"), "v2", "published"),
            (None, "v2", "no-version"),
            (None, "v1", "no-version"),  # one version: replace must be empty, even for the only one
        ):
            with self.subTest(replace=replace, two=versions is not None):
                m = rig(self, versions=versions)
                before = Snapshot(m)
                result = bring_and_take(m, hyperfy_zip(), replace=replace)
                self.assertEqual((result["state"], result["rule"], result["version"]), ("failed", rule, replace))
                self.assertNotIn("made", result)
                self.assertEqual(m.log()[-1]["rule"], rule)
                before.same(self)
                nothing_left(self, m)

    def test_a_world_that_used_every_name_is_full(self):
        m = rig(self, versions={**TV.two(), "next": 10_000})
        result = bring_and_take(m, hyperfy_zip(), replace="v2")
        self.assertEqual((result["state"], result["rule"]), ("failed", "full"))
        nothing_left(self, m)


class Refusals(unittest.TestCase):
    def assert_refused(self, m, result, rule, before, detail=None):
        self.assertEqual((result["state"], result["rule"]), ("failed", rule))
        if detail is not None:
            self.assertEqual(result["entry"], detail)
        self.assertEqual(m.log()[-1]["rule"], rule)
        before.same(self)
        nothing_left(self, m)

    def two(self):
        m = rig(self, versions=TV.two())
        return m, Snapshot(m)

    def test_a_zip_of_the_wrong_hash_is_a_fingerprint_failure(self):
        m, before = self.two()
        data = hyperfy_zip()
        result = bring_and_take(m, data, replace="v2", sha="00" * 32)
        self.assert_refused(m, result, "fingerprint", before)
        # Nor does a zip of another size pass; nor a request that names no zip.
        m2, before2 = self.two()
        self.assert_refused(m2, bring_and_take(m2, data + b"x" * 5, replace="v2", size=len(data)), "fingerprint",
                            before2)
        m3, before3 = self.two()
        nonce = bring(m3, data, replace="v2", door={"zip": "somewhere-else.zip"})
        with mock.patch.object(R, "run_tool", Tools()):
            m3.take()
        self.assert_refused(m3, m3.result(nonce), "fingerprint", before3)

    def test_a_zip_over_the_cap_is_too_big_before_anything_is_read(self):
        m, before = self.two()
        result = bring_and_take(m, hyperfy_zip(), replace="v2", size=R.ZIP_MAX + 1)
        self.assert_refused(m, result, "zip-too-big", before)

    def test_a_database_left_open_is_refused_db_open(self):
        m, before = self.two()
        result = bring_and_take(m, hyperfy_zip(extra=[("world/db.sqlite-wal", b"half a write")]), replace="v2")
        self.assert_refused(m, result, "db-open", before, "world/db.sqlite-wal")

    def test_a_broken_database_is_refused_and_the_old_version_stays(self):
        m, before = self.two()
        result = bring_and_take(m, hyperfy_zip(db=Z.broken_db_bytes()), replace="v2")
        self.assertEqual(result["rule"], "db-broken")
        before.same(self)
        self.assertEqual((m.home / "versions" / "plaza" / "v2" / "storage.json").read_bytes(), b'{"only": "v2"}')
        nothing_left(self, m)

    def test_the_zips_own_rules_hold(self):
        for extra, rule in (([("world/../out.txt", b"x")], "zip-slip"), ([("world/notes.txt", b"x")], "unexpected")):
            with self.subTest(rule=rule):
                m, before = self.two()
                self.assert_refused(m, bring_and_take(m, hyperfy_zip(extra=extra), replace="v2"), rule, before)
        m, before = self.two()
        self.assert_refused(m, bring_and_take(m, b"this is no zip at all", replace="v2"), "not-a-zip", before)

    def test_a_numinia_engine_zip_into_a_hyperfy_world_is_not_a_world(self):
        m, before = self.two()
        self.assert_refused(m, bring_and_take(m, engine_zip(), replace="v2"), "not-a-world", before)

    def test_a_hyperfy_zip_into_a_numinia_engine_world_is_refused_by_the_engines_checks(self):
        m = rig(self, engine=True, versions=TV.two())
        before = Snapshot(m)
        tools = Tools()
        result = bring_and_take(m, hyperfy_zip(), tools=tools, replace="v2")
        self.assert_refused(m, result, "engine-refused", before, "mundo.json is not here")
        self.assertEqual([c[-1] for c in tools.calls], ["comprobar"])  # it never reached importar
        # The line the engine gave is for whoever asked, never for the log.
        self.assertNotIn("mundo.json is not here", (m.home / "log.jsonl").read_text())

    def test_an_engine_that_cannot_run_is_not_called_a_bad_zip(self):
        m = rig(self, engine=True)

        def broken(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 125, b"", b"docker: no such image\n")

        result = bring_and_take(m, engine_zip(), tools=broken)
        self.assertEqual(result["rule"], "the engine's tools could not run here")
        nothing_left(self, m)
        self.assertFalse((m.home / "versions" / "plaza").exists())

    def test_the_disk_must_keep_its_reserve(self):
        m, before = self.two()
        with mock.patch.object(shutil, "disk_usage", return_value=TD.usage(R.RESERVE)):
            result = bring_and_take(m, hyperfy_zip(), replace="v2")
        self.assert_refused(m, result, "no-room", before)

    def test_a_disk_that_fills_halfway_is_no_room_and_leaves_nothing(self):
        m, before = self.two()
        full = OSError(28, "No space left on device")
        with mock.patch.object(R, "unpack", side_effect=full):
            result = bring_and_take(m, hyperfy_zip(), replace="v2")
        self.assert_refused(m, result, "no-room", before)

    def test_what_fails_for_no_rule_is_the_plain_message(self):
        m, before = self.two()
        with mock.patch.object(R, "unpack", side_effect=OSError(5, "io error")):
            result = bring_and_take(m, hyperfy_zip(), replace="v2")
        self.assert_refused(m, result, V.NOT_WRITTEN, before)

    def test_metadata_that_cannot_be_written_leaves_the_old_version_and_no_new_folder(self):
        m, before = self.two()
        with mock.patch.object(V, "write_versions", side_effect=OSError(30, "read-only file system")):
            result = bring_and_take(m, hyperfy_zip(), replace="v2")
        self.assert_refused(m, result, "versions-unwritable", before)

    def test_an_unlisted_leftover_under_the_new_name_is_cleared_first(self):
        m, before = self.two()
        leftover = m.home / "versions" / "plaza" / "v3"
        leftover.mkdir()
        (leftover / "half").write_bytes(b"a dead run left this")
        result = bring_and_take(m, hyperfy_zip(), replace="v2")
        self.assertEqual((result["state"], result["made"]), ("done", "v3"))
        self.assertFalse((m.home / "versions" / "plaza" / "v3" / "half").exists())

    def test_a_world_deleted_while_the_zip_was_uploading_is_missing_and_the_zip_goes(self):
        m, before = self.two()
        nonce = bring(m, hyperfy_zip(), replace="v2")
        (m.orders / "plaza.json").unlink()  # its DeleteWorld was taken first
        with mock.patch.object(R, "run_tool", Tools()):
            m.take()
        result = m.result(nonce)
        self.assertEqual((result["state"], result["rule"]), ("failed", "missing"))
        nothing_left(self, m)
        self.assertEqual(tree(m.home / "versions" / "plaza"), before.picture["versions"])  # purge_world takes them later

    def test_metadata_that_cannot_be_trusted_is_never_taken_for_the_implicit_one(self):
        m = rig(self)
        (m.home / "versions").mkdir()
        (m.home / "versions" / "plaza.json").write_text("{not json")
        result = bring_and_take(m, hyperfy_zip())
        self.assertEqual((result["state"], result["rule"]), ("failed", "versions-unreadable"))
        self.assertEqual((m.home / "versions" / "plaza.json").read_text(), "{not json")
        nothing_left(self, m)

    def test_a_world_of_an_engine_this_machine_does_not_know_is_refused(self):
        m = rig(self)
        order = json.loads((m.orders / "plaza.json").read_text())
        order["image"] = "ghcr.io/numengames/other:sha-1"
        (m.orders / "plaza.json").write_text(json.dumps(order))
        result = bring_and_take(m, hyperfy_zip())
        self.assertEqual(result["rule"], "unknown-engine")
        nothing_left(self, m)

    def test_a_world_being_switched_is_busy_and_its_zip_goes(self):
        m = rig(self, versions=TV.two())
        R.write_switching(m.home, {"plaza": {"nonce": "a" * 32, "by": None, "from": "v1", "to": "v2",
                                              "inoData": 1, "inoTo": 2}})
        result = bring_and_take(m, hyperfy_zip(), replace="v2")
        self.assertEqual((result["state"], result["rule"]), ("failed", "busy"))
        self.assertEqual(left(m, "uploads"), [])


class Leftovers(unittest.TestCase):
    """A run that died in the middle of an ImportVersion: the next run answers it and clears what it left."""

    def dead(self, m, replace="", made="v2"):
        nonce = os.urandom(16).hex()
        message = TV.message("ImportVersion", nonce=nonce, replace=replace)
        TR.write_json(m.home / "run" / R.IMPORTING, {"plaza": {"nonce": nonce, "by": m.oracle.address,
                                                               "type": "ImportVersion", "made": made,
                                                               "message": message}})
        TR.write_json(m.home / "requests" / f"{nonce}.json",
                      {"request": nonce, "type": "ImportVersion", "world": "plaza", "state": "working", "rule": None,
                       "at": R.utc(NOW), "version": replace})
        (m.home / "staging" / nonce).mkdir()
        (m.home / "staging" / nonce / "half").write_bytes(b"half")
        (m.home / "uploads" / f"{nonce}.zip").write_bytes(b"zip")
        (m.home / "uploads" / f"{nonce}.json").write_text("{}")
        return nonce

    def test_a_run_that_died_before_the_metadata_failed_and_its_folder_goes(self):
        m = rig(self, versions=TV.two())
        folder = m.home / "versions" / "plaza" / "v3"
        folder.mkdir()
        (folder / "db.sqlite").write_bytes(b"whole but not listed")
        nonce = self.dead(m, replace="v2", made="v3")
        meta_before = (m.home / "versions" / "plaza.json").read_bytes()
        m.take()
        result = m.result(nonce)
        self.assertEqual((result["state"], result["rule"], result["version"]),
                         ("failed", "the run stopped in the middle of the import", "v2"))
        nothing_left(self, m)
        self.assertFalse(folder.exists())
        self.assertTrue((m.home / "versions" / "plaza" / "v2").is_dir())  # the old one was never touched
        self.assertEqual((m.home / "versions" / "plaza.json").read_bytes(), meta_before)

    def test_a_run_that_died_after_the_metadata_is_done_and_the_replaced_folder_goes(self):
        m = rig(self, versions=TV.two())
        # The metadata already says v3 and no v2; the run died before it removed v2's folder.
        folder = m.home / "versions" / "plaza" / "v3"
        folder.mkdir()
        (folder / "db.sqlite").write_bytes(b"listed")
        (m.home / "versions" / "plaza.json").unlink()
        m.write_meta({**TV.two(), "next": 4, "versions": {"v1": TV.info(), "v3": TV.info("zip", 6)}})
        nonce = self.dead(m, replace="v2", made="v3")
        m.take()
        result = m.result(nonce)
        self.assertEqual((result["state"], result["rule"], result["version"], result["made"]),
                         ("done", None, "v2", "v3"))
        nothing_left(self, m)
        self.assertTrue(folder.is_dir())
        self.assertFalse((m.home / "versions" / "plaza" / "v2").exists())
        self.assertEqual(m.log()[-1]["state"], "done")

    def test_a_marker_of_a_world_that_is_gone_fails_its_request(self):
        m = rig(self, versions=TV.two())
        nonce = self.dead(m)
        (m.orders / "plaza.json").unlink()
        m.take()
        self.assertEqual(m.result(nonce)["state"], "failed")
        nothing_left(self, m)

    def test_an_answered_request_is_not_answered_again_and_a_marker_of_an_import_of_a_world_is_as_it_was(self):
        m = rig(self, versions=TV.two())
        nonce = self.dead(m)
        TR.write_json(m.home / "requests" / f"{nonce}.json",
                      {"request": nonce, "type": "ImportVersion", "world": "plaza", "state": "done", "rule": None,
                       "at": R.utc(NOW), "version": "", "made": "v2"})
        m.take()
        self.assertEqual(m.result(nonce)["state"], "done")
        self.assertEqual(m.log(), [])
        self.assertFalse((m.home / "run" / R.IMPORTING).exists())
        self.assertEqual(left(m, "staging"), [])
        # A world's import marker has no type: it is still judged by its order.
        other = os.urandom(16).hex()
        TR.write_json(m.home / "run" / R.IMPORTING, {"plaza-copia": {"nonce": other, "by": None, "message": {}}})
        m.take()
        result = m.result(other)
        self.assertEqual((result["type"], result["state"]), ("ImportWorld", "failed"))


if __name__ == "__main__":
    unittest.main()
