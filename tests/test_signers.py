# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""The machine follows the house's signed list of Oracles and Archons (signers.py).

The records are the real EIP-712 signatures in fleet_vectors.json (key
`fleetSigners`), made by viem with a fixed, public test key. The network and
the clock are fakes; nothing leaves the machine running the tests.
"""

from pathlib import Path
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MACHINE = ROOT / "open-worlds" / "machine"
VECTORS = json.loads((Path(__file__).parent / "fleet_vectors.json").read_text(encoding="utf-8"))
FS = VECTORS["fleetSigners"]
HOUSE = FS["signer"]
MAIN = FS["records"]["main"]["wire"]
FEWER = FS["records"]["fewer"]["wire"]
OLDER = FS["records"]["older"]["wire"]
NOW = float(MAIN["message"]["issuedAt"]) + 60  # a minute after the house signed `main`
A, B, C = MAIN["message"]["signers"]
HAND = "0x" + "d4" * 20


def load(name):
    if str(MACHINE) not in sys.path:
        sys.path.insert(0, str(MACHINE))
    spec = importlib.util.spec_from_file_location(name, MACHINE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


R = load("reconcile")
W = load("walletsig")
S = load("signers")
REL = load("release")


def signed(message, record=MAIN):
    """A record whose message is changed but whose signature is the house's original."""
    return {"message": message, "signature": record["signature"]}


class Vectors(unittest.TestCase):
    def test_the_vector_is_what_viem_signed(self):
        for name, record in FS["records"].items():
            with self.subTest(record=name):
                self.assertEqual(",".join(record["wire"]["message"]["signers"]), record["signed"]["signers"])
                digest = W.typed_digest("FleetSigners", record["signed"])
                self.assertEqual("0x" + digest.hex(), record["digest"])
                self.assertEqual(W.signer_of("FleetSigners", record["signed"], record["wire"]["signature"]), HOUSE)

    def test_the_vector_is_listed_with_the_others(self):
        listed = [v for v in VECTORS["vectors"] if v["type"] == "FleetSigners"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["signer"], HOUSE)

    def test_the_type_is_the_contract(self):
        self.assertEqual(
            W.ACTIONS["FleetSigners"], (("signers", "string"), ("seq", "uint256"), ("issuedAt", "uint256"))
        )

    def test_it_is_not_a_request_a_door_can_take(self):
        message = {"signers": A, "seq": 1, "issuedAt": 1}
        self.assertEqual(R.request_problem("FleetSigners", message), "type is not a request this machine knows")
        self.assertEqual(
            R.request_problem("FleetRelease", {"engine": "x", "image": "", "commit": "", "seq": 1, "issuedAt": 1}),
            "type is not a request this machine knows",
        )


class Verify(unittest.TestCase):
    def check(self, raw, now=NOW, seen=0, house=HOUSE):
        return S.verify(raw, now, seen, house)

    def test_a_valid_list_is_accepted(self):
        record, why = self.check(MAIN)
        self.assertEqual(why, "ok")
        self.assertEqual(record, {"signers": [A, B, C], "seq": 1893456000, "issuedAt": 1893456000})

    def test_off_while_the_house_signer_is_empty(self):
        self.assertEqual(S.HOUSE_SIGNER, "")  # shipped off: the house wallet does not exist yet
        self.assertEqual(self.check(MAIN, house=""), (None, "off"))
        self.assertEqual(self.check(MAIN, house="not-an-address"), (None, "off"))

    def test_a_wrong_signer_is_refused(self):
        other = VECTORS["stranger"]["signer"]
        self.assertEqual(self.check(MAIN, house=other), (None, "not signed by the house"))

    def test_an_oracle_of_the_machine_is_not_the_house(self):
        oracle = VECTORS["vectors"][0]["signer"]
        self.assertEqual(self.check(MAIN, house=oracle)[1], "not signed by the house")

    def test_a_changed_list_breaks_the_signature(self):
        message = {**MAIN["message"], "signers": [A, B, HAND]}
        self.assertEqual(self.check(signed(message))[1], "not signed by the house")
        for field in ("seq", "issuedAt"):
            with self.subTest(field=field):
                changed = {**MAIN["message"], field: MAIN["message"][field] + 1}
                self.assertEqual(self.check(signed(changed))[1], "not signed by the house")

    def test_a_stale_seq_is_refused(self):
        seq = MAIN["message"]["seq"]
        self.assertEqual(self.check(MAIN, seen=seq - 1)[1], "ok")
        self.assertEqual(self.check(MAIN, seen=seq)[1], "seq is not above the last one taken")
        self.assertEqual(self.check(MAIN, seen=seq + 1)[1], "seq is not above the last one taken")

    def test_a_list_too_old_is_refused(self):
        issued = MAIN["message"]["issuedAt"]
        self.assertEqual(self.check(MAIN, now=issued + 24 * 3600)[1], "ok")  # exactly 24 h is still fine
        self.assertEqual(self.check(MAIN, now=issued + 24 * 3600 + 1), (None, "too old"))

    def test_a_list_from_the_future_is_refused(self):
        issued = MAIN["message"]["issuedAt"]
        self.assertEqual(self.check(MAIN, now=issued - 300)[1], "ok")  # exactly 5 minutes ahead is still fine
        self.assertEqual(self.check(MAIN, now=issued - 301), (None, "from the future"))

    def test_an_empty_list_is_refused(self):
        self.assertEqual(self.check(signed({**MAIN["message"], "signers": []}))[1], "1 to 200 wallets expected")

    def test_more_than_200_wallets_is_refused(self):
        many = sorted(f"0x{i:040x}" for i in range(201))
        self.assertEqual(self.check(signed({**MAIN["message"], "signers": many}))[1], "1 to 200 wallets expected")
        # the edge is the sender's to sign: 200 passes the shape rules and reaches the signature check
        self.assertEqual(self.check(signed({**MAIN["message"], "signers": many[:200]}))[1], "not signed by the house")

    def test_a_malformed_wallet_is_refused(self):
        for bad in ("0x" + "A1" * 20, "0x" + "a1" * 19, "a1" * 20, "0x" + "g1" * 20, "", 5, None, ["0x"]):
            with self.subTest(wallet=bad):
                message = {**MAIN["message"], "signers": [bad]}
                self.assertEqual(self.check(signed(message))[1], "a wallet is not 0x and 40 lowercase hex")

    def test_wallets_must_be_sorted_and_unique(self):
        for order in ([B, A, C], [A, A, B]):
            with self.subTest(order=order):
                self.assertEqual(
                    self.check(signed({**MAIN["message"], "signers": order}))[1], "wallets are not sorted and unique"
                )

    def test_not_a_record(self):
        for raw in (None, [], "x", 5, {}, {"message": "x"}, {"message": []}, {"signature": MAIN["signature"]}):
            with self.subTest(raw=raw):
                self.assertEqual(self.check(raw), (None, "not a record"))

    def test_fields_must_be_exactly_the_three(self):
        message = MAIN["message"]
        for changed in ({**message, "extra": 1}, {k: v for k, v in message.items() if k != "issuedAt"}):
            with self.subTest(keys=sorted(changed)):
                self.assertEqual(self.check(signed(changed))[1], "wrong fields")

    def test_numbers_must_be_whole_numbers(self):
        for field in ("seq", "issuedAt"):
            for bad in ("1893456000", 1.5, -1, True, None):
                with self.subTest(field=field, value=bad):
                    self.assertEqual(
                        self.check(signed({**MAIN["message"], field: bad}))[1], "seq or issuedAt is not a whole number"
                    )

    def test_a_bad_signature_is_refused(self):
        for signature in (None, "", "0x12", 5, "0x" + "00" * 65, MAIN["signature"][:-2] + "zz"):
            with self.subTest(signature=signature):
                self.assertEqual(self.check({"message": MAIN["message"], "signature": signature})[0], None)
        self.assertEqual(self.check({"message": MAIN["message"]})[1], "not signed by the house")

    def test_a_signature_is_the_whole_check_not_the_shape(self):
        # a stranger can write a list of the right shape; only the house's signature makes it count
        stranger = VECTORS["stranger"]
        forged = {"message": MAIN["message"], "signature": stranger["signature"]}
        self.assertEqual(self.check(forged)[1], "not signed by the house")


class ReadSigners(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.hand = self.dir / "signers"
        self.house = self.dir / "signers.house"

    def test_the_union_of_both_files(self):
        self.hand.write_text(f"# by hand\n{HAND.upper()}  # me\n")
        self.house.write_text(S.render({"signers": [A, B], "seq": 5, "issuedAt": 5}))
        self.assertEqual(R.read_signers(self.hand), frozenset({HAND, A, B}))

    def test_each_file_alone(self):
        self.assertEqual(R.read_signers(self.hand), frozenset())  # neither exists
        self.hand.write_text(f"{HAND}\n")
        self.assertEqual(R.read_signers(self.hand), frozenset({HAND}))
        self.hand.unlink()
        self.house.write_text(f"{A}\n")
        self.assertEqual(R.read_signers(self.hand), frozenset({A}))

    def test_a_broken_house_file_costs_nothing(self):
        self.hand.write_text(f"{HAND}\n")
        self.house.write_bytes(b"\xff\xfe\x00 not text \x80")
        self.assertEqual(R.read_signers(self.hand), frozenset({HAND}))

    def test_the_old_call_is_unchanged(self):
        self.hand.write_text(f"{HAND}\nnot a wallet\n0x12\n")
        self.assertEqual(R.read_signers(self.hand), frozenset({HAND}))
        self.assertEqual(R.read_signers(path=self.hand), frozenset({HAND}))

    def test_the_house_file_can_be_named(self):
        elsewhere = self.dir / "elsewhere"
        elsewhere.write_text(f"{C}\n")
        self.hand.write_text(f"{HAND}\n")
        self.assertEqual(R.read_signers(self.hand, elsewhere), frozenset({HAND, C}))

    def test_the_house_file_sits_beside_the_signers_file(self):
        self.assertEqual(S.house_file(Path("/etc/fleet/signers")), Path("/etc/fleet/signers.house"))
        self.assertEqual(R.SIGNERS_FILE.with_name(R.HOUSE_FILE_NAME), Path("/etc/fleet/signers.house"))

    def test_the_comment_lines_read_back_as_the_seq(self):
        self.house.write_text(S.render({"signers": [A], "seq": 1893456000, "issuedAt": 1893455999}))
        self.assertEqual(S.last_seq(self.house), 1893456000)
        self.assertEqual(S.last_seq(self.dir / "nothing"), 0)
        self.house.write_text(f"{A}\n")
        self.assertEqual(S.last_seq(self.house), 0)


class Refresh(unittest.TestCase):
    """Whole passes: the fake network answers with a record, the file on disk is what changes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.hand = self.dir / "signers"
        self.hand.write_text(f"{HAND}\n")
        self.house = self.dir / "signers.house"
        self.now = NOW
        self.answer = json.dumps(MAIN).encode()
        self.logs = []
        self.asked = []

    def get(self, url):
        self.asked.append(url)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def run_pass(self, house=HOUSE):
        return S.refresh(
            self.hand, now=lambda: self.now, get=self.get, url=lambda: "https://example.test/signers.json",
            log=self.logs.append, house=house,
        )

    def obeyed(self):
        return R.read_signers(self.hand)

    def test_a_valid_list_is_stored_and_obeyed(self):
        self.assertEqual(self.run_pass(), "stored")
        self.assertEqual(self.asked, ["https://example.test/signers.json"])
        self.assertEqual(self.obeyed(), frozenset({HAND, A, B, C}))
        self.assertEqual(S.last_seq(self.house), MAIN["message"]["seq"])
        self.assertEqual(self.house.stat().st_mode & 0o777, 0o644)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["signers", "signers.house"])  # no temp file left

    def test_off_asks_nothing_and_writes_nothing(self):
        self.assertEqual(self.run_pass(house=""), "off")
        self.assertEqual(self.asked, [])
        self.assertFalse(self.house.exists())

    def test_the_shipped_recipe_is_off(self):
        with mock.patch.object(S, "HOUSE_SIGNER", ""):
            self.assertEqual(S.refresh(self.hand, get=self.get, log=self.logs.append), "off")
        self.assertEqual(self.asked, [])

    def test_a_replay_changes_nothing(self):
        self.run_pass()
        before = self.house.read_text()
        self.assertEqual(self.run_pass(), "refused")  # the very same record again: seq is not above
        self.assertEqual(self.house.read_text(), before)
        self.answer = json.dumps(OLDER).encode()  # an older record, validly signed by the house
        self.assertEqual(self.run_pass(), "refused")
        self.assertEqual(self.house.read_text(), before)
        self.assertEqual(self.obeyed(), frozenset({HAND, A, B, C}))

    def test_a_newer_list_replaces_and_a_dropped_wallet_stops_being_obeyed(self):
        self.run_pass()
        self.answer = json.dumps(FEWER).encode()
        self.now = FEWER["message"]["issuedAt"] + 1
        self.assertEqual(self.run_pass(), "stored")
        self.assertEqual(self.obeyed(), frozenset({HAND, A, B}))  # C was dropped; HAND, added by hand, stays
        self.assertNotIn(C, self.obeyed())
        self.assertEqual(self.hand.read_text(), f"{HAND}\n")  # the hand file is never touched

    def test_a_bad_answer_never_strips_the_machine(self):
        self.run_pass()
        good = self.house.read_text()
        later = FEWER["message"]["issuedAt"] + 1
        for name, answer in {
            "empty body": b"",
            "garbage": b"<html>502</html>",
            "not json object": b"[]",
            "empty list": json.dumps(signed({**FEWER["message"], "signers": []}, FEWER)).encode(),
            "unsigned": json.dumps({"message": FEWER["message"]}).encode(),
            "stranger": json.dumps({"message": FEWER["message"], "signature": VECTORS["stranger"]["signature"]}).encode(),
            "not utf-8": b"\xff\xfe",
            "network down": OSError("no route"),
            "timeout": TimeoutError("slow"),
        }.items():
            with self.subTest(answer=name):
                self.answer, self.now = answer, later
                self.assertIn(self.run_pass(), ("refused", "failed"))
                self.assertEqual(self.house.read_text(), good)
                self.assertEqual(self.obeyed(), frozenset({HAND, A, B, C}))

    def test_too_old_and_future_leave_the_file_alone(self):
        self.run_pass()
        good = self.house.read_text()
        self.answer = json.dumps(FEWER).encode()
        for now in (FEWER["message"]["issuedAt"] + 24 * 3600 + 1, FEWER["message"]["issuedAt"] - 301):
            with self.subTest(now=now):
                self.now = now
                self.assertEqual(self.run_pass(), "refused")
                self.assertEqual(self.house.read_text(), good)

    def test_the_first_pass_with_a_bad_answer_leaves_no_file(self):
        self.answer = OSError("down")
        self.assertEqual(self.run_pass(), "failed")
        self.assertFalse(self.house.exists())
        self.assertEqual(self.obeyed(), frozenset({HAND}))

    def test_a_failed_write_changes_nothing(self):
        self.run_pass()
        good = self.house.read_text()
        self.answer, self.now = json.dumps(FEWER).encode(), FEWER["message"]["issuedAt"] + 1
        with mock.patch.object(R, "write_file", side_effect=OSError("disk full")):
            self.assertEqual(self.run_pass(), "failed")
        self.assertEqual(self.house.read_text(), good)

    def test_the_log_names_the_rule_never_the_list(self):
        self.answer = json.dumps(signed({**MAIN["message"], "signers": []})).encode()
        self.run_pass()
        self.assertEqual(self.logs, ["the house's signers list was not taken: 1 to 200 wallets expected"])


class Fetch(unittest.TestCase):
    class Opener:
        def __init__(self, body=b"{}", status=200):
            self.body, self.status, self.request = body, status, None

        def open(self, request, timeout=None):
            self.request, self.timeout = request, timeout
            outer = self

            class Response(io.BytesIO):
                status = outer.status

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            return Response(self.body)

    def test_it_asks_with_a_timeout_and_returns_the_body(self):
        opener = self.Opener(b'{"a":1}')
        self.assertEqual(S.fetch("https://example.test/s.json", opener), b'{"a":1}')
        self.assertEqual(opener.request.full_url, "https://example.test/s.json")
        self.assertEqual(opener.timeout, S.TIMEOUT)

    def test_another_status_is_an_error(self):
        with self.assertRaises(OSError):
            S.fetch("https://example.test/s.json", self.Opener(status=204))

    def test_a_big_answer_is_an_error(self):
        with self.assertRaises(OSError):
            S.fetch("https://example.test/s.json", self.Opener(b"x" * (S.MAX_BYTES + 1)))
        self.assertEqual(len(S.fetch("https://example.test/s.json", self.Opener(b"x" * S.MAX_BYTES))), S.MAX_BYTES)

    def test_a_redirect_to_something_else_is_refused(self):
        guard = None

        class Capture:
            def open(self, request, timeout=None):
                raise AssertionError("not used")

        with mock.patch("urllib.request.build_opener") as build:
            build.return_value = self.Opener()
            S.fetch("https://example.test/s.json")
            guard = build.call_args.args[0]
        request = mock.Mock()
        with self.assertRaises(OSError):
            guard().redirect_request(request, None, 302, "Found", {}, "file:///etc/passwd")
        with self.assertRaises(OSError):
            guard().redirect_request(request, None, 302, "Found", {}, "ftp://example.test/x")

    def test_a_redirect_to_https_is_followed(self):
        import urllib.request

        with mock.patch("urllib.request.build_opener") as build:
            build.return_value = self.Opener()
            S.fetch("https://example.test/s.json")
            guard = build.call_args.args[0]
        request = urllib.request.Request("https://example.test/s.json")
        followed = guard().redirect_request(request, None, 302, "Found", {}, "https://numinia.com/fleet/signers.json")
        self.assertEqual(followed.full_url, "https://numinia.com/fleet/signers.json")


class Url(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "signers-url"

    def test_default_when_there_is_nothing(self):
        self.assertEqual(S.signers_url({}, self.file), S.DEFAULT_URL)
        self.assertEqual(S.DEFAULT_URL, "https://numinia.com/fleet/signers.json")

    def test_the_file_then_the_environment(self):
        self.file.write_text("https://other.example/s.json\n")
        self.assertEqual(S.signers_url({}, self.file), "https://other.example/s.json")
        self.assertEqual(S.signers_url({"FLEET_SIGNERS_URL": "http://env.example/s"}, self.file), "http://env.example/s")

    def test_anything_but_http_falls_back_to_the_house_address(self):
        for bad in ("", "file:///etc/passwd", "ftp://x/y", "not a url", "https://a b"):
            with self.subTest(value=bad):
                self.file.write_text(bad)
                self.assertEqual(S.signers_url({}, self.file), S.DEFAULT_URL)
                self.assertEqual(S.signers_url({"FLEET_SIGNERS_URL": bad}, self.file), S.DEFAULT_URL)

    def test_the_real_environment_is_read_by_default(self):
        with mock.patch.dict("os.environ", {"FLEET_SIGNERS_URL": "https://env.example/x"}):
            self.assertEqual(S.signers_url(), "https://env.example/x")


class Main(unittest.TestCase):
    def test_off_exits_clean_and_says_so(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(S.main(["--signers-file", str(Path(tmp) / "signers")]), 0)
        self.assertEqual(out.getvalue().strip(), "off")

    def test_a_refusal_exits_with_an_error(self):
        out = io.StringIO()
        with mock.patch.object(S, "refresh", return_value="refused"), mock.patch("sys.stdout", out):
            self.assertEqual(S.main([]), 1)
        with mock.patch.object(S, "refresh", return_value="stored"), mock.patch("sys.stdout", out):
            self.assertEqual(S.main([]), 0)


class InTheReleasePass(unittest.TestCase):
    def test_the_release_timer_takes_the_list_first(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(S, "refresh") as refresh, \
                mock.patch.object(REL, "run_pass", return_value={"action": "none"}), mock.patch("sys.stdout", io.StringIO()):
            hand = Path(tmp) / "signers"
            order = mock.Mock()
            order.attach_mock(refresh, "refresh")
            order.attach_mock(REL.run_pass, "run_pass")
            self.assertEqual(REL.main(["--home", tmp, "--signers-file", str(hand)]), 0)
            self.assertEqual([c[0] for c in order.mock_calls], ["refresh", "run_pass"])
            refresh.assert_called_once_with(hand)

    def test_a_dry_run_asks_nobody(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(S, "refresh") as refresh, \
                mock.patch.object(REL, "parse_release", return_value=None), \
                mock.patch.object(REL.Env, "fetch", create=True), mock.patch("sys.stdout", io.StringIO()):
            REL.main(["--home", tmp, "--signers-file", str(Path(tmp) / "signers"), "--dry-run"])
            refresh.assert_not_called()

    def test_a_broken_signers_py_never_stops_the_engine_update(self):
        err = io.StringIO()
        with mock.patch.object(S, "refresh", side_effect=RuntimeError("boom")), mock.patch("sys.stderr", err):
            REL.refresh_house_signers(Path("/nonexistent/signers"))  # does not raise
        self.assertIn("RuntimeError", err.getvalue())
        err = io.StringIO()
        with mock.patch.dict(sys.modules, {"signers": None}), mock.patch("sys.stderr", err):  # import fails
            REL.refresh_house_signers(Path("/nonexistent/signers"))
        self.assertIn("ModuleNotFoundError", err.getvalue())


class Bootstrap(unittest.TestCase):
    TEXT = (MACHINE / "bootstrap.sh").read_text(encoding="utf-8")

    def test_the_program_is_installed_beside_the_others(self):
        loop = re.search(r"for program in ([^;]+); do", self.TEXT).group(1).split()
        self.assertIn("signers.py", loop)
        self.assertTrue(all((MACHINE / name).is_file() for name in loop))

    def test_the_url_is_written_only_when_there_is_none(self):
        self.assertIn('DEFAULT_SIGNERS_URL="https://numinia.com/fleet/signers.json"', self.TEXT)
        block = self.TEXT[self.TEXT.index("== where the machine fetches the house's signed list") :]
        block = block[: block.index("== the Oracles who may act")]
        self.assertIn("if [[ ! -e /etc/fleet/signers-url ]]; then", block)
        self.assertIn('echo "$DEFAULT_SIGNERS_URL" > /etc/fleet/signers-url', block)
        self.assertIn("chmod 644 /etc/fleet/signers-url", block)
        self.assertNotIn("signers.house", self.TEXT)  # the bootstrap never touches the house's list

    def test_the_bootstrap_does_not_overwrite_the_hand_wallets_with_the_list(self):
        # /etc/fleet/signers is still written only from the line's wallets (or created with its comment), as before
        writes = [line.strip() for line in self.TEXT.splitlines() if line.rstrip().endswith("> /etc/fleet/signers")]
        self.assertEqual(len(writes), 2)

    def test_the_url_default_matches_the_program(self):
        self.assertIn(f'DEFAULT_SIGNERS_URL="{S.DEFAULT_URL}"', self.TEXT)


class Shape(unittest.TestCase):
    def test_the_vectors_file_is_not_changed_by_a_test(self):
        again = json.loads((Path(__file__).parent / "fleet_vectors.json").read_text(encoding="utf-8"))
        self.assertEqual(again["fleetSigners"], FS)


if __name__ == "__main__":
    unittest.main()
