# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: AGPL-3.0-only
"""A read token for one machine's vitals: the right Oracle, the right machine, a short life; numbers only."""

from pathlib import Path
import base64
import json
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_fleetd as T  # noqa: E402  (its Rig builds a door over a temporary home)
import test_vitals as TV  # noqa: E402

NOW = T.NOW


def token(oracle, machine="open-1", issued=NOW - 10, expires=NOW + 3600, nonce="abcdefgh12345678", sign_as=None):
    message = {"machine": machine, "issuedAt": issued, "expires": expires, "nonce": nonce}
    signature = (sign_as or oracle).sign("FleetVitalsRead", message)
    return "Bearer " + base64.b64encode(json.dumps({"message": message, "signature": signature}).encode()).decode()


class VitalsRead(unittest.TestCase):
    def setUp(self):
        self.rig = T.Rig(self)
        self.door = self.rig.door
        out = self.rig.root / "vitals-out"
        out.mkdir()
        (out / "minute.json").write_text(json.dumps({"every_s": 60, "at": NOW, "scopes": {"machine": {"cpu_pct": [["1", 3.5]]}}}))
        self.door.vitals_dir = out

    def get(self, header, which="minute", client="203.0.113.7"):
        return self.door.vitals(which, header, client)

    def test_the_right_token_reads_numbers_only(self):
        status, reply = self.get(token(self.rig.oracle))
        self.assertEqual(status, 200)
        self.assertEqual(reply["machine"], "open-1")
        self.assertEqual(reply["scopes"]["machine"]["cpu_pct"], [["1", 3.5]])
        self.assertEqual(set(reply), {"machine", "every_s", "at", "scopes"})

    def test_a_token_for_another_machine_is_refused(self):
        self.assertEqual(self.get(token(self.rig.oracle, machine="bronte-xs-ca-003")), (403, {"error": "other-machine"}))

    def test_no_token_expired_future_and_too_long(self):
        self.assertEqual(self.get(None)[0], 401)
        self.assertEqual(self.get(token(self.rig.oracle, expires=NOW - 1))[1], {"error": "expired"})
        self.assertEqual(self.get(token(self.rig.oracle, issued=NOW + 3600, expires=NOW + 7200))[1], {"error": "from-the-future"})
        self.assertEqual(self.get(token(self.rig.oracle, issued=NOW - 10, expires=NOW + 86400))[1], {"error": "too-long"})
        self.assertEqual(self.get(token(self.rig.oracle, issued=NOW - 10, expires=NOW - 10 + 86400))[0], 200)

    def test_a_stranger_wallet_is_refused_and_a_forged_signature_too(self):
        stranger = T.Z.Oracle(T.D.W)
        self.assertEqual(self.get(token(stranger))[1], {"error": "not-an-oracle-here"})
        other = token(self.rig.oracle, nonce="zzzzzzzz00000000")
        raw = json.loads(base64.b64decode(other[7:]))
        raw["message"]["expires"] += 1  # the signed words changed
        forged = "Bearer " + base64.b64encode(json.dumps(raw).encode()).decode()
        self.assertIn(self.get(forged)[1]["error"], ("bad-signature", "not-an-oracle-here"))
        self.assertEqual(self.get("Bearer not-base64!!")[1], {"error": "no-token"})

    def test_it_only_serves_the_three_files_and_sixty_a_minute(self):
        good = token(self.rig.oracle)
        self.assertEqual(self.get(good, "../../etc/passwd")[0], 404)
        self.assertEqual(self.get(good, "five")[0], 404)  # nothing exported yet
        for _ in range(58):
            self.get(good)
        self.assertEqual(self.get(good)[0], 429)


if __name__ == "__main__":
    unittest.main()
