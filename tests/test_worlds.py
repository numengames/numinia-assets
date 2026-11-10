# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Every world under mundos/ carries a valid ficha (DECISIONES-MUNDOS: ficha de mundo)."""

import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ("name", "description", "type", "state", "version", "creator", "owner", "languages",
            "visibility", "license", "pieces")
PIECE_REQUIRED = ("id", "kind", "path", "creator", "made_by", "license", "provenance")
MADE_BY = ("person", "person with AI", "AI")


class WorldFichaTests(unittest.TestCase):
    def fichas(self):
        return sorted((ROOT / "mundos").glob("*/ficha.json"))

    def test_every_world_has_a_valid_ficha(self):
        for path in self.fichas():
            with self.subTest(world=path.parent.name):
                ficha = json.loads(path.read_text(encoding="utf-8"))
                for key in REQUIRED:
                    self.assertIn(key, ficha, key + " missing")
                self.assertRegex(ficha["version"], r"^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$")
                langs = ficha["languages"]
                self.assertEqual(langs["default"], "en")
                self.assertIn("en", langs["playable"])
                ids = set()
                for piece in ficha["pieces"]:
                    for key in PIECE_REQUIRED:
                        self.assertTrue(piece.get(key), piece.get("id", "?") + ": " + key)
                    self.assertNotIn(piece["id"], ids)
                    ids.add(piece["id"])
                    self.assertIn(piece["made_by"], MADE_BY)
                    target = path.parent / piece["path"].split("#")[0]
                    self.assertTrue(target.exists(), str(target))
                    if "sha256" in piece and "#" not in piece["path"]:
                        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), piece["sha256"])


class SoundDesignTests(unittest.TestCase):
    def test_every_station_has_cues_and_every_cue_has_a_known_sound(self):
        path = ROOT / "mundos" / "tabletop-rpg-room" / "diseno" / "sonido.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], "sonido-gabinete@1")
        for key, sound in data["sounds"].items():
            self.assertIn(sound["source"], data["sources"], key)
            self.assertIn(sound["bus"], data["buses"], key)
        stations = {cue["station"] for cue in data["cues"]}
        self.assertEqual(stations, {"E%d" % i for i in range(14)})
        for cue in data["cues"]:
            self.assertIn(cue["sound"], data["sounds"], cue["id"])
            self.assertTrue(cue["caption"] == "silent" or {"en", "es"} <= set(cue["caption"]), cue["id"])


if __name__ == "__main__":
    unittest.main()
