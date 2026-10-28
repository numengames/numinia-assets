# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""fleet-condiciones: the by-hand switch for a world's conditions door — file first, restart apart, nothing lost, no Docker needed."""

from pathlib import Path
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest


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
C = load("condiciones")

ENGINE = "ghcr.io/numengames/numinia-engine@sha256:" + "a" * 64
HYPERFY = "ghcr.io/numengames/numinia-hyperfy2:sha-abc"
CANARY = "canary-value-must-never-be-printed"
T0 = 1_800_000_000.0

ON_DOC = {
    "formato": "condiciones@1",
    "puerta": True,
    "capas": [
        {"capa": "casa", "version": "1.0", "puntos": [{"id": "a"}, {"id": "b"}]},
        {"capa": "comunidad", "version": "2.1", "puntos": [{"id": "c"}]},
    ],
}
LAYERS = "casa 1.0 (2) → comunidad 2.1 (1)"
REAL = b'{"formato": "condiciones@1", "capas": [{"capa": "casa", "version": "9", "puntos": []}]}\n'  # a world's own file: no puerta


def doc(**changes):
    out = dict(ON_DOC)
    out.update(changes)
    return (json.dumps(out, indent=2) + "\n").encode()


class FakeDocker:
    """Just enough docker for the program: inspect, logs, restart. Every call is recorded."""

    def __init__(self, env_dir):
        self.calls = []
        self.containers = {}
        self.env_dir = env_dir
        self.restart_exit = 0
        self.after_restart = ["running healthy"]  # what inspect says on each look after a restart, the last one repeating
        self.looks = []
        self.write_door_line = True

    def add(self, wid, status="running", health="healthy", logs=()):
        self.containers[f"run-{wid}-1"] = {"status": status, "health": health, "logs": list(logs), "restarts": 0, "started": "2026-10-21T10:00:00Z", "wid": wid}

    def door_line(self, wid):
        aventura = self.env_dir / "data" / wid / "aventura" / "condiciones.json"
        try:
            asks = json.loads(aventura.read_text()).get("puerta") is True
        except (OSError, ValueError):
            asks = False
        asks = asks or self.force_env.get(wid) is True
        return ("[condiciones] puerta ENCENDIDA: nadie se mueve, habla ni construye hasta aceptar las normas obligatorias" if asks
                else '[condiciones] puerta apagada (MOTOR_CONDICIONES_PUERTA=si o "puerta": true en condiciones.json la enciende)')

    force_env: dict = {}

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        if args[0] != "docker":
            return 127, "", "not found"
        name = args[-1]
        box = self.containers.get(name)
        if args[1] == "inspect":
            if box is None:
                return 1, "[]", "Error: No such object"
            if self.looks and len(self.looks) > 0 and box.get("restarted"):
                status, health = self.looks.pop(0) if len(self.looks) > 1 else self.looks[0]
                box["status"], box["health"] = status, health
            info = {"RestartCount": box["restarts"], "State": {"Status": box["status"], "StartedAt": box["started"]}}
            if box["health"]:
                info["State"]["Health"] = {"Status": box["health"]}
            return 0, json.dumps([info]), ""
        if args[1] == "logs":
            if box is None:
                return 1, "", "no such container"
            lines = box["logs"][box.get("since", 0):] if "--since" in args else box["logs"]
            return 0, "\n".join(["noise", *lines]) + "\n", ""
        if args[1] == "restart":
            if self.restart_exit or box is None:
                return self.restart_exit or 1, "", "Error response from daemon: boom"
            box["restarted"] = True
            box["since"] = len(box["logs"])
            box["started"] = "2026-10-21T10:05:00Z"
            box["logs"].append("[mundo] up")
            if self.write_door_line:
                box["logs"] += [self.door_line(box["wid"]), "[condiciones] del mundo: " + LAYERS]
            return 0, name + "\n", ""
        raise AssertionError(f"unexpected command {args}")


class Base(unittest.TestCase):
    wid = "alchemiststower"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.aventura = self.home / "data" / self.wid / "aventura"
        self.aventura.mkdir(parents=True)
        (self.home / "run").mkdir()
        (self.home / "env").mkdir()
        (self.home / "env" / f"{self.wid}.env").write_text(f"JWT_SECRET={CANARY}\nADMIN_CODE=abcd1234\n")
        self.services = {"caddy": {"image": "caddy:2"}, self.wid: {"image": ENGINE, "environment": {"MOTOR_PASE_MUNDO": self.wid}}}
        self.write_compose()
        self.docker = FakeDocker(self.home)
        self.docker.force_env = {}
        self.docker.add(self.wid, logs=["[condiciones] puerta apagada (x)", "[condiciones] de la casa: casa 1.0 (6)"])
        self.chowns = []
        self.clock = T0
        self.sleeps = []
        self.downloads = []
        self.fetched = doc()

        def fetch(url):
            self.downloads.append(url)
            if isinstance(self.fetched, Exception):
                raise self.fetched
            return self.fetched

        def sleep(seconds):
            self.sleeps.append(seconds)
            self.clock += seconds

        self.env = C.Env(home=self.home, run=self.docker, fetch=fetch, euid=lambda: 0,
                         chown=lambda path, uid, gid: self.chowns.append((Path(path).name, uid, gid)),
                         now=lambda: self.clock, sleep=sleep, echo=False)
        self.source = self.home / "mine.json"
        self.source.write_bytes(doc())

    def write_compose(self):
        (self.home / "run" / "docker-compose.yml").write_text(json.dumps({"services": self.services}))

    def go(self, *argv):
        self.env.out.clear()
        code = C.main(list(argv), env=self.env)
        return code, "\n".join(self.env.out)

    def tree(self):
        return {str(p.relative_to(self.home)): p.read_bytes() for p in sorted(self.home.rglob("*")) if p.is_file()}

    def file(self, name="condiciones.json"):
        path = self.aventura / name
        return path.read_bytes() if path.exists() else None

    def docker_verbs(self):
        return [c[1] for c in self.docker.calls]


class On(Base):
    def test_places_the_file_and_changes_nothing_else(self):
        code, said = self.go(self.wid, "on", "--source", str(self.source))
        self.assertEqual(code, 0, said)
        self.assertEqual(json.loads(self.file())["puerta"], True)
        self.assertEqual(json.loads(self.file("condiciones.json.puesta")), {
            "antes": False, "sha256": C.sha(doc()), "fuente": str(self.source), "en": R.utc(T0)})
        self.assertIsNone(self.file("condiciones.json.antes"))
        self.assertIn((".condiciones.json.fleet-tmp", 10001, 10001), self.chowns)  # the engine's user owns it
        self.assertEqual(self.docker.calls, [])  # the harmless step never touches docker
        self.assertIn(f"fleet-condiciones {self.wid} restart", said.splitlines()[-1])
        self.assertIn("has NOT changed", said.splitlines()[-1])
        self.assertIn(LAYERS, said)
        self.assertEqual(sorted(p.name for p in self.aventura.iterdir()), ["condiciones.json", "condiciones.json.puesta"])  # no temp file left

    def test_default_source_is_pinned_and_checked_against_its_hash(self):
        self.assertRegex(C.DEFAULT_SOURCE, r"^https://raw\.githubusercontent\.com/numengames/numinia-engine/[0-9a-f]{40}/aventuras/condiciones-prueba/condiciones\.json$")
        self.assertRegex(C.DEFAULT_SHA256, r"^[0-9a-f]{64}$")
        code, said = self.go(self.wid, "on")  # the download is not the pinned file
        self.assertEqual(code, C.SOURCE, said)
        self.assertEqual(self.downloads, [C.DEFAULT_SOURCE])
        self.assertIsNone(self.file())
        C.DEFAULT_SHA256, saved = C.sha(self.fetched), C.DEFAULT_SHA256
        self.addCleanup(setattr, C, "DEFAULT_SHA256", saved)
        code, said = self.go(self.wid, "on")
        self.assertEqual(code, 0, said)
        self.assertEqual(self.file(), self.fetched)

    def test_an_existing_file_is_kept_and_off_puts_it_back_exactly(self):
        (self.aventura / "condiciones.json").write_bytes(REAL)
        code, said = self.go(self.wid, "on", "--source", str(self.source))
        self.assertEqual(code, 0, said)
        self.assertEqual(self.file("condiciones.json.antes"), REAL)
        self.assertEqual(self.file(), doc())
        self.assertTrue(json.loads(self.file("condiciones.json.puesta"))["antes"])
        code, said = self.go(self.wid, "off")
        self.assertEqual(code, 0, said)
        self.assertEqual(self.file(), REAL)  # restored byte for byte
        self.assertEqual(self.file("condiciones.json.antes"), REAL)  # and the copy stays
        self.assertIsNone(self.file("condiciones.json.puesta"))
        self.assertEqual(self.docker.calls, [])

    def test_off_deletes_only_what_on_placed_when_there_was_none(self):
        self.go(self.wid, "on", "--source", str(self.source))
        code, said = self.go(self.wid, "off")
        self.assertEqual(code, 0, said)
        self.assertEqual(sorted(p.name for p in self.aventura.iterdir()), [])
        self.assertIn("no file", said.splitlines()[-1])

    def test_on_twice_is_harmless_and_never_replaces_the_original_copy(self):
        (self.aventura / "condiciones.json").write_bytes(REAL)
        self.go(self.wid, "on", "--source", str(self.source))
        before = self.tree()
        code, said = self.go(self.wid, "on", "--source", str(self.source))
        self.assertEqual(code, 0, said)
        self.assertIn("already in place", said)
        self.assertEqual(self.tree(), before)
        self.go(self.wid, "off")
        self.assertEqual(self.file(), REAL)

    def test_a_new_source_replaces_ours_and_still_restores_the_original(self):
        (self.aventura / "condiciones.json").write_bytes(REAL)
        self.go(self.wid, "on", "--source", str(self.source))
        self.source.write_bytes(doc(capas=[{"capa": "casa", "version": "3.0", "puntos": []}]))
        self.assertEqual(self.go(self.wid, "on", "--source", str(self.source))[0], 0)
        self.assertEqual(self.file(), self.source.read_bytes())
        self.assertEqual(self.file("condiciones.json.antes"), REAL)
        self.go(self.wid, "off")
        self.assertEqual(self.file(), REAL)

    def test_a_file_edited_by_someone_after_on_is_kept_aside_not_lost(self):
        self.go(self.wid, "on", "--source", str(self.source))
        (self.aventura / "condiciones.json").write_bytes(REAL)  # the world's owner changed it
        self.source.write_bytes(doc(capas=[{"capa": "casa", "version": "3.0", "puntos": []}]))
        code, said = self.go(self.wid, "on", "--source", str(self.source))
        self.assertEqual(code, 0, said)
        saved = [p for p in self.aventura.iterdir() if p.name.startswith("condiciones.json.antes-")]
        self.assertEqual([p.read_bytes() for p in saved], [REAL])
        self.go(self.wid, "off")
        self.assertEqual(self.file(), None)  # nothing was there before our first `on`
        self.assertEqual([p.read_bytes() for p in saved if p.exists()], [REAL])  # and the edited file is still kept

    def test_a_file_placed_by_hand_with_an_antes_is_restored_by_off(self):
        (self.aventura / "condiciones.json.antes").write_bytes(REAL)
        (self.aventura / "condiciones.json").write_bytes(doc())  # Pablo's manual `on`: no note
        code, said = self.go(self.wid, "off")
        self.assertEqual(code, 0, said)
        self.assertEqual(self.file(), REAL)
        self.assertEqual([p.read_bytes() for p in self.aventura.iterdir() if p.name.startswith("condiciones.json.quitado-")], [doc()])  # kept aside, not destroyed

    def test_off_will_not_guess_about_a_file_nobody_recorded(self):
        (self.aventura / "condiciones.json").write_bytes(REAL)
        before = self.tree()
        code, said = self.go(self.wid, "off")
        self.assertEqual(code, C.FILES, said)
        self.assertEqual(self.tree(), before)
        code, said = self.go(self.wid, "off", "--force")
        self.assertEqual(code, 0, said)
        self.assertIsNone(self.file())
        self.assertEqual([p.read_bytes() for p in self.aventura.iterdir() if p.name.startswith("condiciones.json.quitado-")], [REAL])

    def test_off_with_nothing_to_undo_says_so(self):
        code, said = self.go(self.wid, "off")
        self.assertEqual(code, 0, said)
        self.assertIn("nothing to do", said)

    def test_off_refuses_when_the_original_copy_is_gone(self):
        (self.aventura / "condiciones.json").write_bytes(REAL)
        self.go(self.wid, "on", "--source", str(self.source))
        (self.aventura / "condiciones.json.antes").unlink()
        before = self.tree()
        code, said = self.go(self.wid, "off")
        self.assertEqual(code, C.FILES, said)
        self.assertEqual(self.tree(), before)

    def test_a_world_that_never_started_has_no_aventura_and_gets_none_made(self):
        for entry in self.aventura.iterdir():
            entry.unlink()
        self.aventura.rmdir()
        code, said = self.go(self.wid, "on", "--source", str(self.source))
        self.assertEqual(code, C.FILES, said)
        self.assertFalse(self.aventura.exists())  # making it would stop the engine seeding its default adventure

    def test_links_planted_in_the_world_are_not_followed(self):
        outside = self.home / "outside.txt"
        outside.write_text("keep me")
        (self.aventura / "condiciones.json").symlink_to(outside)
        for argv in (("on", "--source", str(self.source)), ("off",), ("status",)):
            code, said = self.go(self.wid, *argv)
            self.assertEqual(code, C.FILES, said)
        self.assertEqual(outside.read_text(), "keep me")
        (self.aventura / "condiciones.json").unlink()
        (self.aventura / "condiciones.json.antes").symlink_to(outside)
        self.assertEqual(self.go(self.wid, "on", "--source", str(self.source))[0], C.FILES)
        (self.aventura / "condiciones.json.antes").unlink()
        moved = self.home / "elsewhere"
        self.aventura.rename(moved)
        self.aventura.symlink_to(moved)
        self.assertEqual(self.go(self.wid, "on", "--source", str(self.source))[0], C.FILES)
        self.assertFalse((moved / "condiciones.json").exists())

    def test_a_planted_temp_name_is_replaced_not_written_through(self):
        outside = self.home / "outside.txt"
        outside.write_text("keep me")
        (self.aventura / ".condiciones.json.fleet-tmp").symlink_to(outside)
        self.assertEqual(self.go(self.wid, "on", "--source", str(self.source))[0], 0)
        self.assertEqual(outside.read_text(), "keep me")
        self.assertEqual(self.file(), doc())

    def test_a_note_that_is_not_ours_stops_everything(self):
        (self.aventura / "condiciones.json.puesta").write_text("not json")
        code, said = self.go(self.wid, "on", "--source", str(self.source))
        self.assertEqual(code, C.FILES, said)
        (self.aventura / "condiciones.json.puesta").write_text(json.dumps({"antes": "yes"}))
        self.assertEqual(self.go(self.wid, "off")[0], C.FILES)


class Source(Base):
    def refuses(self, *argv):
        before = self.tree()
        code, said = self.go(self.wid, "on", *argv)
        self.assertEqual(code, C.SOURCE, said)
        self.assertEqual(self.tree(), before)  # nothing placed, nothing written
        self.assertEqual(self.docker.calls, [])
        return said

    def test_failed_download(self):
        self.fetched = OSError("Network is unreachable")
        self.assertIn("Network is unreachable", self.refuses("--source", "https://example.org/c.json"))

    def test_invalid_json(self):
        self.source.write_bytes(b'{"formato": "condiciones@1", ')
        self.assertIn("not valid JSON", self.refuses("--source", str(self.source)))
        self.source.write_bytes(b"\xff\xfe")
        self.refuses("--source", str(self.source))

    def test_not_a_conditions_document(self):
        for body in (b"[]", b'{"puerta": true}', doc(formato="otro@1"), doc(capas=[]), doc(capas=[{"capa": "casa"}]), doc(capas="x")):
            self.source.write_bytes(body)
            self.refuses("--source", str(self.source))

    def test_a_source_that_would_not_turn_the_door_on(self):
        for body in (doc(puerta=False), doc(puerta="true"), json.dumps({k: v for k, v in ON_DOC.items() if k != "puerta"}).encode()):
            self.source.write_bytes(body)
            self.assertIn('"puerta": true', self.refuses("--source", str(self.source)))

    def test_too_big(self):
        self.source.write_bytes(doc(nota="x" * C.MAX_BYTES))
        self.assertIn("bigger", self.refuses("--source", str(self.source)))

    def test_source_kinds(self):
        self.refuses("--source", "relative/path.json")
        self.refuses("--source", "http://example.org/c.json")
        self.refuses("--source", "file:///etc/passwd")
        self.refuses("--source", str(self.home / "missing.json"))
        self.refuses("--source", str(self.home))  # a folder
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.go(self.wid, "on", "--source", "https://example.org/c.json")[0], 0)
        self.assertEqual(self.downloads, ["https://example.org/c.json"])


class Worlds(Base):
    def test_unknown_worlds_are_refused_before_anything_happens(self):
        (self.home / "data" / "orphan").mkdir()  # a folder with no container in the compose
        self.services["hyper"] = {"image": HYPERFY}
        (self.home / "data" / "hyper").mkdir()
        self.write_compose()
        (self.home / "data" / "nofolder-link").symlink_to(self.home / "data" / self.wid)
        self.services["nofolder-link"] = {"image": ENGINE}
        self.write_compose()
        before = self.tree()
        for name in ("missing", "orphan", "hyper", "caddy", "Bad_Name", "../x", "a/b", "", "nofolder-link"):
            for action in ("on", "off", "status", "restart"):
                code, said = self.go(name, action, "--dry-run") if action != "status" else self.go(name, action)
                self.assertEqual(code, C.UNKNOWN_WORLD, f"{name} {action}: {said}")
                self.assertIn("Nothing was touched", said)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.docker.calls, [])

    def test_compose_that_cannot_be_read_refuses(self):
        (self.home / "run" / "docker-compose.yml").write_text("services: [")
        self.assertEqual(self.go(self.wid, "status")[0], C.UNKNOWN_WORLD)
        (self.home / "run" / "docker-compose.yml").unlink()
        self.assertEqual(self.go(self.wid, "status")[0], C.UNKNOWN_WORLD)

    def test_must_be_root(self):
        self.env.euid = lambda: 1000
        before = self.tree()
        for action in ("on", "off", "status", "restart"):
            code, said = self.go(self.wid, action)
            self.assertEqual(code, C.GENERAL, said)
            self.assertIn("sudo", said)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.docker.calls, [])

    def test_permission_errors_are_a_sentence_not_a_traceback(self):
        def deny(self_):
            raise PermissionError(13, "denied", "/x")
        original = C.read_state
        C.read_state = lambda world: deny(world)
        self.addCleanup(setattr, C, "read_state", original)
        code, said = self.go(self.wid, "status")
        self.assertEqual(code, C.GENERAL)
        self.assertIn("sudo", said)

    def test_usage_errors(self):
        for argv in ([self.wid, "sideways"], []):
            with contextlib.redirect_stderr(io.StringIO()) as noise, self.assertRaises(SystemExit) as stop:
                C.main(argv, env=self.env)
            self.assertEqual(stop.exception.code, 2)
            self.assertIn("usage: fleet-condiciones", noise.getvalue())
        self.assertEqual(self.go(self.wid, "off", "--source", str(self.source))[0], C.USAGE)
        self.assertEqual(self.go(self.wid, "restart", "--wait", "0")[0], C.USAGE)


class DryRun(Base):
    def test_dry_run_changes_nothing_and_says_what_it_would_do(self):
        (self.aventura / "condiciones.json").write_bytes(REAL)
        before = self.tree()
        code, said = self.go(self.wid, "on", "--source", str(self.source), "--dry-run")
        self.assertEqual(code, 0, said)
        self.assertIn("would keep the existing condiciones.json as condiciones.json.antes", said)
        self.assertIn("would write", said)
        self.assertEqual(self.tree(), before)
        self.go(self.wid, "on", "--source", str(self.source))
        before = self.tree()
        code, said = self.go(self.wid, "off", "--dry-run")
        self.assertEqual(code, 0, said)
        self.assertIn("would restore", said)
        self.assertEqual(self.tree(), before)
        code, said = self.go(self.wid, "restart", "--dry-run")
        self.assertEqual(code, 0, said)
        self.assertIn("would run: docker restart run-alchemiststower-1", said)
        self.assertNotIn("restart", self.docker_verbs())
        self.assertEqual(len(self.chowns), 3)  # only the real `on` wrote: the copy, the file, the note

    def test_dry_run_still_refuses_a_bad_source(self):
        self.source.write_bytes(b"nope")
        self.assertEqual(self.go(self.wid, "on", "--source", str(self.source), "--dry-run")[0], C.SOURCE)


class Status(Base):
    def test_status_reads_and_never_writes(self):
        self.go(self.wid, "on", "--source", str(self.source))
        before = self.tree()
        self.docker.calls.clear()
        code, said = self.go(self.wid, "status")
        self.assertEqual(self.tree(), before)
        self.assertEqual(set(self.docker_verbs()), {"inspect", "logs"})
        self.assertEqual(code, C.NOT_AS_ASKED, said)  # file says on, the world started off
        self.assertIn('present, puerta true', said)
        self.assertIn(LAYERS, said)
        self.assertIn("running, health healthy", said)
        self.assertIn("log: [condiciones] puerta apagada", said)
        self.assertIn("NOT the same", said.splitlines()[-1])
        self.assertIn(f"fleet-condiciones {self.wid} restart", said.splitlines()[-1])

    def test_status_consistent_both_ways(self):
        code, said = self.go(self.wid, "status")
        self.assertEqual(code, 0, said)
        self.assertIn("absent", said)
        self.assertIn("All consistent: the door is OFF", said.splitlines()[-1])
        self.go(self.wid, "on", "--source", str(self.source))
        self.go(self.wid, "restart")
        code, said = self.go(self.wid, "status")
        self.assertEqual(code, 0, said)
        self.assertIn("All consistent: the door is ON", said.splitlines()[-1])

    def test_status_after_off_but_before_restart(self):
        self.go(self.wid, "on", "--source", str(self.source))
        self.go(self.wid, "restart")
        self.go(self.wid, "off")
        code, said = self.go(self.wid, "status")
        self.assertEqual(code, C.NOT_AS_ASKED, said)
        self.assertIn("started with the door ON", said.splitlines()[-1])

    def test_status_of_an_unhealthy_or_missing_container(self):
        self.docker.containers[f"run-{self.wid}-1"]["health"] = "unhealthy"
        code, said = self.go(self.wid, "status")
        self.assertEqual(code, C.NOT_AS_ASKED, said)
        self.assertIn("Not healthy", said)
        del self.docker.containers[f"run-{self.wid}-1"]
        code, said = self.go(self.wid, "status")
        self.assertEqual(code, C.NOT_AS_ASKED, said)
        self.assertIn("NOT FOUND", said)

    def test_status_with_no_door_line_in_the_log(self):
        self.docker.containers[f"run-{self.wid}-1"]["logs"] = ["[mundo] up"]
        code, said = self.go(self.wid, "status")
        self.assertEqual(code, C.NOT_AS_ASKED, said)
        self.assertIn("no [condiciones] line", said)

    def test_the_environment_wins_over_the_file_and_is_said(self):
        self.services[self.wid]["environment"] = {"MOTOR_CONDICIONES_PUERTA": "si"}
        self.write_compose()
        code, said = self.go(self.wid, "status")
        self.assertIn("= si (wins over the file)", said)
        self.assertEqual(code, C.NOT_AS_ASKED, said)  # on in the settings, the world's last start said off
        self.services[self.wid]["environment"] = ["MOTOR_CONDICIONES_PUERTA=no"]
        self.write_compose()
        self.go(self.wid, "on", "--source", str(self.source))
        self.assertIn("= no (wins over the file)", self.go(self.wid, "status")[1])
        self.assertIn("after it the door should be OFF", self.go(self.wid, "restart", "--dry-run")[1])

    def test_the_key_file_can_set_it_too_and_off_warns_when_it_would_still_be_on(self):
        (self.home / "env" / f"{self.wid}.env").write_text(f"JWT_SECRET={CANARY}\nexport MOTOR_CONDICIONES_PUERTA = \"si\"\n")
        self.assertIn("= si (wins over the file)", self.go(self.wid, "status")[1])
        code, said = self.go(self.wid, "off")
        self.assertEqual(code, 0)
        self.assertIn("WARNING", said.splitlines()[-1])
        (self.home / "env" / f"{self.wid}.env").write_text("MOTOR_CONDICIONES_PUERTA=quizás\n")  # something the engine ignores
        self.assertIn("not set", self.go(self.wid, "status")[1])


class Restart(Base):
    def prepare(self):
        self.go(self.wid, "on", "--source", str(self.source))
        self.docker.calls.clear()

    def test_restarts_only_that_container_and_checks_it(self):
        self.prepare()
        code, said = self.go(self.wid, "restart")
        self.assertEqual(code, 0, said)
        restarts = [c for c in self.docker.calls if c[1] == "restart"]
        self.assertEqual(restarts, [["docker", "restart", "run-alchemiststower-1"]])
        self.assertFalse([c for c in self.docker.calls if "compose" in c or "up" in c or "down" in c])
        self.assertIn("log: [condiciones] puerta ENCENDIDA", said)
        self.assertIn(f"log: [condiciones] del mundo: {LAYERS}", said)
        self.assertTrue(said.splitlines()[-1].startswith("=> Done: run-alchemiststower-1 restarted, is healthy, and its door is ON"))
        logs = [c for c in self.docker.calls if c[1] == "logs"][-1]
        self.assertIn("2026-10-21T10:05:00Z", logs)  # only the lines since the new start count

    def test_restart_failure_stops_with_its_own_code(self):
        self.prepare()
        self.docker.restart_exit = 1
        code, said = self.go(self.wid, "restart")
        self.assertEqual(code, C.RESTART, said)
        self.assertIn("FAILED", said.splitlines()[-1])
        self.assertIn("boom", said)
        self.assertEqual(self.docker_verbs().count("restart"), 1)  # no retry, no other container

    def test_waits_for_healthy_and_gives_up(self):
        self.prepare()
        self.docker.looks = [("running", "starting"), ("running", "starting"), ("running", "healthy")]
        code, said = self.go(self.wid, "restart")
        self.assertEqual(code, 0, said)
        self.assertEqual(self.sleeps, [2, 2])
        self.docker.looks = [("running", "starting")]
        self.sleeps.clear()
        code, said = self.go(self.wid, "restart", "--wait", "6")
        self.assertEqual(code, C.NOT_AS_ASKED, said)
        self.assertEqual(self.sleeps, [2, 2, 2])
        self.assertIn("did not become healthy in 6s", said)

    def test_a_container_that_dies_or_turns_unhealthy_is_reported_at_once(self):
        for look in (("exited", None), ("running", "unhealthy"), ("restarting", None)):
            self.prepare()
            self.docker.looks = [look]
            self.sleeps.clear()
            code, said = self.go(self.wid, "restart")
            self.assertEqual(code, C.NOT_AS_ASKED, said)
            self.assertEqual(self.sleeps, [])

    def test_a_container_without_a_healthcheck_counts_once_running(self):
        self.prepare()
        self.docker.containers[f"run-{self.wid}-1"]["health"] = None
        self.assertEqual(self.go(self.wid, "restart")[0], 0)

    def test_healthy_but_the_log_says_another_door_state_is_not_success(self):
        self.prepare()
        self.docker.force_env = {}
        original = self.docker.door_line
        self.docker.door_line = lambda wid: "[condiciones] puerta apagada (nope)"
        code, said = self.go(self.wid, "restart")
        self.docker.door_line = original
        self.assertEqual(code, C.NOT_AS_ASKED, said)
        self.assertIn("started with the door OFF and it should be ON", said)

    def test_healthy_but_no_door_line_at_all(self):
        self.prepare()
        self.docker.write_door_line = False
        code, said = self.go(self.wid, "restart")
        self.assertEqual(code, C.NOT_AS_ASKED, said)
        self.assertIn("no door line", said)

    def test_there_is_no_container_to_restart(self):
        del self.docker.containers[f"run-{self.wid}-1"]
        code, said = self.go(self.wid, "restart")
        self.assertEqual(code, C.UNKNOWN_WORLD, said)
        self.assertNotIn("restart", self.docker_verbs())

    def test_off_then_restart_turns_it_off_again(self):
        self.prepare()
        self.go(self.wid, "restart")
        self.go(self.wid, "off")
        code, said = self.go(self.wid, "restart")
        self.assertEqual(code, 0, said)
        self.assertIn("its door is OFF", said.splitlines()[-1])


class Secrets(Base):
    def test_the_key_file_is_never_printed(self):
        for argv in (("on", "--source", str(self.source)), ("status",), ("restart", "--dry-run"), ("restart",), ("off",), ("off", "--dry-run")):
            code, said = self.go(self.wid, *argv)
            self.assertNotIn(CANARY, said)
            self.assertNotIn("abcd1234", said)
            self.assertNotIn("JWT_SECRET", said)
        self.assertNotIn(CANARY, json.dumps(self.docker.calls))


class Installed(unittest.TestCase):
    """The machine gets the command from the bootstrap, like its other programs."""

    def test_the_bootstrap_installs_the_program_and_its_command(self):
        text = (MACHINE / "bootstrap.sh").read_text()
        self.assertIn("nightly.py recipe.py condiciones.py signers.py versions.py cleanup.py vitals.py; do", text)
        self.assertIn("wrapper fleet-condiciones condiciones.py", text)

    def test_the_updater_manages_the_command_with_the_others(self):
        REC = load("recipe")
        with tempfile.TemporaryDirectory() as tmp:
            lib, sbin = Path(tmp, "lib"), Path(tmp, "sbin")
            lib.mkdir(), sbin.mkdir()
            (lib / "condiciones.py").write_text("x")
            (sbin / "fleet-condiciones").write_text("x")
            updater = REC.Env(home=Path(tmp, "home"), lib_dir=lib, sbin_dir=sbin, unit_dir=Path(tmp, "units"), installed_file=Path(tmp, "recipe"))
            self.assertEqual(sorted(p.name for p in updater.managed()), ["condiciones.py", "fleet-condiciones"])

    def test_the_readme_documents_it(self):
        text = (MACHINE / "README.md").read_text()
        self.assertTrue("fleet-condiciones" in text and "`condiciones.py`" in text and "test_condiciones.py" in text)


if __name__ == "__main__":
    unittest.main()
