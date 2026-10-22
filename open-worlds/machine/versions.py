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

import os
import re
import shutil
import stat
from pathlib import Path

import reconcile as R

VERSIONS_DIR = "versions"  # under the fleet home: <id>.json beside a <id>/ folder per world
MAX_VERSIONS = 2  # a world keeps this many, published one included
VERSION = R.VERSION
ORIGIN = re.compile(r"created|zip|copy:v[1-9][0-9]{0,3}")
META_MAX = 64 * 1024  # a world's versions file is read whole only below this
FIRST = "v1"  # the one version every world has


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
