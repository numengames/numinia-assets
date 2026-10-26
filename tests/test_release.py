# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""The machine's updater follows the latest engine by itself, one world at a time, and goes back when a world is not healthy.

Everything outside is fake: the registry (a release file), docker (a dict of
what runs), the reconciler (copies each order's image to that dict) and the clock.
"""

from pathlib import Path
import base64
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


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
        env.signed_url = lambda: None  # no signed-record URL unless a test sets one
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
        return REL.commit_matches({"commitHash": COMMITS[image]}, COMMITS[image] if image != target["image"] else target["commit"])

    def test_status_commit_field_is_the_engines_commitHash_with_commit_as_fallback(self):
        ok = lambda body: REL.commit_matches(body, "bbbbbbb2")[0]
        self.assertTrue(ok({"commitHash": "bbbbbbb2"}))
        self.assertTrue(ok({"commitHash": "BBBBBBB2ffff"}))  # prefix both ways, any case
        self.assertTrue(ok({"commit": "bbbbbbb"}))  # fallback for other builds
        self.assertFalse(ok({"commitHash": "aaaaaaa1", "commit": "bbbbbbb2"}))  # commitHash wins
        self.assertTrue(ok({"commitHash": "", "commit": "bbbbbbb2"}))
        for body in ({}, {"commitHash": None}, {"commitHash": 5}, [], None, {"commitHash": "cccccc"}):
            self.assertFalse(ok(body), body)

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

    def test_a_world_whose_published_version_is_being_switched_is_busy(self):
        self.order("bz")
        self.order("ok")
        job = {"nonce": "a" * 32, "by": None, "from": "v1", "to": "v2", "inoData": 5, "inoTo": 6}
        R.write_json(self.home / "run" / "switching.json", {"bz": job, "bad-job": {"nonce": "x"}})
        self.assertEqual(self.env.busy(), {"bz"})  # the real check, read from the journal; junk in it is ignored
        self.assertEqual(self.pass_()["world"], "ok")
        self.assertEqual([self.image("bz"), self.image("ok")], [OLD, NEW])
        self.assertNotIn("applied", self.state())
        (self.home / "run" / "switching.json").unlink()
        self.assertEqual(self.env.busy(), set())
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
        self.assertIn("for program in reconcile.py walletsig.py fleetd.py release.py nightly.py recipe.py condiciones.py signers.py versions.py cleanup.py; do", self.text)
        self.assertIn("wrapper fleet-release release.py", self.text)
        self.assertIn("ExecStart=/usr/local/sbin/fleet-release", self.text)
        self.assertNotIn("python3 $BOOK_DIR", self.text)

    def test_the_timer_runs_every_five_minutes_and_is_checked_at_the_end(self):
        self.assertIn("OnUnitActiveSec=5min", self.text)
        self.assertIn("enable --now fleet-reconcile.timer fleet-reconcile.path fleet-release.timer", self.text)

    def test_the_updater_is_paused_while_programs_change(self):
        self.assertIn("systemctl stop fleet-reconcile.timer fleet-reconcile.path fleet-release.timer", self.text)


def statement(image=NEW, commit="bbbbbbb2", path=".github/workflows/publicar-motor.yml", ref="refs/heads/main",
              repo="https://github.com/numengames/numinia-engine", predicate="https://slsa.dev/provenance/v1"):
    """The in-toto statement GitHub's attest-build-provenance stores (shape read from the live engine release)."""
    return {"_type": "https://in-toto.io/Statement/v1", "predicateType": predicate,
            "subject": [{"name": "ghcr.io/numengames/numinia-engine", "digest": {"sha256": image.split(":")[-1]}}],
            "predicate": {"buildDefinition": {"externalParameters": {"workflow": {"ref": ref, "repository": repo, "path": path}},
                                              "resolvedDependencies": [{"uri": "git+" + repo + "@" + ref, "digest": {"gitCommit": commit}}]}}}


def listing(*statements):
    return json.dumps({"attestations": [{"bundle": {"dsseEnvelope": {"payload": base64.b64encode(json.dumps(s).encode()).decode()}}}
                                         for s in statements]}).encode()


TARGET = {"image": NEW, "commit": "bbbbbbb2"}


class TestProvenance(unittest.TestCase):
    def api(self, raw, target=TARGET):
        return REL.verify_api(target, fetch=lambda digest: raw if not isinstance(raw, Exception) else (_ for _ in ()).throw(raw))

    def test_mode_is_off_without_a_setting(self):
        self.assertEqual(REL.provenance_mode({}, Path("/nonexistent/provenance")), "off")

    def test_mode_from_env_then_file_and_garbage_is_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "provenance"
            f.write_text("api\n")
            self.assertEqual(REL.provenance_mode({}, f), "api")
            self.assertEqual(REL.provenance_mode({"FLEET_RELEASE_PROVENANCE": "gh"}, f), "gh")
            self.assertEqual(REL.provenance_mode({"FLEET_RELEASE_PROVENANCE": "yes please"}, f), "gh")
            self.assertEqual(REL.provenance_mode({"FLEET_RELEASE_PROVENANCE": ""}, f), "off")

    def test_a_matching_attestation_passes(self):
        self.assertEqual(self.api(listing(statement())), (True, "ok"))

    def test_any_one_matching_attestation_among_others_is_enough(self):
        self.assertTrue(self.api(listing(statement(commit="1234567"), statement()))[0])

    def test_each_wrong_claim_is_refused(self):
        for bad in (statement(image=NEWER), statement(commit="1234567"), statement(path=".github/workflows/other.yml"),
                    statement(ref="refs/heads/evil"), statement(repo="https://github.com/someone/fork"),
                    statement(predicate="https://example.com/not-provenance")):
            self.assertFalse(self.api(listing(bad))[0], bad)

    def test_nothing_or_garbage_is_refused(self):
        for raw in (listing(), b"not json", b"[]", b'{"attestations": [{"bundle": 3}]}', OSError("down")):
            self.assertFalse(self.api(raw)[0], raw)

    def test_gh_mode_needs_gh_and_its_blessing(self):
        self.assertFalse(REL.verify_gh(TARGET, which=lambda n: None)[0])
        calls = []
        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)
        self.assertTrue(REL.verify_gh(TARGET, run=run, which=lambda n: "/usr/bin/gh")[0])
        self.assertIn("--signer-workflow", calls[0])
        self.assertIn("oci://" + NEW, calls[0])
        self.assertFalse(REL.verify_gh(TARGET, run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1), which=lambda n: "gh")[0])

    def test_off_never_asks_anyone(self):
        boom = lambda *a: self.fail("asked")
        self.assertTrue(REL.verify_provenance(TARGET, "off", api=boom, gh=boom)[0])


class TestProvenanceInThePass(Fleet):
    def setUp(self):
        super().setUp()
        self.order("w1", OLD)
        self.asked = []
        self.attested = True
        self.mode = "off"
        self.env.mode = lambda: self.mode
        def provenance(target, mode):
            self.asked.append((target["image"], mode))
            return (True, "ok") if self.attested else (False, "no attestation")
        self.env.provenance = provenance

    def test_off_is_todays_behaviour_and_asks_nobody(self):
        self.attested = False
        self.pass_()
        self.assertEqual(self.image("w1"), NEW)
        self.assertEqual(self.asked, [])

    def test_on_and_attested_updates_and_asks_once_per_image(self):
        self.mode = "api"
        self.pass_()
        self.assertEqual(self.image("w1"), NEW)
        self.pass_()
        self.assertEqual(self.asked, [(NEW, "api")])

    def test_on_and_not_attested_changes_nothing_and_says_why(self):
        self.mode = "api"
        self.attested = False
        result = self.pass_()
        self.assertEqual(self.image("w1"), OLD)
        self.assertNotIn(("pull", NEW), self.calls)
        self.assertEqual(self.state()["provenanceRefused"]["why"], "no attestation")
        self.assertEqual(result["action"], "none")
        self.attested = True
        self.pass_()  # a refusal is asked again next time and the world then moves
        self.assertEqual(self.image("w1"), NEW)
        self.assertNotIn("provenanceRefused", self.state())

    def test_a_new_image_or_mode_is_asked_again(self):
        self.mode = "api"
        self.pass_()
        self.mode = "gh"
        self.pass_()
        self.assertEqual([m for _, m in self.asked], ["api", "gh"])


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# The signed FleetRelease fetched from a URL
# ---------------------------------------------------------------------------

def _test_key():
    from ecdsa import SECP256k1, SigningKey

    key = SigningKey.from_secret_exponent(0x5EED5EED5EED, curve=SECP256k1)
    point = key.get_verifying_key().to_string("raw")
    return key, "0x" + W.keccak256(point)[12:].hex()


KEY, KEY_ADDRESS = _test_key()


def signed(seq, image="", commit="", key=KEY, **extra):
    """A real EIP-712 signature by a throwaway key: {message, signature}."""
    from ecdsa.util import sigencode_string_canonize

    message = {"engine": "numinia-engine", "image": image, "commit": commit, "seq": seq, "issuedAt": 1_800_000_000}
    digest = W.typed_digest("FleetRelease", message)
    rs = key.sign_digest_deterministic(digest, sigencode=sigencode_string_canonize, hashfunc=__import__("hashlib").sha256)
    address = "0x" + W.keccak256(key.get_verifying_key().to_string("raw"))[12:].hex()
    for v in (27, 28):
        candidate = "0x" + (rs + bytes([v])).hex()
        if W.recover(digest, candidate) == address:
            return {"message": message, "signature": candidate, **extra}
    raise AssertionError("no recovery id")


class Site:
    """A local http.server standing in for numinia.com/fleet/release.json."""

    def __init__(self):
        self.status, self.body, self.etag, self.delay, self.location = 200, b"", None, 0.0, None
        self.hits = []  # the If-None-Match header of every request (None when absent)
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                site.hits.append(self.headers.get("If-None-Match"))
                time.sleep(site.delay)
                try:
                    if site.location:
                        self.send_response(302)
                        self.send_header("Location", site.location)
                        self.end_headers()
                    elif site.etag and self.headers.get("If-None-Match") == site.etag:
                        self.send_response(304)
                        self.send_header("ETag", site.etag)
                        self.end_headers()
                    else:
                        self.send_response(site.status)
                        if site.etag:
                            self.send_header("ETag", site.etag)
                        self.send_header("Content-Length", str(len(site.body)))
                        self.end_headers()
                        self.wfile.write(site.body)
                except OSError:
                    pass  # the client gave up (timeout test)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/fleet/release.json"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def serve(self, record, seq=None):
        self.status = 200
        self.body = json.dumps(record).encode() if not isinstance(record, bytes) else record
        self.etag = f'"{seq}"' if seq is not None else None

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class TestFetchSignedRelease(Fleet):
    def setUp(self):
        super().setUp()
        self.site = Site()
        self.addCleanup(self.site.close)
        self.signers.write_text(KEY_ADDRESS + "\n")
        self.env.signed_url = lambda: self.site.url
        self.local = self.home / "release" / REL.OVERRIDE_FILE
        self.order("a", NEW)

    def stored(self):
        return json.loads(self.local.read_text()) if self.local.exists() else None

    def test_it_is_off_by_default(self):
        self.env.signed_url = lambda: None
        self.site.serve(signed(5), 5)
        self.pass_()
        self.assertEqual(self.site.hits, [])
        self.assertFalse(self.local.exists())

    def test_the_url_comes_from_the_environment_then_the_file_and_only_http(self):
        f = self.home / "release-url"
        self.assertIsNone(REL.release_url({}, f))
        f.write_text("https://numinia.com/fleet/release.json\n")
        self.assertEqual(REL.release_url({}, f), "https://numinia.com/fleet/release.json")
        self.assertEqual(REL.release_url({"FLEET_RELEASE_URL": "http://127.0.0.1:1/x"}, f), "http://127.0.0.1:1/x")
        self.assertIsNone(REL.release_url({"FLEET_RELEASE_URL": ""}, f))  # set but empty: off, the file does not win
        for bad in ("ftp://x/y", "file:///etc/passwd", "numinia.com/x", "https://a b"):
            f.write_text(bad)
            self.assertIsNone(REL.release_url({}, f), bad)

    def test_a_good_record_is_stored_where_the_local_file_is_read_and_a_pause_stops_the_fleet(self):
        self.site.serve(signed(5, extra="dropped"), 5)
        self.assertEqual(self.pass_()["action"], "none")
        got = self.stored()
        self.assertEqual(set(got), {"message", "signature"})  # only what was signed
        self.assertEqual(REL.read_override(self.local, R.read_signers(self.signers))["seq"], 5)
        self.assertEqual(self.state()["seq"], 5)
        self.assertIsNone(self.state()["target"])  # paused: no target
        self.assertEqual(self.image("a"), NEW)

    def test_pause_holds_back_a_newer_latest_until_a_higher_seq_resumes(self):
        self.site.serve(signed(5), 5)
        self.file = release(NEWER, "ccccccc3")
        self.assertEqual(self.pass_()["action"], "none")
        self.assertEqual(self.image("a"), NEW)
        self.site.serve(signed(6, NEWER, "ccccccc3"), 6)
        self.assertEqual(self.pass_()["action"], "updated")
        self.assertEqual(self.image("a"), NEWER)

    def test_go_back_is_a_new_higher_seq_naming_the_old_digest(self):
        self.site.serve(signed(2, OLD, "aaaaaaa1"), 2)
        self.assertEqual(self.pass_()["action"], "updated")
        self.assertEqual(self.image("a"), OLD)
        self.assertEqual(self.state()["target"]["why"], "pinned by a signed record")

    def test_a_seq_not_above_the_stored_one_never_replaces_it(self):
        self.site.serve(signed(5), 5)
        self.pass_()
        before = self.local.read_bytes()
        for seq in (4, 5, 1):
            self.site.serve(signed(seq, OLD, "aaaaaaa1"), seq)
            self.pass_()
            self.assertEqual(self.local.read_bytes(), before, seq)
        self.assertEqual(self.image("a"), NEW)  # still paused

    def test_a_seq_not_above_the_highest_seen_is_dropped_even_without_a_local_file(self):
        self.pass_()  # creates the state
        state = self.state()
        state["seq"] = 9
        REL.write_state(self.env, state)
        self.site.serve(signed(9, OLD, "aaaaaaa1"), 9)
        self.pass_()
        self.assertFalse(self.local.exists())
        self.site.serve(signed(10, OLD, "aaaaaaa1"), 10)
        self.pass_()
        self.assertEqual(self.stored()["message"]["seq"], 10)

    def test_it_sends_if_none_match_and_a_304_changes_nothing(self):
        self.site.serve(signed(5), 5)
        self.pass_()
        self.assertEqual(self.site.hits, [None])
        before = self.local.read_bytes()
        self.pass_()
        self.assertEqual(self.site.hits, [None, '"5"'])
        self.assertEqual(self.local.read_bytes(), before)
        self.assertEqual(self.state()["seq"], 5)

    def test_without_a_local_copy_it_never_asks_for_a_304(self):
        self.site.serve(signed(5), 5)
        self.pass_()
        self.local.unlink()
        self.pass_()
        self.assertEqual(self.site.hits, [None, None])
        self.assertFalse(self.local.exists())  # seq 5 is not above the highest seen (5): not written back

    def test_a_new_seq_gets_a_new_etag_and_is_fetched(self):
        self.site.serve(signed(5), 5)
        self.pass_()
        self.site.serve(signed(6), 6)
        self.pass_()
        self.assertEqual(self.site.hits, [None, '"5"'])  # the old etag no longer matches: a full answer
        self.assertEqual(self.stored()["message"]["seq"], 6)
        self.pass_()
        self.assertEqual(self.site.hits[-1], '"6"')

    def _nothing_changes(self, setup, seq=5):
        """With a good record already stored, whatever the site does leaves it, and the pass still follows it."""
        self.site.serve(signed(seq), seq)
        self.pass_()
        before = self.local.read_bytes()
        setup()
        self.file = release(NEWER, "ccccccc3")
        self.assertEqual(self.pass_()["action"], "none")  # still paused by the stored record
        self.assertEqual(self.local.read_bytes(), before)
        self.assertEqual(self.image("a"), NEW)

    def test_404_is_a_no_op(self):
        def go():
            self.site.status, self.site.body, self.site.etag = 404, b'{"error":"no-release"}', None
        self._nothing_changes(go)

    def test_a_server_error_is_a_no_op(self):
        def go():
            self.site.status, self.site.body, self.site.etag = 502, b"", None
        self._nothing_changes(go)

    def test_garbage_is_a_no_op(self):
        bodies = (b"<html>captive portal</html>", b"[]", b'{"message": 3}', b"\xff\xfe", b"null",
                     json.dumps({"message": signed(9)["message"], "signature": "0x" + "1" * 130}).encode())
        for n, body in enumerate(bodies):
            with self.subTest(body=body[:30]):
                self._nothing_changes(lambda: self.site.serve(body), seq=5 + n * 100)  # each round stores a higher good one

    def test_a_record_signed_by_a_stranger_is_a_no_op(self):
        from ecdsa import SECP256k1, SigningKey

        stranger = SigningKey.from_secret_exponent(777, curve=SECP256k1)
        # the signature holds, but its signer is not in this machine's /etc/fleet/signers
        self._nothing_changes(lambda: self.site.serve(signed(50, key=stranger), 50))

    def test_a_record_with_a_changed_message_is_a_no_op(self):
        record = signed(6)
        record["message"]["seq"] = 60  # the signature no longer matches
        self._nothing_changes(lambda: self.site.serve(record, 60))

    def test_a_body_over_the_cap_is_a_no_op(self):
        self._nothing_changes(lambda: self.site.serve(signed(6, extra="x" * (REL.MAX_BYTES + 10)), 6))

    def test_a_server_that_never_answers_is_given_up_on(self):
        self.site.serve(signed(5), 5)
        self.pass_()
        self.assertEqual(REL.SIGNED_TIMEOUT, 15)
        REL.SIGNED_TIMEOUT = 0.3
        self.addCleanup(setattr, REL, "SIGNED_TIMEOUT", 15)
        self.site.delay = 1.5
        started = time.monotonic()
        self.pass_()
        self.assertLess(time.monotonic() - started, 1.2)
        self.assertEqual(self.stored()["message"]["seq"], 5)

    def test_a_dead_server_is_a_no_op(self):
        self.site.serve(signed(5), 5)
        self.pass_()
        self.site.close()
        self.pass_()
        self.assertEqual(self.stored()["message"]["seq"], 5)
        self.assertTrue(any("could not be fetched" in line for kind, *rest in self.calls if kind == "log" for line in rest))

    def test_a_redirect_away_from_http_is_refused(self):
        self._nothing_changes(lambda: setattr(self.site, "location", "file:///etc/passwd"))

    def test_the_first_ever_failure_leaves_no_file_and_the_pass_follows_latest(self):
        self.site.status, self.site.body = 404, b"{}"
        self.assertEqual(self.pass_()["action"], "none")  # NEW is already the latest
        self.assertFalse(self.local.exists())
        self.file = release(NEWER, "ccccccc3")
        self.assertEqual(self.pass_()["action"], "updated")
        self.assertEqual(self.image("a"), NEWER)

    def test_a_hand_placed_higher_record_is_not_overwritten_by_an_older_fetched_one(self):
        (self.home / "release").mkdir(exist_ok=True)
        R.write_json(self.local, signed(8))
        self.site.serve(signed(7, OLD, "aaaaaaa1"), 7)
        self.pass_()
        self.assertEqual(self.stored()["message"]["seq"], 8)
        self.assertEqual(self.image("a"), NEW)

    def test_a_pass_with_an_unreadable_signers_file_trusts_nothing_fetched(self):
        self.signers.write_text("")
        self.site.serve(signed(5), 5)
        self.pass_()
        self.assertFalse(self.local.exists())
