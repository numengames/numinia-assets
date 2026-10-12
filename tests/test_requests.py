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
DOOR_CODE = "c0dec0dec0de4321"  # a world's own admin code, as the door makes them
JWT = "JWT_SECRET=" + "a" * 64


class Machine:
    """A temporary fleet home with an inbox, a book and a world folder."""

    def __init__(self, test):
        temp = tempfile.TemporaryDirectory(prefix="fleet-req-")
        test.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.orders = self.home / "orders"
        self.codes = self.home / "codes"  # /etc/fleet/codes; made by the reconciler when missing
        for folder in ("inbox", "orders", "env", "data", "requests", "copies", "run"):
            (self.home / folder).mkdir()
        self.dropped = 0

    def drop(self, kind, message=None, signature=None, copy=None, **door):
        """A request as the door leaves it in the inbox; `door` adds what the door wrote (adminCode)."""
        vector = SIGNED[kind]
        raw = {
            "type": kind,
            "message": message or vector["message"],
            "signature": signature or vector["signature"],
            "signer": vector["signer"],
            "receivedAt": "2029-12-31T23:59:00Z",
            **door,
        }
        if copy is not None:
            raw["copy"] = copy
        self.dropped += 1
        name = f"{self.dropped:013d}-{raw['message']['nonce']}.json"
        (self.home / "inbox" / name).write_text(json.dumps(raw), encoding="utf-8")

    def take(self, signers=frozenset({ORACLE}), alias="open-1", now=NOW, control=CONTROL):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            taken = R.take_requests(self.home, self.orders, alias, signers, frozenset(), control, now,
                                    codes_dir=self.codes)
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
        (self.home / "env" / "plaza.env").write_text(f"{JWT}\nADMIN_CODE=machinecode\n")
        return order

    def env(self, wid="plaza"):
        return (self.home / "env" / f"{wid}.env").read_text(encoding="utf-8")


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
        R.write_code(m.codes, "plaza", "machinecode")
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
            self.assertEqual(R.purge_deleted(m.home, m.orders, NOW, codes_dir=m.codes), ["plaza"])
        self.assertFalse((m.home / "data" / "plaza").exists())
        self.assertFalse((m.home / "env" / "plaza.env").exists())
        self.assertFalse((m.codes / "plaza").exists())
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

    def test_a_world_being_deleted_is_not_born_again_until_it_is_gone(self):
        """Its folder and keys outlive its order until Docker takes the container down.

        A world born under the same name or address before then would keep them.
        """
        m = Machine(self)
        m.put_world()
        m.drop("DeleteWorld")
        m.drop("CreateWorld")  # signed a moment later, taken in the same run
        m.take()
        self.assertEqual(m.result("DeleteWorld")["state"], "working")
        self.assertEqual(m.result("CreateWorld")["state"], "failed")
        self.assertIn("still being deleted", m.result("CreateWorld")["rule"])
        self.assertIsNone(m.order())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(R.purge_deleted(m.home, m.orders, NOW, codes_dir=m.codes), ["plaza"])
        self.assertFalse((m.home / "env" / "plaza.env").exists())

    def test_an_address_being_deleted_is_not_given_to_another_world(self):
        m = Machine(self)
        create = SIGNED["CreateWorld"]
        entry = {"type": "CreateWorld", "world": "plaza", "nonce": "c" * 32, "by": ORACLE}
        old = {"vieja": "plaza.numinia.com"}  # deleted a moment ago under the address plaza now asks for
        rule = R.change_book(entry, create["message"], m.orders, "open-1", frozenset(), CONTROL, NOW, old)
        self.assertIn("still being deleted", rule)
        self.assertIsNone(m.order())
        self.assertIsNone(R.change_book(entry, create["message"], m.orders, "open-1", frozenset(), CONTROL, NOW,
                                        {"vieja": "vieja.numinia.com"}))
        self.assertEqual(m.order()["domain"], "plaza.numinia.com")

    def test_a_folder_reached_through_a_link_is_unlinked_not_emptied(self):
        m = Machine(self)
        outside = m.home / "elsewhere"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        os.symlink(outside, m.home / "data" / "plaza")
        R.purge_world(m.home, "plaza", codes_dir=m.codes)
        self.assertTrue((outside / "keep.txt").exists())
        self.assertFalse((m.home / "data" / "plaza").exists())
        with self.assertRaises(ValueError):
            R.purge_world(m.home, "../etc", codes_dir=m.codes)


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


class Codes(unittest.TestCase):
    """Each world has its own admin code: made by the door, written by the reconciler, changed by one signature."""

    def test_a_create_keys_the_world_with_the_code_the_door_made(self):
        m = Machine(self)
        m.drop("CreateWorld", adminCode=DOOR_CODE)
        m.take()
        self.assertEqual(m.result("CreateWorld")["state"], "done")
        self.assertRegex(m.env(), rf"\AJWT_SECRET=[0-9a-f]{{64}}\nADMIN_CODE={DOOR_CODE}\n\Z")
        self.assertEqual((m.home / "env" / "plaza.env").stat().st_mode & 0o777, 0o600)
        self.assertEqual((m.codes / "plaza").read_text(encoding="utf-8"), DOOR_CODE + "\n")
        self.assertEqual((m.codes / "plaza").stat().st_mode & 0o777, 0o640)
        self.assertEqual(m.codes.stat().st_mode & 0o777, 0o750)
        said = (m.home / "log.jsonl").read_text() + m.err + json.dumps(m.result("CreateWorld"))
        self.assertNotIn(DOOR_CODE, said)  # a code is never logged nor answered by the machine
        # It runs on its own keys: a machine with no old shared code starts it.
        p = R.plan(m.orders, m.home, "open-1", allowed=set(), admin_code=None, control_host=CONTROL)
        self.assertEqual((p["running"], p["new_keys"], p["keyless"]), (["plaza"], [], []))

    def test_a_create_from_an_older_door_gets_a_code_of_its_own_from_the_machine(self):
        m = Machine(self)
        m.drop("CreateWorld")  # an entry with no adminCode: a door from before each world had one
        m.take()
        code = (m.codes / "plaza").read_text(encoding="utf-8").strip()
        self.assertRegex(code, r"\A[0-9a-f]{16}\Z")
        self.assertIn(f"ADMIN_CODE={code}\n", m.env())

    def test_a_create_never_keeps_the_keys_a_world_gone_before_it_left(self):
        m = Machine(self)
        (m.home / "env" / "plaza.env").write_text(f"{JWT}\nADMIN_CODE=oldcode1\n", encoding="utf-8")
        m.drop("CreateWorld", adminCode=DOOR_CODE)
        m.take()
        self.assertNotIn(JWT, m.env())  # everyone who was in the old one would still be who they were
        self.assertIn(f"ADMIN_CODE={DOOR_CODE}\n", m.env())

    def test_a_code_that_is_not_plain_is_never_written(self):
        for bad in ("short", "abc12345\nJWT_SECRET=x", "abc$12345", "has space1", 12345678, None):
            with self.subTest(code=bad):
                m = Machine(self)
                m.drop("CreateWorld", adminCode=bad)
                m.take()
                self.assertIn("not a plain code", m.result("CreateWorld")["rule"])
                self.assertIsNone(m.order())
                self.assertFalse((m.home / "env" / "plaza.env").exists())
                self.assertFalse(m.codes.exists())

    def test_a_create_whose_keys_cannot_be_written_is_undone(self):
        m = Machine(self)
        (m.home / "env").chmod(0o500)  # nothing can be written there
        self.addCleanup((m.home / "env").chmod, 0o700)
        if os.access(m.home / "env", os.W_OK):
            self.skipTest("root writes anywhere")
        m.drop("CreateWorld", adminCode=DOOR_CODE)
        m.take()
        self.assertIn("could not be written", m.result("CreateWorld")["rule"])
        self.assertIsNone(m.order())  # no world without its code
        self.assertFalse((m.codes / "plaza").exists())

    def test_a_change_of_code_keeps_everything_else_and_queues_a_restart(self):
        m = Machine(self)
        m.put_world()
        voice = "LIVEKIT_WS_URL=wss://voice.example"
        (m.home / "env" / "plaza.env").write_text(f"{JWT}\nADMIN_CODE=machinecode\n{voice}\n", encoding="utf-8")
        m.drop("RotateWorldCode", adminCode=DOOR_CODE)
        m.take()
        self.assertEqual(m.result("RotateWorldCode")["state"], "done")
        # Its JWT secret stays: everyone who has been in the world keeps who they are.
        self.assertEqual(m.env(), f"{JWT}\nADMIN_CODE={DOOR_CODE}\n{voice}\n")
        self.assertEqual((m.codes / "plaza").read_text(encoding="utf-8"), DOOR_CODE + "\n")
        self.assertEqual(R.read_restarts(m.home), ["plaza"])
        self.assertEqual(m.order()["state"], "running")  # the book is as it was
        self.assertEqual(m.log()[-1]["type"], "RotateWorldCode")
        self.assertNotIn(DOOR_CODE, (m.home / "log.jsonl").read_text() + m.err)

    def test_a_change_of_code_must_carry_the_code_the_door_showed(self):
        m = Machine(self)
        m.put_world()
        before = m.env()
        m.drop("RotateWorldCode")
        m.take()
        self.assertIn("carries no new admin code", m.result("RotateWorldCode")["rule"])
        self.assertEqual(m.env(), before)
        self.assertEqual(R.read_restarts(m.home), [])

    def test_a_change_of_code_for_a_world_that_is_not_here_is_refused(self):
        m = Machine(self)
        m.drop("RotateWorldCode", adminCode=DOOR_CODE)
        m.take()
        self.assertIn("no world", m.result("RotateWorldCode")["rule"])
        self.assertFalse((m.home / "env" / "plaza.env").exists())
        self.assertFalse(m.codes.exists())

    def test_a_change_of_code_gives_keys_to_a_world_that_had_none(self):
        """An order adopted on a machine with no old shared code stays down; a signed change of code keys it."""
        m = Machine(self)
        m.put_world()
        (m.home / "env" / "plaza.env").unlink()
        m.drop("RotateWorldCode", adminCode=DOOR_CODE)
        m.take()
        self.assertRegex(m.env(), rf"\AJWT_SECRET=[0-9a-f]{{64}}\nADMIN_CODE={DOOR_CODE}\n\Z")
        p = R.plan(m.orders, m.home, "open-1", allowed=set(), admin_code=None, control_host=CONTROL)
        self.assertEqual(p["running"], ["plaza"])

    def test_a_key_file_that_is_not_text_fails_the_change_and_is_kept(self):
        m = Machine(self)
        m.put_world()
        (m.home / "env" / "plaza.env").write_bytes(b"JWT_SECRET=\xff\xfe\n")
        m.drop("RotateWorldCode", adminCode=DOOR_CODE)
        m.take()
        self.assertIn("could not be written", m.result("RotateWorldCode")["rule"])
        self.assertEqual((m.home / "env" / "plaza.env").read_bytes(), b"JWT_SECRET=\xff\xfe\n")
        self.assertFalse((m.codes / "plaza").exists())

    def test_showing_a_worlds_code_changes_nothing_and_is_logged(self):
        m = Machine(self)
        m.put_world()
        before = m.env()
        m.drop("ShowWorldCode")
        m.take()
        self.assertEqual(m.result("ShowWorldCode")["state"], "done")
        self.assertEqual(m.log()[-1]["type"], "ShowWorldCode")
        self.assertEqual(m.env(), before)
        self.assertEqual(R.read_restarts(m.home), [])

    def test_a_code_is_sixteen_hex_characters_and_new_each_time(self):
        codes = {R.make_code() for _ in range(20)}
        self.assertEqual(len(codes), 20)
        for code in codes:
            self.assertRegex(code, r"\A[0-9a-f]{16}\Z")

    def test_a_key_files_code_is_one_plain_line_or_none(self):
        self.assertEqual(R.env_code(f"{JWT}\nADMIN_CODE=abc12345\n"), "abc12345")
        self.assertEqual(R.env_code("ADMIN_CODE=abc12345"), "abc12345")  # no final newline
        for text in ("", f"{JWT}\n", "ADMIN_CODE=abc12345\nADMIN_CODE=abc12345\n", "export ADMIN_CODE=abc12345\n",
                     "ADMIN_CODE = abc12345\n", "ADMIN_CODE=\n", "ADMIN_CODE=has space1\n", "ADMIN_CODE\n"):
            with self.subTest(text=text):
                self.assertIsNone(R.env_code(text))

    def test_a_change_of_code_rewrites_only_the_lines_that_set_it(self):
        new = DOOR_CODE
        cases = {
            "plain": (f"{JWT}\nADMIN_CODE=old12345\n", f"{JWT}\nADMIN_CODE={new}\n"),
            "export and spaces": (f"{JWT}\n export ADMIN_CODE = old12345\n", f"{JWT}\nADMIN_CODE={new}\n"),
            "twice": (f"ADMIN_CODE=a1234567\n{JWT}\nADMIN_CODE=b1234567\n", f"ADMIN_CODE={new}\n{JWT}\n"),
            "none": (f"{JWT}\n", f"{JWT}\nADMIN_CODE={new}\n"),
            "no final newline": (f"{JWT}\nADMIN_CODE=old12345", f"{JWT}\nADMIN_CODE={new}\n"),
            "a look-alike is another setting": ("ADMIN_CODEX=1\nADMIN_CODE=old12345\n",
                                                f"ADMIN_CODEX=1\nADMIN_CODE={new}\n"),
        }
        for name, (before, after) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(R.with_code(before, new), after)
        for bad in ("short", f"{new}\nX=1", "a b c d e f"):
            with self.subTest(code=bad), self.assertRaises(ValueError):
                R.with_code(f"{JWT}\n", bad)

    def test_a_code_file_is_written_only_for_a_world_and_a_plain_code(self):
        m = Machine(self)
        for wid, code in (("../etc", DOOR_CODE), ("plaza", "bad code"), ("Plaza", DOOR_CODE)):
            with self.subTest(world=wid, code=code), self.assertRaises(ValueError):
                R.write_code(m.codes, wid, code)
        with self.assertRaises(ValueError):
            R.give_code(m.home, m.codes, "plaza", "bad\ncode")
        self.assertFalse(m.codes.exists())


class Migration(unittest.TestCase):
    """The first run of this version: each world keeps the code it has until an Oracle changes it."""

    def setUp(self):
        self.m = Machine(self)
        self.env = self.m.home / "env"

    def write_env(self, wid, text):
        (self.env / f"{wid}.env").write_text(text, encoding="utf-8")

    def test_each_world_keeps_the_code_it_has_and_no_key_file_changes(self):
        self.write_env("prueba", f"{JWT}\nADMIN_CODE=machinecode\n")
        self.write_env("pablofm", "JWT_SECRET=" + "b" * 64 + "\nADMIN_CODE=machinecode\n")
        before = {p.name: p.read_text() for p in self.env.iterdir()}
        self.assertEqual(R.sync_codes(self.m.home, self.m.codes), ["pablofm", "prueba"])
        self.assertEqual((self.m.codes / "prueba").read_text(), "machinecode\n")
        self.assertEqual((self.m.codes / "pablofm").read_text(), "machinecode\n")
        self.assertEqual(R.sync_codes(self.m.home, self.m.codes), [])  # once: then nothing to do
        self.assertEqual({p.name: p.read_text() for p in self.env.iterdir()}, before)

    def test_the_key_file_is_the_truth(self):
        self.write_env("plaza", f"{JWT}\nADMIN_CODE=fromkeys1\n")
        R.write_code(self.m.codes, "plaza", "byhand123")
        R.write_code(self.m.codes, "gone", "gonecode1")  # its world and key file are gone
        (self.m.codes / "notes.txt").write_text("not a world's file")
        self.assertEqual(R.sync_codes(self.m.home, self.m.codes), ["gone", "plaza"])
        self.assertEqual((self.m.codes / "plaza").read_text(), "fromkeys1\n")
        self.assertFalse((self.m.codes / "gone").exists())
        self.assertTrue((self.m.codes / "notes.txt").exists())

    def test_a_key_file_with_no_plain_code_leaves_its_world_without_a_code_file(self):
        """The door then answers no-admin-code, and a signed change of code puts it right."""
        self.write_env("plaza", f"{JWT}\nexport ADMIN_CODE=abc12345\n")
        R.write_code(self.m.codes, "plaza", "abc12345")
        self.assertEqual(R.sync_codes(self.m.home, self.m.codes), ["plaza"])
        self.assertFalse((self.m.codes / "plaza").exists())

    def test_a_key_file_that_cannot_be_read_proves_nothing(self):
        (self.env / "plaza.env").write_bytes(b"\xff\xfe")
        R.write_code(self.m.codes, "plaza", "abc12345")
        self.assertEqual(R.sync_codes(self.m.home, self.m.codes), [])
        self.assertEqual((self.m.codes / "plaza").read_text(), "abc12345\n")

    def test_a_machine_with_no_key_files_and_no_folder_does_nothing(self):
        self.env.rmdir()
        self.assertEqual(R.sync_codes(self.m.home, self.m.codes), [])
        self.assertFalse(self.m.codes.exists())


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
