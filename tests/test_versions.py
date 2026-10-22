# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""A world keeps up to two versions: what is written about them, and how one is deleted.

The five new requests (DuplicateVersion, PublishVersion, DeleteVersion,
DownloadVersion, ImportVersion) are signed here by a throwaway Oracle: the
shared viem vectors learn them in a later story. DeleteVersion is exercised
through the reconciler's own take_requests, as the door leaves it in the inbox.
"""

from pathlib import Path
import contextlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MACHINE = ROOT / "open-worlds" / "machine"
HERE = Path(__file__).resolve().parent


def load(name, folder=MACHINE):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    spec = importlib.util.spec_from_file_location(name, folder / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


R = load("reconcile")
W = load("walletsig")
V = load("versions")
Z = load("fleet_zips", HERE)

NOW = 1893456000 - 60
EXPIRES = NOW + 600
CONTROL = "open-1.numinia.com"
IMAGE = "ghcr.io/numengames/numinia-hyperfy2:sha-0e2045370989d051e73f81c7c9d53121d5c374b3"
SHA = "ab" * 32
COPY = "c0ffee" + "0" * 26
WHO = "0x" + "12" * 20


def info(origin="created", size=None, at="2026-10-01T00:00:00Z", by=None):
    return {"createdAt": at, "createdBy": by, "origin": origin, "bytes": size}


def two(published="v1", following=3):
    """The metadata of a world with two versions, v1 and v2."""
    return {
        "world": "plaza",
        "published": published,
        "next": following,
        "versions": {"v1": info(), "v2": info("copy:v1", 1234, by=WHO)},
    }


def message(kind, world="plaza", **over):
    """A well-formed message of one of the new request types."""
    base = {"machine": "open-1", "world": world, "version": "v2", "nonce": os.urandom(16).hex(), "expires": EXPIRES}
    if kind == "ImportVersion":
        base = {"machine": "open-1", "world": world, "zipBytes": 4096, "zipSha256": SHA, "replace": "",
                "nonce": base["nonce"], "expires": EXPIRES}
    base.update(over)
    return base


class Machine:
    """A temporary fleet home with an inbox, a book, a world `plaza` and a throwaway Oracle."""

    def __init__(self, test):
        temp = tempfile.TemporaryDirectory(prefix="fleet-versions-")
        test.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.orders = self.home / "orders"
        self.codes = self.home / "codes"
        for folder in ("inbox", "orders", "env", "data", "requests", "copies", "run"):
            (self.home / folder).mkdir()
        self.oracle = Z.Oracle(W)
        self.dropped = 0
        self.err = ""

    def put_world(self, wid="plaza", created="2026-10-01T00:00:00Z"):
        order = {
            "id": wid, "card": wid, "state": "running", "server": "open-1", "domain": f"{wid}.numinia.com",
            "image": IMAGE, "limits": {"memory": "2g", "cpus": "1.5", "maxUploadMb": 50}, "createdAt": created,
        }
        (self.orders / f"{wid}.json").write_text(json.dumps(order), encoding="utf-8")
        data = self.home / "data" / wid
        data.mkdir(parents=True)
        (data / "db.sqlite").write_bytes(b"published bytes")
        return order

    def put_version(self, vid, wid="plaza", content=b"unpublished bytes"):
        folder = self.home / "versions" / wid / vid
        folder.mkdir(parents=True)
        (folder / "db.sqlite").write_bytes(content)
        return folder

    def write_meta(self, meta, wid="plaza"):
        V.write_versions(self.home, wid, meta)

    def meta(self, wid="plaza"):
        return json.loads((self.home / "versions" / f"{wid}.json").read_text(encoding="utf-8"))

    def drop(self, kind, msg, copy=None, **door):
        raw = {"type": kind, "message": msg, "signature": self.oracle.sign(kind, msg), "signer": self.oracle.address,
               "receivedAt": R.utc(NOW), **door}
        if copy is not None:
            raw["copy"] = copy
        self.dropped += 1
        (self.home / "inbox" / f"{self.dropped:013d}-{msg['nonce']}.json").write_text(json.dumps(raw), encoding="utf-8")
        return msg["nonce"]

    def take(self, now=NOW):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            taken = R.take_requests(self.home, self.orders, "open-1", frozenset({self.oracle.address}), frozenset(),
                                    CONTROL, now, codes_dir=self.codes)
        self.err = err.getvalue()
        return taken

    def result(self, nonce):
        return json.loads((self.home / "requests" / f"{nonce}.json").read_text(encoding="utf-8"))

    def log(self):
        path = self.home / "log.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def delete(self, version="v2", world="plaza"):
        """Sign and take one DeleteVersion; its result."""
        nonce = self.drop("DeleteVersion", message("DeleteVersion", world=world, version=version))
        self.take()
        return self.result(nonce)


class Metadata(unittest.TestCase):
    def test_a_world_without_a_file_has_one_implicit_published_version(self):
        m = Machine(self)
        order = m.put_world(created="2026-09-30T12:00:00Z")
        meta = V.read_versions(m.home, order)
        self.assertEqual(meta, {
            "world": "plaza", "published": "v1", "next": 2,
            "versions": {"v1": {"createdAt": "2026-09-30T12:00:00Z", "createdBy": None, "origin": "created",
                                "bytes": None}},
        })
        self.assertFalse((m.home / "versions").exists())  # reading writes nothing

    def test_an_order_without_a_date_gives_a_version_without_one(self):
        m = Machine(self)
        order = m.put_world()
        del order["createdAt"]
        self.assertIsNone(V.read_versions(m.home, order)["versions"]["v1"]["createdAt"])
        order["createdAt"] = 7
        self.assertIsNone(V.read_versions(m.home, order)["versions"]["v1"]["createdAt"])

    def test_written_metadata_reads_back_and_is_the_doors_to_read(self):
        m = Machine(self)
        order = m.put_world()
        meta = two()
        V.write_versions(m.home, "plaza", meta)
        self.assertEqual(V.read_versions(m.home, order), meta)
        mode = lambda p: stat.S_IMODE(p.stat().st_mode)  # noqa: E731
        self.assertEqual(mode(m.home / "versions" / "plaza.json"), 0o640)
        self.assertEqual(mode(m.home / "versions"), 0o750)
        self.assertEqual(mode(m.home / "versions" / "plaza"), 0o700)
        self.assertEqual([p.name for p in (m.home / "versions").iterdir() if p.name.startswith(".")], [])

    def test_a_bad_shape_is_never_written(self):
        m = Machine(self)
        for name, meta in {
            "another world": {**two(), "world": "other"},
            "three versions": {**two(), "versions": {**two()["versions"], "v3": info()}},
            "published unknown": {**two(), "published": "v9"},
        }.items():
            with self.subTest(case=name), self.assertRaises(ValueError):
                V.write_versions(m.home, "plaza", meta)
        with self.assertRaises(ValueError):
            V.write_versions(m.home, "../plaza", two())
        self.assertFalse((m.home / "versions").exists())

    def test_a_file_that_cannot_be_trusted_is_not_the_implicit_metadata(self):
        m = Machine(self)
        order = m.put_world()
        good = two()
        broken = {
            "not json": "{",
            "a list": "[]",
            "another world": json.dumps({**good, "world": "other"}),
            "extra field": json.dumps({**good, "extra": 1}),
            "published unknown": json.dumps({**good, "published": "v9"}),
            "bad name": json.dumps({**good, "versions": {"v1": info(), "v01": info()}}),
            "three versions": json.dumps({**good, "versions": {**good["versions"], "v3": info()}}),
            "no versions": json.dumps({**good, "versions": {}}),
            "next below a number": json.dumps({**good, "next": 2}),
            "next not a number": json.dumps({**good, "next": "3"}),
            "next as bool": json.dumps({**good, "next": True}),
            "next too far": json.dumps({**good, "next": 10_001}),
            "bad origin": json.dumps({**good, "versions": {"v1": info("elsewhere"), "v2": info()}}),
            "bad creator": json.dumps({**good, "versions": {"v1": info(by="me"), "v2": info()}}),
            "bytes as text": json.dumps({**good, "versions": {"v1": info(size="9"), "v2": info()}}),
            "bytes negative": json.dumps({**good, "versions": {"v1": info(size=-1), "v2": info()}}),
            "version missing a field": json.dumps({**good, "versions": {"v1": {"origin": "created"}, "v2": info()}}),
            "huge": " " * (V.META_MAX + 1) + json.dumps(good),
        }
        (m.home / "versions").mkdir()
        path = m.home / "versions" / "plaza.json"
        for name, text in broken.items():
            with self.subTest(case=name):
                path.write_text(text, encoding="utf-8")
                self.assertIsNone(V.read_versions(m.home, order))
                self.assertEqual(V.listed(m.home, order), [])
        path.write_text(json.dumps(good), encoding="utf-8")
        self.assertEqual(V.read_versions(m.home, order), good)

    def test_a_link_in_place_of_the_file_is_refused_not_followed(self):
        m = Machine(self)
        order = m.put_world()
        m.write_meta(two())
        real = m.home / "elsewhere.json"
        (m.home / "versions" / "plaza.json").rename(real)
        (m.home / "versions" / "plaza.json").symlink_to(real)
        self.assertIsNone(V.read_versions(m.home, order))

    def test_an_order_without_a_usable_id_has_no_metadata(self):
        m = Machine(self)
        self.assertIsNone(V.read_versions(m.home, {"id": "../x"}))
        self.assertIsNone(V.read_versions(m.home, {}))

    def test_a_link_in_place_of_the_folders_is_refused(self):
        m = Machine(self)
        target = m.home / "elsewhere"
        target.mkdir()
        (m.home / "versions").symlink_to(target)
        with self.assertRaises(OSError):
            V.write_versions(m.home, "plaza", two())
        (m.home / "versions").unlink()
        (m.home / "versions").mkdir()
        (m.home / "versions" / "plaza").symlink_to(target)
        with self.assertRaises(OSError):
            V.write_versions(m.home, "plaza", two())
        self.assertEqual(list(target.iterdir()), [])


class Names(unittest.TestCase):
    def test_a_name_is_never_given_twice(self):
        meta = V.implicit("plaza")
        self.assertEqual(V.new_vid(meta), "v2")
        self.assertEqual(meta["next"], 3)
        meta["versions"]["v2"] = info()
        del meta["versions"]["v2"]  # deleted again: its name stays spent
        self.assertEqual(V.new_vid(meta), "v3")
        self.assertEqual(V.new_vid(meta), "v4")

    def test_the_last_name_is_v9999_and_then_there_is_none(self):
        meta = {**V.implicit("plaza"), "next": 9999}
        self.assertEqual(V.new_vid(meta), "v9999")
        self.assertEqual(meta["next"], 10_000)
        self.assertTrue(V.valid("plaza", meta))
        self.assertIsNone(V.new_vid(meta))
        self.assertEqual(meta["next"], 10_000)

    def test_the_regex_matches_the_signed_one(self):
        self.assertIs(V.VERSION, R.VERSION)
        for good in ("v1", "v9", "v10", "v9999"):
            self.assertTrue(R.matches(V.VERSION, good), good)
        for bad in ("", "v", "v0", "v01", "v10000", "V1", "1", "v1\n", " v1", "v1.0", "v-1"):
            self.assertFalse(R.matches(V.VERSION, bad), repr(bad))

    def test_the_published_version_is_the_worlds_own_folder(self):
        home = Path("/srv/fleet")
        meta = two(published="v2")
        self.assertEqual(V.folder_of(home, "plaza", meta, "v2"), home / "data" / "plaza")
        self.assertEqual(V.folder_of(home, "plaza", meta, "v1"), home / "versions" / "plaza" / "v1")
        for wid, vid in (("../x", "v1"), ("plaza", "v1/../../x"), ("plaza", "v0")):
            with self.subTest(wid=wid, vid=vid), self.assertRaises(ValueError):
                V.folder_of(home, wid, meta, vid)


class Measure(unittest.TestCase):
    def test_bytes_count_files_once_and_follow_no_link(self):
        temp = tempfile.TemporaryDirectory(prefix="fleet-measure-")
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        folder = base / "version"
        (folder / "assets").mkdir(parents=True)
        (folder / "db.sqlite").write_bytes(b"x" * 100)
        (folder / "assets" / "a.glb").write_bytes(b"y" * 1000)
        os.link(folder / "assets" / "a.glb", folder / "assets" / "again.glb")  # the same inode, a second name
        outside = base / "outside"
        outside.mkdir()
        (outside / "big.bin").write_bytes(b"z" * 5000)
        (folder / "link-to-file").symlink_to(outside / "big.bin")
        (folder / "link-to-folder").symlink_to(outside)
        self.assertEqual(V.measure(folder), 1100)

    def test_a_missing_folder_or_a_link_is_nothing(self):
        temp = tempfile.TemporaryDirectory(prefix="fleet-measure-")
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        (base / "real").mkdir()
        (base / "real" / "f").write_bytes(b"abc")
        (base / "link").symlink_to(base / "real")
        self.assertEqual(V.measure(base / "nowhere"), 0)
        self.assertEqual(V.measure(base / "link"), 0)
        self.assertEqual(V.measure(base / "real"), 3)

    def test_a_file_that_goes_while_counting_is_not_counted(self):
        temp = tempfile.TemporaryDirectory(prefix="fleet-measure-")
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        (folder / "f").write_bytes(b"abc")
        with mock.patch("os.lstat", side_effect=FileNotFoundError):
            self.assertEqual(V.measure(folder), 0)


class Listed(unittest.TestCase):
    def test_versions_are_listed_by_number_not_by_text(self):
        m = Machine(self)
        order = m.put_world()
        meta = {"world": "plaza", "published": "v10", "next": 11,
                "versions": {"v10": info("zip", 5, at="2026-10-05T00:00:00Z", by=WHO), "v2": info("created", None)}}
        m.write_meta(meta)
        self.assertEqual(V.listed(m.home, order), [
            {"id": "v2", "published": False, "bytes": None, "createdAt": "2026-10-01T00:00:00Z", "origin": "created"},
            {"id": "v10", "published": True, "bytes": 5, "createdAt": "2026-10-05T00:00:00Z", "origin": "zip"},
        ])

    def test_a_world_without_a_file_lists_its_implicit_version(self):
        m = Machine(self)
        order = m.put_world(created="2026-09-30T12:00:00Z")
        self.assertEqual(V.listed(m.home, order), [
            {"id": "v1", "published": True, "bytes": None, "createdAt": "2026-09-30T12:00:00Z", "origin": "created"},
        ])


class Delete(unittest.TestCase):
    def test_a_signed_delete_removes_the_folder_then_the_entry(self):
        m = Machine(self)
        m.put_world()
        m.write_meta(two())
        folder = m.put_version("v2")
        nonce = m.drop("DeleteVersion", message("DeleteVersion", version="v2"))
        taken = m.take()
        self.assertEqual([t["type"] for t in taken], ["DeleteVersion"])
        result = m.result(nonce)
        self.assertEqual((result["state"], result["rule"], result["type"], result["world"], result["version"]),
                         ("done", None, "DeleteVersion", "plaza", "v2"))
        self.assertFalse(os.path.lexists(folder))
        self.assertTrue((m.home / "versions" / "plaza").is_dir())  # the world's own versions folder stays
        meta = m.meta()
        self.assertEqual(list(meta["versions"]), ["v1"])
        self.assertEqual((meta["published"], meta["next"]), ("v1", 3))  # the name v2 is spent
        self.assertEqual((m.home / "data" / "plaza" / "db.sqlite").read_bytes(), b"published bytes")
        line = m.log()[-1]
        self.assertEqual((line["type"], line["world"], line["state"], line["by"]),
                         ("DeleteVersion", "plaza", "done", W.short(m.oracle.address)))
        self.assertNotIn("rule", line)
        self.assertNotIn("v2", json.dumps(line))  # the log names no value
        self.assertEqual(list((m.home / "inbox").iterdir()), [])

    def test_the_published_version_may_be_the_second_one(self):
        m = Machine(self)
        m.put_world()
        m.write_meta(two(published="v2"))
        m.put_version("v1")
        self.assertEqual(m.delete("v1")["state"], "done")
        self.assertEqual(list(m.meta()["versions"]), ["v2"])
        self.assertTrue((m.home / "data" / "plaza").is_dir())

    def test_a_world_without_a_file_is_left_without_one(self):
        m = Machine(self)
        m.put_world()
        result = m.delete("v2")
        self.assertEqual((result["state"], result["rule"]), ("failed", "no-version"))
        self.assertFalse((m.home / "versions").exists())

    def test_each_refusal_is_a_named_rule_and_leaves_everything_as_it_was(self):
        m = Machine(self)
        m.put_world()
        m.write_meta(two())
        folder = m.put_version("v2")
        before = (m.home / "versions" / "plaza.json").read_text()
        cases = [
            ("missing", dict(world="ghost", version="v2")),
            ("no-version", dict(version="v7")),
            ("published", dict(version="v1")),
        ]
        for rule, over in cases:
            with self.subTest(rule=rule):
                result = m.delete(**over)
                self.assertEqual((result["state"], result["rule"]), ("failed", rule))
                self.assertEqual(m.log()[-1]["rule"], rule)
                self.assertIn("refuse one request", m.err)
        self.assertTrue((folder / "db.sqlite").is_file())
        self.assertEqual((m.home / "versions" / "plaza.json").read_text(), before)
        self.assertEqual((m.home / "data" / "plaza" / "db.sqlite").read_bytes(), b"published bytes")

    def test_a_world_with_one_version_keeps_it(self):
        m = Machine(self)
        m.put_world()
        for case, meta in {"implicit": None, "written": {**V.implicit("plaza")}}.items():
            with self.subTest(case=case):
                if meta:
                    m.write_meta(meta)
                result = m.delete("v1")
                self.assertEqual((result["state"], result["rule"]), ("failed", "only-version"))
        self.assertEqual((m.home / "data" / "plaza" / "db.sqlite").read_bytes(), b"published bytes")

    def test_metadata_that_cannot_be_trusted_stops_a_delete(self):
        m = Machine(self)
        m.put_world()
        folder = m.put_version("v2")
        (m.home / "versions" / "plaza.json").write_text("{", encoding="utf-8")
        result = m.delete("v2")
        self.assertEqual((result["state"], result["rule"]), ("failed", "versions-unreadable"))
        self.assertTrue(folder.is_dir())

    def test_a_folder_that_cannot_go_keeps_its_entry(self):
        m = Machine(self)
        m.put_world()
        m.write_meta(two())
        folder = m.put_version("v2")
        with mock.patch("shutil.rmtree", side_effect=OSError("busy")):
            result = m.delete("v2")
        self.assertEqual((result["state"], result["rule"]), ("failed", "not-removed"))
        self.assertEqual(list(m.meta()["versions"]), ["v1", "v2"])
        self.assertTrue(folder.is_dir())
        # A second request, once it can go, ends it.
        self.assertEqual(m.delete("v2")["state"], "done")
        self.assertFalse(folder.exists())

    def test_a_metadata_that_cannot_be_written_is_a_named_rule(self):
        m = Machine(self)
        m.put_world()
        m.write_meta(two())
        m.put_version("v2")
        with mock.patch.object(V, "write_versions", side_effect=OSError("disk")):
            rule = V.delete_version(m.home, m.orders, {"world": "plaza"}, {"version": "v2"})
        self.assertEqual(rule, "versions-unwritable")
        # The folder went, the entry stayed; the same request again finishes it.
        self.assertEqual(list(m.meta()["versions"]), ["v1", "v2"])
        self.assertEqual(m.delete("v2")["state"], "done")
        self.assertEqual(list(m.meta()["versions"]), ["v1"])

    def test_a_version_folder_reached_through_a_link_is_unlinked_not_emptied(self):
        m = Machine(self)
        m.put_world()
        m.write_meta(two())
        precious = m.home / "precious"
        precious.mkdir()
        (precious / "keep.bin").write_bytes(b"keep")
        (m.home / "versions" / "plaza" / "v2").symlink_to(precious)
        result = m.delete("v2")
        self.assertEqual(result["state"], "done")
        self.assertFalse(os.path.lexists(m.home / "versions" / "plaza" / "v2"))
        self.assertEqual((precious / "keep.bin").read_bytes(), b"keep")
        self.assertEqual(list(m.meta()["versions"]), ["v1"])

    def test_a_versions_folder_that_is_a_link_removes_nothing(self):
        m = Machine(self)
        m.put_world()
        m.write_meta(two())
        precious = m.home / "precious"
        (precious / "v2").mkdir(parents=True)
        (precious / "v2" / "keep.bin").write_bytes(b"keep")
        mine = m.home / "versions" / "plaza"
        mine.rmdir()
        mine.symlink_to(precious)
        self.assertFalse(V.remove_folder(m.home, "plaza", "v2"))
        self.assertEqual((precious / "v2" / "keep.bin").read_bytes(), b"keep")
        result = m.delete("v2")
        self.assertEqual((result["state"], result["rule"]), ("failed", "not-removed"))

    def test_a_folder_whose_parent_is_not_the_worlds_is_not_removed(self):
        m = Machine(self)
        m.put_world()
        m.write_meta(two())
        folder = m.put_version("v2")
        with mock.patch.object(Path, "resolve", return_value=m.home / "somewhere" / "else"):
            self.assertFalse(V.remove_folder(m.home, "plaza", "v2"))
        self.assertTrue(folder.is_dir())

    def test_remove_folder_is_true_for_a_folder_already_gone_and_never_touches_data(self):
        m = Machine(self)
        m.put_world()
        m.write_meta(two())
        self.assertTrue(V.remove_folder(m.home, "plaza", "v2"))
        self.assertTrue(V.remove_folder(m.home, "plaza", "v1"))  # not in versions/: nothing to remove
        self.assertEqual((m.home / "data" / "plaza" / "db.sqlite").read_bytes(), b"published bytes")
        stray = m.home / "versions" / "plaza" / "v2"
        stray.write_text("a file with a version's name")
        self.assertTrue(V.remove_folder(m.home, "plaza", "v2"))
        self.assertFalse(os.path.lexists(stray))
        for wid, vid in (("../x", "v1"), ("plaza", "../v1")):
            with self.subTest(wid=wid, vid=vid), self.assertRaises(ValueError):
                V.remove_folder(m.home, wid, vid)

    def test_an_unlinkable_stray_is_not_removed(self):
        m = Machine(self)
        m.put_world()
        m.write_meta(two())
        stray = m.home / "versions" / "plaza" / "v2"
        stray.write_text("x")
        with mock.patch.object(Path, "unlink", side_effect=OSError("denied")):
            self.assertFalse(V.remove_folder(m.home, "plaza", "v2"))


class Purge(unittest.TestCase):
    def test_deleting_a_world_removes_every_version_and_its_file(self):
        m = Machine(self)
        m.put_world()
        m.put_world("other")
        m.write_meta(two())
        m.write_meta({**two(), "world": "other"}, "other")
        m.put_version("v2")
        m.put_version("v2", wid="other")
        R.write_code(m.codes, "plaza", "machinecode")
        with contextlib.redirect_stderr(io.StringIO()):
            R.purge_world(m.home, "plaza", codes_dir=m.codes)
        self.assertFalse((m.home / "data" / "plaza").exists())
        self.assertFalse((m.home / "versions" / "plaza").exists())
        self.assertFalse((m.home / "versions" / "plaza.json").exists())
        # Another world's versions are not touched.
        self.assertTrue((m.home / "versions" / "other" / "v2" / "db.sqlite").is_file())
        self.assertTrue((m.home / "versions" / "other.json").is_file())

    def test_a_signed_delete_world_purges_the_versions_with_the_world(self):
        m = Machine(self)
        m.put_world()
        m.write_meta(two())
        m.put_version("v2")
        msg = {"machine": "open-1", "world": "plaza", "domain": "plaza.numinia.com",
               "nonce": os.urandom(16).hex(), "expires": EXPIRES}
        nonce = m.drop("DeleteWorld", msg)
        m.take()
        self.assertEqual(m.result(nonce)["state"], "working")
        self.assertTrue((m.home / "versions" / "plaza" / "v2").is_dir())  # not until the container is gone
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(R.purge_deleted(m.home, m.orders, NOW, codes_dir=m.codes), ["plaza"])
        self.assertFalse((m.home / "versions" / "plaza").exists())
        self.assertFalse((m.home / "versions" / "plaza.json").exists())
        self.assertEqual(m.result(nonce)["state"], "done")
        self.assertNotIn("version", m.result(nonce))

    def test_a_world_that_never_had_versions_purges_as_before(self):
        m = Machine(self)
        m.put_world()
        R.purge_world(m.home, "plaza", codes_dir=m.codes)
        self.assertFalse((m.home / "data" / "plaza").exists())
        self.assertFalse((m.home / "versions").exists())

    def test_a_link_in_place_of_the_worlds_versions_is_unlinked_not_emptied(self):
        m = Machine(self)
        precious = m.home / "precious"
        precious.mkdir()
        (precious / "keep.bin").write_bytes(b"keep")
        (m.home / "versions").mkdir()
        (m.home / "versions" / "plaza").symlink_to(precious)
        V.purge_versions(m.home, "plaza")
        self.assertFalse(os.path.lexists(m.home / "versions" / "plaza"))
        self.assertEqual((precious / "keep.bin").read_bytes(), b"keep")

    def test_a_linked_versions_folder_is_not_emptied_through(self):
        m = Machine(self)
        precious = m.home / "precious"
        (precious / "plaza").mkdir(parents=True)
        (precious / "plaza" / "keep.bin").write_bytes(b"keep")
        (m.home / "versions").symlink_to(precious)
        V.purge_versions(m.home, "plaza")
        self.assertEqual((precious / "plaza" / "keep.bin").read_bytes(), b"keep")

    def test_only_a_world_id_is_purged(self):
        with self.assertRaises(ValueError):
            V.purge_versions(Path("/nonexistent"), "../x")


class Shapes(unittest.TestCase):
    def test_the_five_new_shapes_are_accepted(self):
        for kind in ("DuplicateVersion", "PublishVersion", "DeleteVersion", "DownloadVersion", "ImportVersion"):
            with self.subTest(kind=kind):
                self.assertIsNone(R.request_problem(kind, message(kind)))
        self.assertIsNone(R.request_problem("ImportVersion", message("ImportVersion", replace="v2")))
        self.assertIsNone(R.request_problem("DeleteVersion", message("DeleteVersion", version="v9999")))

    def test_the_field_order_is_the_signed_one(self):
        fields = {k: [n for n, _ in v] for k, v in W.ACTIONS.items()}
        same = ["machine", "world", "version", "nonce", "expires"]
        for kind in ("DuplicateVersion", "PublishVersion", "DeleteVersion", "DownloadVersion"):
            self.assertEqual(fields[kind], same)
        self.assertEqual(fields["ImportVersion"],
                         ["machine", "world", "zipBytes", "zipSha256", "replace", "nonce", "expires"])
        self.assertEqual(dict(W.ACTIONS["ImportVersion"])["zipBytes"], "uint256")
        self.assertEqual(dict(W.ACTIONS["ImportVersion"])["replace"], "string")

    def test_a_bad_version_or_replace_is_refused(self):
        for bad in ("", "v0", "v01", "v10000", "V1", "v1\n", "2", "v1 ", "v1;rm"):
            for kind in ("DuplicateVersion", "PublishVersion", "DeleteVersion", "DownloadVersion"):
                with self.subTest(kind=kind, version=bad):
                    self.assertEqual(R.request_problem(kind, message(kind, version=bad)),
                                     "version does not have its shape")
            if bad != "":
                with self.subTest(replace=bad):
                    self.assertEqual(R.request_problem("ImportVersion", message("ImportVersion", replace=bad)),
                                     "replace is neither empty nor a version")
        self.assertIsNotNone(R.request_problem("ImportVersion", message("ImportVersion", replace=2)))
        self.assertIsNotNone(R.request_problem("DeleteVersion", message("DeleteVersion", version=None)))

    def test_the_other_fields_keep_their_rules(self):
        self.assertIsNotNone(R.request_problem("DeleteVersion", {**message("DeleteVersion"), "image": IMAGE}))
        self.assertIsNotNone(R.request_problem("ImportVersion", message("ImportVersion", zipBytes=0)))
        self.assertIsNotNone(R.request_problem("ImportVersion", message("ImportVersion", zipBytes="4096")))
        self.assertIsNotNone(R.request_problem("ImportVersion", message("ImportVersion", zipSha256="AB" * 32)))
        self.assertIsNotNone(R.request_problem("DeleteVersion", message("DeleteVersion", world="Plaza")))
        self.assertIsNotNone(R.request_problem("DeleteVersion", message("DeleteVersion", expires=2**63)))
        # An empty image still means release for PinWorld alone.
        self.assertIsNone(R.request_problem("PinWorld", {"machine": "open-1", "world": "plaza", "image": "",
                                                          "reason": "r", "nonce": "a" * 32, "expires": EXPIRES}))

class Requests(unittest.TestCase):
    def test_a_download_of_a_version_needs_a_copy_number_like_a_world_download(self):
        m = Machine(self)
        signers = frozenset({m.oracle.address})
        msg = message("DownloadVersion")
        raw = {"type": "DownloadVersion", "message": msg, "signature": m.oracle.sign("DownloadVersion", msg)}
        entry, checked, rule = R.check_request(raw, "open-1", signers, NOW)
        self.assertEqual((checked, rule), (None, "no copy number"))
        self.assertEqual(entry["version"], "v2")
        entry, checked, rule = R.check_request({**raw, "copy": "nothex"}, "open-1", signers, NOW)
        self.assertEqual(rule, "no copy number")
        entry, checked, rule = R.check_request({**raw, "copy": COPY}, "open-1", signers, NOW)
        self.assertIsNone(rule)
        self.assertEqual((entry["copy"], entry["by"], entry["version"]), (COPY, m.oracle.address, "v2"))

    def test_an_import_of_a_version_has_the_slots_extra_hour(self):
        m = Machine(self)
        signers = frozenset({m.oracle.address})
        for kind, late in (("ImportVersion", True), ("DeleteVersion", False)):
            msg = message(kind, expires=1000)
            raw = {"type": kind, "message": msg, "signature": m.oracle.sign(kind, msg)}
            with self.subTest(kind=kind):
                inside = 1000 + R.GRACE + R.SLOT_SECONDS - 1 if late else 1000 + R.GRACE - 1
                self.assertIsNone(R.check_request(raw, "open-1", signers, inside)[2])
                self.assertEqual(R.check_request(raw, "open-1", signers, inside + 2)[2], "the request expired")

    def test_the_entry_of_an_old_type_is_what_it_was(self):
        m = Machine(self)
        signers = frozenset({m.oracle.address})
        msg = {"machine": "open-1", "world": "plaza", "nonce": "a" * 32, "expires": EXPIRES}
        raw = {"type": "StopWorld", "message": msg, "signature": m.oracle.sign("StopWorld", msg)}
        entry, _, rule = R.check_request(raw, "open-1", signers, NOW)
        self.assertIsNone(rule)
        self.assertEqual(entry, {"nonce": "a" * 32, "type": "StopWorld", "world": "plaza", "by": m.oracle.address,
                                 "copy": None})

    def test_a_result_carries_the_version_for_the_new_types_only(self):
        m = Machine(self)
        m.put_world()
        # A bad version is not carried; a good one is, even in a refusal.
        stop = {"machine": "open-1", "world": "plaza", "nonce": os.urandom(16).hex(), "expires": EXPIRES}
        m.drop("StopWorld", stop)
        m.take()
        self.assertNotIn("version", m.result(stop["nonce"]))
        bad = message("DeleteVersion", version="v0")
        m.drop("DeleteVersion", bad)
        m.take()
        self.assertNotIn("version", m.result(bad["nonce"]))
        self.assertEqual(m.result(bad["nonce"])["rule"], "version does not have its shape")

    def test_the_programs_load_in_any_order(self):
        for name in ("release", "nightly", "versions", "reconcile"):
            with self.subTest(program=name):
                self.assertIsNotNone(load(name))


if __name__ == "__main__":
    unittest.main()
