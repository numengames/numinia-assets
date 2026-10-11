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
from unittest import mock


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
CODE = "a1b2c3d4e5f6"  # a machine's admin code, as bootstrap.sh makes them


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

    def plan(self, alias="open-1", admin_code=None):
        return R.plan(self.book, self.home, alias, allowed=set(), admin_code=admin_code)


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

    def test_a_running_world_without_keys_on_a_machine_without_admin_code_is_reported_and_not_started(self):
        p = Book([order()], keys=()).plan()
        self.assertEqual(p["keyless"], ["plaza"])
        self.assertEqual(p["new_keys"], [])
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

    def test_the_index_is_the_books_contents_never_an_order(self):
        b = Book([order()], keys=["plaza", "index"])
        (b.book / "open-worlds" / "index.json").write_text('{"orders": ["plaza"]}', encoding="utf-8")
        # Even an index shaped like a running order of this machine is not obeyed.
        (b.book / "open-worlds" / "index.json").write_text(json.dumps(order(id="index", card="index")), encoding="utf-8")
        plan = b.plan()
        self.assertEqual(plan["running"], ["plaza"])
        self.assertNotIn("index", json.dumps(plan))

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

    # --- the engine boots ---------------------------------------------------

    def test_the_engine_gets_every_setting_it_needs_to_boot(self):
        """numinia-hyperfy2's `check envs` block (src/server/index.js, pinned build) throws without these.

        JWT_SECRET is the one it gets from the world's key file instead.
        """
        p = Book([order()], keys=["plaza"]).plan()
        env = json.loads(p["compose"])["services"]["plaza"]["environment"]
        for name in ("WORLD", "SAVE_INTERVAL", "PUBLIC_MAX_UPLOAD_SIZE", "PUBLIC_WS_URL",
                     "PUBLIC_API_URL", "ASSETS", "ASSETS_BASE_URL"):
            with self.subTest(setting=name):
                self.assertTrue(env.get(name))
        self.assertEqual(env["ASSETS_BASE_URL"], "https://plaza.numen.games/assets")
        self.assertEqual(env["SAVE_INTERVAL"], "60")
        self.assertTrue(env["PUBLIC_WS_URL"].startswith("wss://"))
        self.assertNotIn("PUBLIC_ASSETS_URL", env)  # a name this engine never reads
        for key in ("JWT_SECRET", "ADMIN_CODE"):
            self.assertNotIn(key, env)  # keys live in the key file, never in the compose file

    # --- the machine makes the keys ----------------------------------------

    def test_a_running_world_without_keys_gets_them_from_the_machine(self):
        p = Book([order()], keys=()).plan(admin_code=CODE)
        self.assertEqual(p["new_keys"], ["plaza"])
        self.assertEqual(p["keyless"], [])
        self.assertEqual(p["running"], ["plaza"])
        self.assertIn("plaza.numen.games {\n\treverse_proxy plaza:3000\n}", p["caddyfile"])
        self.assertNotIn(CODE, p["compose"])

    def test_a_world_with_keys_or_a_stopped_one_is_never_keyed(self):
        self.assertEqual(Book([order()], keys=["plaza"]).plan(admin_code=CODE)["new_keys"], [])
        self.assertEqual(Book([order(state="stopped")]).plan(admin_code=CODE)["new_keys"], [])

    def test_new_and_keyed_worlds_run_together_in_id_order(self):
        two = [order(), order(id="foro", card="foro", domain="foro.numen.games")]
        p = Book(two, keys=["plaza"]).plan(admin_code=CODE)
        self.assertEqual(p["running"], ["foro", "plaza"])
        self.assertEqual(p["new_keys"], ["foro"])

    def test_an_admin_code_that_is_not_plain_keys_nothing(self):
        for bad in (None, "", "short", "has space 1", "abc12345\nJWT_SECRET=x", "abc$12345", "abc=12345", "abc'1234"):
            with self.subTest(code=bad):
                p = Book([order()]).plan(admin_code=bad)
                self.assertEqual(p["keyless"], ["plaza"])
                self.assertEqual(p["new_keys"], [])
                self.assertEqual(p["running"], [])

    def test_the_machines_admin_code_is_read_plain_or_not_at_all(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Path(temp) / "admin-code"
            self.assertIsNone(R.read_admin_code(f))
            f.write_text(CODE + "\n", encoding="utf-8")
            self.assertEqual(R.read_admin_code(f), CODE)
            for bad in ("", "\n", "short\n", f"{CODE}\nJWT_SECRET=x\n", f"{CODE} more\n", "$(reboot)xx\n"):
                with self.subTest(content=bad):
                    f.write_text(bad, encoding="utf-8")
                    self.assertIsNone(R.read_admin_code(f))

    def test_a_key_file_is_two_plain_lines(self):
        jwt = "0f" * 32
        self.assertEqual(R.world_env(CODE, jwt), f"JWT_SECRET={jwt}\nADMIN_CODE={CODE}\n")
        for code, j in ((CODE + "\nX=1", jwt), (CODE, "x" * 64), (CODE, jwt + "\n"), ("short", jwt), (CODE, "0f")):
            with self.subTest(code=code, jwt=j):
                with self.assertRaises(ValueError):
                    R.world_env(code, j)

    def test_keys_are_written_once_private_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            env = Path(temp) / "env"
            self.assertTrue(R.make_keys(env, "plaza", CODE))
            path = env / "plaza.env"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(env.stat().st_mode & 0o777, 0o700)
            first = path.read_text(encoding="utf-8")
            self.assertRegex(first, r"\AJWT_SECRET=[0-9a-f]{64}\nADMIN_CODE=a1b2c3d4e5f6\n\Z")
            self.assertFalse(R.make_keys(env, "plaza", "zzzzzzzz9"))
            self.assertEqual(path.read_text(encoding="utf-8"), first)

    def test_a_key_file_written_by_hand_is_kept(self):
        b = Book([order()], keys=["plaza"])
        hand = (b.home / "env" / "plaza.env").read_text(encoding="utf-8")
        with mock.patch.object(R, "sh"):
            R.apply(b.plan(admin_code=CODE), b.home, CODE)
        self.assertEqual((b.home / "env" / "plaza.env").read_text(encoding="utf-8"), hand)

    def test_each_world_gets_its_own_jwt_secret_and_the_machines_admin_code(self):
        with tempfile.TemporaryDirectory() as temp:
            env = Path(temp) / "env"
            R.make_keys(env, "plaza", CODE)
            R.make_keys(env, "foro", CODE)
            a, b = ((env / f"{w}.env").read_text(encoding="utf-8").splitlines() for w in ("plaza", "foro"))
            self.assertNotEqual(a[0], b[0])
            self.assertEqual(a[1], b[1])
            self.assertEqual(a[1], f"ADMIN_CODE={CODE}")

    def test_apply_keys_the_new_worlds_before_docker_runs(self):
        b = Book([order()])
        seen = []

        def fake_sh(*cmd):
            seen.append((cmd[:2], (b.home / "env" / "plaza.env").is_file()))

        with mock.patch.object(R, "sh", fake_sh):
            R.apply(b.plan(admin_code=CODE), b.home, CODE)
        self.assertEqual(seen, [(("docker", "compose"), True)])
        self.assertTrue((b.home / "data" / "plaza").is_dir())

    def test_apply_refuses_to_key_without_an_admin_code(self):
        b = Book([order()])
        p = b.plan(admin_code=CODE)
        with mock.patch.object(R, "sh") as fake:
            with self.assertRaises(ValueError):
                R.apply(p, b.home, None)
            fake.assert_not_called()
        self.assertFalse((b.home / "env" / "plaza.env").exists())

    def test_dry_run_names_the_worlds_it_would_key_and_writes_nothing(self):
        b = Book([order()])
        code = b.home / "admin-code"
        code.write_text(CODE + "\n", encoding="utf-8")
        out = subprocess.run(
            [sys.executable, str(MACHINE / "reconcile.py"), "--book", str(b.book), "--home", str(b.home),
             "--alias", "open-1", "--admin-code-file", str(code), "--dry-run"],
            capture_output=True, text=True, check=True,
        )
        self.assertIn('"new_keys": [\n    "plaza"\n  ]', out.stdout)
        self.assertNotIn(CODE, out.stdout + out.stderr)
        self.assertFalse((b.home / "env" / "plaza.env").exists())

    # --- the bootstrap ------------------------------------------------------

    def test_bootstrap_is_valid_bash(self):
        self.assertEqual(subprocess.run(["bash", "-n", str(MACHINE / "bootstrap.sh")]).returncode, 0)

    def test_bootstrap_clears_a_stray_debian_docker_source_before_the_first_update(self):
        text = (MACHINE / "bootstrap.sh").read_text()
        self.assertLess(text.index("rm -f /etc/apt/sources.list.d/docker.list"), text.index("apt-get update"))

    def test_bootstrap_makes_the_admin_code_once_privately_and_before_the_first_run(self):
        text = (MACHINE / "bootstrap.sh").read_text()
        self.assertIn("if [[ ! -s /etc/fleet/admin-code ]]", text)
        self.assertIn("umask 077", text)
        self.assertIn("chmod 600 /etc/fleet/admin-code", text)
        self.assertLess(text.index("/etc/fleet/admin-code"), text.index("== first run"))

    def test_bootstrap_runs_the_reconciler_once_before_the_timer_and_survives_a_failed_run(self):
        text = (MACHINE / "bootstrap.sh").read_text()
        self.assertLess(text.index("if ! /usr/local/sbin/fleet-reconcile"), text.index("enable --now fleet-reconcile.timer"))


if __name__ == "__main__":
    unittest.main()
