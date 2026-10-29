# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""The machine's recipe follows main by itself: a canary at once, the others after a rest, GitHub's signature only,
back to the old programs and a halt when the new ones fail, a pin to freeze it.

Everything outside is fake (GitHub, gpgv, the bootstrap, systemd, the clock), except in three places that
use the real thing: a local git repository read through GitRemote, gpgv on a real commit GitHub signed, and
the copy of the installed programs put back over a temporary tree.
"""

from pathlib import Path
import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MACHINE = ROOT / "open-worlds" / "machine"


def load(name):
    if str(MACHINE) not in sys.path:
        sys.path.insert(0, str(MACHINE))
    spec = importlib.util.spec_from_file_location(name, MACHINE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


REC = load("recipe")

A, B, C, C2, D = ("a" * 40, "b" * 40, "c" * 40, "c2" * 20, "d" * 40)  # commits
TA, TC, TD = ("1" * 40, "3" * 40, "4" * 40)  # their open-worlds/machine folders
TREES = {A: TA, B: TA, C: TC, C2: TC, D: TD}  # B only touched content; C2 only content after C

# Commit 60638d3 of numengames/numinia-assets, merged on github.com (#30): its raw object, signed by web-flow.
SIGNED_BY_GITHUB = base64.b64decode("""
dHJlZSAxYjE5YWJlMmE0Y2JhZDRmNjk0NTU4MWE5ZTI1YTEzOTMwYWJhMjkzCnBhcmVudCA1NWM3
NWFkOGExZmEwNGRlNmVlNTUzNzUwODUwOTA2MWMyYTU3NzMxCmF1dGhvciB1cnNhLWRhLW51bWlu
aWFbYm90XSA8MzM5MzA2NDY3K3Vyc2EtZGEtbnVtaW5pYVtib3RdQHVzZXJzLm5vcmVwbHkuZ2l0
aHViLmNvbT4gMTc5MjMzMzkwNCArMDIwMApjb21taXR0ZXIgR2l0SHViIDxub3JlcGx5QGdpdGh1
Yi5jb20+IDE3OTIzMzM5MDQgKzAyMDAKZ3Bnc2lnIC0tLS0tQkVHSU4gUEdQIFNJR05BVFVSRS0t
LS0tCiAKIHdzRmNCQUFCQ0FBUUJRSnExTmhRQ1JDMWFRN3V1NVVobEFBQWFyOFFBQjk1alFCQ1JB
QnBHSFZSTTFmUkZvM0UKIDI1WjBjUUc1Tkp3YzY2TG5rN0psRng3WktNRGFhYkVicGJsTi9RZE1Q
MEcwckc3WVZ0MmJEM3NGYWN5NFVySW0KIEwwOVo2TzFYNVhXc093YnNHck05L2JRK29BcFRmVVpk
Sm9nYkhySDUrbUtQN2NZWWZXc2pyY1NhZlJpRzBueEIKIFkwR2dnZzJHTkNOYm9XbWc1aWU0WDhr
V1BueFhmcG5CU2NadUpXNHZ2aThKT0k4cldSaXhUWHprU3MvdUlTajcKIENVWndSNzZXUnNzS1o2
STZEemYrUWRTcU43VEIzN1ZWcndaUVVZamg1MXJhMVhObHBhck9OWFQxL3BUMEdlWFUKIFdQM1Jx
ZjF4bEpaNEtJSXN5UVJqV0ZCaFQvc1F1b0lCRjZaOHRhZlkwdzdzK3ZCelRmU2RYdTlIVTRkbENo
b04KIE1lTUsvQzFkV2NwYU1PNER0ZFdyNUxUUndGM09hODUrZmE2bjdwNTdsdHowaC9yTW5sT3Iy
Q3Y3RTR3QjYyVVcKIC9OWlVLT0FMOUR6VzBzQlo3MXMvVmdQUzJZT244aS80OFpMUldaTkxjZkYz
WEM2RjRZZWc1MGJvbWlIWFNoa3gKIGZUVDcydDRxdThkRkVIUHJnRXVCOVc1NnBYczV4czhybHY2
bzV2SytxdmQ4VFJQMzRXNkFDbEdCQ3Bya2xDeHkKIEdRdk5MNi8zL3VyNnVvMHQ3LzdzZUsvRTJ6
NHBpbXE2bGtMeXN4cW1WN0lJZDJNM2N6SG1jTjI0STFDanpyYm0KIGkvYXBlQmErVXFydlAyNlJ5
R215cElsR0lYQVBkUFFGYW9kejYwRHVORHVUNmpHaXB1WFFVZjBYVkh2eXdnUGYKIGZmaFlBdXU4
SUdQYTBCSmlEeHFXCiA9ZmZrdQogLS0tLS1FTkQgUEdQIFNJR05BVFVSRS0tLS0tCiAKCmZsZWV0
OiBvcHRpb25hbCBidWlsZC1wcm92ZW5hbmNlIGNoZWNrIGJlZm9yZSBhIG1hY2hpbmUgZm9sbG93
cyBsYXRlc3QgKG9mZiBieSBkZWZhdWx0KSAoIzMwKQoKQ28tYXV0aG9yZWQtYnk6IHVyc2EtZGEt
bnVtaW5pYVtib3RdIDwzMzkzMDY0NjcrdXJzYS1kYS1udW1pbmlhW2JvdF1AdXNlcnMubm9yZXBs
eS5naXRodWIuY29tPg==
""")


class FakeRemote:
    """GitHub as the pass sees it: main's newest commit and each commit's recipe folder."""

    def __init__(self, main):
        self.main = main
        self.down = False
        self.asked = []

    def tip(self):
        self.asked.append("tip")
        if self.down:
            raise REC.RemoteError("down")
        return self.main

    def tree(self, commit):
        if self.down:
            raise REC.RemoteError("down")
        return TREES.get(commit)


class Recipe(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.installed_file = self.root / "etc" / "recipe"
        self.installed_file.parent.mkdir()
        self.installed_file.write_text(A + "\n")
        self.clock = 1_800_000_000.0
        self.remote = FakeRemote(A)
        self.signed = {A, B, C, C2, D}
        self.pin = None
        self.canary = False
        self.soak = REC.SOAK_SECONDS
        self.boot = []  # commits the bootstrap was run for
        self.boot_fails = None  # None | "before" (nothing changed) | "after" (programs changed, then it failed)
        self.healthy = True
        self.busy = None
        self.restored = []
        self.logs = []
        env = REC.Env(self.root / "home", installed_file=self.installed_file)
        env.now = lambda: self.clock
        env.sleep = lambda s: setattr(self, "clock", self.clock + s)
        env.log = self.logs.append
        env.remote = self.remote
        env.pin = lambda: self.pin
        env.canary = lambda: self.canary
        env.soak = lambda: self.soak
        env.verify = lambda commit: (commit in self.signed, "ok" if commit in self.signed else "the commit is not signed")
        env.busy = lambda: self.busy
        env.backup = self._backup
        env.restore = self._restore
        env.bootstrap = self._bootstrap
        env.reconcile_was_ok = lambda: True
        env.health = self._health
        self.env = env

    def _backup(self):
        copy = self.root / f"copy-{len(self.boot)}"
        copy.mkdir()
        (copy / "recipe").write_text(self.installed_file.read_text())
        return copy

    def _restore(self, copy):
        self.restored.append(copy)
        self.installed_file.write_text((copy / "recipe").read_text())
        return True

    def _bootstrap(self, commit):
        self.boot.append(commit)
        if self.boot_fails == "before":
            return False, "apt-get could not reach its mirror"
        self.installed_file.write_text(commit + "\n")
        if self.boot_fails == "after":
            return False, "the bootstrap ended with 1"
        return True, "ok"

    def _health(self, commit, must_pass):
        if self.installed_file.read_text().strip() != commit:
            return False, "the machine does not say it runs this recipe"
        if commit != A and not self.healthy:
            return False, "the door does not answer on its socket"
        return True, "ok"

    def run_pass(self):
        return REC.run_pass(self.env)

    def state(self):
        return json.loads((self.root / "home" / "recipe" / "state.json").read_text())

    def installed(self):
        return self.installed_file.read_text().strip()

    # --- nothing new ------------------------------------------------------

    def test_a_merge_that_leaves_the_recipe_alone_is_only_noted(self):
        self.remote.main = B  # touched content, not open-worlds/machine
        result = self.run_pass()
        self.assertEqual(result["action"], "same")
        self.assertEqual(self.boot, [])
        state = self.state()
        self.assertEqual(state["checked"]["commit"], B)
        self.assertTrue(state["upToDate"])
        self.assertIsNone(state["target"])
        self.assertEqual(state["lastPass"]["action"], "same")
        self.assertEqual(self.installed(), A)  # what was installed stays what it says

    def test_github_down_changes_nothing(self):
        self.remote.main = C
        self.canary = True
        self.remote.down = True
        self.assertEqual(self.run_pass()["action"], "none")
        self.assertEqual(self.boot, [])
        self.assertEqual(self.installed(), A)

    def test_a_machine_with_no_recipe_written_waits_for_the_line(self):
        self.installed_file.unlink()
        self.remote.main = C
        self.canary = True
        self.assertEqual(self.run_pass()["action"], "none")
        self.assertEqual(self.boot, [])

    # --- the canary and the rest ------------------------------------------

    def test_the_canary_takes_a_new_recipe_at_once(self):
        self.canary = True
        self.remote.main = C
        result = self.run_pass()
        self.assertEqual(result["action"], "installed")
        self.assertEqual(self.boot, [C])
        self.assertEqual(self.installed(), C)
        state = self.state()
        self.assertEqual(state["lastInstall"]["commit"], C)
        self.assertEqual(state["lastInstall"]["from"], A)
        self.assertIsNone(state["installing"])
        self.assertTrue(state["upToDate"])
        # the next pass finds nothing to do
        self.assertEqual(self.run_pass()["action"], "same")
        self.assertEqual(self.boot, [C])

    def test_the_others_wait_for_the_rest_counted_from_when_they_first_saw_the_recipe(self):
        self.remote.main = C
        result = self.run_pass()
        self.assertEqual(result["action"], "resting")
        state = self.state()
        self.assertEqual(state["target"]["why"], "resting")
        self.assertEqual(state["target"]["restUntil"], REC.utc(self.clock + REC.SOAK_SECONDS))
        self.clock += 3600
        self.remote.main = C2  # a content merge on top: same recipe, the rest is not reset
        self.assertEqual(self.run_pass()["action"], "resting")
        self.assertEqual(self.boot, [])
        self.clock += 3600
        self.assertEqual(self.run_pass()["action"], "installed")
        self.assertEqual(self.boot, [C2])  # main's newest commit with that recipe

    def test_a_recipe_reverted_within_the_rest_never_reaches_the_others(self):
        self.remote.main = C
        self.assertEqual(self.run_pass()["action"], "resting")
        self.clock += 1800
        self.remote.main = B  # main goes back to the installed recipe
        self.assertEqual(self.run_pass()["action"], "same")
        self.clock += 5 * 3600
        self.assertEqual(self.run_pass()["action"], "same")
        self.assertEqual(self.boot, [])

    def test_the_rest_is_configurable(self):
        self.soak = REC.parse_soak("10")
        self.remote.main = C
        self.assertEqual(self.run_pass()["action"], "resting")
        self.clock += 600
        self.assertEqual(self.run_pass()["action"], "installed")

    def test_parse_soak(self):
        self.assertEqual(REC.parse_soak(None), 2 * 3600)
        self.assertEqual(REC.parse_soak("30"), 1800)
        self.assertEqual(REC.parse_soak(" 0\n"), 0)
        for garbage in ("", "2h", "-5", "1.5", "x"):
            with self.subTest(text=garbage):
                self.assertEqual(REC.parse_soak(garbage), 2 * 3600)
        self.assertEqual(REC.parse_soak("999999"), REC.SOAK_MAX_MINUTES * 60)

    # --- the pin ----------------------------------------------------------

    def test_a_pin_off_freezes_the_machine_without_asking_github(self):
        self.pin = "off"
        self.canary = True
        self.remote.main = C
        self.assertEqual(self.run_pass()["action"], "frozen")
        self.assertEqual(self.remote.asked, [])
        self.assertEqual(self.boot, [])
        self.assertEqual(self.state()["pin"], "off")

    def test_a_pin_to_a_commit_holds_the_machine_there(self):
        self.pin = D  # not main's newest; no rest for what a person chose
        self.remote.main = C
        self.assertEqual(self.run_pass()["action"], "installed")
        self.assertEqual(self.boot, [D])
        self.assertEqual(self.remote.asked, [])  # main is not even read
        self.assertEqual(self.run_pass()["action"], "same")
        self.assertEqual(self.boot, [D])

    def test_parse_pin_reads_anything_strange_as_frozen(self):
        self.assertIsNone(REC.parse_pin(None))
        self.assertIsNone(REC.parse_pin("\n"))
        self.assertEqual(REC.parse_pin("off\n"), "off")
        self.assertEqual(REC.parse_pin("OFF"), "off")
        self.assertEqual(REC.parse_pin(C + "\n"), C)
        for garbage in ("of", "main", C[:7], C + "0", "on", "follow"):
            with self.subTest(text=garbage):
                self.assertEqual(REC.parse_pin(garbage), "off")

    # --- trust --------------------------------------------------------------

    def test_a_commit_github_did_not_sign_is_refused(self):
        self.canary = True
        self.remote.main = C
        self.signed.discard(C)
        result = self.run_pass()
        self.assertEqual(result["action"], "refused")
        self.assertEqual(self.boot, [])
        self.assertEqual(self.installed(), A)
        self.assertEqual(self.state()["refused"]["commit"], C)
        self.signed.add(C)  # (e.g. a later signed commit with the same recipe) lifts the refusal
        self.assertEqual(self.run_pass()["action"], "installed")
        self.assertIsNone(self.state()["refused"])

    def test_another_fleet_job_running_makes_it_wait(self):
        self.canary = True
        self.remote.main = C
        self.busy = "fleet-release.service"
        self.assertEqual(self.run_pass()["action"], "waiting")
        self.assertEqual(self.boot, [])
        self.busy = None
        self.assertEqual(self.run_pass()["action"], "installed")

    # --- failure: back, and halt ------------------------------------------

    def test_an_unhealthy_recipe_goes_back_and_halts_until_a_different_recipe(self):
        self.canary = True
        self.remote.main = C
        self.healthy = False
        result = self.run_pass()
        self.assertEqual(result["action"], "rolled-back")
        self.assertEqual(self.installed(), A)  # the copy went back
        self.assertEqual(len(self.restored), 1)
        state = self.state()
        self.assertEqual(state["halted"]["commit"], C)
        self.assertEqual(state["halted"]["tree"], TC)
        self.assertIn("socket", state["halted"]["reason"])
        self.assertTrue(state["halted"]["rolledBack"])
        self.assertIn(TC, state["bad"])
        # the same recipe is never tried again, not even from another commit
        self.healthy = True
        self.assertEqual(self.run_pass()["action"], "halted")
        self.remote.main = C2
        self.assertEqual(self.run_pass()["action"], "halted")
        self.assertEqual(self.boot, [C])
        # a different recipe lifts the halt
        self.remote.main = D
        self.assertEqual(self.run_pass()["action"], "installed")
        self.assertIsNone(self.state()["halted"])
        self.assertEqual(self.installed(), D)

    def test_a_bootstrap_that_failed_after_changing_things_halts_at_once(self):
        self.canary = True
        self.remote.main = C
        self.boot_fails = "after"
        self.assertEqual(self.run_pass()["action"], "rolled-back")
        self.assertEqual(self.installed(), A)
        self.assertEqual(self.state()["halted"]["commit"], C)

    def test_a_bootstrap_that_changed_nothing_is_tried_three_times_then_halts(self):
        self.canary = True
        self.remote.main = C
        self.boot_fails = "before"
        self.assertEqual(self.run_pass()["action"], "retry")
        self.assertEqual(self.run_pass()["action"], "retry")
        self.assertIsNone(self.state().get("halted"))
        self.assertEqual(self.run_pass()["action"], "rolled-back")
        self.assertEqual(self.boot, [C, C, C])
        self.assertIn("mirror", self.state()["halted"]["reason"])
        self.assertEqual(self.installed(), A)

    def test_an_install_interrupted_before_its_health_check_is_judged_next_pass(self):
        copy = self._backup()
        self.installed_file.write_text(C + "\n")  # the bootstrap ran, then the machine rebooted
        state_dir = self.root / "home" / "recipe"
        state_dir.mkdir(parents=True)
        (state_dir / "state.json").write_text(json.dumps({"installing": {
            "commit": C, "tree": TC, "previous": A, "backup": str(copy), "at": "x", "reconcileMustPass": True}}))
        self.healthy = False
        self.assertEqual(self.run_pass()["action"], "rolled-back")
        self.assertEqual(self.installed(), A)
        self.assertIn("interrupted", self.state()["halted"]["reason"])

    def test_an_interrupted_install_that_is_healthy_is_kept(self):
        copy = self._backup()
        self.installed_file.write_text(C + "\n")
        state_dir = self.root / "home" / "recipe"
        state_dir.mkdir(parents=True)
        (state_dir / "state.json").write_text(json.dumps({"installing": {
            "commit": C, "tree": TC, "previous": A, "backup": str(copy), "at": "x"}}))
        self.remote.main = C
        self.assertEqual(self.run_pass()["action"], "same")
        self.assertEqual(self.installed(), C)
        self.assertEqual(self.state()["lastInstall"]["commit"], C)


class Signature(unittest.TestCase):
    """GitHub's web-flow signature, checked with the real gpgv against the key pinned in recipe.py."""

    def test_the_pinned_key_is_githubs_web_flow_key(self):
        self.assertEqual(REC.WEB_FLOW_FINGERPRINTS, frozenset({"968479A1AFF927E37D1A566BB5690EEEBB952194"}))
        packets = REC.dearmor(REC.WEB_FLOW_KEY)
        self.assertEqual(packets[0], 0x99)  # an OpenPGP public-key packet (tag 6)

    def test_split_signed(self):
        payload, signature = REC.split_signed(SIGNED_BY_GITHUB)
        self.assertNotIn(b"gpgsig", payload)
        self.assertTrue(signature.startswith(b"-----BEGIN PGP SIGNATURE-----"))
        self.assertIn(b"\ncommitter GitHub <noreply@github.com>", payload)
        self.assertIsNone(REC.split_signed(payload))  # unsigned

    @unittest.skipUnless(shutil.which("gpgv"), "gpgv is not installed here")
    def test_a_commit_merged_on_github_is_believed(self):
        self.assertEqual(REC.verify_commit(SIGNED_BY_GITHUB), (True, "signed by GitHub (web-flow)"))

    @unittest.skipUnless(shutil.which("gpgv"), "gpgv is not installed here")
    def test_a_changed_commit_an_unsigned_one_or_another_key_is_not(self):
        changed = SIGNED_BY_GITHUB.replace(b"(off by default)", b"(on by default)")
        self.assertFalse(REC.verify_commit(changed)[0])
        payload, _ = REC.split_signed(SIGNED_BY_GITHUB)
        self.assertEqual(REC.verify_commit(payload), (False, "the commit is not signed"))
        self.assertEqual(REC.verify_commit(SIGNED_BY_GITHUB, pinned=frozenset({"0" * 40}))[0], False)

    def test_the_verdict_needs_a_validsig_by_the_pinned_key(self):
        fpr = "968479A1AFF927E37D1A566BB5690EEEBB952194"
        good = f"[GNUPG:] GOODSIG B5690EEEBB952194 GitHub\n[GNUPG:] VALIDSIG {fpr} 2026-10-18 1 0 4 0 1 8 00 {fpr}\n"
        self.assertTrue(REC.signature_verdict(0, good)[0])
        self.assertFalse(REC.signature_verdict(1, good)[0])
        self.assertFalse(REC.signature_verdict(0, good.replace(fpr, "A" * 40))[0])
        self.assertFalse(REC.signature_verdict(0, good + "[GNUPG:] BADSIG x\n")[0])
        self.assertFalse(REC.signature_verdict(0, "[GNUPG:] EXPKEYSIG B5690EEEBB952194 GitHub\n" + good)[0])
        self.assertFalse(REC.signature_verdict(2, "[GNUPG:] ERRSIG 1234 1 8 00 1 9\n[GNUPG:] NO_PUBKEY 1234\n")[0])
        self.assertFalse(REC.signature_verdict(0, "")[0])


@unittest.skipUnless(shutil.which("git"), "git is not installed here")
class Remote(unittest.TestCase):
    """GitRemote against a real repository: main's newest commit, never another branch's; folder ids; one file."""

    def git(self, *args, cwd=None):
        environ = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org", "GIT_CONFIG_GLOBAL": os.devnull}
        return subprocess.run(["git", *args], cwd=cwd or self.src, check=True, capture_output=True, text=True,
                              env=environ).stdout.strip()

    def commit(self, path, text, message):
        target = self.src / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def test_main_its_recipe_folder_and_the_bootstrap(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.src = Path(tmp.name) / "src"
        self.src.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "uploadpack.allowFilter", "true")
        self.git("config", "uploadpack.allowAnySHA1InWant", "true")
        first = self.commit("open-worlds/machine/bootstrap.sh", "#!/bin/sh\necho one\n", "recipe one")
        content = self.commit("content/README.md", "a model\n", "content only")
        self.git("checkout", "-q", "-b", "other")
        unmerged = self.commit("open-worlds/machine/bootstrap.sh", "#!/bin/sh\necho evil\n", "not merged")
        self.git("checkout", "-q", "main")

        remote = REC.GitRemote(Path(tmp.name) / "clone", url=self.src.as_uri())
        self.assertEqual(remote.tip(), content)  # main's newest, never the other branch
        self.assertEqual(remote.tree(first), remote.tree(content))  # content-only: same recipe folder
        self.assertNotEqual(remote.tree(unmerged), remote.tree(content))
        self.assertEqual(remote.bootstrap(content), b"#!/bin/sh\necho one\n")
        self.assertTrue(remote.commit_object(content).startswith(b"tree "))
        # no file contents were fetched but the one asked for (the content's README never came)
        kinds = self.git("cat-file", "--batch-all-objects", "--batch-check=%(objecttype)",
                         cwd=Path(tmp.name) / "clone").split()
        self.assertEqual(kinds.count("blob"), 1)
        main_moved = self.commit("open-worlds/machine/bootstrap.sh", "#!/bin/sh\necho two\n", "recipe two")
        self.assertEqual(remote.tip(), main_moved)
        self.assertNotEqual(remote.tree(main_moved), remote.tree(content))
        with self.assertRaises(REC.RemoteError):
            remote.tree("0" * 40)


class CopyAndRestore(unittest.TestCase):
    """The real copy of the installed programs and its putting back, over a temporary tree, with systemctl faked."""

    def test_the_copy_puts_back_exactly_what_was_installed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        lib, sbin, units, etc = root / "lib", root / "sbin", root / "units", root / "etc"
        for folder in (lib, sbin, units, etc):
            folder.mkdir()
        (lib / "fleetd.py").write_text("old door\n")
        (lib / "recipe.py").write_text("old updater\n")
        (sbin / "fleetd").write_text("#!/bin/sh\n")
        (sbin / "other-tool").write_text("not ours\n")
        (units / "fleetd.service").write_text("[Service]\n")
        (units / "fleet-recipe.timer").write_text("[Timer]\n")
        (units / "caddy.service").write_text("not ours\n")
        (etc / "recipe").write_text(A + "\n")
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        env = REC.Env(root / "home", lib_dir=lib, sbin_dir=sbin, unit_dir=units, installed_file=etc / "recipe")
        env.run = run
        env.now = lambda: 1_800_000_000.0
        copy = env.backup()
        # the new recipe changes a program, adds one and a unit, and writes its commit
        (lib / "fleetd.py").write_text("new door\n")
        (lib / "extra.py").write_text("new\n")
        (units / "fleet-new.timer").write_text("[Timer]\n")
        (etc / "recipe").write_text(C + "\n")
        self.assertTrue(env.restore(copy))
        self.assertEqual((lib / "fleetd.py").read_text(), "old door\n")
        self.assertFalse((lib / "extra.py").exists())
        self.assertFalse((units / "fleet-new.timer").exists())
        self.assertEqual((etc / "recipe").read_text(), A + "\n")
        self.assertEqual((sbin / "other-tool").read_text(), "not ours\n")  # never touched
        self.assertEqual((units / "caddy.service").read_text(), "not ours\n")
        self.assertIn(["systemctl", "disable", "--now", "fleet-new.timer"], calls)
        self.assertIn(["systemctl", "daemon-reload"], calls)
        self.assertIn(["systemctl", "restart", "fleetd.service"], calls)
        self.assertIn(["systemctl", "enable", "--now", "fleet-recipe.timer"], calls)
        self.assertEqual(oct(copy.stat().st_mode & 0o777), oct(0o700))


class Door(unittest.TestCase):
    """The door shows what the updater does (`recipeUpdate`), with commits and short words only."""

    def door(self, root):
        D = load("fleetd")
        etc = root / "etc"
        etc.mkdir()
        (etc / "signers").write_text("")
        return D.Door(home=root / "home", alias="open-1", signers_file=etc / "signers",
                      admin_code_file=etc / "admin-code", codes_dir=etc / "codes", recipe_file=etc / "recipe",
                      control_host_file=etc / "control-host", domains_file=etc / "domains",
                      origins_file=etc / "origins", state=root / "state", clock=lambda: 1_800_000_000.0)

    def test_the_health_check_asks_the_real_door_over_its_socket(self):
        import threading
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        door = self.door(root)
        (root / "etc" / "recipe").write_text(C + "\n")
        sock = root / "home" / "sock" / "fleetd.sock"
        sock.parent.mkdir(parents=True)
        D = sys.modules.get("fleetd") or load("fleetd")
        server = D.make_server(door, sock)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        env = REC.Env(root / "home")
        self.assertEqual(env.door_recipe(), C)
        server.shutdown()
        server.server_close()
        sock.unlink()
        self.assertIsNone(env.door_recipe())

    def test_recipe_update(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        door = self.door(root)
        empty = door.machine()["recipeUpdate"]
        self.assertEqual(empty, {"installed": None, "upToDate": None, "mainCommit": None, "checkedAt": None,
                                 "target": None, "targetWhy": None, "restUntil": None, "canary": False, "pin": None,
                                 "halted": False, "haltedCommit": None, "haltedReason": None, "refused": None,
                                 "lastPass": None})
        folder = root / "home" / "recipe"
        folder.mkdir(parents=True)
        (folder / "state.json").write_text(json.dumps({
            "installed": A, "upToDate": False, "checked": {"commit": C, "tree": TC, "at": "2027-01-15T08:00:00Z"},
            "target": {"commit": C, "tree": TC, "why": "resting", "restUntil": "2027-01-15T10:00:00Z"},
            "canary": False, "pin": None, "halted": {"commit": D, "reason": "the door does not answer", "tree": TD},
            "refused": None, "lastPass": {"at": "2027-01-15T08:00:00Z", "action": "resting"},
            "installing": {"backup": "/srv/fleet/recipe/previous/x"}, "seen": {TC: 1}}))
        facts = door.machine()["recipeUpdate"]
        self.assertEqual(facts["installed"], A)
        self.assertEqual(facts["mainCommit"], C)
        self.assertEqual((facts["target"], facts["targetWhy"], facts["restUntil"]), (C, "resting", "2027-01-15T10:00:00Z"))
        self.assertEqual((facts["halted"], facts["haltedCommit"]), (True, D))
        self.assertEqual(facts["lastPass"], {"at": "2027-01-15T08:00:00Z", "action": "resting"})
        self.assertNotIn("/srv/fleet", json.dumps(facts))  # no path, no folder id
        self.assertNotIn(TC, json.dumps(facts))
        (folder / "state.json").write_text(json.dumps({"installed": "main", "pin": "anything", "target": {"why": "x"}}))
        facts = door.machine()["recipeUpdate"]
        self.assertEqual((facts["installed"], facts["pin"], facts["targetWhy"]), (None, None, None))


class Bootstrap(unittest.TestCase):
    """The bootstrap installs the updater and its timer, and never pauses the timer it may run under."""

    def test_the_bootstrap_installs_and_starts_the_recipe_updater(self):
        text = (MACHINE / "bootstrap.sh").read_text()
        self.assertIn("nightly.py recipe.py condiciones.py signers.py versions.py cleanup.py vitals.py vitalsread.py; do", text)
        self.assertIn("wrapper fleet-recipe recipe.py", text)
        self.assertIn("cat > /etc/systemd/system/fleet-recipe.timer", text)
        self.assertIn("ExecStart=/usr/local/sbin/fleet-recipe", text)
        self.assertIn("fleet-nightly.timer fleet-cleanup.timer fleet-vitals.timer fleet-recipe.timer\n", text)  # enabled with the others
        self.assertIn("fleet-nightly.timer fleet-cleanup.timer fleet-vitals.timer fleet-recipe.timer; do", text)  # and checked at the end
        self.assertIn(" gpgv ", text)
        stop = next(line for line in text.splitlines() if line.strip().startswith("systemctl stop"))
        self.assertNotIn("fleet-recipe", stop)

    def test_the_updater_reads_the_files_the_bootstrap_writes(self):
        text = (MACHINE / "bootstrap.sh").read_text()
        self.assertEqual(REC.INSTALLED_FILE, Path("/etc/fleet/recipe"))
        self.assertIn('echo "$RECIPE" > /etc/fleet/recipe', text)
        self.assertEqual(REC.ALIAS_FILE, Path("/etc/fleet/alias"))
        self.assertIn('echo "$ALIAS" > /etc/fleet/alias', text)
        self.assertIn('LIB_DIR="/usr/local/lib/fleet"', text)
        self.assertEqual(REC.LIB_DIR, Path("/usr/local/lib/fleet"))
        self.assertEqual(REC.BOOTSTRAP, "open-worlds/machine/bootstrap.sh")
        self.assertIn('FLEET_REF="${FLEET_REF:-}"', text)


if __name__ == "__main__":
    unittest.main()
