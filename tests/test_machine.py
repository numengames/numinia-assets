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
        plaza = json.loads(p["compose"])["services"]["plaza"]
        self.assertEqual(plaza["image"], "ghcr.io/numengames/numinia-hyperfy2:sha-0123456")
        self.assertEqual(plaza["environment"]["PUBLIC_WS_URL"], "wss://plaza.numen.games/ws")
        self.assertEqual(plaza["environment"]["PUBLIC_MAX_UPLOAD_SIZE"], "50")
        self.assertEqual(plaza["deploy"]["resources"]["limits"], {"memory": "2g", "cpus": "1.5"})
        self.assertIn("plaza.numen.games {\n\treverse_proxy plaza:3000\n}", p["caddyfile"])

    def test_a_valid_order_renders_exactly_the_expected_service(self):
        p = Book([order()], keys=["plaza"]).plan()
        plaza = json.loads(p["compose"])["services"]["plaza"]
        self.assertEqual(
            sorted(plaza), ["deploy", "env_file", "environment", "image", "restart", "volumes"]
        )
        self.assertEqual(plaza["restart"], "unless-stopped")
        self.assertNotIn("privileged", p["compose"])
        self.assertNotIn("cap_add", p["compose"])

    def test_a_stopped_world_is_listed_as_stopped_and_absent_from_compose(self):
        p = Book([order(state="stopped")], keys=["plaza"]).plan()
        self.assertEqual(p["stopped"], ["plaza"])
        self.assertEqual(p["running"], [])
        self.assertNotIn("plaza", p["caddyfile"])

    def test_only_caddy_publishes_ports(self):
        p = Book([order()], keys=["plaza"]).plan()
        services = json.loads(p["compose"])["services"]
        self.assertEqual([name for name, s in services.items() if "ports" in s], ["caddy"])
        self.assertEqual(services["caddy"]["ports"], ["80:80", "443:443"])

    def test_each_world_has_its_own_folder_and_env_file(self):
        two = [order(), order(id="foro", card="foro", domain="foro.numen.games")]
        p = Book(two, keys=["plaza", "foro"]).plan()
        services = json.loads(p["compose"])["services"]
        for wid in ("plaza", "foro"):
            self.assertTrue(services[wid]["volumes"][0].endswith(f"/data/{wid}:/app/world"))
            self.assertTrue(services[wid]["env_file"].endswith(f"/env/{wid}.env"))

    # --- an order is data, never configuration -----------------------------

    def test_the_reported_injection_is_refused_and_never_rendered(self):
        """A domain that smuggles settings into the compose file (root on the machine)."""
        evil = order(
            id="evil", card="evil",
            image="ghcr.io/numengames/numinia-hyperfy2:dev",
            domain='x\n    privileged: true\n    volumes: ["/:/host"]\n    y.numen.games',
            limits={"memory": "1g", "cpus": "1", "maxUploadMb": 100},
        )
        p = Book([evil], keys=["evil"]).plan()
        self.assertEqual(p["running"], [])
        self.assertNotIn("privileged", p["compose"])
        self.assertNotIn("/:/host", p["compose"])
        self.assertNotIn("privileged", p["caddyfile"])
        # The same domain with a pinned image is still refused: the domain alone is enough.
        self.assertIsNotNone(R.refusal(order(domain=evil["domain"])))

    def test_a_newline_in_any_field_is_refused(self):
        tails = {
            "id": order(id="plaza\n"),
            "domain": order(domain="plaza.numen.games\n"),
            "image": order(image="ghcr.io/numengames/numinia-hyperfy2:sha-0123456\n"),
            "image inside": order(image="ghcr.io/numengames/x\n    privileged: true\n    y:sha-0123456"),
            "memory": order(limits={"memory": "2g\n    privileged: true"}),
            "cpus": order(limits={"cpus": "1.5\n"}),
            "state": order(state="running\n"),
        }
        for name, o in tails.items():
            with self.subTest(field=name):
                self.assertIsNotNone(R.refusal(o))

    def test_every_field_must_match_its_pattern(self):
        bad = {
            "unpinned image": order(image="ghcr.io/numengames/numinia-hyperfy2:dev"),
            "latest image": order(image="ghcr.io/numengames/numinia-hyperfy2:latest"),
            "another registry": order(image="ghcr.io/evil/numinia-hyperfy2:sha-0123456"),
            "space in image": order(image="ghcr.io/numengames/a b:sha-0123456"),
            "colon in domain": order(domain="plaza.numen.games:8080"),
            "space in domain": order(domain="pla za.numen.games"),
            "brace in domain": order(domain="plaza{.numen.games"),
            "memory word": order(limits={"memory": "lots"}),
            "memory unit": order(limits={"memory": "2gb"}),
            "cpus word": order(limits={"cpus": "all"}),
            "cpus number": order(limits={"cpus": 1.5}),
            "upload text": order(limits={"maxUploadMb": "50"}),
            "upload bool": order(limits={"maxUploadMb": True}),
            "upload zero": order(limits={"maxUploadMb": 0}),
            "upload float": order(limits={"maxUploadMb": 1.5}),
            "unknown state": order(state="paused"),
            "no limits": {k: v for k, v in ORDER.items() if k != "limits"},
            "limits not an object": {**ORDER, "limits": "2g"},
            "reserved id": order(id="caddy", card="caddy"),
        }
        for name, o in bad.items():
            with self.subTest(case=name):
                self.assertIsNotNone(R.refusal(o))

    def test_a_bad_order_never_stops_the_good_ones(self):
        """One malformed order is refused alone; the reconciler does not crash on it."""
        broken = {k: v for k, v in order(id="roto", card="roto").items() if k != "limits"}
        p = Book([order(), broken], keys=["plaza", "roto"]).plan()
        self.assertEqual(p["running"], ["plaza"])

    def test_the_machines_list_of_client_domains_ignores_garbage(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Path(temp) / "domains"
            f.write_text("*.cliente.example\nnot a domain\n*.\n*.*.x.example\nok.example\n", encoding="utf-8")
            self.assertEqual(R.read_allowed_domains(f), {"*.cliente.example", "ok.example"})

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
