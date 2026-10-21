# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""The Numinia Engine beside Hyperfy on one machine: its service, its site, its zip by its own tools, its updates.

The update requests are the UpdateWorld vector of fleet_vectors.json (machine
`open-1`, world `plaza`, moved to a numinia-engine build) and requests signed
here by a throwaway Oracle (fleet_zips.Oracle). The engine's tools never run
here: `run_tool` is replaced by a stand-in that answers as the image would.
"""

from pathlib import Path
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MACHINE = ROOT / "open-worlds" / "machine"
HERE = Path(__file__).resolve().parent
VECTORS = json.loads((HERE / "fleet_vectors.json").read_text(encoding="utf-8"))


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
T = load("test_fleetd", HERE)  # its Rig: a door over a temporary fleet home
SIGNED = {v["type"]: v for v in VECTORS["vectors"]}
UPDATE = SIGNED["UpdateWorld"]
ORACLE = UPDATE["signer"]
NOW = UPDATE["message"]["expires"] - 60
HYPERFY = "ghcr.io/numengames/numinia-hyperfy2:sha-0e2045370989d051e73f81c7c9d53121d5c374b3"
ENGINE = "ghcr.io/numengames/numinia-engine:sha-0000000000000000000000000000000000000001"
NEWER = UPDATE["message"]["image"]  # the build the vector moves plaza to
OTHER = "ghcr.io/numengames/numinia-world:sha-0123456"  # a repository of the registry that is no engine here
CONTROL = "open-1.numinia.com"
CODE = "b0b0cafe5eed1234"
COPY = "c0ffee" + "0" * 26
LIMITS = {"memory": "2g", "cpus": "1.5", "maxUploadMb": 50}


def order(wid="plaza", image=ENGINE, state="running", domain=None):
    return {"id": wid, "card": wid, "state": state, "server": "open-1", "domain": domain or f"{wid}.numinia.com",
            "image": image, "limits": dict(LIMITS)}


class Home:
    """A temporary fleet home: the book, the keys, the inbox; and a throwaway Oracle of its own."""

    def __init__(self, test, orders=(), keys=()):
        temp = tempfile.TemporaryDirectory(prefix="fleet-engine-")
        test.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.orders = self.home / "orders"
        self.codes = self.home / "codes"
        for folder in ("inbox", "orders", "env", "data", "requests", "copies", "run", "uploads", "staging"):
            (self.home / folder).mkdir()
        for o in orders:
            self.put(o)
        for wid in keys:
            (self.home / "env" / f"{wid}.env").write_text("JWT_SECRET=" + "a" * 64 + "\nADMIN_CODE=machinecode\n")
        self.oracle = Z.Oracle(R.walletsig)
        self.dropped = 0
        self.err = ""
        self.pulled = []

    def put(self, o):
        (self.orders / f"{o['id']}.json").write_text(json.dumps(o), encoding="utf-8")

    def order(self, wid="plaza"):
        return R.read_order(self.orders / f"{wid}.json")

    def drop(self, kind, message, signature, **door):
        self.dropped += 1
        raw = {"type": kind, "message": message, "signature": signature, "signer": ORACLE,
               "receivedAt": R.utc(NOW), **door}
        (self.home / "inbox" / f"{self.dropped:013d}-{message['nonce']}.json").write_text(json.dumps(raw))

    def sign_and_drop(self, kind, message, **door):
        self.drop(kind, message, self.oracle.sign(kind, message), **door)

    def take(self, pull_fails=False):
        def pull(image):
            self.pulled.append(image)
            if pull_fails:
                raise subprocess.CalledProcessError(1, ["docker", "pull", image])

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            taken = R.take_requests(self.home, self.orders, "open-1", frozenset({ORACLE, self.oracle.address}),
                                    frozenset(), CONTROL, NOW, codes_dir=self.codes, pull=pull)
        self.err += err.getvalue()
        return taken

    def result(self, nonce):
        return json.loads((self.home / "requests" / f"{nonce}.json").read_text())

    def log(self):
        path = self.home / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def plan(self):
        return R.plan(self.orders, self.home, "open-1", allowed=set(), admin_code=None, control_host=CONTROL)


class Engines(unittest.TestCase):
    def test_an_engine_is_known_by_its_images_repository(self):
        self.assertEqual(R.engine_of(HYPERFY), "numinia-hyperfy2")
        self.assertEqual(R.engine_of(ENGINE), "numinia-engine")
        self.assertEqual(R.engine_of("ghcr.io/numengames/numinia-engine@sha256:" + "ab" * 32), "numinia-engine")
        self.assertEqual(R.engine_of("ghcr.io/numengames/numinia-hyperfy2:sha-0123456"), "numinia-hyperfy2")
        for image in (OTHER, "ghcr.io/numengames/numinia-engine-dev:sha-0123456", "ghcr.io/evil/numinia-engine:sha-0123456",
                      "ghcr.io/numengames/numinia-engine:latest", "ghcr.io/numengames/numinia-engine:sha-0123456\n",
                      None, 5, ""):
            with self.subTest(image=image):
                self.assertIsNone(R.engine_of(image))

    def test_an_engine_world_gets_its_own_service_and_nothing_else(self):
        h = Home(self, [order()], keys=["plaza"])
        p = h.plan()
        service = json.loads(p["compose"])["services"]["plaza"]
        self.assertEqual(service, {
            "image": ENGINE,
            "restart": "unless-stopped",
            "user": "10001:10001",
            "env_file": f"{h.home}/env/plaza.env",
            "environment": {"MOTOR_PASE_CLAVE": R.WORLD_PASS_KEY, "MOTOR_PASE_MUNDO": "plaza"},
            "volumes": [f"{h.home}/data/plaza:/mundo"],
            "stop_grace_period": "15s",
            "deploy": {"resources": {"limits": {"memory": "2g", "cpus": "1.5"}}},
        })
        # Its keys come from its key file alone; only Caddy publishes ports.
        self.assertNotIn("JWT_SECRET", p["compose"])
        self.assertEqual([n for n, s in json.loads(p["compose"])["services"].items() if "ports" in s], ["caddy"])
        self.assertEqual(p["engine_worlds"], ["plaza"])

    def test_an_engine_world_without_org_gets_no_motor_pase_org(self):
        service = json.loads(Home(self, [order()], keys=["plaza"]).plan()["compose"])["services"]["plaza"]
        self.assertNotIn("MOTOR_PASE_ORG", service["environment"])

    def test_an_engine_world_with_an_org_gets_motor_pase_org(self):
        """Without a signed request to set it yet, an order edited by hand can still carry `org`."""
        service = json.loads(
            Home(self, [{**order(), "org": "numen-games"}], keys=["plaza"]).plan()["compose"]
        )["services"]["plaza"]
        self.assertEqual(service["environment"]["MOTOR_PASE_ORG"], "numen-games")

    def test_an_order_with_a_bad_org_is_refused(self):
        self.assertIsNotNone(R.refusal({**order(), "org": "Numen Games"}))
        self.assertIsNone(R.refusal({**order(), "org": "numen-games"}))

    def test_people_cap_by_talla(self):
        self.assertEqual(R.people_cap({"memory": "0.75g", "cpus": "0.25"}), 25)  # Pequeño
        self.assertEqual(R.people_cap({"memory": "1.5g", "cpus": "0.5"}), 60)  # Mediano
        self.assertEqual(R.people_cap({"memory": "2g", "cpus": "1"}), 120)  # Grande
        self.assertIsNone(R.people_cap({"memory": "2g", "cpus": "1.5"}))  # today's one legacy tariff

    def test_an_engine_world_of_each_talla_gets_its_motor_tope_personas(self):
        for memory, cpus, cap in (("0.75g", "0.25", 25), ("1.5g", "0.5", 60), ("2g", "1", 120)):
            with self.subTest(memory=memory, cpus=cpus, cap=cap):
                o = {**order(), "limits": {"memory": memory, "cpus": cpus, "maxUploadMb": 50}}
                service = json.loads(Home(self, [o], keys=["plaza"]).plan()["compose"])["services"]["plaza"]
                self.assertEqual(service["environment"]["MOTOR_TOPE_PERSONAS"], str(cap))

    def test_fit_cpus_never_asks_more_than_the_machine_has(self):
        self.assertEqual(R.fit_cpus("3", host=2), "2")
        self.assertEqual(R.fit_cpus("2", host=2), "2")
        self.assertEqual(R.fit_cpus("1.5", host=2), "1.5")
        self.assertEqual(R.fit_cpus("0.25", host=1), "0.25")
        with mock.patch.object(R.os, "cpu_count", return_value=None):
            self.assertEqual(R.fit_cpus("3"), "1")

    def test_a_world_asking_more_cpus_than_the_machine_has_runs_at_the_machine_size(self):
        # Docker refuses a cpus tope over the machine's count, and one refused
        # service stops `docker compose up` for every world in the book.
        big = {**order(), "limits": {"memory": "4g", "cpus": "3", "maxUploadMb": 100}}
        small = {**order(wid="vic", image=HYPERFY), "limits": {"memory": "4g", "cpus": "3", "maxUploadMb": 100}}
        with mock.patch.object(R.os, "cpu_count", return_value=2):
            services = json.loads(Home(self, [big, small], keys=["plaza", "vic"]).plan()["compose"])["services"]
            tool = R.engine_tool(ENGINE, "copia", {"memory": "4g", "cpus": "3"})
        self.assertEqual(services["plaza"]["deploy"]["resources"]["limits"], {"memory": "4g", "cpus": "2"})
        self.assertEqual(services["vic"]["deploy"]["resources"]["limits"], {"memory": "4g", "cpus": "2"})
        self.assertEqual(tool[tool.index("--cpus") + 1], "2")

    def test_an_engine_world_of_no_known_talla_gets_no_motor_tope_personas(self):
        service = json.loads(Home(self, [order()], keys=["plaza"]).plan()["compose"])["services"]["plaza"]
        self.assertNotIn("MOTOR_TOPE_PERSONAS", service["environment"])

    def test_an_engine_worlds_site_keeps_metrics_and_sessions_private(self):
        h = Home(self, [order()], keys=["plaza"])
        site = ("plaza.numinia.com {\n\tencode zstd gzip\n\trespond /metrics* 404\n\trespond /sesiones* 404\n"
                "\treverse_proxy plaza:8790 {\n\t\tstream_close_delay 12h\n\t}\n}")
        self.assertIn(site, h.plan()["caddyfile"])

    def test_a_hyperfy_world_beside_an_engine_world_renders_byte_for_byte_as_alone(self):
        hyperfy = order("foro", image=HYPERFY)
        alone = Home(self, [hyperfy], keys=["foro"]).plan()
        both = Home(self, [hyperfy, order()], keys=["foro", "plaza"])
        p = both.plan()
        self.assertEqual(json.loads(p["compose"])["services"]["foro"],
                         {**json.loads(alone["compose"])["services"]["foro"],
                          "env_file": f"{both.home}/env/foro.env", "volumes": [f"{both.home}/data/foro:/app/world"]})
        foro_site = "foro.numinia.com {\n\treverse_proxy foro:3000 {\n\t\tstream_close_delay 12h\n\t}\n}"
        self.assertIn(foro_site, p["caddyfile"])
        self.assertIn(foro_site, alone["caddyfile"])
        self.assertEqual(p["engine_worlds"], ["plaza"])

    def test_an_order_of_another_repository_already_in_a_book_runs_as_before(self):
        p = Home(self, [order(image=OTHER)], keys=["plaza"]).plan()
        service = json.loads(p["compose"])["services"]["plaza"]
        self.assertEqual(service["volumes"][0][-len(":/app/world"):], ":/app/world")
        self.assertIn("reverse_proxy plaza:3000", p["caddyfile"])

    def test_an_engine_worlds_folder_is_private(self):
        h = Home(self, [order(), order("foro", image=HYPERFY)], keys=["plaza", "foro"])
        with mock.patch.object(R, "sh"):
            R.apply(h.plan(), h.home, None)
        self.assertEqual((h.home / "data" / "plaza").stat().st_mode & 0o777, 0o700)
        (h.home / "data" / "plaza").chmod(0o755)  # anything else is put right on the next run
        with mock.patch.object(R, "sh"):
            R.apply(h.plan(), h.home, None)
        self.assertEqual((h.home / "data" / "plaza").stat().st_mode & 0o777, 0o700)
        self.assertTrue((h.home / "data" / "foro").is_dir())

    def test_the_engine_is_not_waited_for_it_saves_when_it_stops(self):
        before = {"services": {"plaza": {"image": ENGINE}, "foro": {"image": HYPERFY,
                                                                 "environment": {"SAVE_INTERVAL": "10"}}}}
        after = {"services": {"plaza": {"image": NEWER}, "foro": {"image": "x"}}}
        self.assertEqual(R.save_waits(before, after, {"plaza", "foro"}), {"plaza": R.SAVE_MARGIN, "foro": 12})
        h = Home(self, [order()])
        queue = h.home / "run" / "copy-queue.json"
        queue.write_text(json.dumps([{"world": "plaza"}]))
        self.assertEqual(R.copies_wait(h.home, h.orders), 0)

    def test_an_engine_tool_runs_with_no_network_as_the_engines_user(self):
        cmd = R.engine_tool(ENGINE, "importar", LIMITS, "/srv/fleet/staging/x:/mundo", stdin=True)
        self.assertEqual(cmd, ["docker", "run", "--rm", "--network", "none", "--user", "10001:10001", "--cap-drop",
                               "ALL", "--security-opt", "no-new-privileges", "--memory", "2g", "--cpus", "1.5", "-i",
                               "-v", "/srv/fleet/staging/x:/mundo", ENGINE, "importar"])
        for image, tool, limits in ((HYPERFY, "copia", LIMITS), (ENGINE, "sh", LIMITS),
                                    (ENGINE, "copia", {"memory": "2g\n", "cpus": "1"})):
            with self.subTest(image=image, tool=tool), self.assertRaises(ValueError):
                R.engine_tool(image, tool, limits)


class Updates(unittest.TestCase):
    """UpdateWorld: another build of the world's own engine, fetched first, started after its save."""

    def test_a_running_engine_world_moves_to_its_new_build(self):
        h = Home(self, [order()], keys=["plaza"])
        h.drop("UpdateWorld", UPDATE["message"], UPDATE["signature"])
        self.assertEqual([t["type"] for t in h.take()], ["UpdateWorld"])
        nonce = UPDATE["message"]["nonce"]
        self.assertEqual(h.pulled, [NEWER])
        self.assertEqual((h.order()["image"], h.order()["updatedBy"]), (NEWER, ORACLE))
        self.assertEqual(h.result(nonce)["state"], "working")  # until Docker has started the new build
        self.assertIn("plaza", R.read_updates(h.home))
        self.assertEqual(json.loads(h.plan()["compose"])["services"]["plaza"]["image"], NEWER)
        self.assertEqual(R.finish_updates(h.home, NOW), ["plaza"])
        self.assertEqual(h.result(nonce)["state"], "done")
        self.assertEqual(R.read_updates(h.home), {})
        self.assertEqual(h.log()[-1], {"at": R.utc(NOW), "type": "UpdateWorld", "world": "plaza",
                                       "by": R.walletsig.short(ORACLE), "state": "done"})

    def test_a_stopped_world_takes_its_new_build_when_it_starts(self):
        h = Home(self, [order(state="stopped")], keys=["plaza"])
        h.drop("UpdateWorld", UPDATE["message"], UPDATE["signature"])
        h.take()
        self.assertEqual(h.result(UPDATE["message"]["nonce"])["state"], "done")
        self.assertEqual(h.order()["image"], NEWER)
        self.assertEqual(R.read_updates(h.home), {})

    def test_an_update_is_refused_by_rule_and_changes_nothing(self):
        cases = {
            "engine-mismatch": (order(image=HYPERFY), False),
            "unknown-engine": (order(image=OTHER), False),
            "no world with this name on this machine": (None, False),
            "the image could not be fetched here": (order(), True),
        }
        for rule, (current, pull_fails) in cases.items():
            with self.subTest(rule=rule):
                h = Home(self, [current] if current else [], keys=["plaza"])
                h.drop("UpdateWorld", UPDATE["message"], UPDATE["signature"])
                h.take(pull_fails=pull_fails)
                result = h.result(UPDATE["message"]["nonce"])
                self.assertEqual((result["state"], result["rule"]), ("failed", rule))
                self.assertEqual(h.order(), current)
                self.assertEqual(R.read_updates(h.home), {})
                self.assertEqual(h.pulled, [NEWER] if pull_fails else [])

    def test_the_new_build_replaces_only_that_container_after_its_save(self):
        h = Home(self, [order(), order("foro", image=HYPERFY)], keys=["plaza", "foro"])
        calls, sleeps = [], []
        with mock.patch.object(R, "sh", lambda *cmd: calls.append(cmd)), contextlib.redirect_stderr(io.StringIO()):
            R.apply(h.plan(), h.home, None, sleep=sleeps.append)  # as Docker last took them
            calls.clear()
            h.drop("UpdateWorld", UPDATE["message"], UPDATE["signature"])
            h.take()
            R.apply(h.plan(), h.home, None, sleep=sleeps.append)
        self.assertEqual(sleeps, [R.SAVE_MARGIN])
        self.assertEqual(calls, [("docker", "compose", "-f", str(h.home / "run" / "docker-compose.yml"), "up", "-d",
                                  "--remove-orphans")])  # compose remakes the one service that changed

    def test_a_change_of_code_reaches_an_engine_world_the_same_way(self):
        h = Home(self, [order()], keys=["plaza"])
        message = {"machine": "open-1", "world": "plaza", "nonce": "a1" * 16, "expires": NOW + 600}
        h.sign_and_drop("RotateWorldCode", message, adminCode=CODE)
        h.take()
        self.assertEqual(h.result(message["nonce"])["state"], "done")
        self.assertEqual((h.home / "env" / "plaza.env").read_text(), "JWT_SECRET=" + "a" * 64 + f"\nADMIN_CODE={CODE}\n")
        self.assertEqual(R.read_restarts(h.home), ["plaza"])

    def test_a_new_world_must_name_an_engine_this_machine_runs(self):
        h = Home(self)
        message = {"machine": "open-1", "world": "nuevo", "domain": "nuevo.numinia.com", "image": OTHER,
                   "memory": "2g", "cpus": "1.5", "maxUploadMb": 50, "nonce": "b2" * 16, "expires": NOW + 600}
        h.sign_and_drop("CreateWorld", message, adminCode=CODE)
        h.take()
        self.assertEqual((h.result(message["nonce"])["state"], h.result(message["nonce"])["rule"]),
                         ("failed", "unknown-engine"))
        self.assertIsNone(h.order("nuevo"))
        h.sign_and_drop("CreateWorld", {**message, "image": ENGINE, "nonce": "b3" * 16}, adminCode=CODE)
        h.take()
        self.assertEqual(h.order("nuevo")["image"], ENGINE)


class Door(unittest.TestCase):
    """What the door says and refuses for engines and updates."""

    def rig(self, *orders, now=NOW):
        rig = T.Rig(self, now=now)
        for o in orders:
            (rig.home / "orders" / f"{o['id']}.json").write_text(json.dumps(o))
        return rig

    def update_body(self):
        return json.dumps({"type": "UpdateWorld", "message": UPDATE["message"],
                           "signature": UPDATE["signature"]}).encode()

    def test_the_door_says_its_engines_and_that_it_updates(self):
        facts = self.rig().door.machine()
        self.assertEqual((facts["engines"], facts["updates"]), (["numinia-hyperfy2", "numinia-engine"], True))

    def test_an_update_is_taken_and_the_world_says_updating_until_its_build_runs(self):
        rig = self.rig(order())
        status, reply = rig.accept(self.update_body())
        self.assertEqual((status, reply), (202, {"request": UPDATE["message"]["nonce"]}))
        [raw] = rig.dropped()
        self.assertEqual((raw["type"], raw["signer"]), ("UpdateWorld", ORACLE))
        self.assertNotIn("adminCode", raw)
        self.assertEqual(rig.door.worlds()["worlds"][0]["phase"], "updating")
        with contextlib.redirect_stderr(io.StringIO()):
            R.take_requests(rig.home, rig.home / "orders", "open-1", frozenset({ORACLE}), frozenset(), CONTROL, NOW,
                            codes_dir=rig.etc / "codes", pull=lambda image: None)
        world = rig.door.worlds()["worlds"][0]
        self.assertEqual((world["phase"], world["image"]), ("updating", NEWER))
        self.assertEqual(rig.door.request(UPDATE["message"]["nonce"])["state"], "working")
        R.finish_updates(rig.home, NOW)
        self.assertEqual(rig.door.worlds()["worlds"][0]["phase"], "running")
        self.assertIn("accepted UpdateWorld plaza", rig.err)

    def test_an_update_is_refused_by_name(self):
        # Each case on its own door: a refused request still spends its number.
        self.assertEqual(self.rig().accept(self.update_body()), (409, {"error": "missing"}))
        self.assertEqual(self.rig(order(image=HYPERFY)).accept(self.update_body()),
                         (409, {"error": "engine-mismatch"}))
        self.assertEqual(self.rig(order(image=OTHER)).accept(self.update_body()), (409, {"error": "unknown-engine"}))
        for kind in ("RotateWorldCode", "UpdateWorld", "StopWorld", "StartWorld", "CreateWorld"):
            with self.subTest(waiting=kind):
                rig = self.rig(order())
                rig.wait(kind)
                self.assertEqual(rig.accept(self.update_body()), (409, {"error": "busy"}))
        rig = self.rig(order())
        (rig.home / "run" / "updating.json").write_text(json.dumps({"plaza": {"nonce": "d" * 32}}))
        self.assertEqual(rig.accept(self.update_body()), (409, {"error": "busy"}))
        rig = self.rig(order())
        rig.wait("DeleteWorld")
        self.assertEqual(rig.accept(self.update_body()), (409, {"error": "still-deleting"}))

    def test_a_change_of_code_waits_for_a_new_build_but_showing_it_does_not(self):
        rotate = json.dumps({"type": "RotateWorldCode", "message": SIGNED["RotateWorldCode"]["message"],
                             "signature": SIGNED["RotateWorldCode"]["signature"]}).encode()
        rig = self.rig(order())
        rig.wait("UpdateWorld")
        self.assertEqual(rig.accept(rotate), (409, {"error": "busy"}))
        rig = self.rig(order())
        (rig.home / "run" / "updating.json").write_text(json.dumps({"plaza": {"nonce": "d" * 32}}))
        self.assertEqual(rig.accept(rotate), (409, {"error": "busy"}))
        (rig.etc / "codes" / "plaza").write_text("f1a2f1a2f1a2f1a2\n")
        show = json.dumps({"type": "ShowWorldCode", "message": SIGNED["ShowWorldCode"]["message"],
                           "signature": SIGNED["ShowWorldCode"]["signature"]}).encode()
        self.assertEqual(rig.accept(show)[0], 202)

    def test_a_new_world_of_an_engine_the_machine_does_not_run_is_refused(self):
        rig = self.rig()
        for image, answer in ((OTHER, (409, {"error": "unknown-engine"})), (ENGINE, 202)):
            with self.subTest(image=image):
                message = {"machine": "open-1", "world": "nuevo", "domain": "nuevo.numinia.com", "image": image,
                           "memory": "2g", "cpus": "1.5", "maxUploadMb": 50, "nonce": os.urandom(16).hex(),
                           "expires": NOW + 600}
                status, reply = rig.accept(json.dumps({"type": "CreateWorld", "message": message,
                                                       "signature": rig.oracle.sign("CreateWorld", message)}).encode())
                self.assertEqual((status, reply) if isinstance(answer, tuple) else status, answer)


def engine_zip(extra=()):
    """A world's zip as the engine makes it (numinia-engine/mundo@2): mundo.json, datos/, aventura/."""
    manifest = {"esquema": "numinia-engine/mundo@2", "ficheros": []}
    return Z.make_zip([("mundo.json", json.dumps(manifest).encode()), ("datos/objetos.json", b"[]"),
                       ("aventura/misiones/una.json", b"{}"), *extra])


class FakeEngine:
    """Answers as the engine image's tools would: comprobar says yes or no, importar writes the world."""

    def __init__(self, refuse=None, copia=None, exit_code=None):
        self.refuse, self.copia, self.exit_code, self.calls = refuse, copia, exit_code, []

    def __call__(self, cmd, **kwargs):
        tool = cmd[-1]
        self.calls.append(cmd)
        if self.exit_code is not None:
            return subprocess.CompletedProcess(cmd, self.exit_code, b"", b"docker: no such image\n")
        if tool == "comprobar":
            data = kwargs["stdin"].read()
            if self.refuse:
                return subprocess.CompletedProcess(cmd, 1, b"", f"starting\n{self.refuse}\n".encode())
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                assert "mundo.json" in z.namelist()
            return subprocess.CompletedProcess(cmd, 0, b"ok\n", b"")
        if tool == "importar":
            folder = Path(cmd[cmd.index("-v") + 1].rsplit(":/mundo", 1)[0])
            assert list(folder.iterdir()) == []  # importar unpacks into an EMPTY /mundo
            with zipfile.ZipFile(io.BytesIO(kwargs["stdin"].read())) as z:
                z.extractall(folder)
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        if tool == "copia":
            kwargs["stdout"].write(self.copia if self.copia is not None else engine_zip())
            return subprocess.CompletedProcess(cmd, 0, None, b"")
        raise AssertionError(tool)


class EngineZips(unittest.TestCase):
    """A Numinia Engine world's zip: the generic rules here, the engine's own by its image (C6)."""

    def bring(self, h, data, image=ENGINE, world="mundo-nuevo"):
        message = Z.import_message(data, world=world, image=image, expires=NOW + 600)
        (h.home / "uploads" / f"{message['nonce']}.zip").write_bytes(data)
        h.drop("ImportWorld", message, h.oracle.sign("ImportWorld", message), adminCode=CODE,
               expiresAt=R.utc(NOW + R.SLOT_SECONDS), bytes=len(data), zip=f"{message['nonce']}.zip")
        return message["nonce"]

    def take(self, h, fake):
        with mock.patch.object(R, "run_tool", fake):
            h.take()

    def test_an_engine_zip_comes_back_through_the_engines_own_tools_stopped(self):
        h = Home(self)
        fake = FakeEngine()
        nonce = self.bring(h, engine_zip())
        self.take(h, fake)
        self.assertEqual(h.result(nonce)["state"], "done", h.result(nonce))
        self.assertEqual([c[-1] for c in fake.calls], ["comprobar", "importar"])
        self.assertNotIn("-v", fake.calls[0])  # comprobar sees the zip and nothing else
        self.assertIn("--network", fake.calls[1])
        world = h.home / "data" / "mundo-nuevo"
        self.assertEqual(world.stat().st_mode & 0o777, 0o700)
        self.assertEqual((world / "datos" / "objetos.json").read_bytes(), b"[]")
        self.assertEqual((h.order("mundo-nuevo")["state"], h.order("mundo-nuevo")["image"]), ("stopped", ENGINE))
        self.assertRegex((h.home / "env" / "mundo-nuevo.env").read_text(),
                         rf"\AJWT_SECRET=[0-9a-f]{{64}}\nADMIN_CODE={CODE}\n\Z")
        self.assertEqual(list((h.home / "staging").iterdir()), [])
        self.assertEqual(list((h.home / "uploads").iterdir()), [])

    def test_a_zip_the_engine_does_not_read_is_refused_with_its_line_and_leaves_nothing(self):
        h = Home(self)
        nonce = self.bring(h, engine_zip())
        self.take(h, FakeEngine(refuse="mundo.json no es numinia-engine/mundo@2"))
        result = h.result(nonce)
        self.assertEqual((result["state"], result["rule"], result["entry"]),
                         ("failed", "engine-refused", "mundo.json no es numinia-engine/mundo@2"))
        self.assertIsNone(h.order("mundo-nuevo"))
        self.assertFalse((h.home / "data" / "mundo-nuevo").exists())
        self.assertEqual(list((h.home / "staging").iterdir()), [])
        self.assertNotIn("mundo.json no es", (h.home / "log.jsonl").read_text())  # the rule, never the line

    def test_an_engine_that_cannot_run_is_not_called_a_bad_zip(self):
        h = Home(self)
        nonce = self.bring(h, engine_zip())
        self.take(h, FakeEngine(exit_code=125))
        self.assertEqual(h.result(nonce)["rule"], "the engine's tools could not run here")
        self.assertIsNone(h.order("mundo-nuevo"))

    def test_the_rules_every_zip_keeps_come_first(self):
        h = Home(self)
        fake = FakeEngine()
        nonce = self.bring(h, engine_zip(extra=[("../fuera.json", b"{}")]))
        self.take(h, fake)
        self.assertEqual((h.result(nonce)["rule"], h.result(nonce)["entry"]), ("zip-slip", "../fuera.json"))
        self.assertEqual(fake.calls, [])  # the engine never saw it

    def test_a_zip_for_an_engine_the_machine_does_not_run_is_refused(self):
        h = Home(self)
        nonce = self.bring(h, engine_zip(), image=OTHER)
        self.take(h, FakeEngine())
        self.assertEqual(h.result(nonce)["rule"], "unknown-engine")

    def test_an_engine_worlds_copy_is_made_by_the_engine(self):
        h = Home(self, [order()], keys=["plaza"])
        (h.home / "data" / "plaza").mkdir()
        (h.home / "run" / "copy-queue.json").write_text(json.dumps(
            [{"copy": COPY, "world": "plaza", "nonce": "e" * 32, "by": ORACLE}]))
        fake = FakeEngine(copia=engine_zip())
        with mock.patch.object(R, "run_tool", fake), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(R.make_copies(h.home, h.orders, "open-1", NOW), [COPY])
        self.assertEqual(fake.calls[0][-1], "copia")
        self.assertIn(f"{h.home}/data/plaza:/mundo:ro", fake.calls[0])
        status = json.loads((h.home / "copies" / f"{COPY}.json").read_text())
        zipped = h.home / "copies" / f"{COPY}.zip"
        self.assertEqual((status["state"], status["sha256"]), ("ready", R.file_digest(zipped)))
        self.assertTrue(status["filename"].startswith("plaza-"))
        self.assertEqual(zipped.read_bytes(), fake.copia)
        self.assertEqual(zipped.stat().st_mode & 0o777, 0o640)

    def test_a_copy_the_engine_could_not_make_fails_and_leaves_nothing(self):
        for fake in (FakeEngine(exit_code=1), FakeEngine(copia=b"not a zip")):
            with self.subTest(fake=fake.exit_code):
                h = Home(self, [order()], keys=["plaza"])
                (h.home / "data" / "plaza").mkdir()
                (h.home / "run" / "copy-queue.json").write_text(json.dumps(
                    [{"copy": COPY, "world": "plaza", "nonce": "e" * 32, "by": ORACLE}]))
                with mock.patch.object(R, "run_tool", fake), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(R.make_copies(h.home, h.orders, "open-1", NOW), [])
                self.assertEqual(json.loads((h.home / "copies" / f"{COPY}.json").read_text())["state"], "failed")
                self.assertEqual(sorted(p.name for p in (h.home / "copies").iterdir()), [f"{COPY}.json"])


if __name__ == "__main__":
    unittest.main()
