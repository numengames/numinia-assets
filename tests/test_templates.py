# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""MIS-297: a world born from a named template of its engine (CreateWorldFromTemplate).

Signed here by a throwaway Oracle (test_engines.Home), except for the shared
vector, made with viem: the room (MIS-298) copies that one. The engine is
never run: the tests read the order and the compose file the machine renders.
"""

from pathlib import Path
import json
import sys
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_engines as E  # noqa: E402  (its loaders, Home and Door rig)

R, D, W = E.R, E.D, E.R.walletsig
NOW = E.NOW


def message(template="hermes", world="nomada", image=E.ENGINE, nonce="c1" * 16, **over):
    return {"machine": "open-1", "world": world, "domain": f"{world}.numinia.com", "image": image,
            "memory": "2g", "cpus": "1.5", "maxUploadMb": 50, "template": template,
            "nonce": nonce, "expires": NOW + 600, **over}


def plain(world="nomada", image=E.ENGINE, nonce="c2" * 16):
    return {k: v for k, v in message(world=world, image=image, nonce=nonce).items() if k != "template"}


class Born(unittest.TestCase):
    def create(self, kind, msg):
        h = E.Home(self)
        h.sign_and_drop(kind, msg, adminCode=E.CODE)
        h.take()
        return h

    def test_a_signed_template_reaches_the_compose_file_as_motor_plantilla(self):
        h = self.create("CreateWorldFromTemplate", message())
        self.assertEqual(h.result(message()["nonce"])["state"], "done")
        self.assertEqual(h.order("nomada")["template"], "hermes")
        service = json.loads(h.plan()["compose"])["services"]["nomada"]
        self.assertEqual(service["environment"]["MOTOR_PLANTILLA"], "hermes")

    def test_the_type_is_signed_field_for_field_like_the_rooms_vector(self):
        vector = E.SIGNED["CreateWorldFromTemplate"]
        self.assertEqual([n for n, _ in W.ACTIONS["CreateWorldFromTemplate"]],
                         list(vector["message"]))
        self.assertIsNone(R.request_problem("CreateWorldFromTemplate", vector["message"]))
        self.assertEqual("0x" + W.typed_digest("CreateWorldFromTemplate", vector["message"]).hex(), vector["digest"])

    def test_plain_create_world_renders_the_same_compose_as_before(self):
        h = self.create("CreateWorld", plain())
        self.assertNotIn("template", h.order("nomada"))
        service = json.loads(h.plan()["compose"])["services"]["nomada"]
        self.assertEqual(service["environment"], {"MOTOR_PASE_CLAVE": R.WORLD_PASS_KEY, "MOTOR_PASE_MUNDO": "nomada"})
        self.assertNotIn("MOTOR_PLANTILLA", h.plan()["compose"])

    def test_a_later_pass_changes_nothing(self):
        h = self.create("CreateWorldFromTemplate", message())
        self.assertEqual(h.plan()["compose"], h.plan()["compose"])
        self.assertEqual(h.order("nomada")["template"], "hermes")

    def test_a_template_on_hyperfy_is_refused_by_name(self):
        h = self.create("CreateWorldFromTemplate", message(image=E.HYPERFY))
        result = h.result(message()["nonce"])
        self.assertEqual((result["state"], result["rule"]), ("failed", "engine-has-no-templates"))
        self.assertIsNone(h.order("nomada"))


class Refused(unittest.TestCase):
    def test_a_bad_name_is_refused_with_the_rule_not_the_value(self):
        for bad in ("../x", "Hermes", "-x", "a" * 32, "", "a b", "x\n"):
            with self.subTest(template=bad):
                why = R.refusal({**E.order(), "template": bad})
                self.assertIsNotNone(why)
                self.assertIn("template is not", why)
                self.assertNotIn(bad.strip() or "\0", why.replace("template", ""))
                self.assertIsNotNone(R.request_problem("CreateWorldFromTemplate", message(template=bad)))
        for good in ("hermes", "nomada", "a", "a" * 31, "my_tpl-2"):
            with self.subTest(template=good):
                self.assertIsNone(R.refusal({**E.order(), "template": good}))

    def test_a_signed_bad_name_never_makes_a_world(self):
        h = E.Home(self)
        h.sign_and_drop("CreateWorldFromTemplate", message(template="../x"), adminCode=E.CODE)
        h.take()
        self.assertIsNone(h.order("nomada"))
        self.assertEqual(h.result(message()["nonce"])["state"], "failed")

    def test_an_order_edited_by_hand_with_a_bad_template_is_not_served(self):
        self.assertIsNotNone(R.refusal({**E.order(), "template": "../x"}))


class Kept(unittest.TestCase):
    def test_clean_and_the_nightly_copy_keep_the_template(self):
        o = {**E.order(), "template": "hermes"}
        self.assertEqual(R.clean(o)["template"], "hermes")
        self.assertNotIn("template", R.clean(E.order()))
        source = (E.MACHINE / "nightly.py").read_text(encoding="utf-8")
        self.assertIn('"org", "template"', source)


class Door(unittest.TestCase):
    """What the door says and refuses."""

    def rig(self, *orders, now=NOW):
        rig = E.T.Rig(self, now=now)
        for o in orders:
            (rig.home / "orders" / f"{o['id']}.json").write_text(json.dumps(o))
        return rig

    def test_the_door_says_templates_are_understood(self):
        self.assertIs(self.rig().door.machine()["templates"], True)

    def sent(self, rig, msg):
        return rig.accept(json.dumps({"type": "CreateWorldFromTemplate", "message": msg,
                                      "signature": rig.oracle.sign("CreateWorldFromTemplate", msg)}).encode())

    def test_template_request_is_taken_and_shown_as_creating(self):
        rig = self.rig(now=NOW)
        status, reply = self.sent(rig, message())
        self.assertEqual(status, 202)
        self.assertEqual(len(reply["adminCode"]), 16)
        self.assertEqual(rig.door.worlds()["worlds"][0]["phase"], "creating")
        [raw] = rig.dropped()
        self.assertEqual(raw["message"]["template"], "hermes")

    def test_template_requests_are_refused_by_name(self):
        self.assertEqual(self.sent(self.rig(), message(image=E.HYPERFY)), (409, {"error": "engine-has-no-templates"}))
        self.assertEqual(self.sent(self.rig(), message(image=E.OTHER))[1], {"error": "unknown-engine"})
        status, reply = self.sent(self.rig(), message(template="../x"))
        self.assertEqual(status, 400)
        rig = self.rig(E.order("nomada"))
        self.assertEqual(self.sent(rig, message()), (409, {"error": "exists"}))


if __name__ == "__main__":
    unittest.main()
