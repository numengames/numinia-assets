# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""The fleet machine's reconciler keeps to its alias, its keys and the order's shape."""

from pathlib import Path
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MACHINE = ROOT / "open-worlds" / "machine"


def load_reconcile():
    if str(MACHINE) not in sys.path:
        sys.path.insert(0, str(MACHINE))
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
# A world's site in the Caddyfile: its connections stay open across a reload.
PLAZA_SITE = "plaza.numen.games {\n\treverse_proxy plaza:3000 {\n\t\tstream_close_delay 12h\n\t}\n}"


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
        self.orders = root / "orders"
        self.home = root / "home"
        self.orders.mkdir(parents=True)
        (self.home / "env").mkdir(parents=True)
        for o in orders:
            (self.orders / f"{o['id']}.json").write_text(json.dumps(o), encoding="utf-8")
        for wid in keys:
            (self.home / "env" / f"{wid}.env").write_text("JWT_SECRET=x\nADMIN_CODE=y\n", encoding="utf-8")

    def plan(self, alias="open-1", admin_code=None):
        return R.plan(self.orders, self.home, alias, allowed=set(), admin_code=admin_code)


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

    def test_the_programs_are_installed_not_run_from_the_clone(self):
        text = (MACHINE / "bootstrap.sh").read_text()
        self.assertIn('install -m 0644 "$BOOK_DIR/open-worlds/machine/$program" "$LIB_DIR/$program"', text)
        self.assertIn('install -m 0755 "$tmp" "/usr/local/sbin/$1"', text)
        self.assertIn("ExecStart=/usr/local/sbin/fleet-reconcile", text)
        self.assertIn("ExecStart=/usr/local/sbin/fleetd", text)
        self.assertNotIn("ExecStart=/usr/bin/python3", text)
        for line in text.splitlines():
            if "ExecStart=" in line or "exec /usr/bin/python3" in line:
                with self.subTest(line=line):
                    self.assertNotIn("BOOK_DIR", line)
                    self.assertNotIn("/srv/fleet/book", line)

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
        self.assertIn(PLAZA_SITE, p["caddyfile"])

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
        (b.orders / "roto.json").write_text("{not json", encoding="utf-8")
        (b.orders / "ajeno.json").write_text(json.dumps(order(id="otro")), encoding="utf-8")
        self.assertEqual(b.plan()["running"], ["plaza"])

    def test_the_index_is_the_books_contents_never_an_order(self):
        b = Book([order()], keys=["plaza", "index"])
        (b.orders / "index.json").write_text('{"orders": ["plaza"]}', encoding="utf-8")
        # Even an index shaped like a running order of this machine is not obeyed.
        (b.orders / "index.json").write_text(json.dumps(order(id="index", card="index")), encoding="utf-8")
        plan = b.plan()
        self.assertEqual(plan["running"], ["plaza"])
        self.assertNotIn("index", json.dumps(plan))

    def test_dry_run_prints_the_plan_and_touches_nothing(self):
        b = Book([order()], keys=["plaza"])
        out = subprocess.run(
            [sys.executable, str(MACHINE / "reconcile.py"), "--orders", str(b.orders), "--home", str(b.home),
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
        self.assertEqual(env["SAVE_INTERVAL"], "10")
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
        self.assertIn(PLAZA_SITE, p["caddyfile"])
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
        self.assertTrue(seen)
        self.assertEqual(seen[0], (("docker", "compose"), True))
        self.assertTrue((b.home / "data" / "plaza").is_dir())

    # --- Caddy learns about new sites ---------------------------------------

    def caddy_calls(self, b, p, fail=False):
        """apply() with a fake shell; the commands it ran, in order."""
        calls = []

        def fake_sh(*cmd):
            calls.append(cmd)
            if fail and "reload" in cmd:
                raise subprocess.CalledProcessError(1, cmd)

        with mock.patch.object(R, "sh", fake_sh):
            R.apply(p, b.home, CODE)
        return calls

    def test_a_new_site_makes_caddy_reload_after_the_containers_are_up(self):
        b = Book([order()], keys=["plaza"])
        calls = self.caddy_calls(b, b.plan(admin_code=CODE))
        self.assertEqual(len(calls), 2)
        self.assertIn("up", calls[0])
        compose = str(b.home / "run" / "docker-compose.yml")
        self.assertEqual(
            calls[1],
            ("docker", "compose", "-f", compose, "exec", "-T", "caddy",
             "caddy", "reload", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"),
        )
        self.assertEqual(
            (b.home / "run" / "Caddyfile.applied").read_text(encoding="utf-8"),
            (b.home / "run" / "Caddyfile").read_text(encoding="utf-8"),
        )

    def test_caddy_is_not_reloaded_when_its_sites_did_not_change(self):
        b = Book([order()], keys=["plaza"])
        self.caddy_calls(b, b.plan(admin_code=CODE))
        again = self.caddy_calls(b, b.plan(admin_code=CODE))
        self.assertEqual(len(again), 1)
        self.assertIn("up", again[0])

    def test_a_world_added_later_reaches_caddy(self):
        """What happened on open-1: Caddy started with no site, then a world came."""
        b = Book([], keys=["plaza"])
        self.caddy_calls(b, b.plan(admin_code=CODE))
        (b.orders / "plaza.json").write_text(json.dumps(order()), encoding="utf-8")
        calls = self.caddy_calls(b, b.plan(admin_code=CODE))
        self.assertIn("reload", calls[-1])
        self.assertIn("plaza.numen.games", (b.home / "run" / "Caddyfile.applied").read_text(encoding="utf-8"))

    def test_a_failed_reload_is_reported_and_tried_again_on_the_next_run(self):
        b = Book([order()], keys=["plaza"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.caddy_calls(b, b.plan(admin_code=CODE), fail=True)
        self.assertIn("next run tries again", err.getvalue())
        self.assertFalse((b.home / "run" / "Caddyfile.applied").exists())
        calls = self.caddy_calls(b, b.plan(admin_code=CODE))
        self.assertIn("reload", calls[-1])

    def test_a_caddy_started_before_this_check_is_put_right_on_the_first_run(self):
        """The Caddyfile already on disk is no proof Caddy read it: only the applied copy is."""
        b = Book([order()], keys=["plaza"])
        p = b.plan(admin_code=CODE)
        (b.home / "run").mkdir(parents=True)
        (b.home / "run" / "Caddyfile").write_text(p["caddyfile"], encoding="utf-8")
        calls = self.caddy_calls(b, p)
        self.assertIn("reload", calls[-1])

    def test_caddy_needs_reload_reads_only_the_applied_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            applied = Path(temp) / "Caddyfile.applied"
            self.assertTrue(R.caddy_needs_reload("a\n", applied))
            applied.write_text("a\n", encoding="utf-8")
            self.assertFalse(R.caddy_needs_reload("a\n", applied))
            self.assertTrue(R.caddy_needs_reload("b\n", applied))

    # --- nothing is lost when a world stops; nobody is thrown out -------------

    def test_every_world_site_keeps_its_visitors_connected_across_a_reload(self):
        """A reload replaces Caddy's whole configuration and, by default, closes every WebSocket on it."""
        two = [order(), order(id="foro", card="foro", domain="foro.numen.games")]
        p = Book(two, keys=["plaza", "foro"]).plan()
        self.assertIn(PLAZA_SITE, p["caddyfile"])
        self.assertEqual(p["caddyfile"].count("stream_close_delay 12h"), 2)

    def test_no_command_the_reconciler_runs_reads_its_input(self):
        """Piped into bash, the bootstrap's own text is that input: a child reading it ends the script."""
        with mock.patch.object(R.subprocess, "run") as run:
            R.sh("docker", "compose", "version")
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(run.call_args.kwargs["check"])

    def apply_events(self, b, p, before=None):
        """apply() with a fake shell and a fake clock; what happened, in order."""
        events = []
        if before is not None:
            (b.home / "run").mkdir(parents=True, exist_ok=True)
            (b.home / "run" / "docker-compose.applied.yml").write_text(json.dumps(before), encoding="utf-8")
        with mock.patch.object(R, "sh", lambda *cmd: events.append(("sh", cmd[4] if len(cmd) > 4 else cmd))):
            with contextlib.redirect_stderr(io.StringIO()):
                R.apply(p, b.home, CODE, sleep=lambda s: events.append(("sleep", s)))
        return events

    def test_stopping_a_running_world_waits_for_its_next_save_first(self):
        """The engine writes objects only on its save timer, and not when it is told to stop."""
        b = Book([order()], keys=["plaza"])
        self.assertNotIn("sleep", [e[0] for e in self.apply_events(b, b.plan(admin_code=CODE))])
        (b.orders / "plaza.json").write_text(json.dumps(order(state="stopped")), encoding="utf-8")
        events = self.apply_events(b, b.plan(admin_code=CODE))
        self.assertEqual(events[0], ("sleep", 12))  # SAVE_INTERVAL 10 + 2 for the save itself
        self.assertEqual(events[1], ("sh", "up"))

    def test_a_world_started_with_the_old_interval_waits_as_long_as_that(self):
        """open-1's worlds run with 60 s: the first run of this version recreates them, after their save."""
        b = Book([order()], keys=["plaza"])
        p = b.plan(admin_code=CODE)
        old = json.loads(p["compose"])
        old["services"]["plaza"]["environment"]["SAVE_INTERVAL"] = "60"
        events = self.apply_events(b, p, before=old)
        self.assertEqual(events[0], ("sleep", 62))

    def test_creating_starting_deleting_or_changing_nothing_waits_for_nothing(self):
        b = Book([order()], keys=["plaza"])
        self.assertNotIn("sleep", [e[0] for e in self.apply_events(b, b.plan(admin_code=CODE))])
        self.assertNotIn("sleep", [e[0] for e in self.apply_events(b, b.plan(admin_code=CODE))])
        (b.orders / "plaza.json").unlink()  # deleted: its data goes, there is nothing to keep
        self.assertNotIn("sleep", [e[0] for e in self.apply_events(b, b.plan(admin_code=CODE))])

    def test_a_machine_set_up_before_the_applied_copy_reads_the_last_compose_file(self):
        b = Book([order()], keys=["plaza"])
        p = b.plan(admin_code=CODE)
        old = json.loads(p["compose"])
        old["services"]["plaza"]["environment"]["SAVE_INTERVAL"] = "60"
        (b.home / "run").mkdir(parents=True)
        (b.home / "run" / "docker-compose.yml").write_text(json.dumps(old), encoding="utf-8")
        self.assertEqual(self.apply_events(b, p)[0], ("sleep", 62))
        self.assertEqual((b.home / "run" / "docker-compose.applied.yml").read_text(encoding="utf-8"), p["compose"])

    def test_save_waits_ignore_caddy_garbage_and_unknown_intervals(self):
        after = {"services": {"caddy": {"image": "caddy:2"}, "plaza": {"image": "b"}}}
        before = {"services": {"caddy": {"image": "caddy:1"}, "plaza": {"image": "a"}, "foro": "garbage"}}
        self.assertEqual(R.save_waits(before, after, {"plaza", "foro"}), {"plaza": 62})
        self.assertEqual(R.save_waits("not a compose file", after, {"plaza"}), {})
        self.assertEqual(R.save_waits(None, after, {"plaza"}), {})
        self.assertEqual(R.save_pause("10"), 12)
        self.assertEqual(R.save_pause("ten"), 62)

    def test_a_copy_of_a_running_world_waits_for_its_next_save(self):
        b = Book([order(), order(id="foro", card="foro", domain="foro.numen.games", state="stopped")])
        queue = b.home / "run" / "copy-queue.json"
        queue.parent.mkdir(parents=True)
        self.assertEqual(R.copies_wait(b.home, b.orders), 0)
        queue.write_text(json.dumps([{"world": "foro"}]), encoding="utf-8")
        self.assertEqual(R.copies_wait(b.home, b.orders), 0)
        queue.write_text(json.dumps([{"world": "foro"}, {"world": "plaza"}, "garbage"]), encoding="utf-8")
        self.assertEqual(R.copies_wait(b.home, b.orders), 12)

    def run_main(self, b, **patches):
        signers = b.home / "signers"
        signers.write_text("", encoding="utf-8")
        argv = ["--orders", str(b.orders), "--home", str(b.home), "--alias", "open-1",
                "--admin-code-file", str(b.home / "none"), "--signers-file", str(signers),
                "--control-host-file", str(b.home / "none"), "--codes-dir", str(b.home / "codes")]
        with contextlib.ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(R, name, value))
            err = io.StringIO()
            stack.enter_context(contextlib.redirect_stderr(err))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            try:
                return R.main(argv)
            finally:
                self.main_err = err.getvalue()

    def test_every_run_says_when_it_ran_and_how_it_ended(self):
        b = Book([])
        self.assertEqual(self.run_main(b, sh=lambda *cmd: None), 0)
        last = json.loads((b.home / "run" / "last-run.json").read_text(encoding="utf-8"))
        self.assertEqual((last["ok"], last["rule"]), (True, None))
        self.assertRegex(last["at"], r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")

        def refuse(*cmd):
            raise subprocess.CalledProcessError(1, cmd)

        self.assertEqual(self.run_main(b, sh=refuse), 1)
        last = json.loads((b.home / "run" / "last-run.json").read_text(encoding="utf-8"))
        self.assertEqual((last["ok"], last["rule"]), (False, "docker did not take the book"))

    def test_a_run_that_breaks_still_says_so(self):
        b = Book([])

        def broken(*args, **kwargs):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            self.run_main(b, take_requests=broken)
        last = json.loads((b.home / "run" / "last-run.json").read_text(encoding="utf-8"))
        self.assertEqual((last["ok"], last["rule"]), (False, "the run stopped on an unexpected error"))
        self.assertNotIn("boom", json.dumps(last))  # a rule, never what broke

    def test_every_run_first_makes_each_code_file_say_what_its_key_file_gives(self):
        """How the worlds keyed before each had its own code keep theirs: the first run copies it."""
        b = Book([order()])
        (b.home / "env" / "plaza.env").write_text("JWT_SECRET=" + "0f" * 32 + f"\nADMIN_CODE={CODE}\n",
                                                  encoding="utf-8")
        self.assertEqual(self.run_main(b, sh=lambda *cmd: None), 0)
        self.assertEqual((b.home / "codes" / "plaza").read_text(encoding="utf-8"), CODE + "\n")

    def test_code_files_that_cannot_be_written_do_not_stop_the_run(self):
        b = Book([order()])
        (b.home / "env" / "plaza.env").write_text("JWT_SECRET=" + "0f" * 32 + f"\nADMIN_CODE={CODE}\n",
                                                  encoding="utf-8")
        (b.home / "codes").write_text("a file where the folder should be", encoding="utf-8")
        self.assertEqual(self.run_main(b, sh=lambda *cmd: None), 0)
        self.assertIn("could not be made equal", self.main_err)

    # --- a world whose admin code changed is restarted, alone, after its save ----

    def restart_calls(self, b, p, fail=False):
        """apply() with a fake shell and a fake clock: the commands it ran and the waits."""
        calls, sleeps = [], []

        def fake_sh(*cmd):
            calls.append(cmd)
            if fail and "--force-recreate" in cmd:
                raise subprocess.CalledProcessError(1, cmd)

        with mock.patch.object(R, "sh", fake_sh), contextlib.redirect_stderr(io.StringIO()):
            R.apply(p, b.home, CODE, sleep=sleeps.append)
        return calls, sleeps

    def test_a_world_whose_code_changed_is_restarted_alone_after_its_next_save(self):
        b = Book([order(), order(id="foro", card="foro", domain="foro.numen.games")], keys=["plaza", "foro"])
        p = b.plan(admin_code=CODE)
        self.restart_calls(b, p)  # both running, as Docker last took them
        R.queue_restart(b.home, "plaza")
        calls, sleeps = self.restart_calls(b, p)
        compose = str(b.home / "run" / "docker-compose.yml")
        self.assertEqual(sleeps, [12])  # SAVE_INTERVAL 10 + 2: what was placed in it is saved first
        self.assertEqual(calls[0], ("docker", "compose", "-f", compose, "up", "-d", "--force-recreate", "--no-deps",
                                    "plaza"))
        self.assertEqual(calls[1], ("docker", "compose", "-f", compose, "up", "-d", "--remove-orphans"))
        self.assertEqual(len(calls), 2)  # foro is not touched, Caddy is not reloaded
        self.assertEqual(R.read_restarts(b.home), [])
        self.assertEqual(self.restart_calls(b, p), ([("docker", "compose", "-f", compose, "up", "-d",
                                                      "--remove-orphans")], []))  # once

    def test_a_restart_that_fails_is_tried_again_on_the_next_run(self):
        b = Book([order()], keys=["plaza"])
        p = b.plan(admin_code=CODE)
        self.restart_calls(b, p)
        R.queue_restart(b.home, "plaza")
        with self.assertRaises(subprocess.CalledProcessError):
            self.restart_calls(b, p, fail=True)
        self.assertEqual(R.read_restarts(b.home), ["plaza"])
        calls, _ = self.restart_calls(b, p)
        self.assertIn("--force-recreate", calls[0])

    def test_a_world_stopped_deleted_or_started_needs_no_restart_for_its_code(self):
        """Its container goes, or is made anew from its new key file."""
        for before, after in (("running", "stopped"), ("running", None), ("stopped", "running")):
            with self.subTest(before=before, after=after):
                b = Book([order(state=before)], keys=["plaza"])
                self.restart_calls(b, b.plan(admin_code=CODE))
                if after is None:
                    (b.orders / "plaza.json").unlink()
                else:
                    (b.orders / "plaza.json").write_text(json.dumps(order(state=after)), encoding="utf-8")
                R.queue_restart(b.home, "plaza")
                calls, _ = self.restart_calls(b, b.plan(admin_code=CODE))
                self.assertNotIn("--force-recreate", [part for call in calls for part in call])
                self.assertEqual(R.read_restarts(b.home), [])

    def test_the_restart_queue_holds_world_names_only(self):
        b = Book([])
        (b.home / "run").mkdir(parents=True)
        queue = b.home / "run" / "restart.json"
        queue.write_text(json.dumps(["plaza", "../etc", "caddy", 5, "foro", "plaza"]), encoding="utf-8")
        self.assertEqual(R.read_restarts(b.home), ["caddy", "foro", "plaza"])
        queue.write_text('{"plaza": true}', encoding="utf-8")
        self.assertEqual(R.read_restarts(b.home), [])
        before = {"services": {"caddy": {}, "plaza": {}, "foro": "garbage"}}
        after = {"services": {"caddy": {}, "plaza": {}, "foro": {}}}
        self.assertEqual(R.restarting(before, after, ["caddy", "foro", "plaza", "gone"]), ["plaza"])
        self.assertEqual(R.restarting(None, after, ["plaza"]), [])

    def test_an_adopted_world_keyed_with_the_old_code_gets_its_code_file_at_once(self):
        b = Book([order()])
        codes = b.home / "codes"
        with mock.patch.object(R, "sh"):
            R.apply(b.plan(admin_code=CODE), b.home, CODE, codes_dir=codes)
        self.assertEqual((codes / "plaza").read_text(encoding="utf-8"), CODE + "\n")
        c = Book([order(id="foro", card="foro", domain="foro.numen.games")])
        (c.home / "codes").write_text("a file where the folder should be", encoding="utf-8")
        err = io.StringIO()
        with mock.patch.object(R, "sh"), contextlib.redirect_stderr(err):
            R.apply(c.plan(admin_code=CODE), c.home, CODE, codes_dir=c.home / "codes")
        self.assertIn("next run tries again", err.getvalue())
        self.assertTrue((c.home / "env" / "foro.env").is_file())  # the world still gets its keys

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
            [sys.executable, str(MACHINE / "reconcile.py"), "--orders", str(b.orders), "--home", str(b.home),
             "--alias", "open-1", "--admin-code-file", str(code), "--dry-run"],
            capture_output=True, text=True, check=True,
        )
        self.assertIn('"new_keys": [\n    "plaza"\n  ]', out.stdout)
        self.assertNotIn(CODE, out.stdout + out.stderr)
        self.assertFalse((b.home / "env" / "plaza.env").exists())

    # --- the bootstrap ------------------------------------------------------

    def test_bootstrap_is_valid_bash(self):
        self.assertEqual(subprocess.run(["bash", "-n", str(MACHINE / "bootstrap.sh")]).returncode, 0)

    def test_bootstrap_is_read_whole_before_its_first_step_runs(self):
        """Piped into bash, a script runs as it is read; a child reading stdin then ate the rest of it.

        Everything sits in functions and the last line calls main: outside them
        there is only `set` (heredocs sit inside main, and bash reads them whole).
        """
        top, inside, heredoc = [], False, False
        for line in (MACHINE / "bootstrap.sh").read_text().splitlines():
            if heredoc:
                heredoc = line != "EOF"
                continue
            heredoc = "<<EOF" in line
            if inside:
                inside = line != "}"  # a function's body ends at a brace alone in the first column
            elif line.strip() and not line.startswith("#"):
                top.append(line)
                inside = line.endswith("() {")
        self.assertEqual(top, ["set -euo pipefail", "main() {", "wrapper() {", 'main "$@"'])

    def test_bootstrap_ends_by_checking_that_the_machine_obeys(self):
        """What the 12 October run lacked: it stopped before the timer started, and said nothing."""
        text = (MACHINE / "bootstrap.sh").read_text()
        check = text.index("== does the machine obey?")
        self.assertLess(text.index("enable --now fleet-reconcile.timer fleet-reconcile.path"), check)
        self.assertIn("for unit in fleetd.service fleet-reconcile.timer fleet-reconcile.path; do", text[check:])
        self.assertIn('systemctl is-active --quiet "$unit"', text[check:])
        self.assertIn("exit 1", text[check:])

    def test_bootstrap_clears_a_stray_debian_docker_source_before_the_first_update(self):
        text = (MACHINE / "bootstrap.sh").read_text()
        self.assertLess(text.index("rm -f /etc/apt/sources.list.d/docker.list"), text.index("apt-get update"))

    def test_bootstrap_makes_the_admin_code_once_privately_and_before_the_first_run(self):
        text = (MACHINE / "bootstrap.sh").read_text()
        self.assertIn("if [[ ! -s /etc/fleet/admin-code ]]", text)
        self.assertIn("umask 077", text)
        # Root writes it; the door's group may read it, to show it to a signed Oracle.
        self.assertIn("chown root:fleetd /etc/fleet/admin-code", text)
        self.assertIn("chmod 640 /etc/fleet/admin-code", text)
        self.assertLess(text.index("/etc/fleet/admin-code"), text.index("== first run"))

    def test_bootstrap_makes_the_folder_of_each_worlds_code_for_the_door_alone(self):
        text = (MACHINE / "bootstrap.sh").read_text()
        for line in ("mkdir -p /etc/fleet/codes", "chown root:fleetd /etc/fleet/codes", "chmod 750 /etc/fleet/codes"):
            with self.subTest(line=line):
                self.assertIn(line, text)
        self.assertLess(text.index("useradd --system"), text.index("chown root:fleetd /etc/fleet/codes"))
        self.assertLess(text.index("mkdir -p /etc/fleet/codes"), text.index("== first run"))
        self.assertEqual(R.CODES_DIR, Path("/etc/fleet/codes"))

    def test_bootstrap_never_prints_a_code(self):
        """A code on the screen stays in the terminal's history; the room shows it to a signed Oracle."""
        text = (MACHINE / "bootstrap.sh").read_text()
        self.assertNotIn("cat /etc/fleet/admin-code", text)
        self.assertNotIn("cat /etc/fleet/codes", text)

    def test_bootstrap_installs_exactly_the_commit_it_is_given(self):
        text = (MACHINE / "bootstrap.sh").read_text()
        self.assertIn('FLEET_REF="${FLEET_REF:-}"', text)
        self.assertIn('git -C "$BOOK_DIR" fetch --quiet --depth 1 origin "${FLEET_REF:-main}"', text)
        self.assertIn('git -C "$BOOK_DIR" checkout --quiet --force --detach FETCH_HEAD', text)
        self.assertNotIn("pull --ff-only", text)  # a branch moves under the machine
        # The commit checked out is the one asked for, or nothing is installed.
        self.assertIn('if [[ -n "$FLEET_REF" && "$RECIPE" != "$FLEET_REF" ]]; then', text)
        self.assertLess(text.index("checkout --quiet --force --detach FETCH_HEAD"),
                        text.index('install -m 0644 "$BOOK_DIR/open-worlds/machine/$program"'))
        # Which commit runs here, for the door to say (`recipe`), once the programs came from it.
        self.assertLess(text.index("wrapper fleetd fleetd.py"), text.index('echo "$RECIPE" > /etc/fleet/recipe'))
        self.assertIn("chmod 644 /etc/fleet/recipe", text)
        # The line it documents is the pinned one.
        self.assertIn("numinia-assets/<commit>/open-worlds/machine/bootstrap.sh", text)
        self.assertIn("sudo FLEET_REF=<commit> bash -s -- open-1 0xYourWallet", text)
        self.assertIn("NOT pinned", text)

    def test_bootstrap_refuses_a_ref_that_is_not_a_whole_commit(self):
        """Checked before anything else, so it fails the same as root or not, and touches nothing."""
        for ref in ("main", "abc1234", "A" * 40, "0" * 39, "0" * 41, "0" * 40 + "\n", "0" * 40 + ";reboot", " " + "0" * 40):
            with self.subTest(ref=ref):
                run = subprocess.run(["bash", str(MACHINE / "bootstrap.sh"), "open-1"], capture_output=True, text=True,
                                     env={**os.environ, "FLEET_REF": ref})
                self.assertEqual(run.returncode, 2)
                self.assertIn("FLEET_REF IS NOT A COMMIT", run.stderr)
        if subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip() != "0":
            # A whole commit passes this check; a user who is not root is stopped at the next one.
            run = subprocess.run(["bash", str(MACHINE / "bootstrap.sh"), "open-1"], capture_output=True, text=True,
                                 env={**os.environ, "FLEET_REF": "0123456789abcdef0123456789abcdef01234567"})
            self.assertEqual(run.returncode, 2)
            self.assertNotIn("FLEET_REF", run.stderr)
            self.assertIn("root", run.stderr)

    def test_bootstrap_runs_the_reconciler_once_before_the_timer_and_survives_a_failed_run(self):
        text = (MACHINE / "bootstrap.sh").read_text()
        self.assertLess(text.index("if ! /usr/local/sbin/fleet-reconcile"), text.index("enable --now fleet-reconcile.timer"))

    def test_bootstrap_pauses_the_old_reconciler_and_adopts_the_book_before_any_new_run(self):
        """Else the new reconciler, with an empty book, would take the running worlds down."""
        text = (MACHINE / "bootstrap.sh").read_text()
        pause = text.index("systemctl stop fleet-reconcile.timer")
        self.assertLess(pause, text.index('wrapper fleet-reconcile reconcile.py'))
        self.assertLess(text.index("--adopt-book"), text.index("== first run"))
        self.assertLess(text.index("--adopt-book"), text.index("enable --now fleet-reconcile.timer"))

    def test_bootstrap_takes_the_oracles_wallets_and_refuses_anything_else(self):
        text = (MACHINE / "bootstrap.sh").read_text()
        self.assertIn('SIGNERS="${2:-}"', text)
        self.assertIn("> /etc/fleet/signers", text)
        bad = subprocess.run(["bash", str(MACHINE / "bootstrap.sh"), "open-1", "0x123"], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)
        self.assertIn("usage", bad.stderr)
        smuggled = "0x" + "a" * 40 + ";reboot"
        bad = subprocess.run(["bash", str(MACHINE / "bootstrap.sh"), "open-1", smuggled], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)

    def test_the_door_runs_as_its_own_user_with_no_network_and_its_own_folders(self):
        text = (MACHINE / "bootstrap.sh").read_text()
        unit = text[text.index("fleetd.service <<EOF"):]
        unit = unit[: unit.index("\nEOF")]
        for line in ("User=fleetd", "NoNewPrivileges=yes", "ProtectSystem=strict", "RestrictAddressFamilies=AF_UNIX",
                     "ReadWritePaths=$HOME_DIR/inbox $HOME_DIR/sock $HOME_DIR/uploads"):
            with self.subTest(line=line):
                self.assertIn(line, unit)
        self.assertIn("python3-ecdsa", text)
        self.assertIn("DirectoryNotEmpty=$HOME_DIR/inbox", text)
        # A world's zip arrives in the door's folder; it is unpacked in root's alone.
        self.assertIn('chown fleetd:fleetd "$HOME_DIR/inbox" "$HOME_DIR/sock" "$HOME_DIR/uploads"', text)
        self.assertIn('chmod 750 "$HOME_DIR/inbox" "$HOME_DIR/uploads"', text)
        self.assertIn('chmod 700 "$HOME_DIR/staging"', text)

    # --- the door's address --------------------------------------------------

    def test_the_door_is_a_site_of_its_own_and_reaches_only_the_socket(self):
        b = Book([order()], keys=["plaza"])
        p = R.plan(b.orders, b.home, "open-1", allowed=set(), admin_code=CODE, control_host="open-1.numinia.com")
        door = "\t\treverse_proxy unix//run/fleetd/fleetd.sock\n"
        # A world's zip on its own route, up to the import cap; everything else 64 KB at most.
        self.assertIn("open-1.numinia.com {\n"
                      "\thandle /fleet/v1/uploads/* {\n\t\trequest_body {\n\t\t\tmax_size 2147483648\n\t\t}\n"
                      + door + "\t}\n"
                      "\thandle {\n\t\trequest_body {\n\t\t\tmax_size 64KB\n\t\t}\n" + door + "\t}\n}",
                      p["caddyfile"])
        self.assertEqual(p["caddyfile"].count("max_size"), 2)
        caddy = json.loads(p["compose"])["services"]["caddy"]
        self.assertIn(f"{b.home}/sock:/run/fleetd", caddy["volumes"])
        self.assertNotIn("open-1.numinia.com", p["compose"])

    def test_a_world_may_not_take_the_doors_address(self):
        b = Book([order(domain="open-1.numinia.com")], keys=["plaza"])
        p = R.plan(b.orders, b.home, "open-1", allowed=set(), admin_code=CODE, control_host="open-1.numinia.com")
        self.assertEqual(p["running"], [])
        self.assertEqual(p["caddyfile"].count("open-1.numinia.com {"), 1)

    def test_the_control_host_and_the_signers_are_read_plain(self):
        with tempfile.TemporaryDirectory() as temp:
            host = Path(temp) / "control-host"
            self.assertIsNone(R.read_control_host(host))
            host.write_text("Open-1.Numinia.com\n", encoding="utf-8")
            self.assertEqual(R.read_control_host(host), "open-1.numinia.com")
            host.write_text("x.numinia.com\n    privileged: true\n", encoding="utf-8")
            self.assertIsNone(R.read_control_host(host))
            signers = Path(temp) / "signers"
            self.assertEqual(R.read_signers(signers), frozenset())
            signers.write_text("# Oracles\n0x" + "A" * 40 + "  # Pablo\nnot a wallet\n0x123\n", encoding="utf-8")
            self.assertEqual(R.read_signers(signers), frozenset({"0x" + "a" * 40}))


if __name__ == "__main__":
    unittest.main()
