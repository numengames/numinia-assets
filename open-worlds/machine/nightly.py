#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: AGPL-3.0-only
"""The fleet machine's nightly copy: every world, once a day, kept 14 days.

Runs from a systemd timer (fleet-nightly.timer, 03:30 machine-local time),
installed by the bootstrap and never run from the clone. For each world with
an order on this machine:

* One zip per world per UTC day in <home>/nightly/<world>/, named
  `<world>-YYYYMMDD-HHMMSS.zip`. If today's already exists nothing is done
  (so a timer that fires twice, or `Persistent=` catching up, is harmless).
* Consistent: the world's `db.sqlite` goes in through SQLite's online backup
  (`reconcile.snapshot_sqlite`, the same one `build_copy` uses), every other
  file as it is. Nothing is written in the world's folder and no container is
  stopped or touched: the copy only reads, so a running world is never
  disturbed. The copy is built in a hidden `.part` file and moved into place
  whole, so a reader never sees half a zip.
* What goes in, for a Hyperfy world: the world's folder, its order
  (`order.json`) and a manifest with the sha256 of each file. Never keys or
  admin codes: a copy that is stolen opens nothing. (`env/` and
  `/etc/fleet/codes` stay out.)
* What goes in, for a Numinia Engine world: the engine's own export, made by
  the `copia` tool of the world's own image (`reconcile.engine_copia`, the
  same one the download button uses; the machine never re-implements its
  rules): `mundo.json`, `datos/`, `aventura/`, `entorno/`, never keys nor who
  came in. The zip is exactly what the engine writes, nothing added (the
  engine's importer refuses a file its manifest does not list), so the
  room's «Bring a world back» takes it as it is. The order (image, domain,
  limits; never keys) goes beside it, in `<world>-YYYYMMDD-HHMMSS.order.json`,
  and is kept and pruned together with its zip. A copy of this world made
  before this, in the Hyperfy shape (no `mundo.json`), is not restorable: it
  does not count as tonight's, a proper one is made, and the old ones age out
  as any other. If `copia` fails the world's result is `failed: ...`, nothing
  partial is left and the other worlds still get theirs.
* Kept 14 days. Older zips (and their order files) are deleted by the date in
  their name, but the newest copy of a world is never deleted, however old: a
  machine whose copies stopped must not erase the last one it has.
* Low disk: a world is copied only if the free space left after its folder's
  size is still above MIN_FREE_BYTES; otherwise it is skipped and logged
  (`skipped: low disk`), nothing is deleted to make room and the pass ends
  with success so the other worlds still get theirs. Pruning old zips runs
  anyway: that only frees space.

* Two things leave this folder, and only these. The list of the copies
  (`build_index`: world, stamp, size, kind, restorable; no path, no key) is
  written to <home>/run/nightly.json after each pass, for the door to show
  (the folder is root's alone, the door cannot open it). And a signed
  DownloadNightly copies one zip into `copies/` as an ordinary copy
  (`make_copy`), also for a world that was deleted: its folder stays.

State: <home>/nightly/state.json (when the pass ran and what happened to each
world). Everything outside (clock, free space) is passed in, so the tests run
with a fake clock and a fake disk.
"""

from __future__ import annotations

import argparse
import calendar
import fcntl
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import reconcile as R

NIGHTLY_DIR = "nightly"
STATE_FILE = "state.json"
KEEP_DAYS = 14
MIN_FREE_BYTES = 2 * 1024**3  # what must stay free after a copy is made
ZIP_NAME = re.compile(r"(?P<world>[a-z0-9][a-z0-9-]*)-(?P<day>\d{8})-(?P<time>\d{6})\.zip")
ORDER_NAME = re.compile(r"(?P<world>[a-z0-9][a-z0-9-]*)-(?P<day>\d{8})-(?P<time>\d{6})\.order\.json")
FORMAT = "numinia-world-nightly/1"
ORDER_FORMAT = "numinia-world-nightly-order/1"  # the sidecar of a Numinia Engine world's zip
ENGINE_MANIFEST = "mundo.json"  # what makes an engine zip restorable: the room looks for it at the zip's root
HYPERFY_DB = f"world/{R.ENGINE_DB}"  # what makes a Hyperfy zip restorable
INDEX_FILE = "nightly.json"  # in <home>/run/: the list the door shows (the folder itself is root's alone)
INDEX_PER_WORLD = 14  # the most copies of one world the list names
INDEX_MAX = 200  # the most copies it names in all


class Env:
    """Everything the pass takes from outside."""

    def __init__(self, home: Path):
        self.home = home
        self.dir = home / NIGHTLY_DIR
        self.now = time.time
        self.free = lambda path: shutil.disk_usage(path).free
        self.log = lambda line: print(line, file=sys.stderr)


def day_of(now: float) -> str:
    return time.strftime("%Y%m%d", time.gmtime(now))


def day_start(day: str) -> float:
    return calendar.timegm(time.strptime(day, "%Y%m%d"))  # midnight UTC of that day


def copies_of(folder: Path) -> list[tuple[str, Path]]:
    """The zips in a world's folder as (YYYYMMDD-HHMMSS, path), oldest first. Other files are not ours."""
    found = []
    for path in folder.iterdir() if folder.is_dir() else []:
        match = ZIP_NAME.fullmatch(path.name)
        if match and path.is_file() and not path.is_symlink():
            found.append((f"{match['day']}-{match['time']}", path))
    return sorted(found)


def order_file(zipped: Path) -> Path:
    """The order file that goes with a zip: `<name>.zip` -> `<name>.order.json`."""
    return zipped.with_name(zipped.name[: -len(".zip")] + ".order.json")


def prune(env: Env, wid: str) -> list[str]:
    """Delete the world's copies older than KEEP_DAYS days (each with its order file), always keeping the newest one.

    An order file whose zip is not there (a pass killed between the two) is deleted too.
    """
    gone = []
    folder = env.dir / wid
    copies = copies_of(folder)
    limit = env.now() - KEEP_DAYS * 86400
    for stamp, path in copies[:-1]:  # the newest is never a candidate
        if day_start(stamp[:8]) + 86400 <= limit:  # the whole day it names is past the limit
            try:
                path.unlink()
                gone.append(path.name)
            except OSError:
                continue
            try:
                order_file(path).unlink(missing_ok=True)
            except OSError:
                pass
    for path in folder.iterdir() if folder.is_dir() else []:
        if ORDER_NAME.fullmatch(path.name) and path.is_file() and not path.is_symlink():
            if not path.with_name(path.name[: -len(".order.json")] + ".zip").exists():
                try:
                    path.unlink()
                    gone.append(path.name)
                except OSError:
                    pass
    return gone


def tree_bytes(data: Path) -> int:
    total = 0
    for folder, _dirs, names in os.walk(data, followlinks=False):
        for name in names:
            path = Path(folder) / name
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                pass
    return total


def copy_folder(env: Env, order: dict) -> tuple[str, Path, Path]:
    """The world's id, its folder here (checked) and its copies' folder (made, private)."""
    wid = order["id"]
    data = env.home / "data" / wid
    if data.is_symlink() or not data.is_dir():
        raise OSError("the world has no folder here")
    folder = env.dir / wid
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    return wid, data, folder


def copy_world(env: Env, order: dict, now: float) -> Path:
    """Copy one world, read-only on its folder, into <home>/nightly/<id>/, and return the zip.

    A Numinia Engine world's copy is the engine's own export; any other is the Hyperfy shape.
    """
    if R.runs_engine(order):
        return copy_engine_world(env, order, now)
    wid, data, folder = copy_folder(env, order)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
    final = folder / f"{wid}-{stamp}.zip"
    part = folder / f".{wid}-{stamp}.zip.part"
    snapshot = folder / f".{wid}-{stamp}.sqlite.tmp"
    files = []

    def add(zipped: zipfile.ZipFile, source: Path, name: str) -> None:
        zipped.write(source, name)
        files.append({"path": name, "bytes": source.stat().st_size, "sha256": R.file_digest(source)})

    try:
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
            database = data / R.ENGINE_DB
            if database.is_file() and not database.is_symlink():
                R.snapshot_sqlite(database, snapshot)
                add(zipped, snapshot, f"world/{R.ENGINE_DB}")
            skip = {R.ENGINE_DB, f"{R.ENGINE_DB}-wal", f"{R.ENGINE_DB}-shm", f"{R.ENGINE_DB}-journal"}
            for where, dirs, names in os.walk(data, followlinks=False):
                dirs.sort()
                for name in sorted(names):
                    source = Path(where) / name
                    relative = source.relative_to(data).as_posix()
                    if relative in skip or source.is_symlink() or not source.is_file():
                        continue
                    add(zipped, source, f"world/{relative}")
            zipped.writestr("order.json", json.dumps(order, indent=2) + "\n")
            zipped.writestr("world.json", json.dumps({
                "format": FORMAT,
                "world": wid,
                "domain": order.get("domain"),
                "image": order.get("image"),
                "copiedAt": R.utc(now),
                "files": files,
            }, indent=2) + "\n")
        os.chmod(part, 0o600)
        os.replace(part, final)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        snapshot.unlink(missing_ok=True)
    return final


def copy_engine_world(env: Env, order: dict, now: float) -> Path:
    """A Numinia Engine world's copy: the zip the engine's own `copia` writes, and its order file beside it.

    Nothing is added to the zip (the engine's importer refuses what its
    manifest does not list), so what the engine wrote is what the room's
    «Bring a world back» reads. The order file is written first and the zip
    moved into place last: a pass killed in between leaves an order file
    with no zip, which prune deletes, never a zip without its order.
    """
    wid, data, folder = copy_folder(env, order)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
    final = folder / f"{wid}-{stamp}.zip"
    part = folder / f".{wid}-{stamp}.zip.part"
    side = order_file(final)
    kept = {key: order[key] for key in ("id", "domain", "image", "limits", "org") if key in order}  # never keys
    try:
        R.engine_copia(order, data, part, mode=0o600)
        with zipfile.ZipFile(part) as zipped:
            if ENGINE_MANIFEST not in zipped.namelist():
                raise OSError(f"the engine's copy has no {ENGINE_MANIFEST}")
        os.chmod(part, 0o600)
        R.write_json(side, {"format": ORDER_FORMAT, "world": wid, "copiedAt": R.utc(now), "order": kept}, mode=0o600)
        os.replace(part, final)
    except BaseException:
        part.unlink(missing_ok=True)
        side.unlink(missing_ok=True)
        raise
    return final


def done_today(env: Env, order: dict, folder: Path, today: str) -> bool:
    """Whether the world already has tonight's copy, one the room can bring back.

    A Hyperfy world: any zip dated today. A Numinia Engine world: only an
    engine export (a `mundo.json` at its root) with its order file; a copy
    in the old Hyperfy shape does not count, so tonight's proper one is made.
    """
    for stamp, path in copies_of(folder):
        if not stamp.startswith(today):
            continue
        if not R.runs_engine(order):
            return True
        try:
            with zipfile.ZipFile(path) as zipped:
                if ENGINE_MANIFEST in zipped.namelist() and order_file(path).is_file():
                    return True
        except (OSError, zipfile.BadZipFile):
            pass
    return False


# ---------------------------------------------------------------------------
# The list of the copies (for the door) and a copy brought out (DownloadNightly)
# ---------------------------------------------------------------------------


def stamp_time(stamp: str) -> str | None:
    """The ISO time (UTC) a stamp names; None when it is not a real date and time."""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.strptime(stamp, "%Y%m%d-%H%M%S"))
    except ValueError:
        return None


def describe(path: Path) -> tuple[str | None, bool]:
    """(kind, restorable) of one nightly zip, from its central directory alone: nothing is unpacked.

    `numinia-engine`: a `mundo.json` at the root, restorable when the order
    file beside it is there too. `hyperfy`: a `world/db.sqlite`. Any other
    zip, or a file that is not one, is (None, False).
    """
    try:
        with zipfile.ZipFile(path) as zipped:
            names = set(zipped.namelist())
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, ValueError, RuntimeError):
        return None, False
    if ENGINE_MANIFEST in names:
        side = order_file(path)
        return "numinia-engine", side.is_file() and not side.is_symlink()
    if HYPERFY_DB in names:
        return "hyperfy", True
    return None, False


def build_index(home: Path) -> list[dict]:
    """What the room may list: every nightly copy as {world, stamp, at, bytes, kind, restorable}, newest first.

    At most INDEX_PER_WORLD of a world and INDEX_MAX in all. Names a file
    only by world and stamp: no path, no key. A folder or a zip that is a
    symlink, or a name that is not ours, is not listed.
    """
    root = home / NIGHTLY_DIR
    found: list[dict] = []
    folders = sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink()) if root.is_dir() and not root.is_symlink() else []
    for folder in folders:
        if not R.matches(R.SLUG, folder.name):
            continue
        listed = 0
        for stamp, path in reversed(copies_of(folder)):
            if listed >= INDEX_PER_WORLD:
                break
            match = ZIP_NAME.fullmatch(path.name)
            at = stamp_time(stamp)
            if at is None or match is None or match["world"] != folder.name:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            kind, restorable = describe(path)
            found.append({"world": folder.name, "stamp": stamp, "at": at, "bytes": size, "kind": kind,
                          "restorable": restorable})
            listed += 1
    found.sort(key=lambda e: e["world"])
    found.sort(key=lambda e: e["stamp"], reverse=True)  # stable: newest first, then by world
    return found[:INDEX_MAX]


def write_index(home: Path, now: float | None = None) -> list[dict]:
    """Write <home>/run/nightly.json for the door, which cannot open the (root-only) nightly folder."""
    copies = build_index(home)
    R.write_json(home / "run" / INDEX_FILE, {"at": R.utc(now), "copies": copies}, mode=0o644)
    return copies


def ensure_index(home: Path) -> None:
    """Write the list when there is none yet (a machine installed before it); the nightly pass keeps it after."""
    if not (home / "run" / INDEX_FILE).is_file():
        try:
            write_index(home)
        except OSError as error:
            print(f"the list of nightly copies could not be written ({type(error).__name__})", file=sys.stderr)


def nightly_zip(home: Path, wid: str, stamp: str) -> Path | None:
    """The nightly zip of this world and stamp when it is a regular file in the world's own folder; else None.

    The world need not be here: its folder outlives it. No symlink anywhere on
    the way (the nightly folder, the world's, the file), and the name is
    built from the checked world and stamp, never taken from outside.
    """
    if not R.matches(R.SLUG, wid) or not R.matches(R.STAMP, stamp) or stamp_time(stamp) is None:
        return None
    root = home / NIGHTLY_DIR
    folder = root / wid
    path = folder / f"{wid}-{stamp}.zip"
    try:
        if root.is_symlink() or folder.is_symlink() or not folder.is_dir():
            return None
        if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            return None
    except OSError:
        return None
    return path


def download_nightly(home: Path, message: dict) -> str | None:
    """A signed DownloadNightly: check there is such a copy; the broken rule, or None when it may be queued.

    `not-found`: no nightly folder for the world, no zip of that stamp in
    it, or one that is not a plain file. Nothing is written here.
    """
    return None if nightly_zip(home, message["world"], message["stamp"]) is not None else "not-found"


def make_copy(home: Path, job: dict, now: float) -> bool:
    """Turn the nightly copy a job names into an ordinary copy record + zip, as a download of the world makes one.

    The file is copied (never linked: finishing a copy changes its group
    and mode, and the nightly zip stays root's alone), read through one
    descriptor opened without following a link and checked to be a regular
    file, so nothing can swap it for another in between. It is named as the
    nightly zip is. Fails (state `failed`, the rule in the request's result)
    when it is gone (`not-found`), the disk would be left under R.RESERVE
    (`no-room`) or the copy cannot be made. Returns whether a copy is ready.
    """
    wid, stamp, copy_id = job["world"], job["nightly"], job["copy"]
    copies = home / "copies"
    entry = {"nonce": job["nonce"], "type": "DownloadNightly", "world": wid, "stamp": stamp, "by": job.get("by")}
    failed = {"copy": copy_id, "world": wid, "state": "failed", "at": R.utc(now)}
    group = R.door_gid()

    def fail(rule: str) -> bool:
        R.write_json(copies / f"{copy_id}.json", failed, mode=0o640, group=group)
        R.answer(home, entry, "failed", rule, now)
        return False

    path = nightly_zip(home, wid, stamp)
    if path is None:
        return fail("not-found")
    copies.mkdir(parents=True, exist_ok=True)
    part = copies / f".{copy_id}.zip.part"
    try:
        source = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return fail("not-found")
    try:
        with os.fdopen(source, "rb") as src:
            if not stat.S_ISREG(os.fstat(src.fileno()).st_mode):
                return fail("not-found")
            if shutil.disk_usage(copies).free - os.fstat(src.fileno()).st_size < R.RESERVE:
                return fail("no-room")
            out = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
            with os.fdopen(out, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
        R.finish_copy(home, wid, copy_id, part, path.name, now)
    except (OSError, ValueError) as error:
        part.unlink(missing_ok=True)
        print(f"the nightly copy of {wid} could not be brought out ({type(error).__name__})", file=sys.stderr)
        return fail("the copy could not be made")
    R.answer(home, entry, "done", None, now)
    return True


def run_pass(env: Env) -> dict:
    """One nightly pass over every order. Never raises for one world's trouble; says what happened to each."""
    now = env.now()
    env.dir.mkdir(parents=True, exist_ok=True)
    os.chmod(env.dir, 0o700)
    today = day_of(now)
    results: dict[str, str] = {}
    pruned: list[str] = []
    for order in R.read_orders(env.home / "orders"):
        wid = order["id"]
        if not R.matches(R.SLUG, wid):
            continue
        folder = env.dir / wid
        for stale in folder.glob(".*.part") if folder.is_dir() else []:  # a pass that was killed
            stale.unlink(missing_ok=True)
        for stale in folder.glob(".*.sqlite.tmp") if folder.is_dir() else []:
            stale.unlink(missing_ok=True)
        for stale in folder.glob(".*.order.json.tmp") if folder.is_dir() else []:
            stale.unlink(missing_ok=True)
        if done_today(env, order, folder, today):
            results[wid] = "already done today"
        else:
            data = env.home / "data" / wid
            if data.is_symlink() or not data.is_dir():
                results[wid] = "skipped: no folder"
            else:
                need = tree_bytes(data)
                free = env.free(env.dir)
                if free - need < MIN_FREE_BYTES:
                    results[wid] = f"skipped: low disk ({free // 2**20} MiB free, {need // 2**20} MiB to copy)"
                    env.log(f"nightly {wid}: {results[wid]}")
                else:
                    try:
                        results[wid] = f"copied: {copy_world(env, order, now).name}"
                    except (OSError, ValueError, KeyError, zipfile.BadZipFile, sqlite3.Error, subprocess.SubprocessError) as error:
                        results[wid] = f"failed: {type(error).__name__}: {error}"
                        env.log(f"nightly {wid}: {results[wid]}")
        pruned += [f"{wid}/{name}" for name in prune(env, wid)]
    # Worlds no longer on the machine: their copies still age out, the last one stays.
    for folder in sorted(p for p in env.dir.iterdir() if p.is_dir()):
        if folder.name not in results:
            pruned += [f"{folder.name}/{name}" for name in prune(env, folder.name)]
    summary = {"at": R.utc(now), "worlds": results, "pruned": pruned}
    R.write_json(env.dir / STATE_FILE, summary, mode=0o600)
    try:
        write_index(env.home, now)
    except OSError as error:  # the list is the door's; a pass that made its copies still ends well
        env.log(f"nightly: the list of copies could not be written ({type(error).__name__})")
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Make tonight's copy of every world on this machine; keep 14 days.")
    ap.add_argument("--home", type=Path, default=R.HOME)
    args = ap.parse_args(argv)
    env = Env(args.home)
    env.dir.mkdir(parents=True, exist_ok=True)
    with open(env.dir / "lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another nightly pass is running", file=sys.stderr)
            return 0
        summary = run_pass(env)
    print(json.dumps(summary))
    return 1 if any(v.startswith("failed") for v in summary["worlds"].values()) else 0


if __name__ == "__main__":
    sys.exit(main())
