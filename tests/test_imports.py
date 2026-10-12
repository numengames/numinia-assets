# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""A world comes back from its zip: the reconciler lets it in, stopped and with keys of its own, or refuses it by rule.

Each import here is a real ImportWorld, signed (EIP-712) by a throwaway
Oracle with the exact size and SHA-256 of a zip the test builds; the zip and
the request are left where the door leaves them (uploads/<n>.zip and the
inbox), and the reconciler's own take_requests does the rest. A refused zip
must leave the book, the worlds' folders, the key files and the code files
exactly as they were, and nothing in staging/ or uploads/.
"""

from pathlib import Path
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
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


R = load("reconcile")
W = load("walletsig")
Z = load("fleet_zips", HERE)

NOW = 1893456000 - 60
CONTROL = "open-1.numinia.com"
CODE = "b0b0cafe5eed1234"  # the code the door made for the import (in no vector's nonce)
STATUE = b"glTF" + bytes(range(256)) * 40
SKY = bytes((i * 7 + 3) % 251 for i in range(6000))
COPY = "c0ffee" + "0" * 26


def world_zip(prefix="world/", extra=(), db=None, **zipping):
    """A world's folder zipped: its database and two files of its assets, under `prefix`, and `extra` entries."""
    files = [(f"{prefix}db.sqlite", db if db is not None else Z.db_bytes()),
             (f"{prefix}assets/statue.glb", STATUE), (f"{prefix}assets/sky.hdr", SKY)]
    return Z.make_zip(files + list(extra), **zipping)


def rebuilt(data, change=None, add=(), drop=()):
    """The same zip, with some entries' bytes changed, some added and some dropped."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        files = [(i.filename, z.read(i)) for i in z.infolist() if i.filename not in drop]
    return Z.make_zip([(n, (change or {}).get(n, b)) for n, b in files] + list(add))


def table(path, name):
    db = sqlite3.connect(str(path))
    try:
        return db.execute(f"select * from {name} order by 1").fetchall()
    finally:
        db.close()


class Machine:
    """A temporary fleet home with its folders and a throwaway Oracle of its own."""

    def __init__(self, test):
        temp = tempfile.TemporaryDirectory(prefix="fleet-import-")
        test.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.orders = self.home / "orders"
        self.codes = self.home / "codes"  # /etc/fleet/codes
        for folder in ("inbox", "orders", "env", "data", "requests", "copies", "run", "uploads", "staging"):
            (self.home / folder).mkdir()
        self.oracle = Z.Oracle(W)
        self.dropped = 0
        self.err = ""

    def bring(self, data, world="plaza-copia", code=CODE, upload=True, signer=None, door=None, **over):
        """An import whose zip has arrived, as the door leaves it: the zip in uploads/, the request in the inbox.

        `door` changes what the door wrote beside the signed request.
        """
        message = Z.import_message(data, world=world, **{"expires": NOW + 600, **over})
        signer = signer or self.oracle
        nonce = message["nonce"]
        if upload:
            (self.home / "uploads" / f"{nonce}.zip").write_bytes(data)
        raw = {"type": "ImportWorld", "message": message, "signature": signer.sign("ImportWorld", message),
               "signer": signer.address, "receivedAt": R.utc(NOW), "adminCode": code,
               "expiresAt": R.utc(NOW + R.SLOT_SECONDS), "bytes": len(data), "zip": f"{nonce}.zip"}
        raw.update(door or {})
        self.dropped += 1
        (self.home / "inbox" / f"{self.dropped:013d}-{nonce}.json").write_text(json.dumps(raw), encoding="utf-8")
        return nonce

    def take(self, now=NOW):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            taken = R.take_requests(self.home, self.orders, "open-1", frozenset({self.oracle.address}), frozenset(),
                                    CONTROL, now, codes_dir=self.codes)
        self.err += err.getvalue()
        return taken

    def result(self, nonce):
        return json.loads((self.home / "requests" / f"{nonce}.json").read_text(encoding="utf-8"))

    def log(self):
        path = self.home / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def state(self):
        """Everything an import may write outside uploads/: the book, the worlds' folders, their keys and codes."""
        found = {}
        for folder in (self.orders, self.home / "data", self.home / "env", self.codes):
            for path in sorted(folder.rglob("*")) if folder.exists() else []:
                found[str(path.relative_to(self.home))] = path.read_bytes() if path.is_file() else "folder"
        return found

    def fleet_copy(self, wid="plaza"):
        """A world of this machine, and the zip its Download makes (build_copy)."""
        data = self.home / "data" / wid
        (data / "assets").mkdir(parents=True)
        Z.world_db(data / "db.sqlite")
        (data / "assets" / "statue.glb").write_bytes(STATUE)
        (data / "assets" / "sky.hdr").write_bytes(SKY)
        (self.home / "env" / f"{wid}.env").write_text("JWT_SECRET=" + "a" * 64 + "\nADMIN_CODE=machinecode\n")
        order = {"id": wid, "card": wid, "state": "running", "server": "open-1", "domain": f"{wid}.numinia.com",
                 "image": Z.IMAGE, "limits": {"memory": "2g", "cpus": "1.5", "maxUploadMb": 50}}
        (self.orders / f"{wid}.json").write_text(json.dumps(order), encoding="utf-8")
        R.build_copy(self.home, order, COPY, "open-1", NOW)
        return (self.home / "copies" / f"{COPY}.zip").read_bytes()


class RoundTrip(unittest.TestCase):
    """The mission's first criterion: a copy made by Download comes back under a new name with the same scene."""

    def test_a_fleet_copy_comes_back_under_a_new_name_stopped_with_its_own_keys(self):
        m = Machine(self)
        data = m.fleet_copy("plaza")
        nonce = m.bring(data, world="plaza-copia")
        taken = m.take()
        self.assertEqual([t["type"] for t in taken], ["ImportWorld"])
        self.assertEqual(m.result(nonce)["state"], "done")
        # The scene: the same rows of blueprints and entities as the copy's database.
        new, old = m.home / "data" / "plaza-copia", m.home / "data" / "plaza"
        for name in ("blueprints", "entities", "config"):
            with self.subTest(table=name):
                self.assertEqual(table(new / "db.sqlite", name), table(old / "db.sqlite", name))
        self.assertEqual(len(table(new / "db.sqlite", "entities")), 5)
        # Every file equals its manifest's SHA-256, and nothing else came out of the zip.
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            manifest = json.loads(z.read("world.json"))
            zipped_env = z.read(".env").decode()
        for item in manifest["files"]:
            with self.subTest(path=item["path"]):
                path = new / item["path"].removeprefix("world/")
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"])
                self.assertEqual(path.stat().st_size, item["bytes"])
        self.assertEqual(sorted(p.relative_to(new).as_posix() for p in new.rglob("*")),
                         ["assets", "assets/sky.hdr", "assets/statue.glb", "db.sqlite"])
        self.assertEqual(new.stat().st_mode & 0o777, 0o755)  # as a created world's folder
        # Born stopped, as the request signed it.
        order = R.read_order(m.orders / "plaza-copia.json")
        self.assertEqual({k: order[k] for k in ("id", "card", "state", "server", "domain", "image", "limits")},
                         {"id": "plaza-copia", "card": "plaza-copia", "state": "stopped", "server": "open-1",
                          "domain": "plaza-copia.numinia.com", "image": Z.IMAGE,
                          "limits": {"memory": "2g", "cpus": "1.5", "maxUploadMb": 50}})
        self.assertEqual(order["createdBy"], m.oracle.address)
        # Its keys are the machine's: the code the door showed, a JWT secret of its own — never the zip's.
        self.assertEqual((m.codes / "plaza-copia").read_text(), CODE + "\n")
        env = (m.home / "env" / "plaza-copia.env").read_text()
        self.assertRegex(env, rf"\AJWT_SECRET=[0-9a-f]{{64}}\nADMIN_CODE={CODE}\n\Z")
        zipped_jwt = zipped_env.splitlines()[0]
        self.assertTrue(zipped_jwt.startswith("JWT_SECRET="))
        self.assertNotIn(zipped_jwt, env)
        self.assertNotIn(zipped_env.splitlines()[1], env)  # nor the zip's admin code
        self.assertNotIn("JWT_SECRET=" + "a" * 64, env)  # nor the original world's
        # Stopped: no container, no site, until an Oracle starts it.
        p = R.plan(m.orders, m.home, "open-1", allowed=set(), admin_code=None, control_host=CONTROL)
        self.assertIn("plaza-copia", p["stopped"])
        self.assertNotIn("plaza-copia", p["running"])
        self.assertNotIn("plaza-copia", p["caddyfile"])
        # Nothing left behind; the log says what was done and never the code.
        for folder in ("uploads", "staging"):
            self.assertEqual(list((m.home / folder).iterdir()), [], folder)
        self.assertFalse((m.home / "run" / R.IMPORTING).exists())
        line = m.log()[-1]
        self.assertEqual((line["type"], line["world"], line["state"], line["by"]),
                         ("ImportWorld", "plaza-copia", "done", W.short(m.oracle.address)))
        self.assertNotIn(CODE, (m.home / "log.jsonl").read_text() + m.err + json.dumps(m.result(nonce)))

    def test_a_started_import_runs_on_the_machines_keys_and_compose_never_the_zips(self):
        """A zip carrying a hostile .env and docker-compose.yml: neither is used."""
        hostile_env = "JWT_SECRET=" + "6" * 64 + "\nADMIN_CODE=attacker1\n"
        hostile_compose = ('services:\n  world:\n    image: evil/x\n    privileged: true\n'
                           '    volumes: ["/:/host"]\n    network_mode: host\n')
        m = Machine(self)
        nonce = m.bring(world_zip(extra=[(".env", hostile_env), ("docker-compose.yml", hostile_compose)]))
        m.take()
        self.assertEqual(m.result(nonce)["state"], "done")
        env = (m.home / "env" / "plaza-copia.env").read_text()
        self.assertNotIn("6" * 64, env)
        self.assertNotIn("attacker1", env)
        self.assertIn(f"ADMIN_CODE={CODE}\n", env)
        self.assertFalse((m.home / "data" / "plaza-copia" / ".env").exists())
        self.assertFalse((m.home / "data" / "plaza-copia" / "docker-compose.yml").exists())
        # Started by an Oracle (StartWorld): the compose file is the machine's, rendered from the order.
        order = R.read_order(m.orders / "plaza-copia.json")
        (m.orders / "plaza-copia.json").write_text(json.dumps({**order, "state": "running"}))
        p = R.plan(m.orders, m.home, "open-1", allowed=set(), admin_code=None, control_host=CONTROL)
        self.assertEqual(p["running"], ["plaza-copia"])
        service = json.loads(p["compose"])["services"]["plaza-copia"]
        self.assertEqual(service["image"], Z.IMAGE)
        self.assertEqual(service["volumes"], [f"{m.home}/data/plaza-copia:/app/world"])
        self.assertEqual(service["env_file"], f"{m.home}/env/plaza-copia.env")
        for word in ("privileged", "/:/host", "network_mode", "evil/x"):
            self.assertNotIn(word, p["compose"])

    def test_the_three_layouts_come_back(self):
        """The world's folder as `world/` (a fleet copy, a Tower backup), as one top folder, or as the zip's root."""
        extra_files = [("collections/default/app.js", b"export default 1\n"), ("storage.json", b"{}\n"),
                       ("assets/", b"")]
        for prefix in ("world/", "mi-mundo/", ""):
            with self.subTest(layout=prefix or "root"):
                m = Machine(self)
                data = world_zip(prefix, extra=[(prefix + name, body) for name, body in extra_files])
                if prefix == "world/":  # a Tower backup carries its engine image and guides beside the world
                    data = rebuilt(data, add=[("engine/numinia-hyperfy2.tar.gz", b"\x1f\x8b" + b"\0" * 64),
                                              ("docs/DEPLOY-LOCAL.md", b"# deploy\n"), ("README.md", b"# hi\n")])
                nonce = m.bring(data)
                m.take()
                self.assertEqual(m.result(nonce)["state"], "done", m.result(nonce))
                folder = m.home / "data" / "plaza-copia"
                self.assertEqual(sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()),
                                 ["assets/sky.hdr", "assets/statue.glb", "collections/default/app.js", "db.sqlite",
                                  "storage.json"])
                self.assertEqual((folder / "assets" / "statue.glb").read_bytes(), STATUE)

    def test_the_databases_shared_memory_file_is_left_behind(self):
        m = Machine(self)
        nonce = m.bring(world_zip(extra=[("world/db.sqlite-shm", b"\0" * 32768)]))
        m.take()
        self.assertEqual(m.result(nonce)["state"], "done")
        self.assertFalse((m.home / "data" / "plaza-copia" / "db.sqlite-shm").exists())

    def test_a_zip_with_zip64_end_records_comes_back(self):
        m = Machine(self)
        files = [("world/db.sqlite", Z.db_bytes()), ("world/assets/statue.glb", STATUE)]
        nonce = m.bring(Z.zip64(files))
        m.take()
        self.assertEqual(m.result(nonce)["state"], "done")


class ImportCase(unittest.TestCase):
    def refused(self, data, rule, entry=None, machine=None, **bring):
        """Bring this zip; it must be refused by `rule` (naming `entry`), leaving nothing behind."""
        m = machine or Machine(self)
        before = m.state()
        nonce = m.bring(data, **bring)
        m.take()
        result = m.result(nonce)
        self.assertEqual((result["state"], result["rule"]), ("failed", rule), result)
        if entry is not None:
            self.assertEqual(result["entry"], entry)
        self.assertEqual(m.state(), before)  # the book, the worlds' folders, keys and codes as they were
        self.assertEqual(list((m.home / "staging").iterdir()), [])
        self.assertEqual(list((m.home / "uploads").iterdir()), [])  # its zip went too
        self.assertFalse((m.home / "run" / R.IMPORTING).exists())
        line = m.log()[-1]
        self.assertEqual((line["type"], line["state"], line["rule"]), ("ImportWorld", "failed", rule))
        self.assertNotIn("entry", line)  # the log names the rule, never what the zip carried
        self.assertNotIn(CODE, (m.home / "log.jsonl").read_text() + m.err)
        return m, result


class Refusals(ImportCase):
    """One hostile zip per rule, each refused before anything is written outside uploads/."""

    def test_zip_slip(self):
        for name in ("../x", "/etc/x", "C:\\x", "world/assets/../../x", "c:/x", "world/./db2", "world//x"):
            with self.subTest(name=name):
                self.refused(world_zip(extra=[(name, b"out")]), "zip-slip", entry=name)

    def test_link(self):
        self.refused(world_zip(links=[("world/assets/link.glb", "/etc/shadow")]), "link", entry="world/assets/link.glb")
        self.refused(world_zip(devices=["world/assets/tty"]), "link", entry="world/assets/tty")

    def test_file_too_big(self):
        # The database is small here, so the asset is the first entry over the (lowered) cap.
        with mock.patch.object(R, "FILE_MAX", 4000):
            self.refused(Z.make_zip([("world/db.sqlite", b"0" * 100), ("world/assets/big.bin", b"x" * 4001)]),
                         "file-too-big", entry="world/assets/big.bin")

    def test_ratio(self):
        self.refused(world_zip(extra=[("world/assets/bomb.bin", b"\0" * (2 * 1024**2))]), "ratio",
                     entry="world/assets/bomb.bin")

    def test_unpacked_too_big(self):
        with mock.patch.object(R, "UNPACKED_MAX", 20_000):
            self.refused(world_zip(extra=[(f"world/assets/{i}.bin", os.urandom(4000)) for i in range(6)]),
                         "unpacked-too-big")

    def test_too_many_files(self):
        with mock.patch.object(R, "ENTRIES_MAX", 5):
            self.refused(world_zip(extra=[(f"world/assets/{i}.bin", b"x") for i in range(3)]), "too-many-files")
        # A ZIP64 end that lists more than the cap is refused before zipfile reads a single entry of it.
        self.refused(Z.zip64([("world/db.sqlite", Z.db_bytes())], count=R.ENTRIES_MAX + 1), "too-many-files")

    def test_encrypted(self):
        self.refused(world_zip(encrypted=["world/assets/statue.glb"]), "encrypted", entry="world/assets/statue.glb")

    def test_not_a_zip(self):
        self.refused(b"this is not a zip, only text " * 40, "not-a-zip")
        self.refused(world_zip()[:-30], "not-a-zip")  # cut short: no end record

    def test_not_a_world(self):
        self.refused(Z.make_zip([("mundo/assets/statue.glb", STATUE), ("README.md", b"hi")]), "not-a-world")
        # Two top folders holding a database, none at world/ nor at the root: which one is the world?
        self.refused(Z.make_zip([("a/db.sqlite", Z.db_bytes()), ("b/db.sqlite", Z.db_bytes())]), "not-a-world")

    def test_unexpected(self):
        self.refused(world_zip(extra=[("world/notes.txt", b"hello")]), "unexpected", entry="world/notes.txt")
        self.refused(world_zip(extra=[("world/scripts/", b"")]), "unexpected", entry="world/scripts/")
        # A file and a folder under one name cannot both be unpacked.
        self.refused(world_zip(extra=[("world/assets/a", b"1"), ("world/assets/a/b", b"2")]), "unexpected",
                     entry="world/assets/a/b")

    def test_db_open(self):
        for sidecar in ("db.sqlite-wal", "db.sqlite-journal"):
            with self.subTest(sidecar=sidecar):
                self.refused(world_zip(extra=[(f"world/{sidecar}", b"\0" * 64)]), "db-open", entry=f"world/{sidecar}")

    def test_db_broken(self):
        self.refused(world_zip(db=b"SQLite format 3\0" + os.urandom(8000)), "db-broken")
        self.refused(world_zip(db=os.urandom(8192)), "db-broken")
        self.refused(world_zip(db=Z.broken_db_bytes()), "db-broken")

    def test_db_foreign(self):
        self.refused(world_zip(db=Z.foreign_db_bytes()), "db-foreign")

    def test_manifest(self):
        m = Machine(self)
        copy = m.fleet_copy("plaza")
        # The same size, other bytes: only the SHA-256 tells, while the file is written.
        tampered = rebuilt(copy, change={"world/assets/sky.hdr": bytes(reversed(SKY))})
        self.refused(tampered, "manifest", entry="world/assets/sky.hdr", machine=m)
        for case, data in {
            "another size": rebuilt(copy, change={"world/assets/sky.hdr": SKY + b"!"}),
            "a file it does not list": rebuilt(copy, add=[("world/assets/extra.bin", b"x")]),
            "a file it lists, missing": rebuilt(copy, drop=["world/assets/sky.hdr"]),
            "a list that cannot be read": rebuilt(copy, change={"world.json": json.dumps({"files": [{"path": 1}]})}),
        }.items():
            with self.subTest(case=case):
                self.refused(data, "manifest", machine=m)

    def test_fingerprint(self):
        data = world_zip()
        self.refused(data, "fingerprint", zipSha256="0" * 64)  # not the zip the Oracle signed
        self.refused(data, "fingerprint", zipBytes=len(data) + 1)
        self.refused(data, "fingerprint", upload=False)  # no zip came
        m = Machine(self)
        elsewhere = m.home / "elsewhere.zip"
        elsewhere.write_bytes(data)
        message = Z.import_message(data, world="plaza-copia", expires=NOW + 600)
        os.symlink(elsewhere, m.home / "uploads" / f"{message['nonce']}.zip")  # a link where the zip should be
        raw = {"type": "ImportWorld", "message": message, "signature": m.oracle.sign("ImportWorld", message),
               "signer": m.oracle.address, "receivedAt": R.utc(NOW), "adminCode": CODE,
               "zip": f"{message['nonce']}.zip"}
        (m.home / "inbox" / f"0000000000001-{message['nonce']}.json").write_text(json.dumps(raw))
        m.take()
        self.assertEqual(m.result(message["nonce"])["rule"], "fingerprint")
        self.assertFalse(os.path.lexists(m.home / "uploads" / f"{message['nonce']}.zip"))
        self.assertTrue(elsewhere.exists())  # the link went, never what it pointed at
        # The entry must name the zip of its own request.
        self.refused(data, "fingerprint", door={"zip": "../../etc/shadow"})


class Rechecks(ImportCase):
    """The reconciler checks again what the door checked: a door broken into proves nothing."""

    def test_the_name_and_the_address_must_still_be_free(self):
        data = world_zip()
        m = Machine(self)
        (m.orders / "plaza-copia.json").write_text(json.dumps({"id": "plaza-copia", "state": "running"}))
        self.refused(data, "exists", machine=m)
        m = Machine(self)
        (m.home / "data" / "plaza-copia").mkdir()
        (m.home / "data" / "plaza-copia" / "keep.txt").write_text("another world's")
        self.refused(data, "exists", machine=m)  # never unpacked into what a world left
        m = Machine(self)
        (m.home / "run" / "purge.json").write_text(json.dumps({"plaza-copia": {"nonce": "a" * 32}}))
        self.refused(data, "still-deleting", machine=m)
        m = Machine(self)
        other = {"id": "otra", "card": "otra", "state": "running", "server": "open-1",
                 "domain": "plaza-copia.numinia.com", "image": Z.IMAGE,
                 "limits": {"memory": "2g", "cpus": "1.5", "maxUploadMb": 50}}
        (m.orders / "otra.json").write_text(json.dumps(other))
        self.refused(data, "address-taken", machine=m)
        self.refused(data, "refused", domain="plaza-copia.evil.example")

    def test_the_caps_and_the_disk_are_checked_again(self):
        data = world_zip()
        with mock.patch.object(R, "ZIP_MAX", len(data) - 1):
            self.refused(data, "zip-too-big")
        with mock.patch.object(R, "RESERVE", 10**18):  # the disk this machine cannot keep after unpacking
            self.refused(data, "no-room")

    def test_only_an_oracle_of_this_machine_and_a_fresh_request(self):
        data = world_zip()
        m = Machine(self)
        stranger = Z.Oracle(W)
        nonce = m.bring(data, signer=stranger)
        m.take()
        self.assertIn("not an Oracle", m.result(nonce)["rule"])
        self.assertEqual(list((m.home / "uploads").iterdir()), [])  # its zip went anyway
        self.assertEqual(m.state(), {})
        # An import has the slot's hour more than another request to reach the inbox, and no more.
        m = Machine(self)
        late = m.bring(data, expires=NOW - R.GRACE - R.SLOT_SECONDS + 30)
        m.take()
        self.assertEqual(m.result(late)["state"], "done")
        m = Machine(self)
        too_late = m.bring(data, expires=NOW - R.GRACE - R.SLOT_SECONDS - 1)
        m.take()
        self.assertIn("expired", m.result(too_late)["rule"])

    def test_the_code_must_be_plain(self):
        m = Machine(self)
        nonce = m.bring(world_zip(), code="bad code\nJWT_SECRET=x")
        m.take()
        self.assertIn("not a plain code", m.result(nonce)["rule"])
        self.assertEqual(m.state(), {})
        self.assertEqual(list((m.home / "uploads").iterdir()), [])


class Leftovers(unittest.TestCase):
    def test_an_import_stopped_halfway_is_answered_and_cleared_on_the_next_run(self):
        m = Machine(self)
        message = Z.import_message(b"x", world="plaza-copia", expires=NOW + 600)
        nonce = message["nonce"]
        (m.home / "run" / R.IMPORTING).write_text(json.dumps(
            {"plaza-copia": {"nonce": nonce, "by": m.oracle.address, "message": message}}))
        (m.home / "staging" / nonce / "assets").mkdir(parents=True)
        (m.home / "staging" / nonce / "db.sqlite").write_bytes(b"half")
        (m.home / "uploads" / f"{nonce}.zip").write_bytes(b"zip")
        m.take()
        result = m.result(nonce)
        self.assertEqual((result["state"], result["type"], result["world"]), ("failed", "ImportWorld", "plaza-copia"))
        self.assertEqual(list((m.home / "staging").iterdir()), [])
        self.assertEqual(list((m.home / "uploads").iterdir()), [])
        self.assertFalse((m.home / "run" / R.IMPORTING).exists())
        self.assertEqual(m.state(), {})
        # One whose order was written (its last step) is answered done; a final answer is never rewritten.
        m = Machine(self)
        (m.orders / "plaza-copia.json").write_text(json.dumps({"id": "plaza-copia", "state": "stopped"}))
        (m.home / "run" / R.IMPORTING).write_text(json.dumps({"plaza-copia": {"nonce": nonce, "by": None}}))
        m.take()
        self.assertEqual(m.result(nonce)["state"], "done")
        (m.home / "run" / R.IMPORTING).write_text(json.dumps({"plaza-copia": {"nonce": nonce}}))
        m.take()
        self.assertEqual(len(m.log()), 1)

    def test_slots_past_their_hour_and_what_nothing_names_go(self):
        m = Machine(self)
        uploads = m.home / "uploads"
        n = [f"{i:x}" * 32 for i in range(1, 8)]

        def slot(nonce, ends):
            (uploads / f"{nonce}.json").write_text(json.dumps({"type": "ImportWorld", "expiresAt": R.utc(ends)}))

        slot(n[0], NOW - 1)  # past its hour
        (uploads / f".{n[0]}.zip.part").write_bytes(b"part")
        slot(n[1], NOW + 60)  # still open, its upload under way
        (uploads / f".{n[1]}.zip.part").write_bytes(b"part")
        old = NOW - R.SLOT_SECONDS - 1
        for name in (f"{n[2]}.zip", f"{n[3]}.zip", f".{n[4]}.zip.part", f".{n[5]}.json.tmp"):
            (uploads / name).write_bytes(b"left")
            os.utime(uploads / name, (old, old))
        (m.home / "inbox" / f"0000000000001-{n[3]}.json").write_text("{}")  # its request waits: kept
        (uploads / f"{n[6]}.zip").write_bytes(b"fresh")  # its request is about to be dropped: kept
        os.utime(uploads / f"{n[6]}.zip", (NOW, NOW))
        (uploads / "notes.txt").write_bytes(b"not ours")
        os.utime(uploads / "notes.txt", (old, old))
        self.assertEqual(R.prune_uploads(m.home, NOW), [n[0]])
        self.assertEqual(sorted(p.name for p in uploads.iterdir()),
                         sorted([f"{n[1]}.json", f".{n[1]}.zip.part", f"{n[3]}.zip", f"{n[6]}.zip", "notes.txt"]))


class Inspection(unittest.TestCase):
    """The pieces of the inspection, one by one."""

    def test_names_that_leave_their_folder_are_never_safe(self):
        for name in ("db.sqlite", "world/assets/a b/c.glb", "assets/", "world/c:x"):
            with self.subTest(name=name):
                self.assertTrue(R.safe_name(zipfile.ZipInfo(name)))
        for name in ("../x", "/x", "a/../x", "a\\x", "C:x", "c:\\x", "./x", "a//x", "a/./x", "..", "", "/"):
            with self.subTest(name=name):
                self.assertFalse(R.safe_name(zipfile.ZipInfo(name)))

    def test_what_a_worlds_folder_keeps(self):
        cases = {"db.sqlite": "file", "storage.json": "file", "assets/x.glb": "file", "collections/a/b.js": "file",
                 "assets/": "folder", "collections/a/": "folder", "db.sqlite-shm": "skip",
                 "db.sqlite-wal": "db-open", "db.sqlite-journal": "db-open", "notes.txt": "unexpected",
                 "assets": "unexpected", "scripts/": "unexpected", "world.json": "unexpected", ".env": "unexpected"}
        for relative, where in cases.items():
            with self.subTest(relative=relative):
                self.assertEqual(R.place(relative), where)

    def test_where_the_world_starts(self):
        self.assertEqual(R.world_root(["world/db.sqlite", "mundo/db.sqlite", "db.sqlite"]), "world/")
        self.assertEqual(R.world_root(["mundo/db.sqlite", "otro/x", "db.sqlite"]), "mundo/")
        self.assertEqual(R.world_root(["a/db.sqlite", "b/db.sqlite", "db.sqlite"]), "")
        self.assertIsNone(R.world_root(["a/db.sqlite", "b/db.sqlite"]))
        self.assertIsNone(R.world_root(["world/b/db.sqlite", "x/db.sqlite.bak"]))

    def test_the_index_is_read_from_the_end_zip64_included(self):
        files = [("world/db.sqlite", b"d"), ("world/assets/a", b"a"), ("world/assets/b", b"b")]
        plain, wide = Z.make_zip(files), Z.zip64(files)
        self.assertEqual(R.zip_index(io.BytesIO(plain))[0], 3)
        self.assertEqual(R.zip_index(io.BytesIO(wide))[0], 3)
        self.assertEqual(R.zip_index(io.BytesIO(Z.zip64(files, count=123456)))[0], 123456)
        self.assertIsNone(R.zip_index(io.BytesIO(b"PK\x05\x06")))
        self.assertIsNone(R.zip_index(io.BytesIO(b"")))

    def test_unpacking_counts_what_it_writes_and_never_trusts_the_index(self):
        """An entry that says it is small but gives more: refused over the cap while it is written."""

        class Lying:
            def __init__(self, size):
                self.size = size

            def open(self, info):
                return io.BytesIO(b"x" * self.size)

        info = zipfile.ZipInfo("assets/big.bin")
        info.file_size = 10
        plan = {"root": "", "kept": [info], "manifest": None}
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(R, "FILE_MAX", 1000), self.assertRaises(R.Refused) as caught:
                R.unpack(Lying(5000), plan, Path(temp))
            self.assertEqual(caught.exception.rule, "file-too-big")
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(R, "UNPACKED_MAX", 1000), self.assertRaises(R.Refused) as caught:
                R.unpack(Lying(5000), plan, Path(temp))
            self.assertEqual(caught.exception.rule, "unpacked-too-big")

    def test_the_room_an_import_holds(self):
        self.assertEqual(R.reservation(1000), 1000 + 100_000)
        self.assertEqual(R.reservation(R.ZIP_MAX), R.ZIP_MAX + R.UNPACKED_MAX)
        self.assertGreaterEqual(R.unpacking(R.ZIP_MAX), R.ZIP_MAX)  # the machine's own copy of a zip fits in it

    def test_an_import_request_has_its_shape(self):
        vector = json.loads((HERE / "fleet_vectors.json").read_text())
        message = next(v["message"] for v in vector["vectors"] if v["type"] == "ImportWorld")
        self.assertIsNone(R.request_problem("ImportWorld", message))
        for name, bad in {
            "upper-case fingerprint": {**message, "zipSha256": message["zipSha256"].upper()},
            "short fingerprint": {**message, "zipSha256": message["zipSha256"][:-1]},
            "fingerprint and a newline": {**message, "zipSha256": message["zipSha256"] + "\n"},
            "no bytes": {**message, "zipBytes": 0},
            "bytes as text": {**message, "zipBytes": "50331648"},
            "bytes as a bool": {**message, "zipBytes": True},
            "a create's fields": {k: v for k, v in message.items() if k not in ("zipBytes", "zipSha256")},
            "an extra field": {**message, "zip": "../x"},
        }.items():
            with self.subTest(case=name):
                self.assertIsNotNone(R.request_problem("ImportWorld", bad))
        # Over the cap is not a shape: the door and the reconciler refuse it by name (zip-too-big).
        self.assertIsNone(R.request_problem("ImportWorld", {**message, "zipBytes": R.ZIP_MAX + 1}))


if __name__ == "__main__":
    unittest.main()
