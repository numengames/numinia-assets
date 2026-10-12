# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""The machine's door takes an Oracle's signed request once, and shows only public facts.

The requests are the real EIP-712 signatures in fleet_vectors.json (machine
`open-1`, world `plaza`, valid until 2030); the door's clock is set just
before they expire.
"""

from pathlib import Path
import contextlib
import http.client
import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest


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
D = load("fleetd")
SIGNED = {v["type"]: v for v in VECTORS["vectors"]}
ORACLE = SIGNED["CreateWorld"]["signer"]
EXPIRES = SIGNED["CreateWorld"]["message"]["expires"]
NOW = EXPIRES - 60
CODE = "a1b2c3d4e5f6"


def body(kind, message=None, signature=None, **extra):
    vector = SIGNED[kind]
    return json.dumps({"type": kind, "message": message or vector["message"],
                       "signature": signature or vector["signature"], **extra}).encode()


class Rig:
    """A door over a temporary fleet home, its clock just before the vectors expire."""

    def __init__(self, test, alias="open-1", admin_code=True, now=NOW):
        temp = tempfile.TemporaryDirectory(prefix="fleet-door-")
        test.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "home"
        for folder in ("inbox", "orders", "requests", "copies", "run", "sock"):
            (self.home / folder).mkdir(parents=True)
        etc = self.root / "etc"
        etc.mkdir()
        (etc / "signers").write_text(f"{ORACLE}\n", encoding="utf-8")
        (etc / "control-host").write_text("open-1.numinia.com\n", encoding="utf-8")
        if admin_code:
            (etc / "admin-code").write_text(CODE + "\n", encoding="utf-8")
        self.now = now
        self.door = D.Door(
            home=self.home, alias=alias, signers_file=etc / "signers", admin_code_file=etc / "admin-code",
            control_host_file=etc / "control-host", domains_file=etc / "domains",
            origins_file=etc / "origins", state=self.root / "state", clock=lambda: self.now,
        )

    def accept(self, payload, client="203.0.113.7"):
        with contextlib.redirect_stderr(io.StringIO()):
            return self.door.accept(payload, client)

    def put_world(self, wid="plaza", domain="plaza.numinia.com"):
        order = {"id": wid, "card": wid, "state": "running", "server": "open-1", "domain": domain,
                 "image": SIGNED["CreateWorld"]["message"]["image"],
                 "limits": {"memory": "2g", "cpus": "1.5", "maxUploadMb": 50},
                 "createdBy": ORACLE}
        (self.home / "orders" / f"{wid}.json").write_text(json.dumps(order), encoding="utf-8")


class Accept(unittest.TestCase):
    def test_an_oracles_create_is_dropped_into_the_inbox_with_the_admin_code_in_reply(self):
        rig = Rig(self)
        status, reply = rig.accept(body("CreateWorld"))
        self.assertEqual(status, 202)
        self.assertEqual(reply, {"request": SIGNED["CreateWorld"]["message"]["nonce"], "adminCode": CODE})
        [dropped] = list((rig.home / "inbox").iterdir())
        raw = json.loads(dropped.read_text())
        self.assertEqual(raw["signer"], ORACLE)
        self.assertEqual(raw["message"], SIGNED["CreateWorld"]["message"])
        self.assertEqual(dropped.stat().st_mode & 0o777, 0o640)
        self.assertEqual(rig.door.request(raw["message"]["nonce"]), {"request": raw["message"]["nonce"],
                                                                     "state": "pending"})
        [world] = rig.door.worlds()["worlds"]
        self.assertEqual((world["id"], world["phase"], world["domain"]), ("plaza", "creating", "plaza.numinia.com"))

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
        self.assertEqual((facts["machine"], facts["oracles"]), ("open-1", 1))
        self.assertNotIn(ORACLE, json.dumps(facts))

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
        self.assertEqual(json.loads(data)["adminCode"], CODE)
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


if __name__ == "__main__":
    unittest.main()
