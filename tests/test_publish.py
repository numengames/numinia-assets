# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""PublishVersion: the journal, the world taken down, the folders swapped by inode, the world brought back.

The rig is test_duplicate's (a temporary fleet home with a world `plaza`, a
throwaway Oracle, requests dropped in the inbox) with a fake Docker on top: a
set of containers that `docker compose up` makes equal to the compose file,
and the reconciler's own run_once driving it all. The stand-ins go on `V.R`,
the `reconcile` that `versions` imported (see test_duplicate).
"""

from pathlib import Path
import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent


def load(name, folder=HERE):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    spec = importlib.util.spec_from_file_location(name, folder / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TD = load("test_duplicate")  # its rig, and through it the machine's modules
TV, V, R = TD.TV, TD.V, TD.R
CONTROL = TV.CONTROL
KEYS = "JWT_SECRET=x\nADMIN_CODE=abcd1234efgh\n"


def ino(path):
    return os.lstat(path).st_ino


def request(kind, **over):
    return TV.message(kind, **over)


class Rig:
    """A world `plaza` with two versions (v1 published, v2 beside it) on a fake Docker, run by run_once."""

    def __init__(self, test, *, state="running", engine=False, versions=True):
        self.test = test
        self.m = m = TD.rig(test, engine=engine, state=state)
        self.home = m.home
        (self.home / "env" / "plaza.env").write_text(KEYS, encoding="utf-8")
        m.codes.mkdir()
        (self.home / "signers").write_text("", encoding="utf-8")
        self.data = self.home / "data" / "plaza"
        (self.data / "marker").write_bytes(b"A")
        if versions:
            m.write_meta(TV.two())
            self.v2 = m.put_version("v2")
            (self.v2 / "marker").write_bytes(b"B")
        self.v1 = self.home / "versions" / "plaza" / "v1"
        self.containers = set()
        self.ups = []  # the services of each full `docker compose up`
        self.sleeps = []
        self.gone_calls = []  # what the folders looked like each time Docker was asked about the container
        self.hold = False  # the container refuses to go
        self.refuse = False  # Docker takes no book
        self.err = ""
        self.args = argparse.Namespace(home=self.home, codes_dir=m.codes, admin_code_file=self.home / "none",
                                       signers_file=self.home / "signers")
        original = R.apply
        for patcher in (
            mock.patch.object(R, "sh", self.sh),
            mock.patch.object(R, "apply", lambda p, home, code, codes_dir=None: original(
                p, home, code, sleep=self.sleeps.append, codes_dir=codes_dir)),
            mock.patch.object(V, "docker_gone", self.gone),
        ):
            patcher.start()
            test.addCleanup(patcher.stop)

    # --- the fake Docker
    def sh(self, *cmd):
        if cmd[4:5] != ("up",):
            return
        if self.refuse:
            raise subprocess.CalledProcessError(1, cmd)
        names = set(json.loads(Path(cmd[3]).read_text(encoding="utf-8"))["services"]) - {"caddy"}
        if "--no-deps" in cmd:
            self.containers.add(cmd[-1])
        else:
            self.containers = names
            self.ups.append(sorted(names))

    def gone(self, wid):
        self.gone_calls.append((wid, self.data.exists(), self.v1.exists()))
        return wid not in self.containers and not self.hold

    # --- the machine
    def run(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            ok = R.run_once(self.args, "open-1", self.m.orders, None, CONTROL, set())
        self.err = err.getvalue()
        return ok

    def publish(self, version="v2", world="plaza"):
        nonce = self.m.drop("PublishVersion", request("PublishVersion", world=world, version=version))
        self.m.take()
        return self.m.result(nonce)

    def journal(self):
        return R.read_switching(self.home)

    def plan(self):
        return R.plan(self.m.orders, self.home, "open-1", allowed=set(), control_host=CONTROL)

    def published(self):
        return self.m.meta()["published"]

    def order_state(self, state):
        order = json.loads((self.m.orders / "plaza.json").read_text(encoding="utf-8"))
        order["state"] = state
        (self.m.orders / "plaza.json").write_text(json.dumps(order), encoding="utf-8")


class Happy(unittest.TestCase):
    def test_a_running_world_is_taken_down_switched_and_brought_back(self):
        r = Rig(self)
        self.assertTrue(r.run())  # Docker took the book: the world runs
        self.assertEqual((r.containers, r.sleeps), ({"plaza"}, []))
        before_data, before_to = ino(r.data), ino(r.v2)

        result = r.publish()
        self.assertEqual((result["state"], result["rule"], result["version"]), ("working", None, "v2"))
        job = r.journal()["plaza"]
        self.assertEqual((job["from"], job["to"], job["inoData"], job["inoTo"], job["by"]),
                         ("v1", "v2", before_data, before_to, r.m.oracle.address))
        self.assertEqual(job["nonce"], result["request"])
        self.assertEqual(os.stat(r.home / "run" / "switching.json").st_mode & 0o777, 0o644)

        # The plan leaves the world out of the book and says why.
        p = r.plan()
        self.assertNotIn("plaza", p["running"])
        self.assertEqual((p["switching"], p["stopped"]), (["plaza"], []))
        self.assertNotIn("plaza", json.loads(p["compose"])["services"])
        self.assertNotIn("plaza", p["engine_worlds"])

        self.assertTrue(r.run())
        # It was given its save pause before Docker stopped it, and Docker had taken it down before anything moved.
        self.assertEqual(r.sleeps, [R.save_pause(R.SAVE_INTERVAL)])
        self.assertEqual(r.ups, [["plaza"], [], ["plaza"]])  # up; down for the switch; back
        self.assertEqual(r.gone_calls, [("plaza", True, False)])  # data/plaza was still there; v1 not yet
        # The folders swapped by inode.
        self.assertEqual((ino(r.data), ino(r.v1)), (before_to, before_data))
        self.assertEqual((r.data / "marker").read_bytes(), b"B")
        self.assertEqual((r.v1 / "marker").read_bytes(), b"A")
        self.assertFalse(r.v2.exists())
        # The metadata, measured again for both.
        meta = r.m.meta()
        self.assertEqual(meta["published"], "v2")
        self.assertEqual(meta["versions"]["v2"]["bytes"], V.measure(r.data))
        self.assertEqual(meta["versions"]["v1"]["bytes"], V.measure(r.v1))
        self.assertNotEqual(meta["versions"]["v2"]["bytes"], 1234)
        self.assertEqual(meta["next"], 3)
        # Answered done, journal empty, world back in its order's state.
        done = r.m.result(result["request"])
        self.assertEqual((done["state"], done["rule"], done["version"]), ("done", None, "v2"))
        self.assertEqual(r.m.log()[-1]["state"], "done")
        self.assertFalse((r.home / "run" / "switching.json").exists())
        self.assertEqual(r.containers, {"plaza"})
        again = r.plan()
        self.assertEqual((again["running"], again["switching"]), (["plaza"], []))
        # Nothing more happens on the next run.
        r.sleeps.clear()
        self.assertTrue(r.run())
        self.assertEqual((r.sleeps, ino(r.data), len(r.gone_calls)), ([], before_to, 1))

    def test_a_numinia_engine_world_waits_only_the_engines_margin(self):
        r = Rig(self, engine=True)
        r.run()
        self.assertEqual(r.plan()["engine_worlds"], ["plaza"])
        r.publish()
        self.assertEqual(r.plan()["engine_worlds"], [])
        r.run()
        self.assertEqual(r.sleeps, [R.SAVE_MARGIN])
        self.assertEqual((r.data / "marker").read_bytes(), b"B")
        self.assertEqual(r.published(), "v2")
        self.assertEqual(r.containers, {"plaza"})

    def test_a_stopped_world_is_published_and_stays_stopped(self):
        r = Rig(self, state="stopped")
        r.run()
        self.assertEqual(r.containers, set())
        before_to = ino(r.v2)
        result = r.publish()
        self.assertEqual(result["state"], "working")
        p = r.plan()
        self.assertEqual((p["stopped"], p["switching"], p["running"]), (["plaza"], ["plaza"], []))
        r.run()
        self.assertEqual((ino(r.data), r.published()), (before_to, "v2"))
        self.assertEqual(r.m.result(result["request"])["state"], "done")
        self.assertEqual((r.containers, r.sleeps, r.ups), (set(), [], [[], [], []]))
        self.assertEqual(r.plan()["switching"], [])

    def test_a_container_that_is_not_gone_moves_nothing(self):
        r = Rig(self)
        r.run()
        before_data, before_to = ino(r.data), ino(r.v2)
        result = r.publish()
        r.hold = True
        for _ in range(2):
            r.run()
            self.assertEqual((ino(r.data), ino(r.v2)), (before_data, before_to))
            self.assertEqual(r.m.result(result["request"])["state"], "working")
            self.assertIn("plaza", r.journal())
            self.assertEqual(r.published(), "v1")
        r.hold = False
        r.run()
        self.assertEqual((ino(r.data), r.published(), r.containers), (before_to, "v2", {"plaza"}))

    def test_docker_not_taking_the_book_moves_nothing_until_it_does(self):
        r = Rig(self)
        r.run()
        before_to = ino(r.v2)
        result = r.publish()
        r.refuse = True
        self.assertFalse(r.run())
        self.assertEqual((r.gone_calls, r.published(), r.m.result(result["request"])["state"]), ([], "v1", "working"))
        r.refuse = False
        self.assertTrue(r.run())
        self.assertEqual((ino(r.data), r.published()), (before_to, "v2"))

    def test_docker_refusing_the_second_book_is_a_failed_run_and_the_next_one_brings_the_world_back(self):
        r = Rig(self)
        r.run()
        r.publish()
        calls = []

        fake = r.sh

        def second_fails(*cmd):
            if cmd[4:5] == ("up",):
                calls.append(cmd)
                if len(calls) == 2:
                    raise subprocess.CalledProcessError(1, cmd)
            return fake(*cmd)

        with mock.patch.object(R, "sh", second_fails):
            self.assertFalse(r.run())
        self.assertEqual(r.published(), "v2")  # the switch itself ended
        self.assertEqual(r.containers, set())
        self.assertTrue(r.run())
        self.assertEqual(r.containers, {"plaza"})

    def test_the_asker_can_publish_the_other_version_back_and_the_original_folder_returns(self):
        """An implicit v1 world, duplicated to v2 by the real code, published to v2 and back to v1."""
        r = Rig(self, state="stopped", versions=False)
        self.assertFalse((r.home / "versions" / "plaza.json").exists())
        original = ino(r.data)
        nonce = r.m.drop("DuplicateVersion", request("DuplicateVersion", version="v1"))
        r.m.take()
        self.assertEqual(r.m.result(nonce)["state"], "done")
        copied = r.home / "versions" / "plaza" / "v2"
        self.assertTrue(copied.is_dir())

        r.run()
        self.assertEqual(r.publish("v2")["state"], "working")
        r.run()
        self.assertEqual((r.published(), ino(r.v1)), ("v2", original))
        self.assertNotEqual(ino(r.data), original)
        meta = r.m.meta()
        self.assertEqual(set(meta["versions"]), {"v1", "v2"})
        self.assertEqual((meta["next"], meta["versions"]["v2"]["origin"]), (3, "copy:v1"))
        self.assertEqual(meta["versions"]["v1"]["origin"], "created")

        self.assertEqual(r.publish("v1")["state"], "working")
        r.run()
        meta = r.m.meta()
        self.assertEqual(meta["published"], "v1")
        self.assertEqual(ino(r.data), original)  # the world's first folder is the published one again
        self.assertTrue(V.valid("plaza", meta))
        self.assertEqual((meta["versions"]["v1"]["bytes"], meta["versions"]["v2"]["bytes"]),
                         (V.measure(r.data), V.measure(r.home / "versions" / "plaza" / "v2")))
        self.assertEqual(meta["next"], 3)  # a name is never given twice
        self.assertEqual(r.journal(), {})
        self.assertEqual([x["state"] for x in r.m.log() if x["type"] == "PublishVersion"], ["done", "done"])


class Recovery(unittest.TestCase):
    """The machine died at each point; the next run finishes forward from the inodes."""

    def killed(self, steps):
        r = Rig(self)
        r.run()
        result = r.publish()
        # The container went (Docker took the book) and `steps` renames were done, as the dead run left them.
        r.containers = set()
        if steps >= 1:
            os.rename(r.data, r.v1)
        if steps >= 2:
            os.rename(r.v2, r.data)
        return r, result

    def test_after_the_first_rename_the_second_and_the_metadata_follow(self):
        r, result = self.killed(1)
        before_to = ino(r.v2)
        r.run()
        self.assertEqual(r.gone_calls[0][1:], (False, True))  # data/plaza was NOT recreated on the way
        self.assertEqual((ino(r.data), r.published(), r.containers), (before_to, "v2", {"plaza"}))
        self.assertEqual((r.data / "marker").read_bytes(), b"B")
        self.assertEqual((r.v1 / "marker").read_bytes(), b"A")
        self.assertEqual(r.m.result(result["request"])["state"], "done")

    def test_after_the_second_rename_the_metadata_is_what_is_left(self):
        r, result = self.killed(2)
        r.run()
        self.assertEqual((r.published(), r.containers, r.journal()), ("v2", {"plaza"}, {}))
        self.assertEqual((r.data / "marker").read_bytes(), b"B")
        self.assertEqual(r.m.result(result["request"])["state"], "done")

    def test_after_the_metadata_but_before_the_journal_went_it_ends_again_the_same(self):
        r, result = self.killed(2)
        meta = r.m.meta()
        meta["published"] = "v2"
        r.m.write_meta(meta)
        r.run()
        self.assertEqual((r.published(), r.journal()), ("v2", {}))
        self.assertEqual(r.m.result(result["request"])["state"], "done")

    def test_a_world_is_never_started_on_a_folder_in_motion(self):
        r, _ = self.killed(1)
        r.hold = True  # Docker still holds something of it
        r.run()
        self.assertFalse(r.data.exists())  # apply did not make data/plaza for a world it left out
        self.assertEqual(r.containers, set())
        r.hold = False
        r.run()
        self.assertTrue(r.data.is_dir())

    def test_a_picture_no_step_fits_fails_once_keeps_the_entry_and_keeps_the_world_down(self):
        r = Rig(self)
        r.run()
        result = r.publish()
        before_data = ino(r.data)
        # Someone moved the world's folder away: nothing is at data/plaza, nothing at v1.
        elsewhere = r.home / "elsewhere"
        os.rename(r.data, elsewhere)
        r.run()
        self.assertEqual(r.containers, set())
        done = r.m.result(result["request"])
        self.assertEqual((done["state"], done["rule"], done["version"]), ("failed", "switch-stuck", "v2"))
        self.assertIn("a person looks", r.err)
        self.assertTrue(r.journal()["plaza"]["stuck"])
        self.assertEqual(r.plan()["switching"], ["plaza"])
        self.assertNotIn("plaza", r.plan()["running"])
        self.assertFalse(r.data.exists())
        self.assertTrue(r.v2.is_dir())
        self.assertEqual(r.published(), "v1")
        # Answered once: later runs say nothing more and change nothing.
        lines = len(r.m.log())
        r.err = ""
        r.run()
        r.run()
        self.assertEqual((len(r.m.log()), r.err.count("a person looks")), (lines, 0))
        # A person who puts the folder back is followed.
        os.rename(elsewhere, r.data)
        self.assertEqual(ino(r.data), before_data)
        r.run()
        self.assertEqual((r.published(), r.journal(), r.containers), ("v2", {}, {"plaza"}))
        self.assertEqual(r.m.result(result["request"])["state"], "done")

    def test_a_target_folder_replaced_by_another_is_stuck_not_swapped(self):
        r = Rig(self)
        r.run()
        r.publish()
        before_data = ino(r.data)
        os.rename(r.v2, r.home / "old-v2")  # kept, so the new folder cannot reuse its inode
        r.v2.mkdir()
        (r.v2 / "marker").write_bytes(b"C")
        r.run()
        self.assertEqual(ino(r.data), before_data)  # nothing moved
        self.assertTrue(r.journal()["plaza"]["stuck"])
        self.assertEqual(r.containers, set())

    def test_a_link_where_a_folder_should_be_is_stuck(self):
        r = Rig(self)
        r.run()
        r.publish()
        outside = r.home / "outside"
        outside.mkdir()
        os.rename(r.v2, r.home / "old-v2")
        r.v2.symlink_to(outside)
        r.run()
        self.assertTrue(r.journal()["plaza"]["stuck"])
        self.assertTrue(r.v2.is_symlink())
        self.assertTrue(outside.is_dir() and r.data.is_dir())

    def test_an_error_mid_way_leaves_the_journal_for_the_next_run(self):
        r = Rig(self)
        r.run()
        result = r.publish()
        before_to = ino(r.v2)
        real = os.rename

        def second_fails(a, b):
            if Path(a) == r.v2 and Path(b) == r.data:  # the second step
                raise OSError(5, "I/O error")
            return real(a, b)

        with mock.patch.object(V.os, "rename", second_fails):
            r.run()
        self.assertIn("next run goes on", r.err)
        self.assertEqual(r.m.result(result["request"])["state"], "working")
        self.assertIn("plaza", r.journal())
        self.assertFalse(r.data.exists())  # halfway: the picture says step 2
        r.run()
        self.assertEqual((ino(r.data), r.published(), r.journal()), (before_to, "v2", {}))

    def test_a_metadata_that_cannot_be_written_is_tried_again(self):
        r = Rig(self)
        r.run()
        r.publish()
        with mock.patch.object(V, "write_versions", side_effect=OSError(28, "full")):
            r.run()
        self.assertIn("plaza", r.journal())
        self.assertEqual(r.published(), "v1")
        self.assertIn("versions file could not be written", r.err)
        r.run()
        self.assertEqual((r.published(), r.journal(), r.containers), ("v2", {}, {"plaza"}))

    def test_a_metadata_that_is_gone_bad_is_stuck(self):
        r = Rig(self)
        r.run()
        r.publish()
        (r.home / "versions" / "plaza.json").write_text("not json", encoding="utf-8")
        r.run()
        self.assertTrue(r.journal()["plaza"]["stuck"])
        self.assertEqual(r.containers, set())

    def test_docker_that_cannot_be_asked_counts_as_not_gone(self):
        with mock.patch.object(subprocess, "run", side_effect=OSError("no docker")):
            self.assertFalse(V.docker_gone("plaza"))
        with mock.patch.object(subprocess, "run", side_effect=subprocess.CalledProcessError(1, "docker")):
            self.assertFalse(V.docker_gone("plaza"))
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("docker", 60)):
            self.assertFalse(V.docker_gone("plaza"))

    def test_the_default_check_asks_docker_about_the_worlds_compose_service(self):
        for out, gone in (("", True), ("\n", True), ("1234abcd\n", False)):
            with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, out, "")) as run:
                self.assertEqual(V.docker_gone("plaza"), gone)
            cmd = run.call_args.args[0]
            self.assertEqual(cmd[:2], ["docker", "ps"])
            self.assertIn("label=com.docker.compose.service=plaza", cmd)
            self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_finish_switches_takes_an_injected_check_and_asks_nothing_when_there_is_no_journal(self):
        r = Rig(self)
        asked = []
        self.assertEqual(V.finish_switches(r.home, r.m.orders, container_gone=asked.append), [])
        self.assertEqual(asked, [])
        r.publish()
        self.assertEqual(V.finish_switches(r.home, r.m.orders, container_gone=lambda wid: False), [])
        self.assertEqual(V.finish_switches(r.home, r.m.orders, container_gone=lambda wid: True), ["plaza"])


class Plan(unittest.TestCase):
    def mid_switch(self, engine):
        r = Rig(self, engine=engine)
        r.run()
        r.publish()
        shutil.rmtree(r.data)  # mid-switch: nothing at data/plaza
        with contextlib.redirect_stderr(io.StringIO()):
            R.apply(r.plan(), r.home, None, codes_dir=r.m.codes)
        self.assertFalse(r.data.exists())  # a running world's folder is made; this one is not running

    def test_apply_makes_no_folder_for_a_hyperfy_world_that_is_switching(self):
        self.mid_switch(False)

    def test_apply_makes_no_folder_for_a_numinia_engine_world_that_is_switching(self):
        self.mid_switch(True)

    def test_save_waits_keep_a_switching_world(self):
        before = {"services": {"plaza": {"image": "a", "environment": {"SAVE_INTERVAL": "10"}}}}
        self.assertEqual(R.save_waits(before, {"services": {}}, set()), {})
        self.assertEqual(R.save_waits(before, {"services": {}}, {"plaza"}), {"plaza": 12})

    def test_the_dry_run_names_the_worlds_being_switched(self):
        r = Rig(self)
        r.publish()
        out = io.StringIO()
        argv = ["--orders", str(r.m.orders), "--home", str(r.home), "--alias", "open-1", "--dry-run",
                "--admin-code-file", str(r.home / "none"), "--control-host-file", str(r.home / "none")]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            R.main(argv)
        shown = json.loads(out.getvalue().split("\n}\n")[0] + "\n}")
        self.assertEqual((shown["switching"], shown["running"]), (["plaza"], []))

    def test_a_job_that_is_not_whole_is_ignored(self):
        r = Rig(self)
        good = {"nonce": "a" * 32, "by": "0x" + "12" * 20, "from": "v1", "to": "v2", "inoData": 5, "inoTo": 6}
        bad = {
            "ghost": "no", "a-b": {**good, "to": "v1"}, "c": {**good, "nonce": "zz"},
            "d": {**good, "by": "0xAB"}, "e": {**good, "from": "v0"}, "f": {**good, "inoData": True},
            "g": {**good, "inoTo": -1}, "h": {**good, "inoTo": "6"}, "i": {**good, "extra": 1},
            "j": {k: v for k, v in good.items() if k != "to"}, "k": {**good, "stuck": 1}, "l": {**good, "stuck": "yes"},
        }
        ok = {"fine": good, "stuck-one": {**good, "stuck": True}, "nobody": {**good, "by": None}}
        (r.home / "run" / "switching.json").write_text(json.dumps({**bad, "Bad Id": good, **ok}), encoding="utf-8")
        self.assertEqual(set(r.journal()), {"fine", "stuck-one", "nobody"})
        self.assertNotIn("Bad Id", r.journal())
        for name in ("ghost", "a-b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l"):
            self.assertNotIn(name, r.journal())
        (r.home / "run" / "switching.json").write_text("[1]", encoding="utf-8")
        self.assertEqual(r.journal(), {})
        (r.home / "run" / "switching.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(r.journal(), {})


class Refusals(unittest.TestCase):
    def test_the_rules_in_order(self):
        r = Rig(self)
        self.assertEqual(r.publish("v1")["rule"], "already-published")
        self.assertEqual(r.publish("v7")["rule"], "no-version")
        self.assertEqual(r.publish("v2", world="ghost")["rule"], "missing")
        self.assertEqual(r.journal(), {})
        (r.home / "versions" / "plaza.json").write_text("not json", encoding="utf-8")
        self.assertEqual(r.publish()["rule"], "versions-unreadable")
        self.assertEqual(r.journal(), {})
        self.assertEqual([x["rule"] for x in r.m.log() if x["state"] == "failed"],
                         ["already-published", "no-version", "missing", "versions-unreadable"])

    def test_a_world_of_one_version_has_nothing_to_publish(self):
        r = Rig(self, versions=False)
        self.assertEqual(r.publish("v1")["rule"], "already-published")
        self.assertEqual(r.publish("v2")["rule"], "no-version")

    def test_a_folder_that_is_not_a_folder_is_refused(self):
        r = Rig(self)
        shutil.rmtree(r.v2)
        r.v2.write_bytes(b"a file")
        self.assertEqual(r.publish()["rule"], "not-a-folder")
        r.v2.unlink()
        self.assertEqual(r.publish()["rule"], "not-a-folder")  # nothing there
        r.v2.mkdir()
        shutil.rmtree(r.data)
        self.assertEqual(r.publish()["rule"], "not-a-folder")  # the world's own is gone
        outside = r.home / "outside"
        outside.mkdir()
        r.data.symlink_to(outside)
        self.assertEqual(r.publish()["rule"], "not-a-folder")  # a link
        r.data.unlink()
        r.data.mkdir()
        shutil.rmtree(r.home / "versions" / "plaza")
        (r.home / "versions" / "plaza").symlink_to(outside)
        self.assertEqual(r.publish()["rule"], "not-a-folder")
        self.assertEqual(r.journal(), {})

    def test_a_world_that_is_updating_or_waits_for_a_restart_is_busy(self):
        r = Rig(self)
        R.write_json(r.home / "run" / "updating.json", {"plaza": {"nonce": "b" * 32, "by": None}})
        self.assertEqual(r.publish()["rule"], "busy")
        (r.home / "run" / "updating.json").unlink()
        R.queue_restart(r.home, "plaza")
        self.assertEqual(r.publish()["rule"], "busy")
        (r.home / "run" / "restart.json").unlink()
        self.assertEqual(r.publish()["state"], "working")
        self.assertEqual(r.journal()["plaza"]["to"], "v2")

    def test_a_second_publish_while_one_is_pending_is_busy_and_the_first_stays(self):
        r = Rig(self)
        first = r.publish()
        second = r.publish()
        self.assertEqual((second["state"], second["rule"]), ("failed", "busy"))
        self.assertEqual(r.journal()["plaza"]["nonce"], first["request"])

    def test_every_other_change_to_a_switching_world_is_busy(self):
        r = Rig(self)
        r.run()
        r.publish()
        before = (r.m.orders / "plaza.json").read_text(encoding="utf-8")
        plain = {"machine": "open-1", "world": "plaza", "expires": TV.EXPIRES}
        image = TV.IMAGE
        asks = [
            ("StopWorld", plain, {}),
            ("StartWorld", plain, {}),
            ("DeleteWorld", {**plain, "domain": "plaza.numinia.com"}, {}),
            ("UpdateWorld", {**plain, "image": image}, {}),
            ("PinWorld", {**plain, "image": image, "reason": "r"}, {}),
            ("RotateWorldCode", plain, {"adminCode": "Abcdef123456"}),
            ("DownloadWorld", plain, {"copy": "d" * 32}),
            ("DeleteVersion", {**plain, "version": "v2"}, {}),
            ("DuplicateVersion", {**plain, "version": "v1"}, {}),
            ("PublishVersion", {**plain, "version": "v2"}, {}),
            ("DownloadVersion", {**plain, "version": "v2"}, {"copy": TV.COPY}),
            ("ImportVersion", {"machine": "open-1", "world": "plaza", "zipBytes": 4096, "zipSha256": TV.SHA,
                               "replace": "", "expires": TV.EXPIRES}, {}),
        ]
        for kind, msg, door in asks:
            with self.subTest(kind=kind):
                msg = {**msg, "nonce": os.urandom(16).hex()}
                r.m.drop(kind, msg, **door)
                r.m.take()
                result = r.m.result(msg["nonce"])
                self.assertEqual((result["state"], result["rule"]), ("failed", "busy"))
                self.assertEqual(r.m.log()[-1]["rule"], "busy")
        self.assertEqual((r.m.orders / "plaza.json").read_text(encoding="utf-8"), before)
        self.assertTrue(r.v2.is_dir() and r.data.is_dir())
        self.assertFalse((r.home / "run" / "purge.json").exists())
        self.assertFalse((r.home / "run" / "copy-queue.json").exists())
        self.assertEqual(list((r.home / "copies").iterdir()), [])
        self.assertEqual(list(r.journal()), ["plaza"])
        # The record of a shown code is no change, so it is not held back.
        shown = {"machine": "open-1", "world": "plaza", "nonce": os.urandom(16).hex(), "expires": TV.EXPIRES}
        r.m.drop("ShowWorldCode", shown)
        r.m.take()
        self.assertEqual(r.m.result(shown["nonce"])["state"], "done")

    def test_a_world_that_is_not_switching_is_not_held_back(self):
        r = Rig(self)
        r.publish()
        r.m.put_world("other")
        stop = {"machine": "open-1", "world": "other", "nonce": os.urandom(16).hex(), "expires": TV.EXPIRES}
        r.m.drop("StopWorld", stop)
        r.m.take()
        self.assertEqual(r.m.result(stop["nonce"])["state"], "done")

    def test_after_the_switch_the_world_takes_changes_again_and_a_purge_finds_every_folder(self):
        r = Rig(self)
        r.run()
        r.publish()
        r.run()
        stop = {"machine": "open-1", "world": "plaza", "nonce": os.urandom(16).hex(), "expires": TV.EXPIRES}
        r.m.drop("StopWorld", stop)
        r.m.take()
        self.assertEqual(r.m.result(stop["nonce"])["state"], "done")
        R.purge_world(r.home, "plaza", codes_dir=r.m.codes)
        self.assertEqual([p.name for p in (r.home / "data").iterdir()], [])
        self.assertFalse((r.home / "versions" / "plaza").exists())
        self.assertFalse((r.home / "versions" / "plaza.json").exists())


if __name__ == "__main__":
    unittest.main()
