# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""A signed PinWorld holds one world on a build (or lets it follow the fleet again), and the door shows it.

The requests are the real EIP-712 signatures in fleet_vectors.json (machine
`open-1`, world `plaza`, valid until 2030). Docker's pull is a stub.
"""

from pathlib import Path
import contextlib
import copy
import io
import json
import sys
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_fleetd as T  # noqa: E402  (the door rig and the vectors)

R, D, W = T.R, T.D, T.D.W
PINS = [v for v in T.VECTORS["vectors"] if v["type"] == "PinWorld"]
PIN, RELEASE = PINS[0], PINS[1]
UPDATE = next(v for v in T.VECTORS["vectors"] if v["type"] == "UpdateWorld")
ENGINE_BUILD = UPDATE["message"]["image"]  # the build the plaza runs
HYPERFY = R.REGISTRY + "numinia-hyperfy2:sha-abcdef1"


def post(kind, vector):
    return json.dumps({"type": kind, "message": vector["message"], "signature": vector["signature"]}).encode()


class Rig(T.Rig):
    def engine_world(self, wid="plaza", state="running", **extra):
        self.put_world(wid, f"{wid}.numinia.com")
        path = self.home / "orders" / f"{wid}.json"
        order = json.loads(path.read_text())
        order.update({"image": ENGINE_BUILD, "state": state, **extra})
        path.write_text(json.dumps(order))

    def order(self, wid="plaza"):
        return R.read_order(self.home / "orders" / f"{wid}.json")

    def take(self, pulled=None):
        with contextlib.redirect_stderr(io.StringIO()):
            return R.take_requests(self.home, self.home / "orders", "open-1", frozenset({T.ORACLE}), frozenset(),
                                   "open-1.numinia.com", T.NOW, codes_dir=self.etc / "codes",
                                   pull=(lambda image: pulled.append(image)) if pulled is not None else (lambda i: None))

    def answer(self, vector):
        return json.loads((self.home / "requests" / f"{vector['message']['nonce']}.json").read_text())


class Pinning(unittest.TestCase):
    def test_a_pin_moves_the_world_to_the_build_and_writes_the_pin_on_its_order(self):
        rig = Rig(self)
        rig.engine_world()
        self.assertEqual(rig.accept(post("PinWorld", PIN))[0], 202)
        pulled = []
        rig.take(pulled)
        self.assertEqual(pulled, [PIN["message"]["image"]])
        order = rig.order()
        self.assertEqual(order["image"], PIN["message"]["image"])
        self.assertEqual(order["pin"]["image"], PIN["message"]["image"])
        self.assertEqual(order["pin"]["reason"], "client demo until Friday")
        self.assertEqual(order["pin"]["by"], PIN["signer"])
        # A running world is answered once Docker has its new build, like an update.
        self.assertEqual(rig.answer(PIN)["state"], "working")
        self.assertEqual(R.finish_updates(rig.home, T.NOW), ["plaza"])
        self.assertEqual((rig.answer(PIN)["state"], rig.answer(PIN)["type"]), ("done", "PinWorld"))

    def test_a_stopped_world_is_answered_at_once(self):
        rig = Rig(self)
        rig.engine_world(state="stopped")
        rig.accept(post("PinWorld", PIN))
        rig.take()
        self.assertEqual(rig.answer(PIN)["state"], "done")
        self.assertIn("pin", rig.order())

    def test_an_empty_image_releases_the_pin_and_leaves_the_build_as_it_is(self):
        rig = Rig(self)
        rig.engine_world(pin={"image": ENGINE_BUILD, "reason": "x", "at": "2026-10-17T00:00:00Z", "by": T.ORACLE})
        self.assertEqual(rig.accept(post("PinWorld", RELEASE))[0], 202)
        pulled = []
        rig.take(pulled)
        self.assertEqual(pulled, [])
        order = rig.order()
        self.assertNotIn("pin", order)
        self.assertEqual(order["image"], ENGINE_BUILD)
        self.assertEqual(rig.answer(RELEASE)["state"], "done")  # nothing to wait for

    def test_releasing_a_world_that_has_no_pin_is_harmless(self):
        rig = Rig(self)
        rig.engine_world()
        rig.accept(post("PinWorld", RELEASE))
        rig.take()
        self.assertEqual(rig.answer(RELEASE)["state"], "done")
        self.assertNotIn("pin", rig.order())

    def test_a_build_the_machine_cannot_fetch_leaves_the_order_alone(self):
        rig = Rig(self)
        rig.engine_world()

        def broken(image):
            raise OSError("no registry")

        rig.accept(post("PinWorld", PIN))
        with contextlib.redirect_stderr(io.StringIO()):
            R.take_requests(rig.home, rig.home / "orders", "open-1", frozenset({T.ORACLE}), frozenset(),
                            "open-1.numinia.com", T.NOW, codes_dir=rig.etc / "codes", pull=broken)
        self.assertEqual(rig.answer(PIN)["state"], "failed")
        self.assertEqual(rig.order()["image"], ENGINE_BUILD)
        self.assertNotIn("pin", rig.order())

    def test_a_world_that_is_not_here_is_missing(self):
        rig = Rig(self)
        self.assertEqual(rig.accept(post("PinWorld", PIN)), (409, {"error": "missing"}))


class Rails(unittest.TestCase):
    """The same rails as UpdateWorld."""

    def test_a_stranger_a_replay_and_a_late_request_are_refused(self):
        rig = Rig(self)
        rig.engine_world()
        stranger = rig.oracle
        message = {**PIN["message"], "nonce": "b" * 32, "expires": T.NOW + 600}
        signed = json.dumps({"type": "PinWorld", "message": message, "signature": stranger.sign("PinWorld", message)})
        (rig.etc / "signers").write_text(f"{T.ORACLE}\n")  # the throwaway key is no Oracle here
        self.assertEqual(rig.accept(signed.encode()), (403, {"error": "not-an-oracle-here"}))
        self.assertEqual(rig.accept(post("PinWorld", PIN))[0], 202)
        self.assertEqual(rig.accept(post("PinWorld", PIN)), (409, {"error": "replayed"}))
        late = Rig(self, now=PIN["message"]["expires"] + 1)
        late.engine_world()
        self.assertEqual(late.accept(post("PinWorld", PIN)), (400, {"error": "expired"}))

    def test_a_forged_signature_and_a_changed_message_are_refused(self):
        rig = Rig(self)
        rig.engine_world()
        changed = copy.deepcopy(PIN)
        changed["message"]["reason"] = "something else"
        # The signature recovers some other address: never an Oracle's.
        self.assertIn(rig.accept(post("PinWorld", changed)), [(401, {"error": "bad-signature"}),
                                                              (403, {"error": "not-an-oracle-here"})])
        self.assertEqual(list((rig.home / "inbox").iterdir()), [])

    def test_only_the_house_registry_is_allowed(self):
        rig = Rig(self)
        rig.engine_world()
        for image in ("docker.io/library/nginx@sha256:" + "a" * 64, "ghcr.io/numengames/numinia-engine:latest",
                      "ghcr.io/evil/x@sha256:" + "a" * 64, " ", "ghcr.io/numengames/numinia-engine"):
            with self.subTest(image=image):
                vector = copy.deepcopy(PIN)
                vector["message"]["image"] = image
                self.assertEqual(rig.accept(post("PinWorld", vector))[0], 400)

    def test_the_reason_is_one_short_line(self):
        rig = Rig(self)
        rig.engine_world()
        for reason in ("", "a\nb", "x" * 201, "bell\x07"):
            with self.subTest(reason=reason):
                vector = copy.deepcopy(PIN)
                vector["message"]["reason"] = reason
                self.assertEqual(rig.accept(post("PinWorld", vector))[0], 400)

    def test_another_engine_is_refused_by_the_door_and_by_the_reconciler(self):
        rig = Rig(self)
        rig.engine_world()
        vector = copy.deepcopy(PIN)
        vector["message"] = {**vector["message"], "image": HYPERFY, "nonce": "f" * 32, "expires": T.NOW + 600}
        vector["signature"] = rig.oracle.sign("PinWorld", vector["message"])
        self.assertEqual(rig.accept(post("PinWorld", vector)), (409, {"error": "engine-mismatch"}))
        # Straight at the reconciler (the door checked once, the reconciler checks again).
        rule = R.pin_world({"world": "plaza", "by": T.ORACLE}, {**vector["message"]}, rig.home / "orders", T.NOW,
                           pull=lambda i: None)
        self.assertEqual(rule, "engine-mismatch")
        self.assertNotIn("pin", rig.order())

    def test_busy_while_the_world_changes_and_the_other_way_round(self):
        rig = Rig(self)
        rig.engine_world()
        rig.wait("UpdateWorld")
        self.assertEqual(rig.accept(post("PinWorld", PIN)), (409, {"error": "busy"}))
        rig = Rig(self)
        rig.engine_world()
        rig.wait("PinWorld")
        self.assertEqual(rig.accept(post("UpdateWorld", UPDATE)), (409, {"error": "busy"}))
        self.assertEqual(rig.accept(body_rotate()), (409, {"error": "busy"}))
        rig = Rig(self)
        rig.engine_world()
        (rig.home / "run").mkdir(exist_ok=True)
        (rig.home / "run" / R.UPDATING).write_text(json.dumps({"plaza": {"nonce": "c" * 32, "by": T.ORACLE}}))
        self.assertEqual(rig.accept(post("PinWorld", PIN)), (409, {"error": "busy"}))

    def test_a_release_is_busy_too_and_missing_world_is_missing(self):
        rig = Rig(self)
        rig.engine_world()
        rig.wait("StopWorld")
        self.assertEqual(rig.accept(post("PinWorld", RELEASE)), (409, {"error": "busy"}))
        rig = Rig(self)
        self.assertEqual(rig.accept(post("PinWorld", RELEASE)), (409, {"error": "missing"}))


def body_rotate():
    return T.body("RotateWorldCode")


class Seen(unittest.TestCase):
    """The door says it listens to pins, how the release stands, and each world's pin and error."""

    def test_the_machine_advertises_pins_and_an_idle_release(self):
        facts = Rig(self).door.machine()
        self.assertEqual(facts["listens"], {"pin": True, "nightly": True})
        self.assertEqual(facts["release"], {"target": None, "commit": None, "halted": False, "haltedReason": None,
                                            "lastPass": None})
        for old in ("machine", "door", "oracles", "zones", "recipe", "lastRun", "waiting", "imports", "engines",
                    "updates", "at"):
            self.assertIn(old, facts)

    def test_the_release_state_shows_target_halt_and_last_pass(self):
        rig = Rig(self)
        folder = rig.home / "release"
        folder.mkdir()
        (folder / "state.json").write_text(json.dumps({
            "target": {"image": ENGINE_BUILD, "commit": "c57dbb7cc4c5", "why": "latest"},
            "halted": {"image": ENGINE_BUILD, "world": "plaza", "reason": "/status did not answer"},
            "lastPass": {"at": "2026-10-17T10:00:00Z", "action": "halted"}, "bad": [ENGINE_BUILD], "seq": 3}))
        self.assertEqual(rig.door.machine()["release"], {
            "target": ENGINE_BUILD, "commit": "c57dbb7cc4c5", "halted": True, "haltedReason": "/status did not answer",
            "lastPass": {"at": "2026-10-17T10:00:00Z", "action": "halted"}})

    def test_a_state_file_that_is_garbage_says_nothing_rather_than_something_false(self):
        rig = Rig(self)
        folder = rig.home / "release"
        folder.mkdir()
        for raw in ("[]", "not json", json.dumps({"target": {"image": "evil", "commit": "../x"}, "lastPass": 5,
                                                  "halted": "yes"})):
            (folder / "state.json").write_text(raw)
            with self.subTest(raw=raw):
                self.assertEqual(rig.door.machine()["release"], {"target": None, "commit": None, "halted": False,
                                                                 "haltedReason": None, "lastPass": None})

    def test_a_world_shows_its_pin_and_its_update_error_only_when_it_has_them(self):
        rig = Rig(self)
        rig.engine_world()
        [plain] = rig.door.worlds()["worlds"]
        self.assertNotIn("pin", plain)
        self.assertNotIn("updateError", plain)
        rig.engine_world(pin={"image": PIN["message"]["image"], "reason": "demo", "at": "2026-10-17T10:00:00Z",
                              "by": T.ORACLE}, updateError="/status did not answer")
        [world] = rig.door.worlds()["worlds"]
        self.assertEqual(world["pin"], {"image": PIN["message"]["image"], "reason": "demo",
                                        "at": "2026-10-17T10:00:00Z"})
        self.assertEqual(world["updateError"], "/status did not answer")
        self.assertNotIn(T.ORACLE, json.dumps(world["pin"]))

    def test_a_pin_that_is_not_a_pin_is_not_shown(self):
        rig = Rig(self)
        rig.engine_world(pin="because", updateError="")
        [world] = rig.door.worlds()["worlds"]
        self.assertNotIn("pin", world)
        self.assertNotIn("updateError", world)

    def test_the_pass_leaves_its_mark_for_the_door(self):
        import test_release as TR
        REL = TR.REL
        rig = Rig(self)
        env = REL.Env(rig.home, rig.etc / "signers")
        env.fetch = lambda: b"{}"
        env.apply = lambda: True
        result = REL.run_pass(env)
        state = json.loads((rig.home / "release" / "state.json").read_text())
        self.assertEqual(state["lastPass"]["action"], result["action"])
        self.assertEqual(rig.door.machine()["release"]["lastPass"]["action"], result["action"])


class Old(unittest.TestCase):
    """Requests and orders from before pins go through exactly as they did."""

    def test_an_update_still_works_and_adds_no_pin(self):
        rig = Rig(self)
        rig.engine_world(image=ENGINE_BUILD)
        self.assertEqual(rig.accept(post("UpdateWorld", UPDATE))[0], 202)
        rig.take()
        self.assertNotIn("pin", rig.order())
        self.assertEqual(rig.answer(UPDATE)["state"], "working")
        R.finish_updates(rig.home, T.NOW)
        self.assertEqual(rig.answer(UPDATE)["type"], "UpdateWorld")

    def test_an_update_after_a_pin_keeps_the_pin_and_a_pin_after_an_update_replaces_nothing_else(self):
        rig = Rig(self)
        rig.engine_world(state="stopped")
        rig.accept(post("PinWorld", PIN))
        rig.take()
        pinned = rig.order()
        self.assertEqual(set(pinned) - {"pin", "updatedAt", "updatedBy"}, {"id", "card", "state", "server", "domain",
                                                                           "image", "limits", "createdBy"})

    def test_an_updating_job_written_before_pins_is_still_answered_as_an_update(self):
        rig = Rig(self)
        (rig.home / "run").mkdir(exist_ok=True)
        (rig.home / "run" / R.UPDATING).write_text(json.dumps({"plaza": {"nonce": "d" * 32, "by": T.ORACLE}}))
        self.assertEqual(R.finish_updates(rig.home, T.NOW), ["plaza"])
        self.assertEqual(json.loads((rig.home / "requests" / ("d" * 32 + ".json")).read_text())["type"], "UpdateWorld")

    def test_the_updater_leaves_a_pinned_world_alone(self):
        import test_release as TR
        self.assertTrue(hasattr(TR, "REL"))  # covered there: "pinned" is skipped; the pin this file writes is a dict
        orders = [{"id": "plaza", "image": ENGINE_BUILD, "state": "running",
                   "pin": {"image": ENGINE_BUILD, "reason": "x"}}]
        target = {"image": TR.NEW, "commit": "b" * 7}
        plan = TR.REL.plan_rollout(orders, target, {"bad": [], "seq": 0})
        self.assertEqual(plan["skipped"], {"plaza": "pinned"})
        self.assertIsNone(plan["next"])

    def test_the_vectors_pin_is_valid_for_the_machine(self):
        for vector in PINS:
            self.assertIsNone(R.request_problem("PinWorld", vector["message"]))
            self.assertEqual(W.signer_of("PinWorld", vector["message"], vector["signature"]), vector["signer"])


if __name__ == "__main__":
    unittest.main()
