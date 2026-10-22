#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: AGPL-3.0-only
"""A world's versions: where they live on the machine, what is written about them, and how one goes.

A world keeps up to MAX_VERSIONS versions. One is published: it is the
world's own folder, /srv/fleet/data/<id>/, which never moves, so no
container's mount changes. Every other version waits beside it at
/srv/fleet/versions/<id>/<vid>/ (same disk, so a rename is atomic). A
version is data only: its address, keys, admin code and order are the
world's.

What is known about them is one small file per world,
/srv/fleet/versions/<id>.json, written whole (root:fleetd, mode 640, so the
door can read it and never write it):

    {"world": id, "published": "v1", "next": 3,
     "versions": {"v1": {"createdAt", "createdBy", "origin", "bytes"}, …}}

A version's name (`v1`, `v2`, …) is never reused within a world: `next` only
grows, so a download named v3 can never be mistaken for an older v2. `origin`
is `created`, `zip` or `copy:<vid>`; `bytes` is measured when the folder is
made (None when never measured). A world WITHOUT the file has one implicit
version, v1, published, `created`; the file is written the first time a
version operation changes something. A file that is there but cannot be
trusted is never read as the implicit one — that would lose a version — so
the callers refuse with the rule `versions-unreadable`.

This file is read by reconcile.py (the reconciler, as root) and by the door
(fleetd.py, which only reads). It imports reconcile.py for the small helpers
both share; reconcile.py imports it only inside the functions that need it,
so neither waits for the other at load.
"""

from __future__ import annotations

import errno
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

VERSIONS_DIR = "versions"  # under the fleet home: <id>.json beside a <id>/ folder per world
MAX_VERSIONS = 2  # a world keeps this many, published one included
VERSION = R.VERSION
ORIGIN = re.compile(r"created|zip|copy:v[1-9][0-9]{0,3}")
META_MAX = 64 * 1024  # a world's versions file is read whole only below this
FIRST = "v1"  # the one version every world has
NOT_WRITTEN = "the version could not be written here"  # a duplicate that failed for no rule of its own


def number(vid: str) -> int:
    """The number in a version's name: v7 is 7."""
    return int(vid[1:])


def implicit(wid: str, order: dict | None = None) -> dict:
    """What a world with no versions file has: v1, published, made when its order was."""
    created = (order or {}).get("createdAt")
    return {
        "world": wid,
        "published": FIRST,
        "next": 2,
        "versions": {
            FIRST: {
                "createdAt": created if isinstance(created, str) else None,
                "createdBy": None,
                "origin": "created",
                "bytes": None,
            }
        },
    }


def valid_version(info: object) -> bool:
    """Whether one version's entry has exactly its four fields, each of its shape."""
    if not isinstance(info, dict) or set(info) != {"createdAt", "createdBy", "origin", "bytes"}:
        return False
    size = info["bytes"]
    return (
        (info["createdAt"] is None or isinstance(info["createdAt"], str))
        and (info["createdBy"] is None or R.matches(R.ADDRESS, info["createdBy"]))
        and R.matches(ORIGIN, info["origin"])
        and (size is None or (isinstance(size, int) and not isinstance(size, bool) and size >= 0))
    )


def valid(wid: str, meta: object) -> bool:
    """Whether a world's metadata is whole: its fields, its versions, a published one among them, a `next` ahead."""
    if not isinstance(meta, dict) or set(meta) != {"world", "published", "next", "versions"}:
        return False
    versions = meta["versions"]
    if meta["world"] != wid or not isinstance(versions, dict) or not 1 <= len(versions) <= MAX_VERSIONS:
        return False
    if not all(R.matches(VERSION, vid) and valid_version(info) for vid, info in versions.items()):
        return False
    following = meta["next"]
    if isinstance(following, bool) or not isinstance(following, int):
        return False
    # Never reused: `next` is above every number there is, and a name must still fit v[1-9][0-9]{0,3}.
    return (
        meta["published"] in versions
        and max(number(vid) for vid in versions) < following <= 10_000
    )


def versions_root(home: Path) -> Path:
    return home / VERSIONS_DIR


def meta_path(home: Path, wid: str) -> Path:
    if not R.matches(R.SLUG, wid):
        raise ValueError("not a world id")
    return versions_root(home) / f"{wid}.json"


def read_versions(home: Path, order: dict) -> dict | None:
    """The metadata of the world this order is; the implicit one when there is no file; None when it cannot be trusted.

    None means the file is there but is a link, too big, not JSON, or not whole
    (see `valid`). The caller refuses with `versions-unreadable`; the file is
    never taken for the implicit metadata, so no version is lost by a reader.
    """
    wid = order.get("id") if isinstance(order, dict) else None
    if not isinstance(wid, str) or not R.matches(R.SLUG, wid):
        return None
    path = meta_path(home, wid)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return implicit(wid, order)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > META_MAX:
        return None
    meta = R.read_json(path, None)
    return meta if isinstance(meta, dict) and valid(wid, meta) else None


def ensure_dirs(home: Path, wid: str) -> Path:
    """Make versions/ (root:fleetd 750) and versions/<id>/ (root 700) when missing; refuse a link in their place."""
    root, mine = versions_root(home), versions_root(home) / wid
    if not R.matches(R.SLUG, wid):
        raise ValueError("not a world id")
    for folder, mode, group in ((root, 0o750, R.door_gid()), (mine, 0o700, None)):
        if folder.is_symlink():
            raise OSError("a versions folder is a link")
        folder.mkdir(exist_ok=True)
        os.chmod(folder, mode)
        if group is not None:
            os.chown(folder, 0, group)
    return mine


def write_versions(home: Path, wid: str, meta: dict) -> None:
    """Write a world's metadata whole (root:fleetd, mode 640), after making its folders. Raises on a bad shape."""
    if not valid(wid, meta):
        raise ValueError("not a world's versions")
    ensure_dirs(home, wid)
    R.write_json(meta_path(home, wid), meta, mode=0o640, group=R.door_gid())


def folder_of(home: Path, wid: str, meta: dict, vid: str) -> Path:
    """Where a version's data is: the world's own folder for the published one, versions/<id>/<vid> otherwise."""
    if not R.matches(R.SLUG, wid) or not R.matches(VERSION, vid):
        raise ValueError("not a world id, or not a version")
    if vid == meta["published"]:
        return home / "data" / wid
    return versions_root(home) / wid / vid


def new_vid(meta: dict) -> str | None:
    """The next version name for this world, taking it: `next` grows by one, in `meta`, and is never given twice.

    None when every name is used (v9999 was the last). The caller writes
    `meta` only once the version's folder exists.
    """
    following = meta["next"]
    if following > 9999:
        return None
    meta["next"] = following + 1
    return f"v{following}"


def measure(path: Path) -> int:
    """Bytes in a folder: its regular files, each inode once, no link followed or counted."""
    seen: set[tuple[int, int]] = set()
    total = 0
    if os.path.islink(path):
        return 0  # os.walk starts inside a link it is given; a version is never one
    for here, _, names in os.walk(path, followlinks=False):
        for name in names:
            try:
                info = os.lstat(os.path.join(here, name))
            except OSError:
                continue  # a file that went while we walked is not there to count
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) in seen:
                continue
            seen.add((info.st_dev, info.st_ino))
            total += info.st_size
    return total


def listed(home: Path, order: dict) -> list[dict]:
    """What the door shows of a world's versions: [{id, published, bytes, createdAt, origin}], by number.

    Empty when the metadata cannot be trusted: the door shows nothing it cannot vouch for.
    """
    meta = read_versions(home, order)
    if meta is None:
        return []
    return [
        {
            "id": vid,
            "published": vid == meta["published"],
            "bytes": info["bytes"],
            "createdAt": info["createdAt"],
            "origin": info["origin"],
        }
        for vid, info in sorted(meta["versions"].items(), key=lambda item: number(item[0]))
    ]


def remove_folder(home: Path, wid: str, vid: str) -> bool:
    """Remove versions/<id>/<vid>; True when nothing of it is left, False when it was refused or could not go.

    A link in its place is unlinked, never followed. A folder is removed only
    when its parent really is versions/<id>/ and that really is under
    versions/ (the check purge_world makes), so a link can never lead this
    to data/ or anywhere else. The published version lives in data/ and is
    never touched here.
    """
    if not R.matches(R.SLUG, wid) or not R.matches(VERSION, vid):
        raise ValueError("not a world id, or not a version")
    root = versions_root(home)
    mine = root / wid
    folder = mine / vid
    try:
        if root.is_symlink() or mine.is_symlink():
            return False  # nothing under a linked versions folder is ours to remove
        if folder.is_symlink():
            folder.unlink()
        elif folder.is_dir():
            if folder.resolve().parent != mine.resolve() or mine.resolve().parent != root.resolve():
                return False
            shutil.rmtree(folder)
        elif os.path.lexists(folder):
            folder.unlink()  # a stray file with a version's name
    except OSError:
        return False
    return not os.path.lexists(folder)


def delete_version(home: Path, orders: Path, entry: dict, message: dict) -> str | None:
    """A signed DeleteVersion: remove one unpublished version; the broken rule, or None when it is gone.

    Rules: `missing` (no order for the world), `versions-unreadable` (its
    metadata cannot be trusted), `no-version` (the world has no such
    version), `only-version` (a world always keeps one), `published` (the
    published one is the world's own folder; DeleteWorld deletes that), `not-removed`
    (the folder could not go, so the entry stays and a second request can try
    again), `versions-unwritable` (the folder went but the metadata could not
    be written; the same second request ends it). The folder goes first and
    the metadata last, so a version that is listed always has its data.
    """
    wid, vid = entry["world"], message["version"]
    order = R.read_order(orders / f"{wid}.json")
    if order is None:
        return "missing"
    meta = read_versions(home, order)
    if meta is None:
        return "versions-unreadable"
    if vid not in meta["versions"]:
        return "no-version"
    if len(meta["versions"]) < 2:
        return "only-version"  # a world with one version has only the published one
    if vid == meta["published"]:
        return "published"
    if not remove_folder(home, wid, vid):
        return "not-removed"
    del meta["versions"][vid]
    try:
        write_versions(home, wid, meta)
    except OSError:
        return "versions-unwritable"
    return None


def copy_files(source: Path, target: Path) -> None:
    """A Hyperfy world's files, into an empty folder: the database by SQLite's backup, the rest linked or copied.

    Only what an import keeps (R.WORLD_FILES, R.WORLD_FOLDERS): `db.sqlite`
    goes through the online backup (the world may be writing to it), so the
    `-wal`, `-shm` and `-journal` files are never copied; `storage.json` is
    copied; every regular file under `assets/` and `collections/` is
    hardlinked (those are named by their hash and never rewritten), or
    copied where the disk will not link (`EXDEV`, `EPERM`, `EMLINK`). A
    link is never followed and never copied, whatever it points at.
    """
    database = source / R.ENGINE_DB
    if database.is_symlink() or not database.is_file():
        raise OSError("the world has no database here")
    R.snapshot_sqlite(database, target / R.ENGINE_DB)
    for name in sorted(R.WORLD_FILES - {R.ENGINE_DB}):
        plain = source / name
        if plain.is_file() and not plain.is_symlink():
            shutil.copy2(plain, target / name, follow_symlinks=False)
    for folder in (name.rstrip("/") for name in R.WORLD_FOLDERS):
        top = source / folder
        if top.is_symlink() or not top.is_dir():
            continue
        for here, dirs, names in os.walk(top, followlinks=False):
            dirs[:] = sorted(name for name in dirs if not os.path.islink(os.path.join(here, name)))
            where = target / Path(here).relative_to(source)
            where.mkdir(parents=True, exist_ok=True)
            for name in sorted(names):
                found = os.path.join(here, name)
                if not stat.S_ISREG(os.lstat(found).st_mode):
                    continue
                try:
                    os.link(found, where / name, follow_symlinks=False)
                except OSError as error:
                    if error.errno not in (errno.EXDEV, errno.EPERM, errno.EMLINK):
                        raise
                    shutil.copy2(found, where / name, follow_symlinks=False)


def copy_engine(source: Path, target: Path, order: dict, zipped: Path) -> None:
    """A Numinia Engine world's folder copied by the engine itself: `copia` into a zip, then `comprobar` + `importar`.

    The world's own image runs `copia` on the SOURCE folder, read-only
    (exactly as a download does, R.build_engine_copy); its zip comes out on
    stdout into `zipped`, a file only this run can open. The same zip is
    then handed back through the import's own path (R.engine_import) into
    the empty `target`. The engine writes each snapshot whole, so the
    source may be live. Raises R.Refused (`engine-refused`, or the plain
    rule the tools could not run) or OSError.
    """
    fd = os.open(zipped, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w+b") as out:
        try:
            done = R.run_tool(R.engine_tool(order["image"], "copia", order["limits"], f"{source}:{R.ENGINE_FOLDER}:ro"),
                              stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.PIPE, timeout=R.TOOL_SECONDS)
        except subprocess.TimeoutExpired:
            raise R.Refused("engine-refused", f"copia: it took longer than {R.TOOL_SECONDS} s") from None
        out.flush()
        if done.returncode == 1:
            raise R.Refused("engine-refused", R.tool_line(done))
        if done.returncode != 0:
            raise R.Refused("the engine's tools could not run here")
        if not zipfile.is_zipfile(out):
            raise R.Refused("engine-refused", "copia: its copy is not a zip")
        R.engine_import(out, target, order["image"], order["limits"])


def duplicate_version(home: Path, orders: Path, entry: dict, message: dict, now: float | None = None,
                      sleep=None) -> str | None:
    """A signed DuplicateVersion: copy one version into a new one, unpublished; the broken rule, or None when it is there.

    Rules, in this order: `missing` (no order for the world),
    `versions-unreadable` (its metadata cannot be trusted), `no-version`
    (the world has no such version), `full` (it already keeps
    MAX_VERSIONS, or has used every name), `no-room` (the disk must keep
    R.RESERVE free after the source's size; twice that for a Numinia
    Engine world, whose zip and folder both exist for a while; also when
    the disk fills mid-way), `engine-refused` (the engine's own tools said
    no). Anything else that fails is the plain NOT_WRITTEN.

    The copy is made in a root-only `staging/<nonce>/` (a Numinia Engine
    world's zip beside it, `staging/<nonce>.zip`) and only at the end
    renamed to `versions/<id>/<vid>`; the metadata is written last, with the
    new version, so a version that is listed always has its whole data
    and a failure names nothing a version. The new name is `next`, never
    given before; it goes in `entry["made"]` for the request's result. A
    world that had no versions file gets one, its implicit v1 kept. The
    world may be running: a Hyperfy world saves on a timer, so a running
    published source is given its save pause first (`sleep`, time.sleep
    unless a test puts another in its place). No file of the source is
    changed or removed (SQLite may fold a leftover `-wal` into its database
    when it opens it, as a download's copy does).
    """
    wid, source = entry["world"], message["version"]
    order = R.read_order(orders / f"{wid}.json")
    if order is None:
        return "missing"
    meta = read_versions(home, order)
    if meta is None:
        return "versions-unreadable"
    if source not in meta["versions"]:
        return "no-version"
    if len(meta["versions"]) >= MAX_VERSIONS:
        return "full"
    vid = new_vid(meta)
    if vid is None:
        return "full"
    engine = R.runs_engine(order)
    staging = home / "staging"
    stage = staging / entry["nonce"]
    zipped = staging / f"{entry['nonce']}.zip"
    try:
        here = folder_of(home, wid, meta, source)
        if here.is_symlink() or not here.is_dir():
            return NOT_WRITTEN
        if not staging.is_dir():
            staging.mkdir(mode=0o700, parents=True)  # bootstrap.sh makes it; made here the same way if missing
        size = measure(here)
        if shutil.disk_usage(staging).free < size * (2 if engine else 1) + R.RESERVE:
            return "no-room"
        if not engine and source == meta["published"] and order.get("state") == "running":
            (time.sleep if sleep is None else sleep)(R.save_pause(R.SAVE_INTERVAL))  # the engine saves on a timer; let the next save land
        os.mkdir(stage, 0o700)
        if engine:
            copy_engine(here, stage, order, zipped)
        else:
            copy_files(here, stage)
        os.chmod(stage, 0o700 if engine else 0o755)  # as settle_import leaves a world's folder
        mine = ensure_dirs(home, wid)
        if os.path.lexists(mine / vid) and not remove_folder(home, wid, vid):
            return NOT_WRITTEN  # an unlisted leftover under the new name, and it would not go
        os.rename(stage, mine / vid)
        fresh = not os.path.lexists(meta_path(home, wid))
        if fresh and source == FIRST:
            meta["versions"][FIRST]["bytes"] = size  # the implicit v1 is written now; its size is known
        meta["versions"][vid] = {
            "createdAt": R.utc(now),
            "createdBy": entry["by"],
            "origin": f"copy:{source}",
            "bytes": measure(mine / vid),
        }
        try:
            write_versions(home, wid, meta)
        except (OSError, ValueError):
            remove_folder(home, wid, vid)  # listed nowhere, so it goes
            return "versions-unwritable"
        entry["made"] = vid
        return None
    except R.Refused as refused:
        return refused.rule
    except (OSError, ValueError, sqlite3.Error) as error:
        full = isinstance(error, OSError) and error.errno == errno.ENOSPC
        return "no-room" if full else NOT_WRITTEN
    finally:
        if stage.is_dir() and not stage.is_symlink():
            shutil.rmtree(stage, ignore_errors=True)
        if zipped.is_symlink() or zipped.is_file():
            zipped.unlink(missing_ok=True)


def copy_source(home: Path, order: dict, vid: str) -> tuple[str | None, Path | None]:
    """The folder a copy of one version is made from: (None, the folder), or (the rule it breaks, None).

    Rules: `versions-unreadable` (the world's metadata cannot be trusted),
    `no-version` (the world has no such version), `not-a-folder` (the
    version is listed but its folder is not a real one). The published
    version is the world's own folder; any other waits in versions/<id>/.
    """
    meta = read_versions(home, order)
    if meta is None:
        return "versions-unreadable", None
    if vid not in meta["versions"]:
        return "no-version", None
    here = folder_of(home, order["id"], meta, vid)
    if here.is_symlink() or not here.is_dir():
        return "not-a-folder", None
    return None, here


def download_version(home: Path, orders: Path, entry: dict, message: dict) -> str | None:
    """A signed DownloadVersion: check it can be made; the broken rule, or None when it may be queued.

    Rules, in this order: `missing` (no order for the world),
    `versions-unreadable`, `no-version`, `not-a-folder` (copy_source). Nothing
    is written: take_requests queues the copy as it queues a DownloadWorld,
    its job naming the version, and make_copies builds it from the version's
    own folder (the existing builders, so the engines' rules stay theirs).
    The save wait is the published version's alone, of a running Hyperfy
    world (R.copies_wait): an unpublished folder is written by nobody. The
    folder is looked for again when the copy is built, in case the world
    was switched or the version deleted in between.
    """
    order = R.read_order(orders / f"{entry['world']}.json")
    if order is None:
        return "missing"
    return copy_source(home, order, message["version"])[0]


def import_replace(meta: dict, replace: str) -> str | None:
    """The rule an ImportVersion's `replace` breaks for a world with these versions; None when it holds.

    A world with room keeps `replace` empty (`no-version` when it names
    one). A world with every place used must name the unpublished version
    to take the place of: empty is `full`, the published one `published`,
    one it does not have `no-version`.
    """
    if len(meta["versions"]) < MAX_VERSIONS:
        return None if replace == "" else "no-version"
    if replace == "":
        return "full"
    if replace == meta["published"]:
        return "published"
    return None if replace in meta["versions"] else "no-version"


def import_version(home: Path, orders: Path, entry: dict, message: dict, raw: dict,
                   now: float | None = None) -> tuple[str | None, str | None]:
    """A signed ImportVersion: a zip becomes a new, unpublished version of an existing world; (rule, entry) or (None, None).

    Rules, in this order: `missing` (no order for the world; the zip goes
    with the request), `versions-unreadable`, the `replace` rules
    (import_replace), `full` (every name used), `unknown-engine`,
    `zip-too-big`, `fingerprint` (the zip is not the one the Oracle signed),
    then the import's own, those of an ImportWorld for the world's OWN
    engine (R.stage_import): the zip's rules, `engine-refused`, `no-room`.
    A zip made for the other engine fails that engine's rules. Anything
    else that fails is the plain NOT_WRITTEN.

    The upload is unpacked into a root-only `staging/<nonce>/` and checked
    whole; only then is it renamed to `versions/<id>/<new vid>`, and the
    metadata written last, with the new version (`origin: zip`) and without
    the one it replaced. The replaced version's folder goes after that, so
    whatever fails before the metadata is written leaves the old version and
    the metadata as they were, and a version that is listed always has its
    data. The new name is `next`, never given before; it is `entry["made"]`,
    and the replaced one (or "") is `entry["version"]`, for the request's
    result. A world that had no versions file gets one, its implicit v1
    kept. The world keeps running the whole time: its plan, its folder and
    its container are not touched. run/import.json names the world while
    this runs, with `type: "ImportVersion"` and the vid it will make, for
    the door to show and for clear_imports to judge a run that died.
    """
    wid, nonce, replace = entry["world"], entry["nonce"], message["replace"]
    entry["version"] = replace
    order = R.read_order(orders / f"{wid}.json")
    if order is None:
        return "missing", None
    meta = read_versions(home, order)
    if meta is None:
        return "versions-unreadable", None
    rule = import_replace(meta, replace)
    vid = new_vid(meta) if rule is None else None
    if rule is None and vid is None:
        rule = "full"
    if rule is None and R.engine_of(order.get("image")) is None:
        rule = "unknown-engine"
    if rule is None and message["zipBytes"] > R.ZIP_MAX:
        rule = "zip-too-big"
    if rule is None and raw.get("zip") != f"{nonce}.zip":
        rule = "fingerprint"
    if rule is not None:
        return rule, None
    engine = R.runs_engine(order)
    staging = home / "staging"
    stage = staging / nonce
    placed = committed = False
    try:
        if not staging.is_dir():
            staging.mkdir(mode=0o700, parents=True)  # bootstrap.sh makes it; made here the same way if missing
        R.write_json(home / "run" / R.IMPORTING,
                     {wid: {"nonce": nonce, "by": entry["by"], "type": "ImportVersion", "made": vid,
                            "message": message}})
        R.answer(home, entry, "working", None, now)
        R.stage_import(home, nonce, message, order["image"], order["limits"], stage)
        os.chmod(stage, 0o700 if engine else 0o755)  # as settle_import leaves a world's folder
        mine = ensure_dirs(home, wid)
        if os.path.lexists(mine / vid) and not remove_folder(home, wid, vid):
            return NOT_WRITTEN, None  # an unlisted leftover under the new name, and it would not go
        os.rename(stage, mine / vid)
        placed = True
        if not os.path.lexists(meta_path(home, wid)):
            meta["versions"][FIRST]["bytes"] = measure(home / "data" / wid)  # the implicit v1 is written now
        meta["versions"].pop(replace, None)
        meta["versions"][vid] = {
            "createdAt": R.utc(now),
            "createdBy": entry["by"],
            "origin": "zip",
            "bytes": measure(mine / vid),
        }
        try:
            write_versions(home, wid, meta)
        except (OSError, ValueError):
            return "versions-unwritable", None
        committed = True
        entry["made"] = vid
        if replace and not remove_folder(home, wid, replace):
            print(f"{wid}: the replaced version's folder could not be removed; it is no longer listed",
                  file=sys.stderr)
        return None, None
    except R.Refused as refused:
        return refused.rule, refused.entry
    except (OSError, ValueError, sqlite3.Error) as error:
        full = isinstance(error, OSError) and error.errno == errno.ENOSPC
        return ("no-room" if full else NOT_WRITTEN), None
    finally:
        if stage.is_dir() and not stage.is_symlink():
            shutil.rmtree(stage, ignore_errors=True)
        if placed and not committed:
            remove_folder(home, wid, vid)  # listed nowhere, so it goes


def clear_import(home: Path, orders: Path, entry: dict, job: dict, now: float | None = None) -> None:
    """Answer an ImportVersion whose run died (R.clear_imports): done if its version is listed, failed otherwise.

    The metadata is the import's last step, so a version that is listed
    has its whole data and one that is not never will: any folder the run
    left under the name it was to make goes, and so does the folder of the
    version it replaced when the metadata already dropped it. `entry` is
    the request's, `job` its marker in run/import.json.
    """
    wid, made, message = entry["world"], job.get("made"), job.get("message")
    entry["type"] = "ImportVersion"
    replace = message.get("replace") if isinstance(message, dict) else None
    if replace == "" or R.matches(R.VERSION, replace):
        entry["version"] = replace
    order = R.read_order(orders / f"{wid}.json")
    meta = read_versions(home, order) if order is not None else None
    listed = meta is not None and R.matches(R.VERSION, made) and made in meta["versions"]
    if listed:
        entry["made"] = made
        R.answer(home, entry, "done", None, now)
        if replace and replace not in meta["versions"]:
            remove_folder(home, wid, replace)
    else:
        R.answer(home, entry, "failed", "the run stopped in the middle of the import", now)
        if meta is not None and R.matches(R.VERSION, made):
            remove_folder(home, wid, made)


def folder_ino(path: Path) -> int | None:
    """The inode of a real folder at `path`; None when there is nothing there; -1 when there is a link or a file.

    -1 never equals a recorded inode, so a link or a file in a folder's place
    is "something else", never "missing" and never "the folder".
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return -1
    return info.st_ino if stat.S_ISDIR(info.st_mode) else -1


def publish_version(home: Path, orders: Path, entry: dict, message: dict) -> str | None:
    """A signed PublishVersion: write it to the journal run/switching.json; the broken rule, or None when it is written.

    Rules, in this order: `missing` (no order for the world),
    `versions-unreadable` (its metadata cannot be trusted), `no-version`
    (the world has no such version), `already-published` (it is the
    published one; nothing to do, so it is refused rather than answered
    done), `busy` (the world is already being switched, is updating, or
    waits to be restarted), `not-a-folder` (the world's folder or the
    version's is not a real folder), `versions-unwritable` (the journal could
    not be written). Nothing moves here: the journal names the world,
    the next plan leaves it out of the book, and finish_switches moves the
    folders once Docker has taken its container down. The inode of both
    folders is written now, so a run that finds the machine halfway can
    tell which step was done.
    """
    wid, to = entry["world"], message["version"]
    order = R.read_order(orders / f"{wid}.json")
    if order is None:
        return "missing"
    meta = read_versions(home, order)
    if meta is None:
        return "versions-unreadable"
    if to not in meta["versions"]:
        return "no-version"
    if to == meta["published"]:
        return "already-published"
    jobs = R.read_switching(home)
    if wid in jobs or wid in R.read_updates(home) or wid in R.read_restarts(home):
        return "busy"
    root = versions_root(home)
    if root.is_symlink() or (root / wid).is_symlink():
        return "not-a-folder"
    inode_data = folder_ino(home / "data" / wid)
    inode_to = folder_ino(root / wid / to)
    if inode_data is None or inode_to is None or -1 in (inode_data, inode_to):
        return "not-a-folder"
    jobs[wid] = {"nonce": entry["nonce"], "by": entry["by"], "from": meta["published"], "to": to,
                 "inoData": inode_data, "inoTo": inode_to}
    try:
        R.write_switching(home, jobs)
    except OSError:
        return "versions-unwritable"
    return None


def docker_gone(wid: str) -> bool:
    """Whether Docker has no container of this world left (running or not); any error is "not gone", to try next run."""
    try:
        done = subprocess.run(
            ["docker", "ps", "-a", "-q", "--filter", f"label=com.docker.compose.service={wid}"],
            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL, timeout=60,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return done.stdout.strip() == ""


def sync_folder(path: Path) -> None:
    """Make a folder's last renames reach the disk, so the inode picture a crash leaves is one the recovery reads."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def switch_step(home: Path, wid: str, job: dict) -> int | None:
    """Which step of a switch is next, from where the two recorded inodes are: 1, 2 or 3; None when it fits none.

    `inoData` is the folder that was the published version's, `inoTo` the one
    that is to become it. 3 means both renames are done (the world's folder
    is `inoTo`, the old one waits as `<from>`); the metadata is what is left.
    """
    root = versions_root(home) / wid
    here, old, new = folder_ino(home / "data" / wid), folder_ino(root / job["from"]), folder_ino(root / job["to"])
    if here == job["inoTo"] and old == job["inoData"]:
        return 3
    if old == job["inoData"] and here is None and new == job["inoTo"]:
        return 2
    if here == job["inoData"] and old is None and new == job["inoTo"]:
        return 1
    return None


def switch_world(home: Path, orders: Path, wid: str, job: dict) -> str:
    """Take one world's switch as far as it goes now: `done`, `stuck` (no step fits the folders) or `retry` (an OSError).

    The same function is the first try and the recovery: it reads the
    inode picture at the start of every step and does what the picture says.
    """
    root = versions_root(home)
    for _ in range(4):
        step = switch_step(home, wid, job)
        if step is None:
            return "stuck"
        try:
            if step == 1:
                mine = ensure_dirs(home, wid)
                os.rename(home / "data" / wid, mine / job["from"])
                sync_folder(home / "data")
                sync_folder(mine)
            elif step == 2:
                os.rename(root / wid / job["to"], home / "data" / wid)
                sync_folder(home / "data")
                sync_folder(root / wid)
            else:
                return switch_metadata(home, orders, wid, job)
        except OSError as error:
            print(f"{wid}: a step of a version switch could not be done ({type(error).__name__}); the next run goes on",
                  file=sys.stderr)
            return "retry"
    return "stuck"


def switch_metadata(home: Path, orders: Path, wid: str, job: dict) -> str:
    """The last step: the metadata says `to` is published, with both folders measured again; `done`, `stuck` or `retry`."""
    order = R.read_order(orders / f"{wid}.json")
    meta = read_versions(home, order) if order is not None else None
    if meta is None or job["from"] not in meta["versions"] or job["to"] not in meta["versions"]:
        return "stuck"
    if meta["published"] not in (job["from"], job["to"]):
        return "stuck"
    meta["published"] = job["to"]
    meta["versions"][job["to"]]["bytes"] = measure(home / "data" / wid)
    meta["versions"][job["from"]]["bytes"] = measure(versions_root(home) / wid / job["from"])
    try:
        write_versions(home, wid, meta)
    except ValueError:
        return "stuck"
    except OSError:
        print(f"{wid}: the versions file could not be written; the next run goes on", file=sys.stderr)
        return "retry"
    return "done"


def finish_switches(home: Path, orders: Path, now: float | None = None, container_gone=None) -> list[str]:
    """Move the folders of every world the journal names whose container is gone; the worlds whose switch ended.

    Called after Docker took the book without those worlds
    (reconcile.plan leaves them out). `container_gone(wid)` says whether
    Docker has no container of the world: docker_gone unless a test puts
    another in its place; a world whose container is not gone, or cannot be
    asked about, is left for the next run, and nothing moves. A switch that
    ends is answered `done` before its journal entry goes, so a machine that
    dies in between answers again (the same) on the next run. A switch whose
    folders are in none of the places a step leaves them is answered `failed`
    with `switch-stuck` ONCE (the entry gets `stuck: true`), the line goes to
    stderr, and the entry stays: the world stays out of the book, never
    started on the wrong folder. A person who puts the folders right is
    followed on the next run.
    """
    jobs = R.read_switching(home)
    gone = container_gone or docker_gone
    ended = []
    for wid, job in sorted(jobs.items()):
        if not gone(wid):
            continue
        entry = {"nonce": job["nonce"], "type": "PublishVersion", "world": wid, "by": job["by"], "version": job["to"]}
        outcome = switch_world(home, orders, wid, job)
        if outcome == "done":
            R.answer(home, entry, "done", None, now)
            ended.append(wid)
            jobs.pop(wid)
            R.write_switching(home, jobs)
        elif outcome == "stuck" and not job.get("stuck"):
            print(f"{wid}: the folders of a version switch are not where a step leaves them; "
                  f"the world stays down until a person looks", file=sys.stderr)
            job["stuck"] = True
            R.write_switching(home, jobs)
            R.answer(home, entry, "failed", "switch-stuck", now)
    return ended


def purge_versions(home: Path, wid: str) -> None:
    """Remove every version a deleted world left: versions/<id>/ and versions/<id>.json. Never data/."""
    root = versions_root(home)
    mine = root / wid
    if not R.matches(R.SLUG, wid):
        raise ValueError("not a world id")
    if mine.is_symlink():
        mine.unlink()
    elif mine.is_dir() and not root.is_symlink() and mine.resolve().parent == root.resolve():
        shutil.rmtree(mine)
    (root / f"{wid}.json").unlink(missing_ok=True)
    (root / f".{wid}.json.tmp").unlink(missing_ok=True)
