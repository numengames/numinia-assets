# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""The machine's door takes an Oracle's signed request once, and shows only public facts.

The requests are the real EIP-712 signatures in fleet_vectors.json (machine
`open-1`, world `plaza`, valid until 2030); the door's clock is set just
before they expire.
"""

from pathlib import Path
import collections
import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MACHINE = ROOT / "open-worlds" / "machine"
HERE = Path(__file__).resolve().parent
VECTORS = json.loads((Path(__file__).parent / "fleet_vectors.json").read_text(encoding="utf-8"))


def load(name, folder=MACHINE):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    spec = importlib.util.spec_from_file_location(name, folder / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


R = load("reconcile")
D = load("fleetd")
Z = load("fleet_zips", HERE)
SIGNED = {v["type"]: v for v in VECTORS["vectors"]}
ORACLE = SIGNED["CreateWorld"]["signer"]
EXPIRES = SIGNED["CreateWorld"]["message"]["expires"]
NOW = EXPIRES - 60
CODE = "a1b2c3d4e5f6"  # the machine's old shared code
PLAZA_CODE = "f1a2f1a2f1a2f1a2"  # plaza's own
OWN_CODE = r"\A[0-9a-f]{16}\Z"  # a world's own code, as the door makes them
Usage = collections.namedtuple("Usage", "total used free")


def body(kind, message=None, signature=None, **extra):
    vector = SIGNED[kind]
    return json.dumps({"type": kind, "message": message or vector["message"],
                       "signature": signature or vector["signature"], **extra}).encode()


class Rig:
    """A door over a temporary fleet home, its clock just before the vectors expire.

    Besides the vectors' Oracle it knows a throwaway one (`oracle`), which
    signs imports of zips the tests build; `free` is the disk the door sees.
    """

    def __init__(self, test, alias="open-1", admin_code=True, now=NOW, free=10**15):
        temp = tempfile.TemporaryDirectory(prefix="fleet-door-")
        test.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "home"
        for folder in ("inbox", "orders", "requests", "copies", "run", "sock", "uploads"):
            (self.home / folder).mkdir(parents=True)
        etc = self.etc = self.root / "etc"
        etc.mkdir()
        (etc / "codes").mkdir()
        self.oracle = Z.Oracle(D.W)
        (etc / "signers").write_text(f"{ORACLE}\n{self.oracle.address}\n", encoding="utf-8")
        (etc / "control-host").write_text("open-1.numinia.com\n", encoding="utf-8")
        if admin_code:
            (etc / "admin-code").write_text(CODE + "\n", encoding="utf-8")
        self.now = now
        self.free = free
        self.err = ""
        self.door = D.Door(
            home=self.home, alias=alias, signers_file=etc / "signers", admin_code_file=etc / "admin-code",
            codes_dir=etc / "codes", recipe_file=etc / "recipe",
            control_host_file=etc / "control-host", domains_file=etc / "domains",
            origins_file=etc / "origins", state=self.root / "state", clock=lambda: self.now,
            disk_usage=lambda path: Usage(10**16, 0, self.free),
        )

    def accept(self, payload, client="203.0.113.7"):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            answer = self.door.accept(payload, client)
        self.err += err.getvalue()
        return answer

    def bring(self, data, world="plaza-copia", **over):
        """An ImportWorld for this zip, signed by the throwaway Oracle and sent to the door: (status, reply, message)."""
        message = Z.import_message(data, world=world, **{"expires": self.now + 600, **over})
        signed = json.dumps({"type": "ImportWorld", "message": message,
                             "signature": self.oracle.sign("ImportWorld", message)}).encode()
        status, reply = self.accept(signed)
        return status, reply, message

    def put(self, nonce, data, length="auto", client="203.0.113.7"):
        """The zip's bytes for a slot, as a PUT streams them: (status, reply)."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            answer = self.door.upload(nonce, str(len(data)) if length == "auto" else length, io.BytesIO(data), client)
        self.err += err.getvalue()
        return answer

    def slot(self, nonce):
        path = self.home / "uploads" / f"{nonce}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def dropped(self):
        """What the door left in the inbox, oldest first."""
        return [json.loads(p.read_text()) for p in sorted((self.home / "inbox").iterdir())]

    def wait(self, kind, wid="plaza", nonce="e" * 32):
        """A request of another Oracle's, taken by the door and not yet by the reconciler."""
        message = {"machine": "open-1", "world": wid, "nonce": nonce, "expires": EXPIRES}
        (self.home / "inbox" / f"0000000000001-{nonce}.json").write_text(
            json.dumps({"type": kind, "message": message, "signer": ORACLE, "receivedAt": R.utc(NOW)}))

    def put_world(self, wid="plaza", domain="plaza.numinia.com"):
        order = {"id": wid, "card": wid, "state": "running", "server": "open-1", "domain": domain,
                 "image": SIGNED["CreateWorld"]["message"]["image"],
                 "limits": {"memory": "2g", "cpus": "1.5", "maxUploadMb": 50},
                 "createdBy": ORACLE}
        (self.home / "orders" / f"{wid}.json").write_text(json.dumps(order), encoding="utf-8")


class Accept(unittest.TestCase):
    def test_an_oracles_create_is_dropped_into_the_inbox_with_the_worlds_own_code_in_reply(self):
        rig = Rig(self)
        status, reply = rig.accept(body("CreateWorld"))
        self.assertEqual(status, 202)
        self.assertEqual(set(reply), {"request", "adminCode"})
        self.assertEqual(reply["request"], SIGNED["CreateWorld"]["message"]["nonce"])
        self.assertRegex(reply["adminCode"], OWN_CODE)
        self.assertNotEqual(reply["adminCode"], CODE)  # never the machine's shared one
        [dropped] = list((rig.home / "inbox").iterdir())
        raw = json.loads(dropped.read_text())
        self.assertEqual(raw["signer"], ORACLE)
        self.assertEqual(raw["message"], SIGNED["CreateWorld"]["message"])
        self.assertEqual(raw["adminCode"], reply["adminCode"])  # what the reconciler writes into its keys
        self.assertEqual(dropped.stat().st_mode & 0o777, 0o640)
        self.assertNotIn(reply["adminCode"], rig.err)  # the door never logs a code
        self.assertEqual(rig.door.request(raw["message"]["nonce"]), {"request": raw["message"]["nonce"],
                                                                     "state": "pending"})
        [world] = rig.door.worlds()["worlds"]
        self.assertEqual((world["id"], world["phase"], world["domain"]), ("plaza", "creating", "plaza.numinia.com"))
        self.assertNotIn(reply["adminCode"], json.dumps(rig.door.worlds()) + json.dumps(rig.door.machine()))
        # Each create gets a code of its own.
        self.assertNotEqual(Rig(self).accept(body("CreateWorld"))[1]["adminCode"], reply["adminCode"])

    def test_a_request_counts_once(self):
        rig = Rig(self)
        self.assertEqual(rig.accept(body("CreateWorld"))[0], 202)
        self.assertEqual(rig.accept(body("CreateWorld")), (409, {"error": "replayed"}))
        # The reconciler took it and answered; a door that lost its memory still refuses it.
        for path in (rig.home / "inbox").iterdir():
            path.unlink()
        nonce = SIGNED["CreateWorld"]["message"]["nonce"]
        (rig.home / "requests" / f"{nonce}.json").write_text(json.dumps({"request": nonce, "state": "done"}))
        rig.door.nonces = D.Nonces(rig.root / "fresh-state" / "nonces.json")
        self.assertEqual(rig.accept(body("CreateWorld")), (409, {"error": "replayed"}))

    def test_only_the_oracles_of_this_machine_get_in(self):
        rig = Rig(self)
        stranger = VECTORS["stranger"]
        self.assertEqual(rig.accept(body("StopWorld", stranger["message"], stranger["signature"])),
                         (403, {"error": "not-an-oracle-here"}))
        changed = {**SIGNED["CreateWorld"]["message"], "world": "evil", "domain": "evil.numinia.com"}
        self.assertEqual(rig.accept(body("CreateWorld", changed)), (403, {"error": "not-an-oracle-here"}))
        self.assertEqual(rig.accept(body("CreateWorld", signature="0x" + "11" * 64 + "1b"))[0], 401)
        self.assertEqual(list((rig.home / "inbox").iterdir()), [])

    def test_a_request_is_good_for_fifteen_minutes_at_most(self):
        late = Rig(self, now=EXPIRES + 1)
        self.assertEqual(late.accept(body("CreateWorld")), (400, {"error": "expired"}))
        early = Rig(self, now=EXPIRES - D.WINDOW - 1)
        self.assertEqual(early.accept(body("CreateWorld")), (400, {"error": "expired"}))

    def test_a_request_for_another_machine_is_refused(self):
        rig = Rig(self, alias="open-2")
        self.assertEqual(rig.accept(body("CreateWorld")), (400, {"error": "other-machine"}))

    def test_shapes_are_checked_before_anything_else(self):
        rig = Rig(self)
        self.assertEqual(rig.accept(b"{not json"), (400, {"error": "not-json"}))
        self.assertEqual(rig.accept(body("CreateWorld", extra=1)), (400, {"error": "shape"}))
        missing = {k: v for k, v in SIGNED["CreateWorld"]["message"].items() if k != "image"}
        status, reply = rig.accept(body("CreateWorld", missing))
        self.assertEqual((status, reply["error"]), (400, "invalid"))
        self.assertEqual(rig.accept(b" " * (D.MAX_BODY + 1)), (413, {"error": "too-large"}))

    def test_the_book_must_be_able_to_take_it(self):
        # Each case on its own door: a refused request still spends its number.
        rig = Rig(self)
        self.assertEqual(rig.accept(body("DeleteWorld")), (409, {"error": "missing"}))
        other = Rig(self)
        other.put_world(domain="other.numinia.com")
        self.assertEqual(other.accept(body("DeleteWorld"))[1], {"error": "address-mismatch"})
        self.assertEqual(other.accept(body("CreateWorld")), (409, {"error": "exists"}))
        taken = Rig(self)
        taken.put_world(wid="otra", domain="plaza.numinia.com")
        self.assertEqual(taken.accept(body("CreateWorld")), (409, {"error": "address-taken"}))

    def test_a_download_gets_a_copy_number_and_shows_preparing(self):
        rig = Rig(self)
        rig.put_world()
        status, reply = rig.accept(body("DownloadWorld"))
        self.assertEqual(status, 202)
        self.assertRegex(reply["copy"], r"\A[0-9a-f]{32}\Z")
        self.assertEqual(rig.door.copy(reply["copy"]), {"copy": reply["copy"], "state": "preparing"})
        self.assertIsNone(rig.door.copy_file(reply["copy"]))
        self.assertIsNone(rig.door.copy("0" * 32))
        self.assertIsNone(rig.door.copy("../../etc/passwd"))

    def test_the_admin_code_is_shown_only_to_a_signed_oracle(self):
        rig = Rig(self)
        self.assertEqual(rig.accept(body("ShowAdminCode"))[1]["adminCode"], CODE)
        none = Rig(self, admin_code=False)
        self.assertEqual(none.accept(body("ShowAdminCode")), (503, {"error": "no-admin-code"}))
        self.assertNotIn(CODE, json.dumps(rig.door.worlds()) + json.dumps(rig.door.machine()))

    def test_a_worlds_own_code_is_shown_to_a_signed_oracle(self):
        rig = Rig(self)
        rig.put_world()
        (rig.etc / "codes" / "plaza").write_text(PLAZA_CODE + "\n")
        status, reply = rig.accept(body("ShowWorldCode"))
        self.assertEqual((status, reply), (202, {"request": SIGNED["ShowWorldCode"]["message"]["nonce"],
                                                 "adminCode": PLAZA_CODE}))
        [raw] = rig.dropped()
        self.assertNotIn("adminCode", raw)  # the record for the log carries no code
        self.assertNotIn(PLAZA_CODE, rig.err + json.dumps(rig.door.worlds()) + json.dumps(rig.door.machine()))

    def test_a_world_with_no_readable_code_says_so(self):
        """Its key file gives no plain code: a signed change of code puts it right."""
        for content in (None, "", "bad code\n", "a1b2c3d4\nJWT_SECRET=x\n"):
            with self.subTest(content=content):
                rig = Rig(self)
                rig.put_world()
                if content is not None:
                    (rig.etc / "codes" / "plaza").write_text(content)
                self.assertEqual(rig.accept(body("ShowWorldCode")), (503, {"error": "no-admin-code"}))
                self.assertEqual(rig.dropped(), [])

    def test_a_change_of_code_makes_a_new_one_and_carries_it_to_the_reconciler(self):
        rig = Rig(self)
        rig.put_world()
        (rig.etc / "codes" / "plaza").write_text(PLAZA_CODE + "\n")
        status, reply = rig.accept(body("RotateWorldCode"))
        self.assertEqual(status, 202)
        self.assertRegex(reply["adminCode"], OWN_CODE)
        self.assertNotIn(reply["adminCode"], (PLAZA_CODE, CODE))
        [raw] = rig.dropped()
        self.assertEqual((raw["type"], raw["adminCode"]), ("RotateWorldCode", reply["adminCode"]))
        self.assertEqual((rig.etc / "codes" / "plaza").read_text(), PLAZA_CODE + "\n")  # the reconciler writes it
        self.assertNotIn(reply["adminCode"], rig.err)

    def test_a_code_is_shown_or_changed_only_for_a_settled_world(self):
        """Each case on its own door: a refused request still spends its number."""
        for kind in ("ShowWorldCode", "RotateWorldCode"):
            with self.subTest(kind=kind):
                rig = Rig(self)
                self.assertEqual(rig.accept(body(kind)), (409, {"error": "missing"}))
                rig = Rig(self)
                rig.put_world()
                rig.wait("DeleteWorld")
                self.assertEqual(rig.accept(body(kind)), (409, {"error": "still-deleting"}))
                rig = Rig(self)  # taken by the reconciler, its folder and keys not gone yet
                (rig.home / "run" / "purge.json").write_text(json.dumps({"plaza": {"domain": "plaza.numinia.com"}}))
                self.assertEqual(rig.accept(body(kind)), (409, {"error": "still-deleting"}))
                rig = Rig(self)
                rig.put_world()
                rig.wait("RotateWorldCode")  # its code is changing: the old one is about to stop working
                self.assertEqual(rig.accept(body(kind)), (409, {"error": "busy"}))
                rig = Rig(self)
                rig.wait("CreateWorld")
                self.assertEqual(rig.accept(body(kind)), (409, {"error": "busy"}))
                self.assertEqual(len(rig.dropped()), 1)  # only the one that was waiting
                rig = Rig(self)
                rig.put_world()
                rig.put_world(wid="foro", domain="foro.numinia.com")
                rig.wait("RotateWorldCode", wid="foro")  # another world's change is no reason
                (rig.etc / "codes" / "plaza").write_text(PLAZA_CODE + "\n")
                self.assertEqual(rig.accept(body(kind))[0], 202)

    def test_too_many_requests_from_one_address_slow_down(self):
        rig = Rig(self)
        rig.door.limiter = D.Limiter(per_client=2, overall=100, window=60)
        self.assertEqual(rig.accept(b"{}")[0], 400)
        self.assertEqual(rig.accept(b"{}")[0], 400)
        self.assertEqual(rig.accept(b"{}"), (429, {"error": "slow-down"}))
        self.assertEqual(rig.accept(b"{}", client="198.51.100.1")[0], 400)


class Facts(unittest.TestCase):
    def test_the_machine_says_its_name_and_how_many_oracles_it_knows_never_who(self):
        rig = Rig(self)
        facts = rig.door.machine()
        self.assertEqual((facts["machine"], facts["oracles"]), ("open-1", 2))  # the vectors' and the rig's own
        self.assertNotIn(ORACLE, json.dumps(facts))
        self.assertNotIn(rig.oracle.address, json.dumps(facts))

    def test_the_machine_says_which_commit_of_its_recipe_it_runs(self):
        rig = Rig(self)
        self.assertIsNone(rig.door.machine()["recipe"])  # set up before the bootstrap wrote it
        commit = "0123456789abcdef0123456789abcdef01234567"
        (rig.etc / "recipe").write_text(commit + "\n")
        self.assertEqual(rig.door.machine()["recipe"], commit)
        for garbage in ("main", commit[:7], commit.upper(), commit + "0", f"{commit}\nx", ""):
            with self.subTest(content=garbage):
                (rig.etc / "recipe").write_text(garbage)
                self.assertIsNone(rig.door.machine()["recipe"])
        (rig.etc / "recipe").write_bytes(b"\xff" * 40)
        self.assertIsNone(rig.door.machine()["recipe"])

    def test_the_machine_says_whether_its_reconciler_keeps_up(self):
        """A request nobody takes, or a last run that stops moving, is a machine that no longer obeys."""
        rig = Rig(self)
        facts = rig.door.machine()
        self.assertIsNone(facts["lastRun"])  # this reconciler never ran here
        self.assertEqual(facts["waiting"], {"requests": 0, "since": None})
        self.assertEqual(facts["at"], R.utc(NOW))
        rig.put_world()
        self.assertEqual(rig.accept(body("StopWorld"))[0], 202)
        last = {"at": "2029-12-31T23:58:00Z", "ok": False, "rule": "docker did not take the book"}
        (rig.home / "run" / "last-run.json").write_text(json.dumps(last))
        facts = rig.door.machine()
        self.assertEqual(facts["lastRun"], last)
        self.assertEqual(facts["waiting"], {"requests": 1, "since": R.utc(NOW)})
        for broken in ("{broken", json.dumps({"at": 5, "ok": True}), json.dumps({"at": "x", "ok": "yes"})):
            (rig.home / "run" / "last-run.json").write_text(broken)
            self.assertIsNone(rig.door.machine()["lastRun"])
        (rig.home / "run" / "last-run.json").write_text(json.dumps({"at": "x", "ok": True, "rule": 7}))
        self.assertEqual(rig.door.machine()["lastRun"], {"at": "x", "ok": True, "rule": None})

    def test_a_world_being_deleted_is_not_born_again_until_it_is_gone(self):
        """Its folder and keys outlive its order until Docker takes the container down."""
        for gone in ({"plaza": {"nonce": "a" * 32, "domain": "plaza.numinia.com"}},  # the same name
                     {"vieja": {"nonce": "a" * 32, "domain": "plaza.numinia.com"}}):  # the same address
            rig = Rig(self)
            (rig.home / "run" / "purge.json").write_text(json.dumps(gone))
            self.assertEqual(rig.accept(body("CreateWorld")), (409, {"error": "still-deleting"}))
            self.assertEqual(list((rig.home / "inbox").iterdir()), [])
        rig = Rig(self)
        (rig.home / "run" / "purge.json").write_text(json.dumps({"vieja": {"domain": "vieja.numinia.com"}}))
        self.assertEqual(rig.accept(body("CreateWorld"))[0], 202)

    def test_worlds_show_their_phase_and_no_wallet(self):
        rig = Rig(self)
        rig.put_world()
        rig.put_world(wid="foro", domain="foro.numinia.com")
        self.assertEqual(rig.accept(body("StopWorld"))[0], 202)
        (rig.home / "run" / "purge.json").write_text(json.dumps(
            {"vieja": {"nonce": "a" * 32, "domain": "vieja.numinia.com"}, "BAD": {}}))
        listed = {w["id"]: w for w in rig.door.worlds()["worlds"]}
        self.assertEqual(listed["plaza"]["phase"], "stopping")
        self.assertEqual(listed["foro"]["phase"], "running")
        self.assertEqual(listed["vieja"], {"id": "vieja", "domain": "vieja.numinia.com", "phase": "deleting"})
        self.assertNotIn("BAD", listed)
        self.assertNotIn(ORACLE, json.dumps(listed))

    def test_the_log_is_newest_first_and_bounded(self):
        rig = Rig(self)
        lines = [json.dumps({"at": f"t{i}", "type": "StopWorld"}) for i in range(D.LOG_LINES + 5)]
        (rig.home / "log.jsonl").write_text("\n".join(lines + ["{broken"]) + "\n")
        entries = rig.door.log()["entries"]
        self.assertEqual(len(entries), D.LOG_LINES - 1)
        self.assertEqual(entries[0]["at"], f"t{D.LOG_LINES + 4}")

    def test_a_finished_request_shows_the_reconcilers_result(self):
        rig = Rig(self)
        nonce = "b" * 32
        (rig.home / "requests" / f"{nonce}.json").write_text(json.dumps({"request": nonce, "state": "failed",
                                                                          "rule": "x"}))
        self.assertEqual(rig.door.request(nonce)["state"], "failed")
        self.assertIsNone(rig.door.request("c" * 32))
        self.assertIsNone(rig.door.request("not-a-number"))


class Piecemeal:
    """A request body that hands the zip over piece by piece, the door's clock moving `step` seconds per piece.

    Before each piece it notes how far the slot says the upload got, and it
    may run something in between (another PUT into the same slot).
    """

    def __init__(self, rig, nonce, data, step=0.4, meanwhile=None):
        self.rig, self.nonce, self.data, self.step, self.meanwhile = rig, nonce, data, step, meanwhile
        self.at, self.seen, self.pieces = 0, [], 0

    def read(self, n):
        self.seen.append((self.rig.slot(self.nonce) or {}).get("bytes"))
        if self.meanwhile is not None:
            self.meanwhile()
            self.meanwhile = None
        self.rig.now += self.step
        piece = self.data[self.at:self.at + n]
        self.at += len(piece)
        self.pieces += 1
        return piece


class Unread:
    """A body that must never be read: the door answers before taking any of it."""

    def read(self, n):
        raise AssertionError("the door read a body it should have refused unread")


class Imports(unittest.TestCase):
    """A world brought back from its zip, at the door: a slot for one signed zip, then the zip itself."""

    def open_slot(self, rig, data=None, **over):
        data = Z.make_zip([("world/db.sqlite", Z.db_bytes())]) if data is None else data
        status, reply, message = rig.bring(data, **over)
        self.assertEqual(status, 202, reply)
        return data, reply, message

    def test_a_signed_import_opens_a_slot_for_its_zip_and_nothing_reaches_the_inbox(self):
        rig = Rig(self)
        status, reply = rig.accept(body("ImportWorld"))  # the shared vector, signed by its Oracle
        self.assertEqual(status, 202)
        nonce = SIGNED["ImportWorld"]["message"]["nonce"]
        self.assertEqual(set(reply), {"request", "upload", "adminCode"})
        self.assertEqual((reply["request"], reply["upload"]), (nonce, f"uploads/{nonce}"))
        self.assertRegex(reply["adminCode"], OWN_CODE)
        self.assertEqual(list((rig.home / "inbox").iterdir()), [])
        path = rig.home / "uploads" / f"{nonce}.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        slot = rig.slot(nonce)
        self.assertEqual(slot, {"type": "ImportWorld", "message": SIGNED["ImportWorld"]["message"],
                                "signature": SIGNED["ImportWorld"]["signature"], "signer": ORACLE,
                                "receivedAt": R.utc(NOW), "adminCode": reply["adminCode"],
                                "expiresAt": R.utc(NOW + R.SLOT_SECONDS), "bytes": 0})
        self.assertEqual(rig.door.request(nonce), {"request": nonce, "state": "uploading", "bytes": 0,
                                                   "of": 50331648})
        self.assertEqual(rig.accept(body("ImportWorld")), (409, {"error": "replayed"}))
        self.assertNotIn(reply["adminCode"], rig.err)
        self.assertIn("accepted ImportWorld plaza-copia", rig.err)

    def test_a_zip_over_the_cap_or_the_disk_is_refused_by_name(self):
        rig = Rig(self)
        status, reply, _ = rig.bring(b"x", zipBytes=R.ZIP_MAX + 1)
        self.assertEqual((status, reply), (413, {"error": "zip-too-big"}))
        # The disk must keep its reserve after this zip, what it unpacks to, and every slot already open.
        first = R.reservation(10_000)
        rig = Rig(self, free=first + R.RESERVE)
        _, reply, _ = self.open_slot(rig, b"x" * 10_000)
        status, reply, _ = rig.bring(b"y" * 10_000, world="otra")
        self.assertEqual((status, reply), (507, {"error": "no-room"}))
        rig.free = 2 * first + R.RESERVE
        self.assertEqual(rig.bring(b"y" * 10_000, world="otra")[0], 202)
        self.assertEqual(list((rig.home / "inbox").iterdir()), [])
        rig = Rig(self)
        rig.door.disk_usage = mock.Mock(side_effect=OSError("no such disk"))
        self.assertEqual(rig.bring(b"x")[:2], (507, {"error": "no-room"}))

    def test_a_world_on_its_way_holds_its_name_and_its_address(self):
        rig = Rig(self)
        data, _, _ = self.open_slot(rig, world="plaza")
        self.assertEqual(rig.bring(data, world="plaza")[:2], (409, {"error": "exists"}))
        self.assertEqual(rig.accept(body("CreateWorld")), (409, {"error": "exists"}))  # the vector's plaza
        self.assertEqual(rig.bring(data, world="otra", domain="plaza.numinia.com")[:2],
                         (409, {"error": "address-taken"}))
        # Its code cannot be shown or changed until it has landed.
        self.assertEqual(rig.accept(body("ShowWorldCode")), (409, {"error": "busy"}))
        self.assertEqual(rig.accept(body("RotateWorldCode")), (409, {"error": "busy"}))
        # Being unpacked by the reconciler.
        rig = Rig(self)
        message = Z.import_message(b"x", world="plaza", expires=EXPIRES)
        (rig.home / "run" / R.IMPORTING).write_text(json.dumps({"plaza": {"nonce": message["nonce"],
                                                                          "message": message}}))
        self.assertEqual(rig.accept(body("CreateWorld")), (409, {"error": "exists"}))
        self.assertEqual(rig.bring(b"x", world="otra", domain="plaza.numinia.com")[:2],
                         (409, {"error": "address-taken"}))

    def test_an_import_keeps_the_rules_of_a_birth(self):
        rig = Rig(self)
        rig.put_world(wid="plaza-copia", domain="plaza-copia.numinia.com")
        self.assertEqual(rig.accept(body("ImportWorld")), (409, {"error": "exists"}))
        rig = Rig(self)
        rig.put_world(wid="otra", domain="plaza-copia.numinia.com")
        self.assertEqual(rig.accept(body("ImportWorld")), (409, {"error": "address-taken"}))
        rig = Rig(self)
        (rig.home / "run" / "purge.json").write_text(json.dumps({"plaza-copia": {"nonce": "a" * 32}}))
        self.assertEqual(rig.accept(body("ImportWorld")), (409, {"error": "still-deleting"}))
        rig = Rig(self)
        self.assertEqual(rig.bring(b"x", domain="plaza-copia.evil.example")[:2], (409, {"error": "refused"}))
        rig = Rig(self)
        rig.wait("CreateWorld", wid="plaza-copia")  # a create of the same name waiting in the inbox
        self.assertEqual(rig.accept(body("ImportWorld")), (409, {"error": "exists"}))
        for field, bad in (("zipSha256", "A" * 64), ("zipBytes", 0)):
            with self.subTest(field=field):
                rig = Rig(self)
                status, reply, _ = rig.bring(b"x", **{field: bad})
                self.assertEqual((status, reply["error"]), (400, "invalid"))

    def test_the_signed_zip_moves_the_slot_into_the_inbox(self):
        rig = Rig(self)
        data, reply, message = self.open_slot(rig)
        nonce = message["nonce"]
        self.assertEqual(rig.put(nonce, data), (202, {"request": nonce}))
        self.assertIsNone(rig.slot(nonce))
        self.assertEqual(sorted(p.name for p in (rig.home / "uploads").iterdir()), [f"{nonce}.zip"])
        zipped = rig.home / "uploads" / f"{nonce}.zip"
        self.assertEqual(zipped.read_bytes(), data)
        self.assertEqual(zipped.stat().st_mode & 0o777, 0o640)
        [entry] = rig.dropped()
        self.assertEqual(entry, {"type": "ImportWorld", "message": message, "signature": entry["signature"],
                                 "signer": rig.oracle.address, "receivedAt": R.utc(NOW),
                                 "adminCode": reply["adminCode"], "expiresAt": R.utc(NOW + R.SLOT_SECONDS),
                                 "bytes": len(data), "zip": f"{nonce}.zip"})
        self.assertEqual(rig.door.request(nonce), {"request": nonce, "state": "pending"})
        self.assertEqual(rig.put(nonce, data), (404, {"error": "missing"}))  # the slot is gone: once only
        for leaked in (reply["adminCode"], message["zipSha256"]):
            self.assertNotIn(leaked, rig.err)

    def test_an_upload_is_refused_by_rule(self):
        rig = Rig(self)
        data, _, message = self.open_slot(rig)
        nonce = message["nonce"]
        self.assertEqual(rig.put("f" * 32, data), (404, {"error": "missing"}))
        self.assertEqual(rig.put("../../etc/x", data), (404, {"error": "missing"}))
        for length in (None, "", "12x", "-1", "1" * 21):
            with self.subTest(length=length):
                self.assertEqual(rig.door.upload(nonce, length, Unread()), (411, {"error": "length"}))
        for length in (str(len(data) - 1), str(len(data) + 1), "0"):
            with self.subTest(length=length):
                self.assertEqual(rig.door.upload(nonce, length, Unread()), (400, {"error": "wrong-size"}))
        # Cut short: the part goes, the slot stays and says nothing arrived.
        self.assertEqual(rig.put(nonce, data[:-10], length=str(len(data))), (400, {"error": "cut"}))
        self.assertEqual(rig.slot(nonce)["bytes"], 0)
        self.assertEqual(sorted(p.name for p in (rig.home / "uploads").iterdir()), [f"{nonce}.json"])
        # The right size but not the bytes signed: the slot stays for the right zip.
        other = bytes(reversed(data))
        self.assertEqual(rig.put(nonce, other), (400, {"error": "fingerprint"}))
        self.assertEqual(sorted(p.name for p in (rig.home / "uploads").iterdir()), [f"{nonce}.json"])
        self.assertEqual(list((rig.home / "inbox").iterdir()), [])
        self.assertEqual(rig.put(nonce, data), (202, {"request": nonce}))  # no new signature needed
        self.assertEqual(len(rig.dropped()), 1)

    def test_one_upload_at_a_time_into_a_slot(self):
        rig = Rig(self)
        data, _, message = self.open_slot(rig, Z.make_zip([("world/db.sqlite", os.urandom(5000))],
                                                          compression=0))
        nonce, answers = message["nonce"], []
        piecemeal = Piecemeal(rig, nonce, data, meanwhile=lambda: answers.append(rig.put(nonce, data)))
        with mock.patch.object(D.R, "CHUNK", 1000):
            self.assertEqual(rig.door.upload(nonce, str(len(data)), piecemeal), (202, {"request": nonce}))
        self.assertEqual(answers, [(409, {"error": "busy"})])
        self.assertEqual(rig.door.streaming, set())

    def test_the_slot_says_how_far_the_upload_got_at_most_once_a_second(self):
        rig = Rig(self)
        data, _, message = self.open_slot(rig, Z.make_zip([("world/db.sqlite", os.urandom(4800))], compression=0))
        nonce = message["nonce"]
        piecemeal = Piecemeal(rig, nonce, data, step=0.4)
        limiter = rig.door.limiter.calls["203.0.113.7"]
        before = len(limiter)
        with mock.patch.object(D.R, "CHUNK", 1000):
            self.assertEqual(rig.door.upload(nonce, str(len(data)), piecemeal, "203.0.113.7")[0], 202)
        self.assertEqual(piecemeal.pieces, 5)
        self.assertEqual(piecemeal.seen, [0, 0, 0, 3000, 3000])  # written once, after 1.2 s, not per piece
        self.assertEqual(len(limiter), before + 1)  # the upload counts once, never per piece

    def test_a_slot_lives_an_hour(self):
        rig = Rig(self)
        data, _, message = self.open_slot(rig)
        nonce = message["nonce"]
        (rig.home / "uploads" / f".{nonce}.zip.part").write_bytes(b"half")
        rig.now = NOW + R.SLOT_SECONDS + 1
        self.assertIsNone(rig.door.request(nonce))
        self.assertEqual(rig.door.worlds()["worlds"], [])
        self.assertEqual(rig.put(nonce, data), (410, {"error": "slot-expired"}))
        self.assertEqual(list((rig.home / "uploads").iterdir()), [])
        self.assertEqual(rig.put(nonce, data), (404, {"error": "missing"}))
        # A slot that runs out while its zip is arriving goes, with its part.
        rig = Rig(self)
        data, _, message = self.open_slot(rig, Z.make_zip([("world/db.sqlite", os.urandom(4800))], compression=0))
        nonce = message["nonce"]
        rig.now = NOW + R.SLOT_SECONDS - 1
        with mock.patch.object(D.R, "CHUNK", 1000):
            answer = rig.door.upload(nonce, str(len(data)), Piecemeal(rig, nonce, data, step=1.0))
        self.assertEqual(answer, (410, {"error": "slot-expired"}))
        self.assertEqual(list((rig.home / "uploads").iterdir()), [])
        self.assertEqual(list((rig.home / "inbox").iterdir()), [])

    def test_worlds_on_their_way_show_how_far_they_got_and_no_wallet(self):
        rig = Rig(self)
        data, reply, message = self.open_slot(rig)
        nonce = message["nonce"]
        slot = rig.slot(nonce)
        (rig.home / "uploads" / f"{nonce}.json").write_text(json.dumps({**slot, "bytes": 500}))
        listed = rig.door.worlds()["worlds"]
        self.assertEqual(listed, [{"id": "plaza-copia", "state": "stopped", "domain": "plaza-copia.numinia.com",
                                   "image": Z.IMAGE, "limits": {"memory": "2g", "cpus": "1.5", "maxUploadMb": 50},
                                   "phase": "uploading", "bytes": 500, "of": len(data)}])
        self.assertEqual(rig.door.request(nonce)["bytes"], 500)
        for claimed, shown in ((10**12, len(data)), (-5, 0), ("500", 0), (True, 0)):  # never past 0 … the zip
            with self.subTest(claimed=claimed):
                (rig.home / "uploads" / f"{nonce}.json").write_text(json.dumps({**slot, "bytes": claimed}))
                self.assertEqual(rig.door.worlds()["worlds"][0]["bytes"], shown)
        rig.put(nonce, data)
        [world] = rig.door.worlds()["worlds"]
        self.assertEqual((world["phase"], world["state"]), ("importing", "stopped"))
        self.assertNotIn("bytes", world)
        # Taken by the reconciler, and being unpacked: still importing.
        for path in (rig.home / "inbox").iterdir():
            path.unlink()
        (rig.home / "run" / R.IMPORTING).write_text(json.dumps({"plaza-copia": {"nonce": nonce, "message": message}}))
        [world] = rig.door.worlds()["worlds"]
        self.assertEqual(world["phase"], "importing")
        for leaked in (rig.oracle.address, reply["adminCode"], message["zipSha256"]):
            self.assertNotIn(leaked, json.dumps(rig.door.worlds()))
        # A slot that is not a slot (another type, another nonce, a broken file) shows nothing.
        rig = Rig(self)
        uploads = rig.home / "uploads"
        (uploads / f"{'a' * 32}.json").write_text(json.dumps({**slot, "type": "CreateWorld"}))
        (uploads / f"{'b' * 32}.json").write_text(json.dumps(slot))
        (uploads / f"{'c' * 32}.json").write_text("{broken")
        self.assertEqual(rig.door.worlds()["worlds"], [])
        self.assertIsNone(rig.door.request("b" * 32))

    def test_the_machine_says_how_big_a_zip_it_takes(self):
        self.assertEqual(Rig(self).door.machine()["imports"], {"zipMaxBytes": 2147483648})


class EndToEnd(unittest.TestCase):
    """The door and the reconciler agree on what a request carries: the code an Oracle is shown is the world's."""

    def take(self, rig):
        with contextlib.redirect_stderr(io.StringIO()):
            return R.take_requests(rig.home, rig.home / "orders", "open-1", frozenset({ORACLE}), frozenset(),
                                   "open-1.numinia.com", NOW, codes_dir=rig.etc / "codes")

    def result(self, rig, kind):
        nonce = SIGNED[kind]["message"]["nonce"]
        return json.loads((rig.home / "requests" / f"{nonce}.json").read_text())

    def test_the_code_a_create_shows_is_the_code_the_world_gets(self):
        rig = Rig(self)
        shown = rig.accept(body("CreateWorld"))[1]["adminCode"]
        self.take(rig)
        self.assertEqual(self.result(rig, "CreateWorld")["state"], "done")
        self.assertRegex((rig.home / "env" / "plaza.env").read_text(),
                         rf"\AJWT_SECRET=[0-9a-f]{{64}}\nADMIN_CODE={shown}\n\Z")
        self.assertEqual(R.read_admin_code(rig.etc / "codes" / "plaza"), shown)

    def test_a_change_of_code_reaches_the_world_and_keeps_who_everyone_is(self):
        rig = Rig(self)
        rig.put_world()
        keys = rig.home / "env" / "plaza.env"
        keys.parent.mkdir()
        keys.write_text("JWT_SECRET=" + "ab" * 32 + f"\nADMIN_CODE={CODE}\n")
        R.sync_codes(rig.home, rig.etc / "codes")  # the first run of this version: plaza keeps the code it has
        self.assertEqual(rig.accept(body("ShowWorldCode"))[1]["adminCode"], CODE)
        new = rig.accept(body("RotateWorldCode"))[1]["adminCode"]
        self.take(rig)
        self.assertEqual([self.result(rig, k)["state"] for k in ("ShowWorldCode", "RotateWorldCode")], ["done", "done"])
        self.assertEqual(keys.read_text(), "JWT_SECRET=" + "ab" * 32 + f"\nADMIN_CODE={new}\n")
        self.assertEqual(R.read_admin_code(rig.etc / "codes" / "plaza"), new)  # what the door shows from now on
        self.assertEqual(R.read_restarts(rig.home), ["plaza"])  # and the engine reads it once restarted
        self.assertNotIn(new, (rig.home / "log.jsonl").read_text())

    def test_the_code_an_import_shows_is_the_code_the_world_comes_back_with_stopped(self):
        rig = Rig(self)
        data = Z.make_zip([("world/db.sqlite", Z.db_bytes()), ("world/assets/statue.glb", b"glTF" * 300)])
        status, reply, message = rig.bring(data)
        self.assertEqual(status, 202)
        self.assertEqual(rig.put(message["nonce"], data)[0], 202)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            R.take_requests(rig.home, rig.home / "orders", "open-1", frozenset({ORACLE, rig.oracle.address}),
                            frozenset(), "open-1.numinia.com", NOW, codes_dir=rig.etc / "codes")
        result = json.loads((rig.home / "requests" / f"{message['nonce']}.json").read_text())
        self.assertEqual(result["state"], "done", result)
        self.assertEqual(R.read_order(rig.home / "orders" / "plaza-copia.json")["state"], "stopped")
        self.assertEqual(R.read_admin_code(rig.etc / "codes" / "plaza-copia"), reply["adminCode"])
        self.assertRegex((rig.home / "env" / "plaza-copia.env").read_text(),
                         rf"\AJWT_SECRET=[0-9a-f]{{64}}\nADMIN_CODE={reply['adminCode']}\n\Z")
        self.assertEqual((rig.home / "data" / "plaza-copia" / "assets" / "statue.glb").read_bytes(), b"glTF" * 300)
        self.assertEqual(list((rig.home / "uploads").iterdir()), [])
        # What the door shows now: the world in the book, stopped, and the request done.
        [world] = rig.door.worlds()["worlds"]
        self.assertEqual((world["id"], world["phase"]), ("plaza-copia", "stopped"))
        self.assertEqual(rig.door.request(message["nonce"])["state"], "done")
        self.assertNotIn(reply["adminCode"], (rig.home / "log.jsonl").read_text() + err.getvalue())


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=5)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(self.unix_path)


class OverHTTP(unittest.TestCase):
    """The real server on a real socket, as Caddy reaches it."""

    def setUp(self):
        self.rig = Rig(self)
        here = os.getcwd()
        os.chdir(self.rig.root)  # a short relative path: socket paths are limited to ~100 bytes
        self.addCleanup(os.chdir, here)
        self.server = D.make_server(self.rig.door, Path("fleetd.sock"))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, payload=None, headers=None):
        connection = UnixHTTPConnection("fleetd.sock")
        with contextlib.redirect_stderr(io.StringIO()):
            connection.request(method, path, body=payload, headers=headers or {})
            response = connection.getresponse()
            data = response.read()
        connection.close()
        return response, data

    def test_public_facts_answer_to_anyone(self):
        response, data = self.call("GET", "/fleet/v1/machine")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "*")
        self.assertEqual(json.loads(data)["machine"], "open-1")
        self.assertEqual(self.call("GET", "/fleet/v1/worlds")[0].status, 200)
        self.assertEqual(self.call("GET", "/fleet/v1/log")[0].status, 200)
        for path in ("/", "/fleet/v1/", "/fleet/v1/nothing", "/fleet/v1/copies/../../etc/passwd/file",
                     "/fleet/v1/requests/zzz"):
            with self.subTest(path=path):
                self.assertEqual(self.call("GET", path)[0].status, 404)

    def test_only_the_console_may_send_requests_from_a_browser(self):
        response, _ = self.call("OPTIONS", "/fleet/v1/actions", headers={"Origin": "https://numinia.com"})
        self.assertEqual(response.status, 204)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "https://numinia.com")
        response, _ = self.call("OPTIONS", "/fleet/v1/actions", headers={"Origin": "https://evil.example"})
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        response, _ = self.call("POST", "/fleet/v1/actions", body("CreateWorld"),
                                {"Origin": "https://evil.example", "Content-Type": "application/json"})
        self.assertEqual(response.status, 403)
        self.assertEqual(list((self.rig.home / "inbox").iterdir()), [])

    def test_a_signed_request_from_the_console_is_accepted(self):
        response, data = self.call("POST", "/fleet/v1/actions", body("CreateWorld"),
                                   {"Origin": "https://numinia.com", "Content-Type": "application/json"})
        self.assertEqual(response.status, 202)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "https://numinia.com")
        self.assertRegex(json.loads(data)["adminCode"], OWN_CODE)
        nonce = SIGNED["CreateWorld"]["message"]["nonce"]
        response, data = self.call("GET", f"/fleet/v1/requests/{nonce}")
        self.assertEqual(json.loads(data)["state"], "pending")

    def test_a_request_without_a_length_or_too_big_is_refused(self):
        for length, status in ((None, 411), (str(D.MAX_BODY + 1), 413), ("12x", 411)):
            with self.subTest(length=length):
                connection = UnixHTTPConnection("fleetd.sock")
                connection.putrequest("POST", "/fleet/v1/actions")
                if length is not None:
                    connection.putheader("Content-Length", length)
                connection.endheaders()  # the body never follows: the door answers before reading it
                self.assertEqual(connection.getresponse().status, status)
                connection.close()

    def test_a_ready_copy_downloads_as_a_zip(self):
        copy = "d" * 32
        (self.rig.home / "copies" / f"{copy}.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        (self.rig.home / "copies" / f"{copy}.json").write_text(json.dumps(
            {"copy": copy, "world": "plaza", "state": "ready", "bytes": 22, "sha256": "0" * 64,
             "filename": "plaza-20300101-0000.zip", "expiresAt": "2030-01-02T00:00:00Z", "madeAt": NOW}))
        response, data = self.call("GET", f"/fleet/v1/copies/{copy}")
        self.assertEqual(json.loads(data)["state"], "ready")
        self.assertNotIn("madeAt", json.loads(data))
        response, data = self.call("GET", f"/fleet/v1/copies/{copy}/file")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "application/zip")
        self.assertEqual(response.getheader("Content-Disposition"), 'attachment; filename="plaza-20300101-0000.zip"')
        self.assertEqual(data[:2], b"PK")
        self.assertEqual(self.call("GET", f"/fleet/v1/copies/{'e' * 32}/file")[0].status, 404)

    def test_the_socket_lets_caddy_in(self):
        self.assertEqual(os.stat("fleetd.sock").st_mode & 0o777, 0o666)

    def test_the_console_may_send_a_zip_with_a_put(self):
        path = f"/fleet/v1/uploads/{'a' * 32}"
        response, _ = self.call("OPTIONS", path, headers={"Origin": "https://numinia.com"})
        self.assertEqual(response.status, 204)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "https://numinia.com")
        self.assertEqual(response.getheader("Access-Control-Allow-Methods"), "GET, POST, PUT, OPTIONS")
        response, _ = self.call("OPTIONS", path, headers={"Origin": "https://evil.example"})
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        response, data = self.call("PUT", path, b"PK", {"Origin": "https://evil.example"})
        self.assertEqual((response.status, json.loads(data)), (403, {"error": "origin"}))
        for wrong in ("/fleet/v1/uploads", f"/fleet/v1/uploads/{'a' * 32}/x", "/fleet/v1/actions"):
            with self.subTest(path=wrong):
                self.assertEqual(self.call("PUT", wrong, b"PK")[0].status, 404)
        # A refused body is read before the answer, so the sender gets the 404, not a broken pipe.
        response, data = self.call("PUT", "/fleet/v1/actions", b"x" * (512 * 1024))
        self.assertEqual((response.status, json.loads(data)), (404, {"error": "not-found"}))

    def test_a_zip_streams_into_its_slot_over_the_socket(self):
        data = Z.make_zip([("world/db.sqlite", os.urandom(3 * 1024**2))], compression=0)  # a few pieces
        status, reply, message = self.rig.bring(data)
        self.assertEqual(status, 202)
        nonce = message["nonce"]
        response, answer = self.call("GET", f"/fleet/v1/requests/{nonce}")
        self.assertEqual(json.loads(answer), {"request": nonce, "state": "uploading", "bytes": 0, "of": len(data)})
        # No length: refused before a byte is read.
        connection = UnixHTTPConnection("fleetd.sock")
        connection.putrequest("PUT", f"/fleet/v1/{reply['upload']}")
        connection.endheaders()
        self.assertEqual(connection.getresponse().status, 411)
        connection.close()
        response, answer = self.call("PUT", f"/fleet/v1/{reply['upload']}", data,
                                     {"Origin": "https://numinia.com", "Content-Type": "application/zip"})
        self.assertEqual((response.status, json.loads(answer)), (202, {"request": nonce}))
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "https://numinia.com")
        self.assertEqual(hashlib.sha256((self.rig.home / "uploads" / f"{nonce}.zip").read_bytes()).hexdigest(),
                         message["zipSha256"])
        response, answer = self.call("GET", f"/fleet/v1/requests/{nonce}")
        self.assertEqual(json.loads(answer)["state"], "pending")

    def test_an_upload_cut_on_the_way_leaves_the_slot_waiting(self):
        data = Z.make_zip([("world/db.sqlite", os.urandom(2 * 1024**2))], compression=0)
        status, reply, message = self.rig.bring(data)
        nonce = message["nonce"]
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect("fleetd.sock")
        sock.sendall(f"PUT /fleet/v1/uploads/{nonce} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(data)}\r\n\r\n"
                     .encode() + data[:1024**2])
        sock.shutdown(socket.SHUT_WR)  # the browser went away halfway
        self.assertIn(b" 400 ", sock.recv(4096))  # 'cut', to whoever is left listening
        sock.close()
        names = []
        for _ in range(100):  # the door finishes after it answers; wait until the part is gone
            names = sorted(p.name for p in (self.rig.home / "uploads").iterdir())
            if names == [f"{nonce}.json"] and not self.rig.door.streaming:
                break
            time.sleep(0.05)
        self.assertEqual(names, [f"{nonce}.json"])
        self.assertEqual(self.rig.slot(nonce)["bytes"], 0)
        self.assertEqual(list((self.rig.home / "inbox").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
