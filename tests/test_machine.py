# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""The fleet machine's reconciler keeps to its alias, its keys and the order's shape."""

from pathlib import Path
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MACHINE = ROOT / "open-worlds" / "machine"


def load_reconcile():
    spec = importlib.util.spec_from_file_location("reconcile", MACHINE / "reconcile.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


R = load_reconcile()

ORDER = {
    "id": "plaza",
    "card": "plaza",
    "state": "running",
    "server": "open-1",
    "domain": "plaza.numen.games",
    "image": "ghcr.io/numengames/numinia-hyperfy2:sha-0123456",
    "limits": {"memory": "2g", "cpus": "1.5", "maxUploadMb": 50},
}


def order(**over):
    o = {**ORDER, **over}
    if "limits" in over:
        o["limits"] = {**ORDER["limits"], **over["limits"]}
    return o


class Book:
    """A temporary order book and fleet home for one test."""

    def __init__(self, orders, keys=()):
        self.temp = tempfile.TemporaryDirectory(prefix="fleet-test-")
        root = Path(self.temp.name)
        self.book = root / "book"
        self.home = root / "home"
        (self.book / "open-worlds").mkdir(parents=True)
        (self.home / "env").mkdir(parents=True)
        for o in orders:
            (self.book / "open-worlds" / f"{o['id']}.json").write_text(json.dumps(o), encoding="utf-8")
        for wid in keys:
            (self.home / "env" / f"{wid}.env").write_text("JWT_SECRET=x\nADMIN_CODE=y\n", encoding="utf-8")

    def plan(self, alias="open-1"):
        return R.plan(self.book, self.home, alias, allowed=set())


class ReconcileTests(unittest.TestCase):
    def test_only_orders_for_this_alias_are_kept(self):
        orders = [order(), order(id="otro", card="otro", server="open-2", domain="otro.numen.games")]
        self.assertEqual([o["id"] for o in R.select_orders(orders, "open-1")], ["plaza"])
        self.assertEqual([o["id"] for o in R.select_orders(orders, "open-2")], ["otro"])
        self.assertEqual(R.select_orders(orders, "open-9"), [])

    def test_an_order_with_a_foreign_image_or_domain_is_refused(self):
        foreign = [
            order(image="docker.io/evil/hyperfy:sha-0123456"),
            order(id="x", card="x", domain="plaza.evil.example"),
            order(id="y", card="y", domain="deep.plaza.numen.games"),
            order(id="z", card="z", domain="numen.games"),
            order(id="w", card="w", domain="plaza.numinia.com.evil.example"),
        ]
        self.assertEqual(R.select_orders(foreign, "open-1"), [])
        self.assertIsNone(R.refusal(order()))

    def test_both_house_zones_are_allowed(self):
        self.assertIsNone(R.refusal(order(domain="p1.numinia.com")))
        self.assertIsNone(R.refusal(order(domain="p1.numen.games")))
        self.assertIsNone(R.refusal(order(domain="P1.Numinia.com")))

    def test_a_clients_domain_is_allowed_only_when_listed_on_the_machine(self):
        client = order(domain="mundo.cliente.example")
        self.assertIsNotNone(R.refusal(client))
        self.assertIsNone(R.refusal(client, {"mundo.cliente.example"}))
        self.assertIsNone(R.refusal(client, {"*.cliente.example"}))
        self.assertIsNotNone(R.refusal(order(domain="a.b.cliente.example"), {"*.cliente.example"}))
        with tempfile.TemporaryDirectory() as temp:
            f = Path(temp) / "domains"
            f.write_text("# client X\n*.cliente.example\n\nsolo.otro.example\n", encoding="utf-8")
            self.assertEqual(R.read_allowed_domains(f), {"*.cliente.example", "solo.otro.example"})
        self.assertEqual(R.read_allowed_domains(Path("/nonexistent/domains")), set())

    def test_the_reconciler_is_installed_not_run_from_the_clone(self):
        text = (MACHINE / "bootstrap.sh").read_text()
        self.assertIn("install -m 0755", text)
        self.assertIn("ExecStart=/usr/local/sbin/fleet-reconcile", text)
        self.assertNotIn("ExecStart=/usr/bin/python3", text)

    def test_a_running_world_without_keys_is_reported_and_not_started(self):
        p = Book([order()], keys=()).plan()
        self.assertEqual(p["keyless"], ["plaza"])
        self.assertEqual(p["running"], [])
        self.assertNotIn("plaza:", p["compose"])

    def test_a_running_world_with_keys_gets_a_service_and_a_site(self):
        p = Book([order()], keys=["plaza"]).plan()
        self.assertEqual(p["running"], ["plaza"])
        self.assertIn("  plaza:\n    image: ghcr.io/numengames/numinia-hyperfy2:sha-0123456", p["compose"])
        self.assertIn("PUBLIC_WS_URL: wss://plaza.numen.games/ws", p["compose"])
        self.assertIn("PUBLIC_MAX_UPLOAD_SIZE: '50'", p["compose"])
        self.assertIn("memory: 2g", p["compose"])
        self.assertIn("cpus: '1.5'", p["compose"])
        self.assertIn("plaza.numen.games {\n\treverse_proxy plaza:3000\n}", p["caddyfile"])

    def test_a_stopped_world_is_listed_as_stopped_and_absent_from_compose(self):
        p = Book([order(state="stopped")], keys=["plaza"]).plan()
        self.assertEqual(p["stopped"], ["plaza"])
        self.assertEqual(p["running"], [])
        self.assertNotIn("plaza", p["caddyfile"])

    def test_only_caddy_publishes_ports(self):
        p = Book([order()], keys=["plaza"]).plan()
        self.assertEqual(p["compose"].count("ports:"), 1)
        self.assertIn('ports: ["80:80", "443:443"]', p["compose"])

    def test_each_world_has_its_own_folder_and_env_file(self):
        two = [order(), order(id="foro", card="foro", domain="foro.numen.games")]
        p = Book(two, keys=["plaza", "foro"]).plan()
        for wid in ("plaza", "foro"):
            self.assertIn(f"/data/{wid}:/app/world", p["compose"])
            self.assertIn(f"/env/{wid}.env", p["compose"])

    def test_malformed_orders_are_skipped_not_fatal(self):
        b = Book([order()], keys=["plaza"])
        (b.book / "open-worlds" / "roto.json").write_text("{not json", encoding="utf-8")
        (b.book / "open-worlds" / "ajeno.json").write_text(json.dumps(order(id="otro")), encoding="utf-8")
        self.assertEqual(b.plan()["running"], ["plaza"])

    def test_dry_run_prints_the_plan_and_touches_nothing(self):
        b = Book([order()], keys=["plaza"])
        out = subprocess.run(
            [sys.executable, str(MACHINE / "reconcile.py"), "--book", str(b.book), "--home", str(b.home),
             "--alias", "open-1", "--dry-run"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn('"running": [\n    "plaza"\n  ]', out)
        self.assertFalse((b.home / "run").exists())

    def test_bootstrap_refuses_a_bad_alias_and_a_non_root_user(self):
        bad = subprocess.run(["bash", str(MACHINE / "bootstrap.sh"), "Open_1"], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)
        self.assertIn("usage", bad.stderr)
        if subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip() != "0":
            user = subprocess.run(["bash", str(MACHINE / "bootstrap.sh"), "open-1"], capture_output=True, text=True)
            self.assertEqual(user.returncode, 2)
            self.assertIn("root", user.stderr)

    def test_the_machine_holds_no_github_credential(self):
        text = (MACHINE / "bootstrap.sh").read_text() + (MACHINE / "reconcile.py").read_text()
        for word in ("GITHUB_TOKEN", "ghp_", "x-access-token", "deploy key", "id_ed25519"):
            self.assertNotIn(word, text)
        self.assertIn("https://github.com/numengames/numinia-assets.git", text)


if __name__ == "__main__":
    unittest.main()
