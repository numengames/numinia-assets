# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Integrity pins, not a second editable catalogue.

The canonical intake record is in
numinia-archive/system/SYS-005-digital-resource-register.md.
"""

from pathlib import Path
import hashlib
import json
import struct
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "content/avatars/ndg-019d4075-9ece-7d3e-aafa-41f81370eb63.vrm": (
        1253352, "4f8a44907bb2232178514f230aedfc4d344600287e236724a0b660420f2d995e",
    ),
    "content/models/pot-vapor-02.glb": (
        407284, "330605050f1786591a1f2f65f92ea04be5e29277577cf8ce36d29e83d4b77390",
    ),
}


class FirstIntakeTests(unittest.TestCase):
    def test_only_reviewed_resources_are_present_and_unchanged(self):
        actual = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "content").rglob("*")
            if path.is_file() and path.suffix not in {".md", ".license"}
        }
        self.assertEqual(actual, set(EXPECTED))
        for name, (size, digest) in EXPECTED.items():
            with self.subTest(resource=name):
                payload = (ROOT / name).read_bytes()
                self.assertEqual(len(payload), size)
                self.assertEqual(hashlib.sha256(payload).hexdigest(), digest)
                notice = (ROOT / (name + ".license")).read_text(encoding="utf-8")
                # Split the tags: these assertions do not license this code.
                self.assertIn("SPDX-" + "License-Identifier: CC0-1.0", notice)
                self.assertIn("SPDX-" + "FileCopyrightText: Polygonal Mind", notice)
                self.assertIn("SYS-005-digital-resource-register.md", notice)

    def test_models_are_self_contained_and_avatar_retains_cc0(self):
        for name in EXPECTED:
            with self.subTest(resource=name):
                self.assertTrue((ROOT / name).is_file(), name)
                payload = (ROOT / name).read_bytes()
                self.assertEqual(
                    struct.unpack_from("<4sII", payload),
                    (b"glTF", 2, len(payload)),
                )
                length, kind = struct.unpack_from("<II", payload, 12)
                self.assertEqual(kind, 0x4E4F534A)
                model = json.loads(payload[20:20 + length])
                for entry in model.get("buffers", []) + model.get("images", []):
                    self.assertNotIn("uri", entry, "External objects need separate review")
                if name.endswith(".vrm"):
                    meta = model["extensions"]["VRM"]["meta"]
                    self.assertEqual(meta["licenseName"], "CC0")
                    self.assertEqual(meta["author"], "Polygonal Mind")


if __name__ == "__main__":
    unittest.main()
