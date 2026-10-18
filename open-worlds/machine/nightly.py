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
* What goes in: the world's folder, its order (`order.json`) and a manifest
  with the sha256 of each file. Never keys or admin codes: a copy that is
  stolen opens nothing. (`env/` and `/etc/fleet/codes` stay out.)
* Kept 14 days. Older zips are deleted by the date in their name, but the
  newest copy of a world is never deleted, however old: a machine whose
  copies stopped must not erase the last one it has.
* Low disk: a world is copied only if the free space left after its folder's
  size is still above MIN_FREE_BYTES; otherwise it is skipped and logged
  (`skipped: low disk`), nothing is deleted to make room and the pass ends
  with success so the other worlds still get theirs. Pruning old zips runs
  anyway: that only frees space.

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
FORMAT = "numinia-world-nightly/1"


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


def prune(env: Env, wid: str) -> list[str]:
    """Delete the world's copies older than KEEP_DAYS days, always keeping the newest one."""
    gone = []
    copies = copies_of(env.dir / wid)
    limit = env.now() - KEEP_DAYS * 86400
    for stamp, path in copies[:-1]:  # the newest is never a candidate
        if day_start(stamp[:8]) + 86400 <= limit:  # the whole day it names is past the limit
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


def copy_world(env: Env, order: dict, now: float) -> Path:
    """Zip one world, read-only on its folder, into <home>/nightly/<id>/, and return the zip."""
    wid = order["id"]
    data = env.home / "data" / wid
    if data.is_symlink() or not data.is_dir():
        raise OSError("the world has no folder here")
    folder = env.dir / wid
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
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
        if any(stamp.startswith(today) for stamp, _p in copies_of(folder)):
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
                    except (OSError, ValueError, zipfile.BadZipFile, sqlite3.Error) as error:
                        results[wid] = f"failed: {type(error).__name__}: {error}"
                        env.log(f"nightly {wid}: {results[wid]}")
        pruned += [f"{wid}/{name}" for name in prune(env, wid)]
    # Worlds no longer on the machine: their copies still age out, the last one stays.
    for folder in sorted(p for p in env.dir.iterdir() if p.is_dir()):
        if folder.name not in results:
            pruned += [f"{folder.name}/{name}" for name in prune(env, folder.name)]
    summary = {"at": R.utc(now), "worlds": results, "pruned": pruned}
    R.write_json(env.dir / STATE_FILE, summary, mode=0o600)
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
