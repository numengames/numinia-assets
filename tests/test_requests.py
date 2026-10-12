# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""A signed request changes the machine's book once, and only when an Oracle of the machine signed it.

The requests are the ones in fleet_vectors.json: real EIP-712 signatures made
by viem for the machine `open-1` and the world `plaza`, valid until 2030.
"""

from pathlib import Path
import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
MACHINE = ROOT / "open-worlds" / "machine"
VECTORS = json.loads((Path(__file__).parent / "fleet_vectors.json").read_text(encoding="utf-8"))


def load(name):
    if str(MACHINE) not in sys.path:
        sys.path.insert(0, str(MACHINE))
    spec = importlib.util.spec_from_file_location(name, MACHINE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


R = load("reconcile")
SIGNED = {v["type"]: v for v in VECTORS["vectors"]}
ORACLE = SIGNED["CreateWorld"]["signer"]
EXPIRES = SIGNED["CreateWorld"]["message"]["expires"]
NOW = EXPIRES - 60
CONTROL = "open-1.numinia.com"
COPY = "c0ffee" + "0" * 26


class Machine:
    """A temporary fleet home with an inbox, a book and a world folder."""

    def __init__(self, test):
        temp = tempfile.TemporaryDirectory(prefix="fleet-req-")
        test.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.orders = self.home / "orders"
        for folder in ("inbox", "orders", "env", "data", "requests", "copies", "run"):
            (self.home / folder).mkdir()
        self.dropped = 0

    def drop(self, kind, message=None, signature=None, copy=None):
        vector = SIGNED[kind]
        raw = {
            "type": kind,
            "message": message or vector["message"],
            "signature": signature or vector["signature"],
            "signer": vector["signer"],
            "receivedAt": "2029-12-31T23:59:00Z",
        }
        if copy is not None:
            raw["copy"] = copy
        self.dropped += 1
        name = f"{self.dropped:013d}-{raw['message']['nonce']}.json"
        (self.home / "inbox" / name).write_text(json.dumps(raw), encoding="utf-8")

    def take(self, signers=frozenset({ORACLE}), alias="open-1", now=NOW, control=CONTROL):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            taken = R.take_requests(self.home, self.orders, alias, signers, frozenset(), control, now)
        self.err = err.getvalue()
        return taken

    def result(self, kind):
        nonce = SIGNED[kind]["message"]["nonce"]
        return json.loads((self.home / "requests" / f"{nonce}.json").read_text(encoding="utf-8"))

    def order(self, wid="plaza"):
        return R.read_order(self.orders / f"{wid}.json")

    def log(self):
        path = self.home / "log.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def put_world(self, domain="plaza.numinia.com", state="running"):
        order = {
            "id": "plaza", "card": "plaza", "state": state, "server": "open-1", "domain": domain,
            "image": SIGNED["CreateWorld"]["message"]["image"],
            "limits": {"memory": "2g", "cpus": "1.5", "maxUploadMb": 50},
        }
        (self.orders / "plaza.json").write_text(json.dumps(order), encoding="utf-8")
        data = self.home / "data" / "plaza"
        (data / "assets").mkdir(parents=True)
        db = sqlite3.connect(str(data / "db.sqlite"))
        db.execute("create table config (key text, value text)")
        db.execute("insert into config values ('settings', '{\"title\": \"Plaza\"}')")
        db.commit()
        db.close()
        (data / "assets" / "statue.glb").write_bytes(b"glTF" + bytes(range(256)) * 40)
        (self.home / "env" / "plaza.env").write_text("JWT_SECRET=" + "a" * 64 + "\nADMIN_CODE=machinecode\n")
        return order


class Create(unittest.TestCase):
    def test_a_signed_create_writes_the_order_and_is_answered(self):
        m = Machine(self)
        m.drop("CreateWorld")
        taken = m.take()
        self.assertEqual([t["type"] for t in taken], ["CreateWorld"])
        order = m.order()
        self.assertEqual(
            {k: order[k] for k in ("id", "card", "state", "server", "domain", "limits")},
            {"id": "plaza", "card": "plaza", "state": "running", "server": "open-1",
             "domain": "plaza.numinia.com", "limits": {"memory": "2g", "cpus": "1.5", "maxUploadMb": 50}},
        )
        self.assertEqual(order["createdBy"], ORACLE)
        self.assertEqual(m.result("CreateWorld")["state"], "done")
        self.assertEqual(list((m.home / "inbox").iterdir()), [])
        self.assertEqual(m.log()[-1]["by"], R.walletsig.short(ORACLE))
        self.assertNotIn(ORACLE, (m.home / "log.jsonl").read_text())
        # The machine's own reconciler obeys it: the new world is in the plan.
        p = R.plan(m.orders, m.home, "open-1", allowed=set(), admin_code="machinecode", control_host=CONTROL)
        self.assertEqual(p["running"], ["plaza"])

    def test_only_an_oracle_of_this_machine_counts(self):
        m = Machine(self)
        m.drop("CreateWorld")
        m.take(signers=frozenset())
        self.assertIsNone(m.order())
        self.assertEqual(m.result("CreateWorld")["state"], "failed")
        self.assertIn("not an Oracle", m.result("CreateWorld")["rule"])

    def test_a_changed_request_is_not_the_oracles(self):
        m = Machine(self)
        message = {**SIGNED["CreateWorld"]["message"], "world": "evil", "domain": "evil.numinia.com"}
        m.drop("CreateWorld", message=message)
        m.take()
        self.assertIsNone(m.order("evil"))
        self.assertIsNone(m.order())

    def test_a_broken_signature_is_refused(self):
        m = Machine(self)
        m.drop("CreateWorld", signature="0x" + "00" * 65)
        m.take()
        self.assertIsNone(m.order())
        self.assertIn("signature", m.result("CreateWorld")["rule"])

    def test_a_request_counts_once(self):
        m = Machine(self)
        m.drop("CreateWorld")
        m.take()
        first = m.result("CreateWorld")
        (m.orders / "plaza.json").unlink()
        m.drop("CreateWorld")
        m.take()
        self.assertIsNone(m.order())
        self.assertEqual(m.result("CreateWorld"), first)
        self.assertIn("used before", m.err)

    def test_an_old_request_is_refused_even_if_the_door_took_it(self):
        m = Machine(self)
        m.drop("CreateWorld")
        m.take(now=EXPIRES + R.GRACE + 1)
        self.assertIsNone(m.order())
        self.assertIn("expired", m.result("CreateWorld")["rule"])

    def test_a_request_for_another_machine_is_refused(self):
        m = Machine(self)
        m.drop("CreateWorld")
        m.take(alias="open-2")
        self.assertIsNone(m.order())
        self.assertIn("another machine", m.result("CreateWorld")["rule"])

    def test_a_world_that_exists_or_an_address_in_use_is_refused(self):
        m = Machine(self)
        m.put_world()
        before = (m.orders / "plaza.json").read_text()
        m.drop("CreateWorld")
        m.take()
        self.assertEqual((m.orders / "plaza.json").read_text(), before)
        self.assertIn("already exists", m.result("CreateWorld")["rule"])

        other = Machine(self)
        other.put_world()
        (other.orders / "plaza.json").rename(other.orders / "otra.json")
        moved = json.loads((other.orders / "otra.json").read_text())
        (other.orders / "otra.json").write_text(json.dumps({**moved, "id": "otra"}))
        other.drop("CreateWorld")
        other.take()
        self.assertIsNone(other.order())
        self.assertIn("address", other.result("CreateWorld")["rule"])

    def test_a_world_may_not_take_the_machines_own_address(self):
        m = Machine(self)
        m.drop("CreateWorld")
        m.take(control="plaza.numinia.com")
        self.assertIsNone(m.order())
        self.assertIn("own address", m.result("CreateWorld")["rule"])

    def test_garbage_in_the_inbox_is_dropped_without_an_answer(self):
        m = Machine(self)
        (m.home / "inbox" / "0000000000001-x.json").write_text("{not json", encoding="utf-8")
        (m.home / "inbox" / "0000000000002-y.json").write_text(json.dumps({"type": "CreateWorld"}), encoding="utf-8")
        m.take()
        self.assertEqual(list((m.home / "inbox").iterdir()), [])
        self.assertEqual(list((m.home / "requests").iterdir()), [])

    def test_request_shapes(self):
        create = SIGNED["CreateWorld"]["message"]
        self.assertIsNone(R.request_problem("CreateWorld", create))
        bad = {
            "unknown kind": ("RebootMachine", create),
            "missing field": ("CreateWorld", {k: v for k, v in create.items() if k != "cpus"}),
            "extra field": ("CreateWorld", {**create, "privileged": True}),
            "newline in domain": ("CreateWorld", {**create, "domain": "plaza.numinia.com\n"}),
            "image elsewhere": ("CreateWorld", {**create, "image": "docker.io/evil/x:sha-0123456"}),
            "upload as text": ("CreateWorld", {**create, "maxUploadMb": "50"}),
            "upload as bool": ("CreateWorld", {**create, "maxUploadMb": True}),
            "upload zero": ("CreateWorld", {**create, "maxUploadMb": 0}),
            "upload huge": ("CreateWorld", {**create, "maxUploadMb": 10**9}),
            "negative expiry": ("CreateWorld", {**create, "expires": -1}),
            "short nonce": ("CreateWorld", {**create, "nonce": "abc"}),
            "message not an object": ("CreateWorld", ["machine"]),
        }
        for name, (kind, message) in bad.items():
            with self.subTest(case=name):
                self.assertIsNotNone(R.request_problem(kind, message))


class StopStartShow(unittest.TestCase):
    def test_stop_and_start_change_only_the_state(self):
        m = Machine(self)
        original = m.put_world()
        m.drop("StopWorld")
        m.take()
        self.assertEqual(m.order(), {**original, "state": "stopped"})
        m.drop("StartWorld")
        m.take()
        self.assertEqual(m.order(), original)
        self.assertEqual([m.result(k)["state"] for k in ("StopWorld", "StartWorld")], ["done", "done"])

    def test_a_change_to_a_missing_world_is_refused(self):
        m = Machine(self)
        m.drop("StopWorld")
        m.take()
        self.assertIn("no world", m.result("StopWorld")["rule"])

    def test_showing_the_admin_code_changes_nothing_and_is_logged(self):
        m = Machine(self)
        m.drop("ShowAdminCode")
        m.take()
        self.assertEqual(m.result("ShowAdminCode")["state"], "done")
        self.assertEqual(m.log()[-1]["type"], "ShowAdminCode")
        self.assertEqual(list(m.orders.iterdir()), [])


class Delete(unittest.TestCase):
    def test_a_delete_waits_for_the_container_then_removes_everything(self):
        m = Machine(self)
        m.put_world()
        (m.home / "copies" / f"{COPY}.json").write_text(json.dumps({"copy": COPY, "world": "plaza", "state": "ready"}))
        (m.home / "copies" / f"{COPY}.zip").write_bytes(b"zip")
        m.drop("DeleteWorld")
        m.take()
        self.assertIsNone(m.order())
        self.assertEqual(m.result("DeleteWorld")["state"], "working")
        self.assertTrue((m.home / "data" / "plaza").is_dir())  # Docker has not taken it down yet
        p = R.plan(m.orders, m.home, "open-1", allowed=set(), admin_code="machinecode")
        self.assertEqual(p["running"], [])  # the next compose run removes its container
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(R.purge_deleted(m.home, m.orders, NOW), ["plaza"])
        self.assertFalse((m.home / "data" / "plaza").exists())
        self.assertFalse((m.home / "env" / "plaza.env").exists())
        self.assertEqual(list((m.home / "copies").iterdir()), [])
        self.assertEqual(m.result("DeleteWorld")["state"], "done")
        self.assertFalse((m.home / "run" / "purge.json").exists())

    def test_a_delete_names_the_address_it_deletes(self):
        m = Machine(self)
        m.put_world(domain="other.numinia.com")
        m.drop("DeleteWorld")
        m.take()
        self.assertIsNotNone(m.order())
        self.assertIn("address", m.result("DeleteWorld")["rule"])

    def test_a_folder_reached_through_a_link_is_unlinked_not_emptied(self):
        m = Machine(self)
        outside = m.home / "elsewhere"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        os.symlink(outside, m.home / "data" / "plaza")
        R.purge_world(m.home, "plaza")
        self.assertTrue((outside / "keep.txt").exists())
        self.assertFalse((m.home / "data" / "plaza").exists())
        with self.assertRaises(ValueError):
            R.purge_world(m.home, "../etc")


class Copies(unittest.TestCase):
    def queued_copy(self, m, copy=COPY):
        m.drop("DownloadWorld", copy=copy)
        m.take()

    def test_a_download_is_queued_then_zipped_whole_with_fresh_keys(self):
        m = Machine(self)
        order = m.put_world()
        (m.home / "data" / "plaza" / "db.sqlite-wal").write_bytes(b"half a write")
        os.symlink("/etc/hostname", m.home / "data" / "plaza" / "assets" / "link.txt")
        self.queued_copy(m)
        self.assertEqual(m.result("DownloadWorld")["state"], "working")
        preparing = json.loads((m.home / "copies" / f"{COPY}.json").read_text())
        self.assertEqual(preparing["state"], "preparing")

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(R.make_copies(m.home, m.orders, "open-1", NOW), [COPY])
        status = json.loads((m.home / "copies" / f"{COPY}.json").read_text())
        zipped = m.home / "copies" / f"{COPY}.zip"
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["sha256"], R.file_digest(zipped))
        self.assertTrue(status["filename"].startswith("plaza-2029") or status["filename"].startswith("plaza-2030"))
        self.assertEqual(m.result("DownloadWorld")["state"], "done")

        with zipfile.ZipFile(zipped) as z:
            names = set(z.namelist())
            self.assertEqual(
                names,
                {"world/db.sqlite", "world/assets/statue.glb", "docker-compose.yml", ".env", "README.md", "world.json"},
            )
            with tempfile.TemporaryDirectory() as temp:
                db = Path(z.extract("world/db.sqlite", temp))
                rows = sqlite3.connect(str(db)).execute("select key from config").fetchall()
                self.assertEqual(rows, [("settings",)])
            compose = json.loads(z.read("docker-compose.yml"))
            world = compose["services"]["world"]
            self.assertEqual(world["image"], order["image"])
            self.assertEqual(world["ports"], ["3000:3000"])
            self.assertEqual(world["environment"]["PUBLIC_WS_URL"], "ws://localhost:3000/ws")
            env = z.read(".env").decode()
            self.assertRegex(env, r"\AJWT_SECRET=[0-9a-f]{64}\nADMIN_CODE=[0-9a-f]{12}\n\Z")
            self.assertNotIn("machinecode", env)
            manifest = json.loads(z.read("world.json"))
            self.assertEqual(manifest["world"], "plaza")
            self.assertEqual({f["path"] for f in manifest["files"]}, {"world/db.sqlite", "world/assets/statue.glb"})
            statue = next(f for f in manifest["files"] if f["path"] == "world/assets/statue.glb")
            self.assertEqual(statue["bytes"], (m.home / "data" / "plaza" / "assets" / "statue.glb").stat().st_size)
            self.assertIn("plaza.numinia.com", z.read("README.md").decode())

    def test_a_second_download_minutes_later_gets_the_same_zip(self):
        m = Machine(self)
        m.put_world()
        self.queued_copy(m)
        with contextlib.redirect_stderr(io.StringIO()):
            R.make_copies(m.home, m.orders, "open-1", NOW)
        again = "beef" + "0" * 28
        # A second, separately signed download (its own number) of the same world.
        (m.home / "run" / "copy-queue.json").write_text(
            json.dumps([{"copy": again, "world": "plaza", "nonce": "f" * 32, "by": ORACLE}]))
        with contextlib.redirect_stderr(io.StringIO()):
            R.make_copies(m.home, m.orders, "open-1", NOW + 60)
        first = json.loads((m.home / "copies" / f"{COPY}.json").read_text())
        second = json.loads((m.home / "copies" / f"{again}.json").read_text())
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(second["copy"], again)
        self.assertEqual(os.stat(m.home / "copies" / f"{COPY}.zip").st_ino,
                         os.stat(m.home / "copies" / f"{again}.zip").st_ino)

    def test_a_download_needs_a_copy_number_and_a_world(self):
        m = Machine(self)
        m.put_world()
        m.drop("DownloadWorld")  # no copy number from the door
        m.take()
        self.assertIn("copy number", m.result("DownloadWorld")["rule"])
        n = Machine(self)
        (n.home / "run" / "copy-queue.json").write_text(
            json.dumps([{"copy": COPY, "world": "plaza", "nonce": "e" * 32, "by": ORACLE}]))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(R.make_copies(n.home, n.orders, "open-1", NOW), [])
        self.assertEqual(json.loads((n.home / "copies" / f"{COPY}.json").read_text())["state"], "failed")

    def test_copies_go_after_a_day(self):
        m = Machine(self)
        m.put_world()
        self.queued_copy(m)
        with contextlib.redirect_stderr(io.StringIO()):
            R.make_copies(m.home, m.orders, "open-1", NOW)
        self.assertEqual(R.prune_copies(m.home, NOW + 3600), [])
        self.assertEqual(R.prune_copies(m.home, NOW + R.COPY_SECONDS + 1), [COPY])
        self.assertEqual(list((m.home / "copies").iterdir()), [])


class Adopt(unittest.TestCase):
    def test_the_machine_adopts_its_own_orders_from_the_public_book_once(self):
        m = Machine(self)
        book = m.home / "clone"
        (book / "open-worlds").mkdir(parents=True)
        mine = {"id": "prueba", "card": "prueba", "state": "running", "server": "open-1",
                "domain": "prueba.numinia.com", "image": SIGNED["CreateWorld"]["message"]["image"],
                "limits": {"memory": "2g", "cpus": "1.5", "maxUploadMb": 50}}
        theirs = {**mine, "id": "otro", "card": "otro", "server": "open-2", "domain": "otro.numinia.com"}
        bad = {**mine, "id": "malo", "card": "malo", "image": "ghcr.io/numengames/x:latest"}
        for o in (mine, theirs, bad):
            (book / "open-worlds" / f"{o['id']}.json").write_text(json.dumps(o))
        (book / "open-worlds" / "index.json").write_text('{"orders": ["malo", "otro", "prueba"]}')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(R.adopt_book(book, m.orders, "open-1"), ["prueba"])
        adopted = m.order("prueba")
        self.assertEqual({k: adopted[k] for k in mine}, mine)
        (book / "open-worlds" / "nuevo.json").write_text(json.dumps({**mine, "id": "nuevo", "card": "nuevo"}))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(R.adopt_book(book, m.orders, "open-1"), [])
        self.assertIsNone(m.order("nuevo"))


if __name__ == "__main__":
    unittest.main()
