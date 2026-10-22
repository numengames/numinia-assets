# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""The public fleet's orders: every order is well formed and holds nothing secret."""

from pathlib import Path
import json
import re
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FLEET = ROOT / "open-worlds"
INDEX = FLEET / "index.json"

FIELDS = {"id", "card", "state", "server", "domain", "image", "limits"}
LIMITS = {"memory", "cpus", "maxUploadMb"}
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DOMAIN = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")
IMAGE = re.compile(r"^[a-z0-9./_-]+(?::sha-[0-9a-f]{7,40}|@sha256:[0-9a-f]{64})$")
MEMORY = re.compile(r"^[0-9]+(?:\.[0-9]+)?[mg]$")
CPUS = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
IPV4 = re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")
SECRET = re.compile(
    r"(?i)(secret|password|passwd|token|api[_-]?key|private key|jwt|admin_code|livekit_)"
)


def order_files():
    """The order files of the book: every .json in the folder but the index."""
    return sorted(p for p in FLEET.glob("*.json") if p != INDEX)


def problems(path):
    """Every way an order file breaks the rules; an empty list means it is valid."""
    found = []
    text = path.read_text(encoding="utf-8")
    try:
        order = json.loads(text)
    except json.JSONDecodeError as error:
        return [f"{path.name}: not JSON ({error.msg})"]
    if not isinstance(order, dict):
        return [f"{path.name}: an order is a JSON object"]
    if set(order) != FIELDS:
        found.append(f"{path.name}: fields must be exactly {sorted(FIELDS)}, got {sorted(order)}")
    if order.get("id") != path.stem:
        found.append(f"{path.name}: id must equal the file name")
    for key in ("id", "card", "server"):
        if not SLUG.fullmatch(str(order.get(key, ""))):
            found.append(f"{path.name}: {key} must be lowercase letters, digits and hyphens")
    if order.get("id") in ("index", "caddy"):
        found.append(f"{path.name}: id {order['id']} is reserved")
    if order.get("state") not in ("running", "stopped"):
        found.append(f"{path.name}: state must be running or stopped")
    if not DOMAIN.fullmatch(str(order.get("domain", ""))):
        found.append(f"{path.name}: domain is not a host name")
    if not IMAGE.fullmatch(str(order.get("image", ""))):
        found.append(f"{path.name}: image must be pinned to a sha- tag or a digest")
    limits = order.get("limits")
    if not isinstance(limits, dict) or set(limits) != LIMITS:
        found.append(f"{path.name}: limits must be exactly {sorted(LIMITS)}")
    else:
        if not MEMORY.fullmatch(str(limits["memory"])):
            found.append(f"{path.name}: limits.memory looks like 512m or 2g")
        if not CPUS.fullmatch(str(limits["cpus"])):
            found.append(f"{path.name}: limits.cpus is a number such as 1.5")
        upload = limits["maxUploadMb"]
        if isinstance(upload, bool) or not isinstance(upload, int) or upload <= 0:
            found.append(f"{path.name}: limits.maxUploadMb is a positive integer")
    if IPV4.search(text):
        found.append(f"{path.name}: holds an IP address")
    if SECRET.search(text):
        found.append(f"{path.name}: names a secret")
    return found


VALID = {
    "id": "example-world",
    "card": "example-world",
    "state": "running",
    "server": "open-1",
    "domain": "example.numen.games",
    "image": "ghcr.io/numengames/numinia-hyperfy2:sha-0123456",
    "limits": {"memory": "2g", "cpus": "1.5", "maxUploadMb": 50},
}


class OpenWorldsTests(unittest.TestCase):
    def test_every_order_in_the_fleet_is_valid(self):
        self.assertTrue(FLEET.is_dir(), "open-worlds/ is missing")
        for path in order_files():
            with self.subTest(order=path.name):
                self.assertEqual(problems(path), [])

    def test_the_index_names_every_order_and_nothing_else(self):
        """index.json is the book's table of contents: the console reads it instead
        of asking GitHub's API to list the folder (60 keyless reads an hour per
        address). It must say exactly which orders exist, sorted, so a pull
        request that adds or closes an order must touch it too."""
        self.assertTrue(INDEX.is_file(), "open-worlds/index.json is missing")
        text = INDEX.read_text(encoding="utf-8")
        index = json.loads(text)
        self.assertEqual(set(index), {"orders"}, "the index holds one field, orders")
        self.assertEqual(index["orders"], [p.stem for p in order_files()])
        self.assertEqual(text, json.dumps(index, indent=2) + "\n", "regenerate it: see the README")

    def test_the_fleet_holds_only_orders_its_readme_and_the_machine_recipe(self):
        others = [
            p.name for p in FLEET.iterdir()
            if p.name not in ("README.md", "machine") and p.suffix != ".json"
        ]
        self.assertEqual(others, [])
        recipe = sorted(p.name for p in (FLEET / "machine").iterdir() if p.name != "__pycache__")
        self.assertEqual(recipe, ["README.md", "bootstrap.sh", "condiciones.py", "fleetd.py", "nightly.py", "recipe.py", "reconcile.py", "release.py", "signers.py", "versions.py", "walletsig.py"])

    def check(self, order, name="example-world"):
        with tempfile.TemporaryDirectory(prefix="open-worlds-test-") as temp:
            path = Path(temp) / f"{name}.json"
            path.write_text(json.dumps(order), encoding="utf-8")
            return problems(path)

    def test_a_valid_order_passes(self):
        self.assertEqual(self.check(VALID), [])

    def test_bad_orders_are_refused(self):
        cases = {
            "missing field": {k: v for k, v in VALID.items() if k != "card"},
            "id differs from file": {**VALID, "id": "other"},
            "unknown state": {**VALID, "state": "paused"},
            "unpinned image": {**VALID, "image": "ghcr.io/numengames/numinia-hyperfy2:latest"},
            "ip as server": {**VALID, "server": "51.68.1.2"},
            "ip in domain": {**VALID, "domain": "51.68.1.2"},
            "bad memory": {**VALID, "limits": {**VALID["limits"], "memory": "lots"}},
            "zero upload": {**VALID, "limits": {**VALID["limits"], "maxUploadMb": 0}},
            "extra secret field": {**VALID, "jwt_secret": "x"},
            "uppercase card": {**VALID, "card": "Example"},
            "newline after domain": {**VALID, "domain": "example.numen.games\n"},
            "settings in domain": {**VALID, "domain": "x\n    privileged: true\n    y.numen.games"},
            "newline after image": {**VALID, "image": VALID["image"] + "\n"},
            "upload as true": {**VALID, "limits": {**VALID["limits"], "maxUploadMb": True}},
        }
        for label, order in cases.items():
            with self.subTest(case=label):
                self.assertNotEqual(self.check(order), [])
        cases = {
            "reserved id index": ({**VALID, "id": "index", "card": "index"}, "index"),
            "reserved id caddy": ({**VALID, "id": "caddy", "card": "caddy"}, "caddy"),
        }
        for label, (order, name) in cases.items():
            with self.subTest(case=label):
                self.assertNotEqual(self.check(order, name), [])


if __name__ == "__main__":
    unittest.main()
