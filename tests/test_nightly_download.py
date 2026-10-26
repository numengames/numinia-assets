# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""The nightly copies, listed by the door and brought out by a signed DownloadNightly — also for a deleted world.

The request is the real EIP-712 signature in fleet_vectors.json (machine `open-1`, world `plaza`, valid until
2030). The nightly folder is root's alone; the door lists it only through the list the nightly pass wrote
(run/nightly.json), and the reconciler copies one zip into `copies/` as an ordinary copy.
"""

from pathlib import Path
from unittest import mock
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import stat
import sys
import threading
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_fleetd as T  # noqa: E402  (the door rig and the vectors)
import test_nightly as TN  # noqa: E402  (zip_bytes, the engine fake)

R, D, W, N = T.R, T.D, T.D.W, T.D.N
VECTOR = next(v for v in T.VECTORS["vectors"] if v["type"] == "DownloadNightly")
STAMP = VECTOR["message"]["stamp"]
NONCE = VECTOR["message"]["nonce"]
ENGINE_ZIP = TN.zip_bytes({"mundo.json": "{}", "datos/objetos.json": "{}"})
HYPERFY_ZIP = TN.zip_bytes({"world/db.sqlite": "x", "order.json": "{}", "world.json": "{}"})


def post(message=None, signature=None):
    return json.dumps({"type": "DownloadNightly", "message": message or VECTOR["message"],
                       "signature": signature or VECTOR["signature"]}).encode()


class Rig(T.Rig):
    def nightly(self, world="plaza", stamp=STAMP, data=HYPERFY_ZIP, side=False):
        """A nightly zip (and, for an engine world, its order file), as nightly.py leaves them: root's, 0600."""
        folder = self.home / "nightly" / world
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{world}-{stamp}.zip"
        path.write_bytes(data)
        os.chmod(path, 0o600)
        if side:
            N.order_file(path).write_text("{}")
        return path

    def take(self):
        with contextlib.redirect_stderr(io.StringIO()):
            return R.take_requests(self.home, self.home / "orders", "open-1", frozenset({T.ORACLE}), frozenset(),
                                   "open-1.numinia.com", T.NOW, codes_dir=self.etc / "codes")

    def make(self):
        with contextlib.redirect_stderr(io.StringIO()):
            return R.make_copies(self.home, self.home / "orders", "open-1", T.NOW)

    def result(self, nonce=NONCE):
        return json.loads((self.home / "requests" / f"{nonce}.json").read_text())

    def run(self, message=None, signature=None):
        """Sign-in to the door, then the reconciler: (status, reply)."""
        status, reply = self.accept(post(message, signature))
        self.take()
        self.make()
        return status, reply


class Asking(unittest.TestCase):
    """The door takes a DownloadNightly like a DownloadWorld: a copy number, then the reconciler's word."""

    def test_the_door_gives_a_copy_number_and_shows_preparing(self):
        rig = Rig(self)
        rig.nightly()
        status, reply = rig.accept(post())
        self.assertEqual(status, 202)
        self.assertEqual(set(reply), {"request", "copy"})
        self.assertEqual(reply["request"], NONCE)
        self.assertRegex(reply["copy"], r"\A[0-9a-f]{32}\Z")
        self.assertEqual(rig.door.copy(reply["copy"]), {"copy": reply["copy"], "state": "preparing"})
        self.assertIsNone(rig.door.copy_file(reply["copy"]))
        [dropped] = list((rig.home / "inbox").iterdir())
        raw = json.loads(dropped.read_text())
        self.assertEqual((raw["type"], raw["signer"], raw["copy"]), ("DownloadNightly", T.ORACLE, reply["copy"]))
        self.assertEqual(raw["message"], VECTOR["message"])

    def test_the_vector_is_signed_by_the_machines_oracle(self):
        self.assertEqual(VECTOR["signer"], T.ORACLE)
        self.assertEqual(W.signer_of("DownloadNightly", VECTOR["message"], VECTOR["signature"]), T.ORACLE)
        self.assertEqual("0x" + W.typed_digest("DownloadNightly", VECTOR["message"]).hex(), VECTOR["digest"])

    def test_a_world_that_is_not_here_is_no_reason_to_refuse_at_the_door(self):
        rig = Rig(self)  # no order, no nightly folder: the reconciler says not-found
        self.assertEqual(rig.accept(post())[0], 202)

    def test_the_throwaway_oracle_may_sign_it_only_when_listed(self):
        rig = Rig(self)
        message = {**VECTOR["message"], "nonce": "b" * 32, "expires": T.NOW + 600}
        signed = post(message, rig.oracle.sign("DownloadNightly", message))
        self.assertEqual(rig.accept(signed)[0], 202)
        (rig.etc / "signers").write_text(f"{T.ORACLE}\n")
        again = {**message, "nonce": "c" * 32}
        self.assertEqual(rig.accept(post(again, rig.oracle.sign("DownloadNightly", again))),
                         (403, {"error": "not-an-oracle-here"}))

    def test_every_field_is_anchored(self):
        rig = Rig(self)
        bad = {
            "stamp": ["", "20261018-03301", "20261018-0330123", "202610180-033012", "20261018_033012",
                      " 20261018-033012", "20261018-033012\n", "../x", "2026101a-033012", "２０２６1018-033012",
                      "20261018-033012/../x", "20261018-033012.zip", None, 20261018033012],
            "world": ["", "Plaza", "../plaza", "plaza/x", "-plaza", "plaza-", "plaza\n", None],
            "machine": ["", "open 1", "open-1\n", None],
            "nonce": ["", "e" * 31, "e" * 33, "E" * 32, "e" * 32 + "\n", None],
            "expires": [-1, "1893456000", 1.5, True, 2**63, None],
        }
        sender = 0
        for field, values in bad.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    message = {**VECTOR["message"], field: value}
                    sender += 1  # the door slows one address down; each try comes from its own
                    status, reply = rig.accept(post(message), client=f"203.0.113.{sender}")
                    self.assertEqual((status, reply["error"]), (400, "invalid"))
        self.assertEqual(list((rig.home / "inbox").iterdir()), [])

    def test_a_message_with_more_or_fewer_fields_is_refused(self):
        rig = Rig(self)
        extra = {**VECTOR["message"], "version": "v1"}
        fewer = {k: v for k, v in VECTOR["message"].items() if k != "stamp"}
        for message in (extra, fewer):
            with self.subTest(message=sorted(message)):
                self.assertEqual(rig.accept(post(message))[1]["error"], "invalid")

    def test_a_changed_message_or_a_forged_signature_is_refused(self):
        rig = Rig(self)
        other = {**VECTOR["message"], "stamp": "20261017-033012"}
        self.assertEqual(rig.accept(post(other)), (403, {"error": "not-an-oracle-here"}))  # a recovered stranger
        forged = VECTOR["signature"][:-4] + "0000"
        self.assertEqual(rig.accept(post(signature=forged))[0] in (401, 403), True)
        self.assertEqual(list((rig.home / "inbox").iterdir()), [])

    def test_another_machine_an_expired_request_and_a_replay_are_refused(self):
        other = Rig(self, alias="open-2")
        self.assertEqual(other.accept(post()), (400, {"error": "other-machine"}))
        late = Rig(self, now=VECTOR["message"]["expires"] + 1)
        self.assertEqual(late.accept(post()), (400, {"error": "expired"}))
        rig = Rig(self)
        self.assertEqual(rig.accept(post())[0], 202)
        self.assertEqual(rig.accept(post()), (409, {"error": "replayed"}))

    def test_the_stranger_signature_of_the_vectors_is_not_an_oracle(self):
        rig = Rig(self)
        stranger = T.VECTORS["stranger"]
        self.assertNotEqual(stranger["signer"], T.ORACLE)
        self.assertEqual(rig.accept(post(signature=stranger["signature"]))[0] in (401, 403), True)


class Bringing(unittest.TestCase):
    """The reconciler turns the nightly zip into an ordinary copy, the same one `/copies/<id>` serves."""

    def test_the_nightly_zip_becomes_a_ready_copy_with_the_same_bytes(self):
        rig = Rig(self)
        rig.put_world()
        path = rig.nightly()
        status, reply = rig.run()
        self.assertEqual(status, 202)
        copy = reply["copy"]
        record = json.loads((rig.home / "copies" / f"{copy}.json").read_text())
        self.assertEqual(record["state"], "ready")
        self.assertEqual((record["copy"], record["world"], record["filename"]), (copy, "plaza", path.name))
        self.assertEqual(record["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(record["bytes"], path.stat().st_size)
        for field in ("at", "madeAt", "expiresAt"):
            self.assertIn(field, record)
        self.assertEqual(R.epoch(record["expiresAt"]) - int(record["madeAt"]), R.COPY_SECONDS)
        served = rig.door.copy_file(copy)
        self.assertEqual(served, (rig.home / "copies" / f"{copy}.zip", path.name))
        self.assertEqual(hashlib.sha256(served[0].read_bytes()).hexdigest(), hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(rig.door.copy(copy), {k: record[k] for k in ("copy", "world", "state", "bytes", "sha256",
                                                                      "filename", "expiresAt")})
        result = rig.result()
        self.assertEqual((result["state"], result["type"], result["world"], result["stamp"], result["rule"]),
                         ("done", "DownloadNightly", "plaza", STAMP, None))
        [line] = [json.loads(x) for x in (rig.home / "log.jsonl").read_text().splitlines()]
        self.assertEqual((line["type"], line["world"], line["state"], line["by"]),
                         ("DownloadNightly", "plaza", "done", W.short(T.ORACLE)))

    def test_it_is_a_copy_not_a_link_and_the_nightly_zip_is_untouched(self):
        rig = Rig(self)
        path = rig.nightly()
        before = path.stat()
        _, reply = rig.run()
        after = path.stat()
        self.assertEqual((after.st_ino, after.st_nlink, stat.S_IMODE(after.st_mode), after.st_mtime_ns),
                         (before.st_ino, 1, 0o600, before.st_mtime_ns))
        self.assertNotEqual((rig.home / "copies" / f"{reply['copy']}.zip").stat().st_ino, before.st_ino)
        self.assertEqual(stat.S_IMODE((rig.home / "copies" / f"{reply['copy']}.zip").stat().st_mode), 0o640)
        self.assertEqual(path.read_bytes(), HYPERFY_ZIP)
        self.assertEqual([p.name for p in (rig.home / "copies").iterdir() if p.name.startswith(".")], [])

    def test_a_deleted_worlds_copy_is_still_brought_out(self):
        rig = Rig(self)  # no order for plaza: it was deleted, its nightly folder stayed
        path = rig.nightly()
        self.assertFalse((rig.home / "orders" / "plaza.json").exists())
        _, reply = rig.run()
        self.assertEqual(rig.result()["state"], "done")
        self.assertEqual(json.loads((rig.home / "copies" / f"{reply['copy']}.json").read_text())["state"], "ready")
        self.assertEqual((rig.home / "copies" / f"{reply['copy']}.zip").read_bytes(), path.read_bytes())

    def test_an_engine_worlds_zip_comes_out_as_it_is(self):
        rig = Rig(self)
        rig.put_world()
        path = rig.nightly(data=ENGINE_ZIP, side=True)
        _, reply = rig.run()
        self.assertEqual((rig.home / "copies" / f"{reply['copy']}.zip").read_bytes(), ENGINE_ZIP)
        self.assertEqual(rig.door.copy_file(reply["copy"])[1], path.name)

    def test_a_world_that_never_had_a_nightly_folder_is_not_found(self):
        rig = Rig(self)
        rig.put_world()
        _, reply = rig.run()
        self.assertEqual((rig.result()["state"], rig.result()["rule"]), ("failed", "not-found"))
        # Refused when taken, as a DownloadWorld of a world that is not here: the result says why, no copy exists.
        self.assertIsNone(rig.door.copy(reply["copy"]))
        self.assertIsNone(rig.door.copy_file(reply["copy"]))
        self.assertEqual(list((rig.home / "copies").iterdir()), [])
        [line] = [json.loads(x) for x in (rig.home / "log.jsonl").read_text().splitlines()]
        self.assertEqual((line["state"], line["rule"]), ("failed", "not-found"))

    def test_another_stamp_or_another_worlds_folder_is_not_found(self):
        rig = Rig(self)
        rig.nightly(stamp="20261017-033012")  # a different night
        rig.nightly(world="other")  # this stamp, another world
        rig.run()
        self.assertEqual(rig.result()["rule"], "not-found")

    def test_a_stamp_that_is_no_date_is_not_found(self):
        rig = Rig(self)
        message = {**VECTOR["message"], "stamp": "99999999-999999"}
        # Not signed by the vector's key, so go through the reconciler's own check with a signed throwaway.
        message = {**message, "nonce": "d" * 32, "expires": T.NOW + 600}
        rig.nightly(stamp="99999999-999999")
        rig.accept(post(message, rig.oracle.sign("DownloadNightly", message)))
        with contextlib.redirect_stderr(io.StringIO()):
            R.take_requests(rig.home, rig.home / "orders", "open-1", frozenset({T.ORACLE, rig.oracle.address}),
                            frozenset(), "open-1.numinia.com", T.NOW, codes_dir=rig.etc / "codes")
        self.assertEqual(rig.result("d" * 32)["rule"], "not-found")

    def test_a_symlinked_zip_folder_or_nightly_root_is_not_found(self):
        elsewhere = Rig(self)
        secret = elsewhere.root / "secret.zip"
        secret.write_bytes(HYPERFY_ZIP)
        for how in ("zip", "world folder", "nightly root"):
            with self.subTest(how=how):
                rig = Rig(self)
                if how == "zip":
                    folder = rig.home / "nightly" / "plaza"
                    folder.mkdir(parents=True)
                    (folder / f"plaza-{STAMP}.zip").symlink_to(secret)
                elif how == "world folder":
                    real = rig.root / "real"
                    real.mkdir()
                    (real / f"plaza-{STAMP}.zip").write_bytes(HYPERFY_ZIP)
                    (rig.home / "nightly").mkdir()
                    (rig.home / "nightly" / "plaza").symlink_to(real)
                else:
                    real = rig.root / "real" / "plaza"
                    real.mkdir(parents=True)
                    (real / f"plaza-{STAMP}.zip").write_bytes(HYPERFY_ZIP)
                    (rig.home / "nightly").symlink_to(rig.root / "real")
                _, reply = rig.run()
                self.assertEqual(rig.result()["rule"], "not-found")
                self.assertFalse((rig.home / "copies" / f"{reply['copy']}.zip").exists())

    def test_a_folder_or_a_fifo_with_the_zips_name_is_not_found(self):
        rig = Rig(self)
        folder = rig.home / "nightly" / "plaza"
        (folder / f"plaza-{STAMP}.zip").mkdir(parents=True)
        rig.run()
        self.assertEqual(rig.result()["rule"], "not-found")
        fifo = Rig(self)
        folder = fifo.home / "nightly" / "plaza"
        folder.mkdir(parents=True)
        os.mkfifo(folder / f"plaza-{STAMP}.zip")
        fifo.run()
        self.assertEqual(fifo.result()["rule"], "not-found")

    def test_nothing_can_name_a_path_outside_the_nightly_folder(self):
        rig = Rig(self)
        rig.nightly()
        for world, stamp in (("..", STAMP), ("plaza/..", STAMP), ("plaza", "../" + STAMP), ("plaza", STAMP + "/"),
                             ("", STAMP), ("plaza", ""), ("Plaza", STAMP), (None, STAMP)):
            with self.subTest(world=world, stamp=stamp):
                self.assertIsNone(N.nightly_zip(rig.home, world, stamp))
        self.assertEqual(N.nightly_zip(rig.home, "plaza", STAMP), rig.home / "nightly" / "plaza" / f"plaza-{STAMP}.zip")

    def test_a_file_gone_between_the_check_and_the_copy_is_not_found(self):
        rig = Rig(self)
        path = rig.nightly()
        rig.accept(post())
        rig.take()
        path.unlink()  # pruned by the nightly pass in between
        rig.make()
        self.assertEqual((rig.result()["state"], rig.result()["rule"]), ("failed", "not-found"))

    def test_a_zip_swapped_for_a_link_after_the_check_is_still_not_followed(self):
        rig = Rig(self)
        path = rig.nightly()
        secret = rig.root / "secret.zip"
        secret.write_bytes(b"not for the room")
        rig.accept(post())
        rig.take()
        path.unlink()
        path.symlink_to(secret)  # the swap, after the check at take time
        rig.make()
        self.assertEqual((rig.result()["state"], rig.result()["rule"]), ("failed", "not-found"))
        self.assertEqual(list((rig.home / "copies").glob("*.zip")), [])
        # And with the check itself fooled, the descriptor is opened without following the link.
        rig2 = Rig(self)
        link = rig2.root / "link.zip"
        link.symlink_to(secret)
        job = {"copy": "a" * 32, "world": "plaza", "nightly": STAMP, "nonce": "b" * 32, "by": T.ORACLE}
        with mock.patch.object(N, "nightly_zip", return_value=link), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(N.make_copy(rig2.home, job, T.NOW))
        self.assertEqual(rig2.result("b" * 32)["rule"], "not-found")

    def test_a_disk_without_room_fails_without_leaving_half_a_copy(self):
        rig = Rig(self)
        rig.nightly()
        rig.accept(post())
        rig.take()
        low = mock.Mock(return_value=mock.Mock(free=R.RESERVE + len(HYPERFY_ZIP) - 1))
        with mock.patch.object(N.shutil, "disk_usage", low):
            rig.make()
        self.assertEqual((rig.result()["state"], rig.result()["rule"]), ("failed", "no-room"))
        self.assertEqual(sorted(p.suffix for p in (rig.home / "copies").iterdir()), [".json"])
        self.assertEqual(json.loads(next((rig.home / "copies").iterdir()).read_text())["state"], "failed")

    def test_a_copy_that_breaks_half_way_leaves_nothing_and_says_so(self):
        rig = Rig(self)
        rig.nightly()
        rig.accept(post())
        rig.take()
        with mock.patch.object(N.shutil, "copyfileobj", side_effect=OSError("disk error")):
            rig.make()
        self.assertEqual((rig.result()["state"], rig.result()["rule"]), ("failed", "the copy could not be made"))
        self.assertEqual(sorted(p.suffix for p in (rig.home / "copies").iterdir()), [".json"])

    def test_the_reconciler_checks_the_request_again(self):
        rig = Rig(self)
        rig.nightly()
        rig.accept(post())
        [dropped] = list((rig.home / "inbox").iterdir())
        raw = json.loads(dropped.read_text())
        # A stamp that was changed in the inbox after the door: the signature no longer holds.
        raw["message"]["stamp"] = "20261017-033012"
        dropped.write_text(json.dumps(raw))
        rig.take()
        self.assertEqual(rig.result()["state"], "failed")  # the signature no longer recovers an Oracle
        self.assertIn("Oracle", rig.result()["rule"])
        self.assertEqual(list((rig.home / "copies").glob("*.zip")), [])
        # The same request from another machine's inbox, or one with no copy number.
        for change, rule in (({"copy": None}, "no copy number"),):
            with self.subTest(change=change):
                again = Rig(self)
                again.nightly()
                again.accept(post())
                [one] = list((again.home / "inbox").iterdir())
                data = json.loads(one.read_text())
                data.update(change)
                one.write_text(json.dumps(data))
                again.take()
                self.assertEqual(again.result()["rule"], rule)

    def test_a_replayed_number_is_dropped_by_the_reconciler(self):
        rig = Rig(self)
        rig.nightly()
        rig.accept(post())
        rig.take()
        rig.make()
        first = rig.result()
        [kept] = [p for p in (rig.home / "copies").glob("*.json")]
        # The same signed request dropped into the inbox again (the door's own memory failed): not run twice.
        again = {"type": "DownloadNightly", "message": VECTOR["message"], "signature": VECTOR["signature"],
                 "signer": T.ORACLE, "copy": "f" * 32}
        R.write_json(rig.home / "inbox" / "1-again.json", again)
        self.assertEqual(rig.take(), [])
        self.assertEqual(rig.result(), first)
        self.assertEqual(list((rig.home / "copies").glob("*.json")), [kept])

    def test_a_request_for_another_machine_or_a_late_one_is_refused_by_the_reconciler(self):
        for how in ("machine", "late"):
            with self.subTest(how=how):
                rig = Rig(self)
                rig.nightly()
                rig.accept(post())
                if how == "machine":
                    with contextlib.redirect_stderr(io.StringIO()):
                        R.take_requests(rig.home, rig.home / "orders", "open-2", frozenset({T.ORACLE}), frozenset(),
                                        "open-2.numinia.com", T.NOW, codes_dir=rig.etc / "codes")
                else:
                    with contextlib.redirect_stderr(io.StringIO()):
                        R.take_requests(rig.home, rig.home / "orders", "open-1", frozenset({T.ORACLE}), frozenset(),
                                        "open-1.numinia.com", VECTOR["message"]["expires"] + R.GRACE + 1,
                                        codes_dir=rig.etc / "codes")
                self.assertEqual(rig.result()["state"], "failed")
                self.assertEqual(list((rig.home / "copies").glob("*.zip")), [])

    def test_it_waits_for_no_save_and_is_not_held_by_a_publication(self):
        rig = Rig(self)
        rig.put_world()  # plaza runs: a download of the world would wait for its next save
        rig.nightly()
        rig.accept(post())
        R.write_json(rig.home / "run" / "switching.json", {"plaza": {"nonce": "a" * 32, "by": T.ORACLE, "from": "v1",
                                                                      "to": "v2"}})
        rig.take()
        self.assertEqual(R.copies_wait(rig.home, rig.home / "orders"), 0)
        self.assertEqual(rig.result()["state"], "working")

    def test_the_copy_goes_after_a_day_like_any_other(self):
        rig = Rig(self)
        rig.nightly()
        _, reply = rig.run()
        self.assertEqual(R.prune_copies(rig.home, T.NOW + R.COPY_SECONDS + 5), [reply["copy"]])
        self.assertFalse((rig.home / "copies" / f"{reply['copy']}.zip").exists())
        self.assertTrue(N.nightly_zip(rig.home, "plaza", STAMP).is_file())  # the nightly copy stays


class Listing(unittest.TestCase):
    """What the nightly pass writes for the door, and what the door shows of it."""

    def setUp(self):
        self.rig = Rig(self)
        self.home = self.rig.home

    def zip(self, world, stamp, data=HYPERFY_ZIP, side=False):
        return self.rig.nightly(world, stamp, data, side)

    def listed(self):
        N.write_index(self.home)
        return self.rig.door.nightly()["nightly"]

    def test_an_engine_zip_with_its_order_file_is_restorable(self):
        self.zip("plaza", "20261018-033012", ENGINE_ZIP, side=True)
        [one] = self.listed()
        self.assertEqual(one, {"world": "plaza", "stamp": "20261018-033012", "at": "2026-10-18T03:30:12Z",
                               "bytes": len(ENGINE_ZIP), "kind": "numinia-engine", "restorable": True,
                               "worldExists": False})

    def test_an_engine_zip_with_no_order_file_is_listed_but_not_restorable(self):
        self.zip("plaza", "20261018-033012", ENGINE_ZIP, side=False)
        [one] = self.listed()
        self.assertEqual((one["kind"], one["restorable"]), ("numinia-engine", False))

    def test_a_hyperfy_zip_with_its_database_is_restorable(self):
        self.zip("plaza", "20261018-033012")
        [one] = self.listed()
        self.assertEqual((one["kind"], one["restorable"]), ("hyperfy", True))

    def test_the_old_shape_of_an_engine_world_reads_as_hyperfy_and_an_unknown_zip_as_none(self):
        # An engine world's copy from before the engine's own export: the Hyperfy shape (not an engine zip).
        self.zip("old", "20261001-033012", TN.zip_bytes({"world/datos/objetos.json": "{}", "order.json": "{}"}))
        self.zip("odd", "20261002-033012", TN.zip_bytes({"README.md": "hi"}))
        (self.home / "nightly" / "bad").mkdir()
        (self.home / "nightly" / "bad" / "bad-20261003-033012.zip").write_bytes(b"this is not a zip")
        # A Hyperfy-shaped zip with no database is no world to bring back.
        self.zip("empty", "20261004-033012", TN.zip_bytes({"order.json": "{}", "world.json": "{}"}))
        found = {e["world"]: (e["kind"], e["restorable"]) for e in self.listed()}
        self.assertEqual(found, {"old": (None, False), "odd": (None, False), "bad": (None, False),
                                 "empty": (None, False)})

    def test_foreign_files_symlinks_and_odd_names_are_not_listed(self):
        self.zip("plaza", "20261018-033012")
        folder = self.home / "nightly" / "plaza"
        (folder / "README.txt").write_text("x")
        (folder / ".plaza-20261019-033012.zip.part").write_bytes(b"half")
        (folder / "plaza-20261019-033012.zip.bak").write_bytes(b"x")
        (folder / "PLAZA-20261019-033012.zip").write_bytes(HYPERFY_ZIP)
        (folder / "plaza-2026101-033012.zip").write_bytes(HYPERFY_ZIP)
        (folder / "plaza-20261020-033012.order.json").write_text("{}")  # an order file with no zip
        (folder / "plaza-20261021-033012.zip").mkdir()
        (folder / "plaza-20261022-033012.zip").symlink_to(folder / "plaza-20261018-033012.zip")
        (folder / "plaza-99999999-999999.zip").write_bytes(HYPERFY_ZIP)  # a name that is no date
        other = self.home / "nightly" / "Not_A_World"
        other.mkdir()
        (other / "Not_A_World-20261018-033012.zip").write_bytes(HYPERFY_ZIP)
        mismatch = self.home / "nightly" / "alfa"
        mismatch.mkdir()
        (mismatch / "beta-20261018-033012.zip").write_bytes(HYPERFY_ZIP)  # beta's name in alfa's folder
        (self.home / "nightly" / "state.json").write_text("{}")
        (self.home / "nightly" / "lock").write_text("")
        real = self.home / "real"
        real.mkdir()
        (real / "linked-20261018-033012.zip").write_bytes(HYPERFY_ZIP)
        (self.home / "nightly" / "linked").symlink_to(real)  # a world folder that is a link
        self.assertEqual([(e["world"], e["stamp"]) for e in self.listed()], [("plaza", "20261018-033012")])

    def test_a_nightly_root_that_is_a_link_or_missing_lists_nothing(self):
        self.assertEqual(self.listed(), [])
        real = self.home / "real" / "plaza"
        real.mkdir(parents=True)
        (real / "plaza-20261018-033012.zip").write_bytes(HYPERFY_ZIP)
        (self.home / "nightly").symlink_to(self.home / "real")
        self.assertEqual(self.listed(), [])

    def test_newest_first_at_most_fourteen_a_world_and_two_hundred_in_all(self):
        for day in range(1, 21):
            self.zip("plaza", f"202610{day:02d}-033012")
        copies = self.listed()
        self.assertEqual(len(copies), 14)
        self.assertEqual([e["stamp"] for e in copies], [f"202610{d:02d}-033012" for d in range(20, 6, -1)])
        for number in range(30):
            for day in range(1, 11):
                self.zip(f"w{number:02d}", f"202610{day:02d}-0000{number:02d}")
        copies = self.listed()
        self.assertEqual(len(copies), 200)
        stamps = [e["stamp"] for e in copies]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        self.assertLessEqual(max(sum(1 for e in copies if e["world"] == w) for w in {e["world"] for e in copies}), 14)

    def test_the_list_says_nothing_but_these_fields_no_path_no_key(self):
        self.zip("plaza", "20261018-033012", ENGINE_ZIP, side=True)
        N.write_index(self.home)
        raw = (self.home / "run" / "nightly.json").read_text()
        self.assertEqual(set(json.loads(raw)), {"at", "copies"})
        self.assertEqual(set(json.loads(raw)["copies"][0]), {"world", "stamp", "at", "bytes", "kind", "restorable"})
        self.assertNotIn(str(self.home), raw)
        shown = self.rig.door.nightly()
        self.assertEqual(set(shown), {"machine", "nightly", "at"})
        self.assertEqual(set(shown["nightly"][0]), {"world", "stamp", "at", "bytes", "kind", "restorable", "worldExists"})
        self.assertEqual(stat.S_IMODE((self.home / "run" / "nightly.json").stat().st_mode), 0o644)

    def test_a_world_that_is_here_says_so_and_a_deleted_one_does_not(self):
        self.zip("plaza", "20261018-033012")
        self.zip("gone", "20261018-033012")
        self.rig.put_world("plaza")
        self.assertEqual({e["world"]: e["worldExists"] for e in self.listed()}, {"plaza": True, "gone": False})

    def test_the_door_trusts_nothing_in_the_file_it_reads(self):
        good = {"world": "plaza", "stamp": "20261018-033012", "at": "x", "bytes": 5, "kind": None, "restorable": False,
                "path": "/srv/fleet/nightly/plaza/p.zip", "key": "secret"}
        bad = [{**good, "world": "../etc"}, {**good, "world": "Plaza"}, {**good, "stamp": "20261018_033012"},
               {**good, "stamp": "99999999-999999"}, {**good, "bytes": -1}, {**good, "bytes": True},
               {**good, "bytes": "5"}, {**good, "kind": "other"}, {**good, "restorable": "yes"}, "text", 5, None]
        R.write_json(self.home / "run" / "nightly.json", {"at": "x", "copies": [*bad, good]})
        [one] = self.rig.door.nightly()["nightly"]
        self.assertEqual(one, {"world": "plaza", "stamp": "20261018-033012", "at": "2026-10-18T03:30:12Z", "bytes": 5,
                               "kind": None, "restorable": False, "worldExists": False})
        R.write_json(self.home / "run" / "nightly.json", {"copies": [good] * (N.INDEX_MAX + 20)})
        self.assertEqual(len(self.rig.door.nightly()["nightly"]), N.INDEX_MAX)
        for odd in ("not json", "[]", '{"copies": 5}', '{"copies": null}'):
            (self.home / "run" / "nightly.json").write_text(odd)
            self.assertEqual(self.rig.door.nightly()["nightly"], [])
        (self.home / "run" / "nightly.json").unlink()
        self.assertEqual(self.rig.door.nightly()["nightly"], [])

    def test_the_machine_says_it_listens_for_it(self):
        self.assertTrue(self.rig.door.machine()["listens"]["nightly"])
        self.assertTrue(self.rig.door.machine()["listens"]["pin"])

    def test_the_nightly_pass_writes_the_list_after_pruning(self):
        env = N.Env(self.home)
        env.now = lambda: TN.T0
        env.free = lambda _path: 100 * 1024**3
        env.log = lambda line: None
        (self.home / "orders").mkdir(exist_ok=True)
        data = self.home / "data" / "plaza"
        data.mkdir(parents=True)
        con = sqlite3.connect(data / "db.sqlite")
        con.execute("create table t (v text)")
        con.commit()
        con.close()
        (self.home / "orders" / "plaza.json").write_text(json.dumps(
            {"id": "plaza", "domain": "plaza.example.org", "image": TN.HYPERFY, "limits": TN.LIMITS}))
        self.zip("plaza", "20200101-033012")  # far past the 14 days, but not the newest... it is not: tonight's is
        self.zip("gone", "20200101-033012")  # a deleted world's last copy stays, however old
        N.run_pass(env)
        names = {(e["world"], e["stamp"]) for e in json.loads((self.home / "run" / "nightly.json").read_text())["copies"]}
        tonight = TN.time_stamp(TN.T0)
        self.assertEqual(names, {("plaza", tonight), ("gone", "20200101-033012")})

    def test_the_reconciler_writes_the_list_once_when_there_is_none(self):
        self.zip("plaza", "20261018-033012")
        N.ensure_index(self.home)
        self.assertEqual([e["world"] for e in json.loads((self.home / "run" / "nightly.json").read_text())["copies"]],
                         ["plaza"])
        self.zip("plaza", "20261019-033012")
        N.ensure_index(self.home)  # there is one: left to the nightly pass
        self.assertEqual(len(json.loads((self.home / "run" / "nightly.json").read_text())["copies"]), 1)

    def test_a_list_that_cannot_be_written_does_not_stop_the_reconciler(self):
        with mock.patch.object(N.R, "write_json", side_effect=OSError("read-only")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                N.ensure_index(self.home)
        self.assertIn("could not be written", err.getvalue())


class OverHTTP(unittest.TestCase):
    """The route and the download, on the real server and a real socket, as Caddy reaches it."""

    def setUp(self):
        self.rig = Rig(self)
        here = os.getcwd()
        os.chdir(self.rig.root)  # a short relative path: socket paths are limited to ~100 bytes
        self.addCleanup(os.chdir, here)
        self.server = D.make_server(self.rig.door, Path("fleetd.sock"))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path):
        connection = T.UnixHTTPConnection("fleetd.sock")
        with contextlib.redirect_stderr(io.StringIO()):
            connection.request(method, path)
            response = connection.getresponse()
            data = response.read()
        connection.close()
        return response, data

    def test_the_list_answers_to_anyone_and_nothing_below_it(self):
        self.rig.nightly("plaza", STAMP)
        N.write_index(self.rig.home)
        response, data = self.call("GET", "/fleet/v1/nightly")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "*")
        shown = json.loads(data)
        self.assertEqual((shown["machine"], [e["world"] for e in shown["nightly"]]), ("open-1", ["plaza"]))
        for path in ("/fleet/v1/nightly/plaza", "/fleet/v1/nightly/../machine/x", "/fleet/v1/nightly/file"):
            with self.subTest(path=path):
                self.assertEqual(self.call("GET", path)[0].status, 404)

    def test_the_brought_out_copy_downloads_through_the_existing_route(self):
        path = self.rig.nightly("plaza", STAMP)
        status, reply = self.rig.accept(post())
        self.rig.take()
        self.rig.make()
        response, data = self.call("GET", f"/fleet/v1/copies/{reply['copy']}/file")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Disposition"), f'attachment; filename="{path.name}"')
        self.assertEqual(hashlib.sha256(data).hexdigest(), hashlib.sha256(path.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
