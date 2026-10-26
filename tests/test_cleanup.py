# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""The machine's disk safety: old engine images go, in use ones never; pre-update copies are capped; the door sees the disk."""

from pathlib import Path
import contextlib
import fcntl
import io
import importlib.util
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock


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


R = load("reconcile")
C = load("cleanup")
REL = load("release")

HOUR = 3600
DAY = 86400
GIB = 1024**3
T0 = 1_800_000_000.0  # a fixed instant
ENGINE = "ghcr.io/numengames/numinia-engine"
CADDY = R.CADDY_IMAGE


def image_id(n):
    return "sha256:" + f"{n:064x}"


def digest(n):
    return "sha256:" + f"{n + 5000:064x}"


def stamp(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + ".123456789Z"


def inspect(n, age_hours, repo=ENGINE, tag=None, by_digest=True, tagged_hours=None, size=1000):
    """One entry of `docker image inspect`: image n, built age_hours ago."""
    entry = {"Id": image_id(n), "Created": stamp(T0 - age_hours * HOUR), "Size": size,
             "RepoTags": [f"{repo}:{tag}"] if tag else [],
             "RepoDigests": [f"{repo}@{digest(n)}"] if by_digest and repo else [],
             "Metadata": {"LastTagTime": "0001-01-01T00:00:00Z"}}
    if tagged_hours is not None:
        entry["Metadata"]["LastTagTime"] = stamp(T0 - tagged_hours * HOUR)
    return entry


def engine_ref(n):
    return f"{ENGINE}@{digest(n)}"


class Machine(unittest.TestCase):
    """A fleet home in a temporary folder and a fake Docker that remembers what it was asked to remove."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        for folder in ("orders", "release", "run"):
            (self.home / folder).mkdir()
        self.clock = T0
        self.logs = []
        self.entries = []  # what docker image inspect says
        self.containers = set()  # what containers use
        self.refuse = set()  # ids whose `docker rmi` fails
        self.removed = []
        self.docker_down = False
        self.disks = [(37 * GIB, 6 * GIB)]  # (total, free) now; each call takes the next, the last one stays
        env = C.Env(self.home)
        env.now = lambda: self.clock
        env.log = self.logs.append
        env.disk = self.disk
        env.images = self.images
        env.used = self.used
        env.remove = self.remove
        env.container_log_bytes = lambda: 123
        env.journal_bytes = lambda: 456
        self.env = env

    def disk(self):
        return self.disks.pop(0) if len(self.disks) > 1 else self.disks[0]

    def images(self):
        if self.docker_down:
            raise C.DockerError("docker image ended with 1")
        return C.parse_images(self.entries)

    def used(self):
        if self.docker_down:
            raise C.DockerError("docker ps ended with 1")
        return set(self.containers)

    def remove(self, image):
        if image in self.refuse:
            return False, "docker rmi ended with 1"
        self.removed.append(image)
        return True, "ok"

    def order(self, wid, image, **extra):
        (self.home / "orders" / f"{wid}.json").write_text(json.dumps(
            {"id": wid, "domain": f"{wid}.example.org", "image": image, "state": "running", **extra}))

    def release_state(self, **state):
        (self.home / "release" / "state.json").write_text(json.dumps(state))

    def go(self, dry=False):
        return C.run_pass(self.env, dry_run=dry)

    def state(self):
        return json.loads((self.home / "cleanup" / "state.json").read_text())


class Images(Machine):
    def test_old_unused_images_go_by_their_id_and_the_newest_two_of_the_repository_stay(self):
        self.entries = [inspect(n, 100 * n) for n in (1, 2, 3, 4, 5)]  # 100 h … 500 h old; 1 is the newest
        self.order("a", engine_ref(1))
        summary = self.go()
        self.assertEqual(sorted(self.removed), sorted(image_id(n) for n in (3, 4, 5)))
        self.assertEqual(summary["removedImages"], 3)
        self.assertEqual(summary["kept"], 2)
        self.assertEqual(summary["removedUpToBytes"], 3000)

    def test_an_image_a_container_uses_stays_running_or_stopped(self):
        self.entries = [inspect(n, 100 * n) for n in (1, 2, 3, 4, 5, 6)]
        self.containers = {image_id(5), engine_ref(6)}  # one named by its id, one by the name its container was made from
        self.go()
        self.assertEqual(sorted(self.removed), sorted(image_id(n) for n in (3, 4)))

    def test_an_image_an_order_names_stays_as_image_previous_image_or_pin(self):
        self.entries = [inspect(n, 100 * n) for n in (1, 2, 3, 4, 5, 6, 7)]
        self.order("a", engine_ref(1), previousImage=engine_ref(3))
        self.order("b", engine_ref(2), pin={"image": engine_ref(4), "reason": "held", "at": "x", "by": "y"})
        self.order("c", engine_ref(5))  # the stopped world keeps its image too
        self.go()
        self.assertEqual(sorted(self.removed), sorted(image_id(n) for n in (6, 7)))

    def test_the_release_record_keeps_the_target_the_previous_and_the_applied_image(self):
        self.entries = [inspect(n, 100 * n) for n in (1, 2, 3, 4, 5, 6, 7)]
        self.order("a", engine_ref(1))
        self.release_state(target={"image": engine_ref(3)}, applied={"image": engine_ref(4)},
                           rolling={"previous": engine_ref(5), "target": engine_ref(6)},
                           bad=[engine_ref(7)], halted={"image": engine_ref(7)})
        self.go()
        self.assertEqual(self.removed, [image_id(7)])  # a digest written down as bad is no reason to keep it

    def test_caddy_and_the_compose_files_keep_their_images(self):
        old_caddy = inspect(10, 900, repo="caddy", tag="2-alpine", by_digest=False)
        other = inspect(11, 901, repo="caddy", tag="2.7", by_digest=False)
        newer = [inspect(12, 10, repo="caddy", tag="2.9", by_digest=False), inspect(13, 20, repo="caddy", tag="2.8", by_digest=False)]
        used = inspect(14, 950, repo="ghcr.io/numengames/numinia-hyperfy2", tag="sha-abcdef1", by_digest=False)
        pair = [inspect(15, 10, repo="ghcr.io/numengames/numinia-hyperfy2", tag="sha-1111111", by_digest=False),
                inspect(16, 20, repo="ghcr.io/numengames/numinia-hyperfy2", tag="sha-2222222", by_digest=False)]
        self.entries = [old_caddy, other, *newer, used, *pair]
        (self.home / "run" / "docker-compose.applied.yml").write_text(json.dumps(
            {"services": {"caddy": {"image": CADDY}, "x": {"image": "ghcr.io/numengames/numinia-hyperfy2:sha-abcdef1"}}}))
        old_caddy["RepoTags"] = [CADDY]
        self.go()
        self.assertEqual(self.removed, [image_id(11)])  # only the caddy nobody uses

    def test_caddys_image_stays_on_its_own_even_with_no_compose_file_and_no_container(self):
        self.entries = [inspect(10, 900, repo="caddy", tag="2-alpine", by_digest=False),
                        inspect(11, 901, repo="caddy", tag="2.7", by_digest=False),
                        inspect(12, 10, repo="caddy", tag="2.9", by_digest=False),
                        inspect(13, 20, repo="caddy", tag="2.8", by_digest=False)]
        self.go()
        self.assertEqual(self.removed, [image_id(11)])

    def test_a_young_image_stays_and_so_does_one_tagged_or_pulled_a_moment_ago(self):
        self.entries = [inspect(1, 10), inspect(2, 20), inspect(3, 47), inspect(4, 49),
                        inspect(5, 600, tagged_hours=1),  # built long ago, pulled an hour ago
                        inspect(6, 600, tagged_hours=47.5), inspect(7, 600)]
        self.order("a", engine_ref(1))
        self.go()
        self.assertEqual(sorted(self.removed), sorted(image_id(n) for n in (4, 7)))

    def test_the_newest_two_stay_even_when_nothing_else_says_so(self):
        """State odd (no order, no record): a rollback target still exists."""
        self.entries = [inspect(1, 500), inspect(2, 600), inspect(3, 700)]
        self.go()
        self.assertEqual(self.removed, [image_id(3)])

    def test_dangling_images_go_when_old_and_stay_when_young_or_used(self):
        self.entries = [inspect(1, 500, repo=""), inspect(2, 10, repo=""), inspect(3, 600, repo=""),
                        inspect(4, 100), inspect(5, 200)]
        self.containers = {image_id(3)}
        self.go()
        self.assertEqual(self.removed, [image_id(1)])

    def test_images_of_other_repositories_are_counted_apart(self):
        self.entries = [inspect(1, 500), inspect(2, 600, repo="ghcr.io/numengames/numinia-hyperfy2"),
                        inspect(3, 700, repo="ghcr.io/numengames/numinia-hyperfy2")]
        self.go()
        self.assertEqual(self.removed, [])  # two of each repository at most: nothing is the third of its own

    def test_dry_run_removes_nothing_and_writes_nothing_but_says_what_would_go(self):
        self.entries = [inspect(n, 100 * n) for n in (1, 2, 3, 4)]
        self.order("a", engine_ref(1))
        summary = self.go(dry=True)
        self.assertEqual(self.removed, [])
        self.assertFalse((self.home / "cleanup").exists())
        self.assertTrue(summary["dryRun"])
        self.assertEqual([r["id"] for r in summary["removed"]], [image_id(3), image_id(4)])
        self.assertIn("would remove 2 old image(s)", summary["note"])
        self.assertEqual(summary["freedBytes"], 0)

    def test_an_image_docker_refuses_to_remove_is_reported_and_the_others_still_go(self):
        self.entries = [inspect(n, 100 * n) for n in (1, 2, 3, 4, 5)]
        self.refuse = {image_id(4)}
        summary = self.go()
        self.assertEqual(sorted(self.removed), sorted([image_id(3), image_id(5)]))
        self.assertEqual(summary["failed"], [{"id": image_id(4), "why": "docker rmi ended with 1"}])
        self.assertEqual(summary["removedImages"], 2)
        self.assertIn("1 image(s) could not be removed", summary["note"])
        self.assertTrue(any("not removed" in line for line in self.logs))

    def test_nothing_is_removed_when_docker_cannot_say_what_runs(self):
        self.entries = [inspect(n, 100 * n) for n in (1, 2, 3, 4, 5)]
        self.docker_down = True
        summary = self.go()
        self.assertEqual((self.removed, summary["removedImages"]), ([], 0))
        self.assertIn("images not touched", summary["note"])

    def test_nothing_is_removed_when_an_order_cannot_be_read_or_the_book_is_missing(self):
        self.entries = [inspect(n, 100 * n) for n in (1, 2, 3, 4, 5)]
        (self.home / "orders" / "broken.json").write_text("{not json")
        self.go()
        self.assertEqual(self.removed, [])
        (self.home / "orders" / "broken.json").unlink()
        (self.home / "orders").rmdir()
        self.go()
        self.assertEqual(self.removed, [])

    def test_a_corrupt_release_record_costs_nothing_the_newest_two_still_stay(self):
        self.entries = [inspect(n, 100 * n) for n in (1, 2, 3)]
        self.order("a", engine_ref(1))
        (self.home / "release" / "state.json").write_text("{broken")
        self.go()
        self.assertEqual(self.removed, [image_id(3)])

    def test_nothing_is_removed_while_an_update_holds_the_release_lock(self):
        self.entries = [inspect(n, 100 * n) for n in (1, 2, 3, 4)]
        with open(self.home / "release" / "lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            summary = self.go()
        self.assertEqual(self.removed, [])
        self.assertIn("an update (release.py) is running", summary["note"])
        self.go()  # lock free again
        self.assertEqual(sorted(self.removed), sorted([image_id(3), image_id(4)]))

    def test_a_malformed_listing_is_ignored_entry_by_entry(self):
        good = inspect(1, 500)
        parsed = C.parse_images([good, {"Id": "latest"}, {"Id": image_id(2), "Created": "yesterday", "Size": True},
                                 "x", None, {"Id": image_id(3), "RepoTags": ["<none>:<none>"], "RepoDigests": None}])
        self.assertEqual([i["id"] for i in parsed], [image_id(1), image_id(2), image_id(3)])
        self.assertEqual((parsed[1]["created"], parsed[1]["size"], parsed[2]["refs"]), (None, 0, []))
        self.assertEqual(C.parse_images({"not": "a list"}), [])
        # an image whose age is not known is never removed
        self.entries = [{"Id": image_id(9)}, inspect(1, 10), inspect(2, 20)]
        self.go()
        self.assertEqual(self.removed, [])

    def test_helpers_read_what_docker_writes(self):
        self.assertEqual(C.parse_time("2026-10-20T10:11:12.123456789Z"), 1_792_491_072.0)
        self.assertEqual(C.parse_time("2026-10-20T10:11:12+02:00"), 1_792_491_072.0)
        for bad in (None, 5, "", "2026-10-20", "0001-01-01T00:00:00Z", "2026-13-20T10:11:12Z", "2026-10-20T25:11:12Z"):
            self.assertIsNone(C.parse_time(bad))
        self.assertEqual(C.repository_of("ghcr.io/x/y@sha256:" + "a" * 64), "ghcr.io/x/y")
        self.assertEqual(C.repository_of("localhost:5000/x/y:tag"), "localhost:5000/x/y")
        self.assertEqual(C.repository_of("localhost:5000/x/y"), "localhost:5000/x/y")
        self.assertEqual(C.strings_in({"a": ["x", {"b": "y"}, 3], "c": None}), {"x", "y"})


class Preupdate(Machine):
    def setUp(self):
        super().setUp()
        self.folder = self.home / "release" / "preupdate"
        self.folder.mkdir()

    def copy(self, world, age_days, size=100, name=None):
        at = int(T0 - age_days * DAY)
        path = self.folder / (name or f"{world}-{at}.zip")
        path.write_bytes(b"z" * size)
        os.utime(path, (at, at))
        return path.name

    def names(self):
        return sorted(p.name for p in self.folder.iterdir())

    def test_a_copy_older_than_14_days_goes_and_a_younger_one_stays(self):
        old, young = self.copy("a", 15), self.copy("a", 13)
        done = C.prune_preupdate(self.folder, T0)
        self.assertEqual((done["removed"], self.names()), ([old], [young]))

    def test_over_the_cap_the_oldest_go_first_but_never_the_newest_copy_of_a_world(self):
        a1, a2, a3 = self.copy("a", 10, 400), self.copy("a", 5, 400), self.copy("a", 1, 400)
        b1 = self.copy("b", 9, 400)  # the only copy of b: protected
        done = C.prune_preupdate(self.folder, T0, cap=900)
        self.assertEqual(done["removed"], [a1, a2])  # 1600 → 800 ≤ 900 once the two oldest of `a` are gone
        self.assertEqual(self.names(), sorted([a3, b1]))
        self.assertEqual((done["bytes"], done["left"], done["capped"]), (800, 800, False))

    def test_the_cap_is_met_with_the_newest_of_each_world_if_it_can_be_met_at_all(self):
        newest = [self.copy(w, 1, 500) for w in "abc"] + [self.copy("a", 3, 500)]
        done = C.prune_preupdate(self.folder, T0, cap=100)
        self.assertEqual(self.names(), sorted(newest[:3]))
        self.assertTrue(done["capped"])  # 1500 bytes left, more than the cap: only the last copies, said so
        self.assertEqual(done["left"], 1500)

    def test_a_world_whose_name_has_a_number_in_it_is_told_apart(self):
        first, second = self.copy("plaza-2", 4, 300), self.copy("plaza", 4, 300)
        newest = self.copy("plaza-2", 1, 300)
        C.prune_preupdate(self.folder, T0, cap=700)
        self.assertEqual(self.names(), sorted([second, newest]))
        self.assertNotIn(first, self.names())

    def test_a_zip_with_another_name_is_only_ever_a_candidate_never_protected(self):
        odd = self.copy("x", 3, 600, name="hand-made.zip")
        keep = self.copy("a", 1, 600)
        C.prune_preupdate(self.folder, T0, cap=700)
        self.assertEqual(self.names(), [keep])
        self.assertNotEqual(odd, keep)

    def test_links_other_files_and_a_missing_folder_are_left_alone(self):
        (self.folder / "notes.txt").write_text("x")
        outside = self.home / "outside.zip"
        outside.write_bytes(b"z" * 5000)
        os.symlink(outside, self.folder / "a-1800000000.zip")
        done = C.prune_preupdate(self.folder, T0 + 99 * DAY, cap=1)
        self.assertEqual(done["removed"], [])
        self.assertTrue(outside.exists())
        self.assertEqual(C.prune_preupdate(self.home / "nope", T0), {"removed": [], "bytes": 0, "left": 0, "capped": False})

    def test_dry_run_removes_no_copy(self):
        self.copy("a", 20), self.copy("a", 2, 100), self.copy("a", 1, 100)
        before = self.names()
        done = C.prune_preupdate(self.folder, T0, dry_run=True, cap=150)
        self.assertEqual(len(done["removed"]), 2)
        self.assertEqual(self.names(), before)

    def test_a_copy_that_cannot_be_deleted_is_not_counted_as_gone(self):
        self.copy("a", 20)
        with mock.patch.object(Path, "unlink", side_effect=PermissionError):
            done = C.prune_preupdate(self.folder, T0)
        self.assertEqual((done["removed"], done["bytes"], done["left"]), ([], 0, 100))

    def test_the_default_cap_is_two_gibibytes(self):
        self.assertEqual(C.PREUPDATE_CAP_BYTES, 2 * GIB)
        self.assertEqual(C.PREUPDATE_KEEP_SECONDS, 14 * DAY)

    def test_the_update_pass_prunes_with_the_same_rule_and_never_fails_because_of_it(self):
        old = self.copy("a", 20)
        env = REL.Env(self.home)
        env.now = lambda: T0
        env.log = self.logs.append
        REL.prune_backups(env)
        self.assertNotIn(old, self.names())
        self.assertFalse(hasattr(REL, "KEEP_SECONDS"))  # one rule, in cleanup.py
        with mock.patch.dict(sys.modules, {"cleanup": None}):  # a missing cleanup.py
            REL.prune_backups(env)
        self.assertTrue(any("not pruned" in line for line in self.logs))

    def test_the_pass_prunes_the_copies_and_says_how_many_bytes_are_left(self):
        self.copy("a", 20), self.copy("a", 1, 700)
        summary = self.go()
        self.assertEqual((summary["preupdateRemoved"], summary["preupdateBytes"]), (1, 700))


class Facts(Machine):
    def test_the_state_follows_the_thresholds(self):
        for percent, state in ((0, "ok"), (79.9, "ok"), (80, "warn"), (89.9, "warn"), (90, "full"), (100, "full")):
            with self.subTest(percent=percent):
                self.assertEqual(C.disk_state(percent), state)

    def test_the_pass_writes_the_disk_facts_the_door_reads(self):
        self.disks = [(37 * GIB, 6 * GIB), (37 * GIB, 6 * GIB)]
        summary = self.go()
        saved = self.state()
        self.assertEqual(saved, summary)
        self.assertEqual((saved["usedPct"], saved["freeBytes"], saved["totalBytes"], saved["state"]),
                         (83.8, 6 * GIB, 37 * GIB, "warn"))
        self.assertEqual(saved["at"], R.utc(T0))
        self.assertEqual((saved["containerLogBytes"], saved["journalBytes"]), (123, 456))
        mode = stat.S_IMODE(os.stat(self.home / "cleanup" / "state.json").st_mode)
        self.assertEqual(mode, 0o644)  # the door's user reads it
        self.assertEqual(stat.S_IMODE(os.stat(self.home / "cleanup").st_mode), 0o755)

    def test_the_freed_bytes_are_what_the_disk_really_gained_not_the_sum_of_sizes(self):
        self.entries = [inspect(n, 100 * n, size=5 * GIB) for n in (1, 2, 3, 4)]
        self.disks = [(37 * GIB, 6 * GIB), (37 * GIB, 7 * GIB)]  # two images of 5 GiB shared most of their layers
        summary = self.go()
        self.assertEqual((summary["freedBytes"], summary["removedUpToBytes"]), (GIB, 10 * GIB))
        self.assertIn("up to 10240 MiB", summary["note"])
        self.assertEqual(summary["usedPct"], 81.1)

    def test_still_over_85_after_cleaning_nothing_else_is_deleted_it_is_only_said(self):
        self.entries = [inspect(1, 500), inspect(2, 600)]  # nothing is removable
        self.disks = [(100 * GIB, 13 * GIB)]
        (self.home / "nightly").mkdir()
        (self.home / "nightly" / "keep.zip").write_bytes(b"z")
        summary = self.go()
        self.assertEqual((summary["usedPct"], summary["state"]), (87.0, "warn"))
        self.assertIn("nothing else is deleted automatically", summary["note"])
        self.assertEqual((self.removed, summary["removedImages"]), ([], 0))
        self.assertTrue((self.home / "nightly" / "keep.zip").exists())

    def test_full_is_said_and_the_command_ends_with_an_error_so_systemd_shows_it(self):
        self.disks = [(100 * GIB, 5 * GIB)]
        self.assertEqual(self.go()["state"], "full")
        with mock.patch.object(C, "Env", return_value=self.env), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(C.main(["--home", str(self.home)]), 1)
            self.disks = [(100 * GIB, 50 * GIB)]
            self.assertEqual(C.main(["--home", str(self.home)]), 0)
            self.assertEqual(C.main(["--home", str(self.home), "--dry-run"]), 0)

    def test_nothing_to_do_says_so(self):
        self.disks = [(100 * GIB, 60 * GIB)]
        summary = self.go()
        self.assertEqual((summary["state"], summary["note"], summary["removedImages"]), ("ok", "nothing to clean", 0))

    def test_a_pass_never_raises_when_the_disk_or_a_folder_misbehaves(self):
        self.env.disk = mock.Mock(side_effect=OSError("gone"))
        summary = self.go()
        self.assertEqual((summary["usedPct"], summary["totalBytes"]), (0.0, 0))

    def test_a_second_pass_at_once_does_not_run(self):
        (self.home / "cleanup").mkdir()
        with open(self.home / "cleanup" / "lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(C, "Env", return_value=self.env):
                self.assertEqual(C.main(["--home", str(self.home)]), 0)
        self.assertFalse((self.home / "cleanup" / "state.json").exists())


class RealDockerCommands(Machine):
    """Env's own docker calls, against a shim `docker` on PATH that answers like the real one."""

    def shim(self, listing, inspect_out, containers, container_images, fail=()):
        bin_dir = self.home / "bin"
        bin_dir.mkdir(exist_ok=True)
        script = bin_dir / "docker"
        script.write_text(f"""#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
with open({str(self.home / 'docker-calls.log')!r}, "a") as log:
    log.write(json.dumps(args) + "\\n")
if args[0] in {list(fail)!r}:
    sys.exit(1)
if args[:2] == ["image", "ls"]:
    print({listing!r})
elif args[:2] == ["image", "inspect"]:
    print({(inspect_out if isinstance(inspect_out, str) else json.dumps(inspect_out))!r})
elif args[0] == "ps":
    print({containers!r})
elif args[:2] == ["container", "inspect"]:
    print({container_images!r})
elif args[0] == "rmi":
    print("Deleted: " + args[1])
""")
        script.chmod(0o755)
        return mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})

    def calls(self):
        return [json.loads(line) for line in (self.home / "docker-calls.log").read_text().splitlines()]

    def test_images_containers_and_removal_go_through_docker_by_id_without_force(self):
        entries = [inspect(1, 100), inspect(2, 200), inspect(3, 300)]
        listing = "\n".join([image_id(1), image_id(2), image_id(3), image_id(3), "junk", "-f"])
        env = C.Env(self.home)
        with self.shim(listing, entries, "abc\ndef\n", f"{image_id(2)} {ENGINE}@{digest(2)}\n{image_id(9)} caddy:2-alpine\n"):
            self.assertEqual([i["id"] for i in env.images()], [image_id(1), image_id(2), image_id(3)])
            self.assertEqual(env.used(), {image_id(2), f"{ENGINE}@{digest(2)}", image_id(9), "caddy:2-alpine"})
            self.assertEqual(env.remove(image_id(3)), (True, "ok"))
            self.assertEqual(env.remove("latest"), (False, "not an image id"))
            self.assertEqual(env.remove("-f"), (False, "not an image id"))
        calls = self.calls()
        self.assertIn(["image", "ls", "-q", "--no-trunc"], calls)
        self.assertIn(["image", "inspect", image_id(1), image_id(2), image_id(3)], calls)  # unique, well-formed ids only
        self.assertIn(["container", "inspect", "--format", "{{.Image}} {{.Config.Image}}", "abc", "def"], calls)
        self.assertEqual([c for c in calls if c[0] == "rmi"], [["rmi", image_id(3)]])  # never -f, never prune
        self.assertFalse(any("prune" in c for c in calls))

    def test_docker_failing_or_missing_raises_one_error_the_pass_understands(self):
        env = C.Env(self.home)
        with self.shim("", [], "", "", fail=("image", "ps", "rmi")):
            for call in (env.images, env.used):
                with self.assertRaises(C.DockerError):
                    call()
            ok, why = env.remove(image_id(1))
            self.assertFalse(ok)
            self.assertIn("docker rmi ended with 1", why)
        with mock.patch.dict(os.environ, {"PATH": str(self.home / "nothing-here")}):
            with self.assertRaises(C.DockerError):
                env.images()

    def test_no_images_and_no_containers_need_no_second_call(self):
        env = C.Env(self.home)
        with self.shim("", [], "", ""):
            self.assertEqual((env.images(), env.used()), ([], set()))
        self.assertEqual(len(self.calls()), 2)

    def test_garbage_from_inspect_is_an_error_not_a_guess(self):
        env = C.Env(self.home)
        with self.shim(image_id(1), "<<not json", "", ""):
            with self.assertRaises(C.DockerError):
                env.images()

    def test_the_log_and_journal_sizes_are_only_read(self):
        env = C.Env(self.home)
        logs = self.home / "containers" / "abc"
        logs.mkdir(parents=True)
        (logs / "abc-json.log").write_bytes(b"x" * 10)
        (logs / "config.v2.json").write_bytes(b"x" * 99)
        with mock.patch.object(C, "CONTAINER_LOGS", str(self.home / "containers" / "*" / "*-json.log")):
            self.assertEqual(env.container_log_bytes(), 10)
        journal = self.home / "journal" / "id"
        journal.mkdir(parents=True)
        (journal / "system.journal").write_bytes(b"x" * 7)
        with mock.patch.object(C, "JOURNAL_DIR", str(self.home / "journal")):
            self.assertEqual(env.journal_bytes(), 7)
        with mock.patch.object(C, "JOURNAL_DIR", str(self.home / "no-journal")):
            self.assertIsNone(env.journal_bytes())

    def test_the_disk_is_measured_where_docker_keeps_its_images(self):
        env = C.Env(self.home, docker_dir=str(self.home))
        total, free = env.disk()
        self.assertGreater(total, free)
        self.assertGreater(free, 0)


class Wiring(unittest.TestCase):
    text = (MACHINE / "bootstrap.sh").read_text()

    def test_the_bootstrap_installs_cleanup_the_way_it_installs_the_others(self):
        self.assertIn("versions.py cleanup.py; do", self.text)
        self.assertIn("wrapper fleet-cleanup cleanup.py", self.text)
        self.assertIn("ExecStart=/usr/local/sbin/fleet-cleanup", self.text)
        self.assertIn("cat > /etc/systemd/system/fleet-cleanup.service", self.text)
        self.assertIn("cat > /etc/systemd/system/fleet-cleanup.timer", self.text)
        timer = self.text[self.text.index("cat > /etc/systemd/system/fleet-cleanup.timer"):]
        timer = timer[:timer.index("\nEOF\n")]
        self.assertIn("OnCalendar=*-*-* 04:10:00", timer)
        self.assertIn("Persistent=true", timer)
        nightly = self.text[self.text.index("cat > /etc/systemd/system/fleet-nightly.timer"):][:300]
        self.assertIn("OnCalendar=*-*-* 03:30:00", nightly)  # cleanup runs after it
        self.assertIn("systemctl stop fleet-reconcile.timer fleet-reconcile.path fleet-release.timer fleet-nightly.timer fleet-cleanup.timer", self.text)
        self.assertIn("enable --now fleet-reconcile.timer fleet-reconcile.path fleet-release.timer fleet-nightly.timer fleet-cleanup.timer fleet-recipe.timer", self.text)
        self.assertIn("fleet-nightly.timer fleet-cleanup.timer fleet-recipe.timer; do", self.text)

    def test_the_journal_gets_a_cap_by_a_drop_in_and_docker_is_left_alone(self):
        self.assertIn("/etc/systemd/journald.conf.d/fleet.conf", self.text)
        self.assertIn("SystemMaxUse=200M", self.text)
        self.assertIn("systemctl restart systemd-journald", self.text)
        for line in self.text.splitlines():
            if not line.lstrip().startswith("#"):
                self.assertNotIn("daemon.json", line)
                self.assertNotRegex(line, r"restart\s+docker")

    def test_the_recipe_updater_watches_the_new_timer_and_waits_for_a_cleanup_that_runs(self):
        REC = load("recipe")
        self.assertIn("fleet-cleanup.timer", REC.TIMERS)
        self.assertIn("fleet-cleanup.service", REC.OTHER_JOBS)

    def test_no_world_service_carries_a_logging_key_because_it_would_recreate_every_running_world(self):
        """compose_dict's services are compared by `docker compose up` (their config hash) and by save_waits: a new key
        in every service restarts every world on the next pass. Docker's log caps wait for a natural recreate."""
        order = {"id": "a", "domain": "a.example.org", "image": engine_ref(1), "state": "running",
                 "limits": {"memory": "512m", "cpus": "0.5", "maxUploadMb": 10}}
        services = R.compose_dict([order], self.home_for_compose())["services"]
        for name, service in services.items():
            with self.subTest(service=name):
                self.assertNotIn("logging", service)

    def home_for_compose(self):
        return Path("/srv/fleet")


if __name__ == "__main__":
    unittest.main()
