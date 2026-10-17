# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""The machine's updater follows the latest engine by itself, one world at a time, and goes back when a world is not healthy.

Everything outside is fake: the registry (a release file), docker (a dict of
what runs), the reconciler (copies each order's image to that dict) and the clock.
"""

from pathlib import Path
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MACHINE = ROOT / "open-worlds" / "machine"
VECTORS = json.loads((Path(__file__).parent / "fleet_vectors.json").read_text(encoding="utf-8"))
SIGNED = next(v for v in VECTORS["vectors"] if v["type"] == "FleetRelease")


def load(name):
    if str(MACHINE) not in sys.path:
        sys.path.insert(0, str(MACHINE))
    spec = importlib.util.spec_from_file_location(name, MACHINE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


R = load("reconcile")
W = load("walletsig")
REL = load("release")

REPO = "ghcr.io/numengames/numinia-engine"
OLD = f"{REPO}@sha256:{'a' * 64}"
NEW = f"{REPO}@sha256:{'b' * 64}"
NEWER = f"{REPO}@sha256:{'c' * 64}"
HYPERFY = f"ghcr.io/numengames/numinia-hyperfy2@sha256:{'d' * 64}"
COMMITS = {OLD: "aaaaaaa1", NEW: "bbbbbbb2", NEWER: "ccccccc3"}


def release(image=NEW, commit="bbbbbbb2"):
    return json.dumps({"image": image, "commit": commit, "build": "1", "digest": image.split("@")[1]}).encode()


class Fleet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "orders").mkdir()
        self.signers = self.home / "signers"
        self.signers.write_text(SIGNED["signer"] + "\n")
        self.file = release()
        self.running = {}  # what docker runs: world -> image
        self.calls = []
        self.bad_commit = set()  # images whose /status is wrong
        self.pull_fails = False
        self.backup_fails = False
        self.clock = 1_800_000_000.0
        env = REL.Env(self.home, self.signers)
        env.now = lambda: self.clock
        env.sleep = lambda s: setattr(self, "clock", self.clock + s)
        env.log = lambda line: self.calls.append(("log", line))
        env.fetch = self._fetch
        env.pull = self._pull
        env.backup = self._backup
        env.apply = self._apply
        env.health = self._health
        env.canary = lambda: None
        self.env = env

    # --- fakes
    def _fetch(self):
        if self.file is None:
            raise OSError("network down")
        return self.file

    def _pull(self, image):
        self.calls.append(("pull", image))
        if self.pull_fails:
            raise subprocess.CalledProcessError(1, "docker")

    def _backup(self, order):
        if self.backup_fails:
            raise OSError("no copy")
        self.calls.append(("backup", order["id"]))
        return Path("x")

    def _apply(self):
        self.calls.append(("apply",))
        for o in R.read_orders(self.home / "orders"):
            if o["state"] == "running":
                self.running[o["id"]] = o["image"]
        return True

    def _health(self, order, target):
        image = self.running.get(order["id"])
        if image is None:
            return False, "no container"
        if image in self.bad_commit:
            return False, "/status runs 0000000"
        return REL.commit_matches({"commit": COMMITS[image]}, COMMITS[image] if image != target["image"] else target["commit"])

    def order(self, wid, image=OLD, state="running", **extra):
        order = {"id": wid, "state": state, "domain": f"{wid}.numinia.com", "image": image,
                 "limits": {"memory": "512m", "cpus": "1", "maxUploadMb": 10}, **extra}
        R.write_json(self.home / "orders" / f"{wid}.json", order)
        if state == "running":
            self.running[wid] = image

    def image(self, wid):
        return R.read_order(self.home / "orders" / f"{wid}.json")["image"]

    def pass_(self):
        return REL.run_pass(self.env)

    def state(self):
        return REL.read_state(self.env)

    def passes(self, n):
        return [self.pass_() for _ in range(n)]


class TestTarget(unittest.TestCase):
    def test_a_release_by_digest_is_accepted(self):
        self.assertEqual(REL.parse_release(release()), {"image": NEW, "commit": "bbbbbbb2"})

    def test_a_tag_with_a_separate_digest_becomes_the_digest(self):
        raw = json.dumps({"image": f"{REPO}:sha-bbbbbbb", "commit": "bbbbbbb", "digest": "sha256:" + "b" * 64})
        self.assertEqual(REL.parse_release(raw)["image"], NEW)

    def test_nothing_else_is_believed(self):
        for raw in (
            b"not json", b"[]",
            json.dumps({"image": f"{REPO}:latest", "commit": "bbbbbbb"}),  # moving tag, no digest
            json.dumps({"image": f"{REPO}:sha-bbbbbbb", "commit": "bbbbbbb"}),  # tag, no digest
            json.dumps({"image": "ghcr.io/evil/numinia-engine@sha256:" + "b" * 64, "commit": "bbbbbbb"}),
            json.dumps({"image": "docker.io/numengames/numinia-engine@sha256:" + "b" * 64, "commit": "bbbbbbb"}),
            json.dumps({"image": HYPERFY, "commit": "bbbbbbb"}),  # another engine
            json.dumps({"image": NEW, "commit": "zz"}),
            json.dumps({"image": NEW, "commit": "bbbbbbb", "digest": "sha256:" + "c" * 64}),  # two digests
        ):
            self.assertIsNone(REL.parse_release(raw), raw)

    def test_the_release_address_is_fixed_to_the_engine_repo_over_https(self):
        self.assertEqual(REL.RELEASE_URL, "https://github.com/numengames/numinia-engine/releases/latest/download/imagen.json")
        text = (MACHINE / "release.py").read_text()
        for word in ("Authorization", "GITHUB_TOKEN", "gh auth"):
            self.assertNotIn(word, text)


class TestPlan(unittest.TestCase):
    T = {"image": NEW, "commit": "bbbbbbb2"}

    def o(self, wid, image=OLD, state="running", **kw):
        return {"id": wid, "state": state, "image": image, **kw}

    def test_table(self):
        orders = [self.o("a-hyper", HYPERFY), self.o("b-pinned", pin="why"), self.o("c-current", NEW),
                  self.o("d-busy"), self.o("e-stopped", state="stopped"), self.o("f-run"), self.o("g-run")]
        plan = REL.plan_rollout(orders, self.T, {}, busy={"d-busy"})
        self.assertEqual(plan["skipped"], {"a-hyper": "other-engine", "b-pinned": "pinned", "d-busy": "busy"})
        self.assertEqual(plan["current"], ["c-current"])
        self.assertEqual(plan["stopped"], ["e-stopped"])
        self.assertEqual(plan["next"], "f-run")

    def test_the_canary_goes_first_then_by_id(self):
        orders = [self.o("a"), self.o("b"), self.o("c")]
        self.assertEqual(REL.plan_rollout(orders, self.T, {}, canary="c")["next"], "c")
        self.assertEqual(REL.plan_rollout(orders, self.T, {})["next"], "a")

    def test_halted_or_bad_digest_changes_nothing(self):
        orders = [self.o("a")]
        self.assertIsNone(REL.plan_rollout(orders, self.T, {"halted": {"image": NEW}})["next"])
        self.assertIsNone(REL.plan_rollout(orders, self.T, {"bad": [NEW]})["next"])
        self.assertEqual(REL.plan_rollout(orders, self.T, {"halted": {"image": NEWER}})["next"], "a")

    def test_no_target_no_plan(self):
        self.assertIsNone(REL.plan_rollout([self.o("a")], None, {})["next"])


class TestRollout(Fleet):
    def test_one_running_world_per_pass_canary_first(self):
        for w in ("a", "b", "c"):
            self.order(w)
        self.env.canary = lambda: "b"
        first = self.pass_()
        self.assertEqual((first["world"], first["action"]), ("b", "updated"))
        self.assertEqual([self.image(w) for w in "abc"], [OLD, NEW, OLD])
        self.assertEqual(self.pass_()["world"], "a")
        self.assertEqual(self.pass_()["world"], "c")
        self.assertEqual(self.pass_()["action"], "none")
        self.assertEqual(self.state()["applied"]["state"], "converged")

    def test_stopped_worlds_change_together_without_a_restart_check(self):
        self.order("s1", state="stopped")
        self.order("s2", state="stopped")
        self.order("r")
        result = self.pass_()
        self.assertEqual(result["stopped"], ["s1", "s2"])
        self.assertEqual(self.image("s1"), NEW)
        self.assertEqual(self.image("s2"), NEW)
        self.assertNotIn("s1", self.running)
        self.assertEqual(result["world"], "r")

    def test_a_copy_precedes_the_change_and_the_image_is_pulled_once(self):
        self.order("a")
        self.order("b")
        self.pass_()
        names = [c[0] for c in self.calls if c[0] != "log"]
        self.assertEqual(names, ["pull", "backup", "apply"])

    def test_pinned_hyperfy_busy_and_current_worlds_are_left_alone(self):
        self.order("hy", HYPERFY)
        self.order("pin", pin="needs old")
        self.order("cur", NEW)
        self.order("bz")
        self.env.busy = lambda: {"bz"}
        result = self.pass_()
        self.assertEqual(result["action"], "none")
        self.assertEqual([self.image(w) for w in ("hy", "pin", "cur", "bz")], [HYPERFY, OLD, NEW, OLD])
        self.assertNotIn("applied", self.state())  # busy: not converged yet
        self.env.busy = lambda: set()
        self.assertEqual(self.pass_()["world"], "bz")

    def test_idempotent_when_already_current(self):
        self.order("a", NEW)
        self.order("b", NEW, state="stopped")
        before = len(self.calls)
        for _ in range(3):
            self.assertEqual(self.pass_()["action"], "none")
        self.assertEqual(len(self.calls), before)  # no pull, no copy, no apply
        self.assertEqual(self.state()["applied"]["image"], NEW)

    def test_no_copy_no_change(self):
        self.order("a")
        self.backup_fails = True
        self.assertEqual(self.pass_()["action"], "no-backup")
        self.assertEqual(self.image("a"), OLD)

    def test_failed_pull_leaves_every_order_intact(self):
        self.order("a")
        self.order("s", state="stopped")
        self.pull_fails = True
        self.pass_()
        self.assertEqual((self.image("a"), self.image("s")), (OLD, OLD))

    def test_network_down_is_a_no_op(self):
        self.order("a")
        self.file = None
        self.assertEqual(self.pass_()["action"], "none")
        self.assertEqual(self.image("a"), OLD)

    def test_the_update_records_the_previous_image(self):
        self.order("a")
        self.pass_()
        self.assertEqual(R.read_order(self.home / "orders" / "a.json")["previousImage"], OLD)

    def test_a_pass_changes_only_the_image_never_the_caddyfile(self):
        self.order("a")
        before = R.render_caddyfile([R.clean(R.read_order(self.home / "orders" / "a.json"))])
        self.pass_()
        after = R.render_caddyfile([R.clean(R.read_order(self.home / "orders" / "a.json"))])
        self.assertEqual(before, after)


class TestRollback(Fleet):
    def test_health_failure_goes_back_and_halts_the_machine(self):
        for w in ("a", "b"):
            self.order(w)
        self.bad_commit = {NEW}
        result = self.pass_()
        self.assertEqual((result["world"], result["action"]), ("a", "rolled-back"))
        self.assertEqual(self.image("a"), OLD)
        self.assertEqual(self.running["a"], OLD)
        order = R.read_order(self.home / "orders" / "a.json")
        self.assertIn("/status", order["updateError"])
        self.assertNotIn("previousImage", order)
        state = self.state()
        self.assertEqual(state["bad"], [NEW])
        self.assertEqual(state["halted"]["world"], "a")
        # The machine stops: b is never touched, however many passes go by.
        self.passes(3)
        self.assertEqual(self.image("b"), OLD)
        self.assertEqual(len([c for c in self.calls if c[0] == "backup"]), 1)

    def test_docker_refusing_the_book_also_rolls_back(self):
        self.order("a")
        real = self.env.apply
        answers = iter([False])
        self.env.apply = lambda: next(answers, None) is not False and real()
        self.assertEqual(self.pass_()["action"], "rolled-back")
        self.assertEqual(self.image("a"), OLD)

    def test_a_new_digest_lifts_the_halt_and_the_bad_one_is_not_retried(self):
        self.order("a")
        self.bad_commit = {NEW}
        self.pass_()
        self.pass_()
        self.assertEqual(len([c for c in self.calls if c[0] == "pull"]), 1)
        self.file = release(NEWER, "ccccccc3")
        self.assertEqual(self.pass_()["action"], "updated")
        self.assertEqual(self.image("a"), NEWER)
        self.assertIsNone(self.state()["halted"])

    def test_an_interrupted_update_is_judged_on_the_next_pass(self):
        self.order("a", NEW, previousImage=OLD)
        state = self.state()
        state["rolling"] = {"world": "a", "previous": OLD, "target": NEW, "commit": "bbbbbbb2"}
        REL.write_state(self.env, state)
        self.bad_commit = {NEW}
        self.assertEqual(self.pass_()["action"], "halted")
        self.assertEqual(self.image("a"), OLD)

    def test_an_interrupted_update_that_is_healthy_just_finishes(self):
        self.order("a", NEW, previousImage=OLD)
        state = self.state()
        state["rolling"] = {"world": "a", "previous": OLD, "target": NEW, "commit": "bbbbbbb2"}
        REL.write_state(self.env, state)
        self.assertEqual(self.pass_()["action"], "none")
        self.assertIsNone(self.state()["rolling"])


class TestSignedRecord(Fleet):
    def test_a_signed_record_is_read_with_the_machine_signer(self):
        message = SIGNED["message"]
        (self.home / "release").mkdir()
        R.write_json(self.home / "release" / REL.OVERRIDE_FILE, {"message": message, "signature": SIGNED["signature"]})
        got = REL.read_override(self.home / "release" / REL.OVERRIDE_FILE, R.read_signers(self.signers))
        self.assertEqual(got["seq"], message["seq"])
        self.assertIsNone(REL.read_override(self.home / "release" / REL.OVERRIDE_FILE, frozenset({"0x" + "1" * 40})))

    def test_absence_means_follow_latest_and_a_pause_stops_everything(self):
        self.order("a")
        self.assertEqual(REL.choose_target({"image": NEW, "commit": "b"}, None, 0)[1], "latest")
        paused = {"seq": 5, "image": "", "commit": ""}
        self.assertEqual(REL.choose_target({"image": NEW, "commit": "b"}, paused, 0), (None, "paused by a signed record"))
        pinned = {"seq": 5, "image": OLD, "commit": "aaaaaaa1"}
        self.assertEqual(REL.choose_target({"image": NEW, "commit": "b"}, pinned, 0)[0]["image"], OLD)
        # a record below the highest seq seen is stale
        self.assertEqual(REL.choose_target({"image": NEW, "commit": "b"}, pinned, 6)[1], "latest")

    def test_a_forged_record_changes_nothing(self):
        self.order("a")
        message = dict(SIGNED["message"], image=OLD, commit="aaaaaaa1")
        (self.home / "release").mkdir()
        R.write_json(self.home / "release" / REL.OVERRIDE_FILE, {"message": message, "signature": SIGNED["signature"]})
        self.assertEqual(self.pass_()["action"], "updated")  # followed latest anyway
        self.assertEqual(self.image("a"), NEW)


class TestBootstrapWiring(unittest.TestCase):
    text = (MACHINE / "bootstrap.sh").read_text()

    def test_installed_not_run_from_the_clone(self):
        self.assertIn("for program in reconcile.py walletsig.py fleetd.py release.py; do", self.text)
        self.assertIn("wrapper fleet-release release.py", self.text)
        self.assertIn("ExecStart=/usr/local/sbin/fleet-release", self.text)
        self.assertNotIn("python3 $BOOK_DIR", self.text)

    def test_the_timer_runs_every_five_minutes_and_is_checked_at_the_end(self):
        self.assertIn("OnUnitActiveSec=5min", self.text)
        self.assertIn("enable --now fleet-reconcile.timer fleet-reconcile.path fleet-release.timer", self.text)

    def test_the_updater_is_paused_while_programs_change(self):
        self.assertIn("systemctl stop fleet-reconcile.timer fleet-reconcile.path fleet-release.timer", self.text)


if __name__ == "__main__":
    unittest.main()
