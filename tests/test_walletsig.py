# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""A fleet request's signer is recovered exactly as an Ethereum wallet signs it (EIP-712)."""

from pathlib import Path
import importlib.util
import json
import sys
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


W = load("walletsig")
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def with_v(signature, v):
    return signature[:-2] + f"{v:02x}"


class Keccak(unittest.TestCase):
    def test_known_digests(self):
        for case in VECTORS["keccak"]:
            with self.subTest(length=len(case["input"])):
                self.assertEqual("0x" + W.keccak256(case["input"].encode()).hex(), case["hex"])

    def test_empty_input_is_the_famous_one(self):
        self.assertEqual(
            W.keccak256(b"").hex(),
            "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470",
        )

    def test_block_edges(self):
        # 135 bytes pads with the single byte 0x81; 136 needs a whole new block.
        for size in (135, 136, 137, 272):
            digest = W.keccak256(b"a" * size)
            self.assertEqual(len(digest), 32)
            self.assertNotEqual(digest, W.keccak256(b"a" * (size - 1)))


class TypedData(unittest.TestCase):
    def test_types_match_the_browser(self):
        mirrored = {name: [{"name": f, "type": t} for f, t in fields] for name, fields in W.ACTIONS.items()}
        self.assertEqual(mirrored, VECTORS["types"])
        self.assertEqual(W.DOMAIN, VECTORS["domain"])

    def test_digests_match_viem(self):
        for vector in VECTORS["vectors"]:
            with self.subTest(kind=vector["type"]):
                digest = W.typed_digest(vector["type"], vector["message"])
                self.assertEqual("0x" + digest.hex(), vector["digest"])

    def test_signers_are_recovered(self):
        for vector in VECTORS["vectors"]:
            with self.subTest(kind=vector["type"]):
                self.assertEqual(
                    W.signer_of(vector["type"], vector["message"], vector["signature"]), vector["signer"]
                )

    def test_release_and_pin_types_have_vectors(self):
        kinds = [v["type"] for v in VECTORS["vectors"]]
        self.assertEqual(kinds.count("FleetRelease"), 1)
        self.assertGreaterEqual(kinds.count("PinWorld"), 2)  # a pin and its release (empty image)
        self.assertIn("", [v["message"]["image"] for v in VECTORS["vectors"] if v["type"] == "PinWorld"])

    def test_a_stranger_is_a_stranger(self):
        stranger = VECTORS["stranger"]
        signer = W.signer_of(stranger["type"], stranger["message"], stranger["signature"])
        self.assertEqual(signer, stranger["signer"])
        self.assertNotIn(signer, {v["signer"] for v in VECTORS["vectors"]})

    def test_a_changed_message_is_not_the_oracles(self):
        vector = VECTORS["vectors"][0]
        for field, value in (("world", "other"), ("domain", "x.numinia.com"), ("maxUploadMb", 51), ("expires", 1)):
            with self.subTest(field=field):
                changed = {**vector["message"], field: value}
                self.assertNotEqual(W.signer_of(vector["type"], changed, vector["signature"]), vector["signer"])

    def test_the_same_fields_under_another_kind_are_not_the_oracles(self):
        stop = next(v for v in VECTORS["vectors"] if v["type"] == "StopWorld")
        self.assertNotEqual(W.signer_of("StartWorld", stop["message"], stop["signature"]), stop["signer"])


class Signatures(unittest.TestCase):
    def setUp(self):
        self.vector = VECTORS["vectors"][0]
        self.digest = W.typed_digest(self.vector["type"], self.vector["message"])
        self.sig = self.vector["signature"]

    def test_recovery_byte_zero_or_one_is_accepted(self):
        v = int(self.sig[-2:], 16)
        self.assertEqual(W.recover(self.digest, with_v(self.sig, v - 27)), self.vector["signer"])

    def test_malformed_signatures_are_refused(self):
        r = int(self.sig[2:66], 16)
        s = int(self.sig[66:130], 16)
        v = self.sig[-2:]
        cases = {
            "not a string": None,
            "short": self.sig[:-2],
            "long": self.sig + "00",
            "no prefix": self.sig[2:] + "00",
            "not hex": "0x" + "zz" * 65,
            "v 29": with_v(self.sig, 29),
            "r zero": "0x" + "00" * 32 + self.sig[66:],
            "s zero": self.sig[:66] + "00" * 32 + v,
            "r too big": "0x" + f"{N:064x}" + self.sig[66:],
            "high s": self.sig[:66] + f"{N - s:064x}" + v,
            "newline": self.sig + "\n",
        }
        self.assertGreater(r, 0)
        for name, signature in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(W.recover(self.digest, signature))

    def test_an_r_off_the_curve_is_refused(self):
        # x = 5: 5**3 + 7 = 132 is not a square modulo p, so no point has it.
        bad = "0x" + f"{5:064x}" + self.sig[66:]
        self.assertIsNone(W.recover(self.digest, bad))

    def test_short_form(self):
        self.assertEqual(W.short("0x42e6aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaca26"), "0x42e6…ca26")
        self.assertEqual(W.short("not an address"), "?")


if __name__ == "__main__":
    unittest.main()
