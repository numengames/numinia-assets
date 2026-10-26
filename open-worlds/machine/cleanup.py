#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: AGPL-3.0-only
"""The fleet machine's disk safety: old engine images and pre-update copies get a limit, and the door shows the disk.

Runs once a day from a systemd timer (fleet-cleanup.timer, 04:10 machine time,
after the nightly copy at 03:30), installed by the bootstrap and never run
from the clone. Before this, nothing removed an old engine image: every
engine update leaves the previous image on the disk for good, and a machine
that updates often fills up in weeks. Each pass:

1. Pre-update copies (<home>/release/preupdate/*.zip, made by release.py
   before each update): the existing 14 days AND a cap on the folder
   (PREUPDATE_CAP_BYTES, 2 GiB). Over the cap, the oldest go first, but the
   newest copy of each world is never deleted by the cap. This module owns
   the rule; release.py calls it (`prune_preupdate`), there is no second one.
2. Images, deleted one by one by their exact id with `docker rmi <id>` (never
   `docker image prune`, never `-f`). An image is removed only when ALL hold:
   no container (running or stopped) uses it; no order in <home>/orders
   names it (its `image`, its `previousImage`, a pin's image: every string
   in the order is compared); it is not the target, the previous image or
   the applied image recorded in release/state.json; the compose files Docker
   last accepted do not use it; it is not Caddy's; it is not among the newest
   NEWEST_KEEP local images of its repository (so a rollback target exists
   even when every record is odd); and it is older than MIN_AGE_SECONDS
   (48 h, counted from the later of its build and the last time it was
   tagged or pulled here). Dangling images follow the same rules, minus the
   repository one. If docker cannot list images or containers, or the orders
   cannot be read, no image is touched. While release.py holds its lock (an
   update is under way) images are not touched either.
3. Facts: the disk (used percent, free, total), the size of the containers'
   json logs and of the journal (read only), the state `ok` (< 80 %), `warn`
   (>= 80 %) or `full` (>= 90 %), written to <home>/cleanup/state.json for the
   door (`/fleet/v1/machine` → `disk`). Still at 85 % or more AFTER cleaning:
   nothing else is deleted, the note only says so (a person decides). Exit
   status 1 in the `full` state, so `systemctl --failed` shows it.

`--dry-run` prints what would go and changes nothing (no file, no state).
Nothing here touches data/, copies/, nightly/, env/, the signers or
/etc/fleet. One item's trouble (a `docker rmi` that fails, a file that
cannot be deleted) is reported and never stops the others. Everything outside
(clock, docker, disk) is passed in, so the tests run with fakes. Python's
standard library, plus reconcile.py's small helpers.
"""

from __future__ import annotations

import argparse
import calendar
import contextlib
import fcntl
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import reconcile as R

CLEANUP_DIR = "cleanup"
STATE_FILE = "state.json"
RELEASE_DIR = "release"
PRE_UPDATE_DIR = "preupdate"
MIN_AGE_SECONDS = 48 * 3600  # an image younger than this is never removed
NEWEST_KEEP = 2  # the newest local images of each repository are always kept
PREUPDATE_KEEP_SECONDS = 14 * 24 * 3600  # a copy made before an update stays this long ...
PREUPDATE_CAP_BYTES = 2 * 1024**3  # ... and the folder never holds more than this (the newest per world excepted)
WARN_PERCENT = 80.0
FULL_PERCENT = 90.0
STOP_PERCENT = 85.0  # still here after cleaning: nothing else is deleted, it is only said
DOCKER_TIMEOUT = 300
LIST_LIMIT = 50  # how many removed images the state names
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
PREUPDATE_ZIP = re.compile(r"(?P<world>[a-z0-9]+(?:-[a-z0-9]+)*)-(?P<at>\d{9,12})\.zip")
CONTAINER_LOGS = "/var/lib/docker/containers/*/*-json.log"
JOURNAL_DIR = "/var/log/journal"
DOCKER_DIR = "/var/lib/docker"


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def parse_time(value: object) -> float | None:
    """A Docker timestamp ('2026-10-20T10:11:12.123456789Z') → epoch seconds; None for anything else, or year < 2000."""
    if not isinstance(value, str):
        return None
    match = STAMP.fullmatch(value)
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups())
    if year < 2000 or not (1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60 and second < 61):
        return None
    try:
        return float(calendar.timegm((year, month, day, hour, minute, second)))
    except (ValueError, OverflowError):
        return None


def repository_of(reference: str) -> str:
    """'ghcr.io/x/engine@sha256:…' / 'ghcr.io/x/engine:tag' / 'localhost:5000/x:t' → the repository name."""
    name = reference.split("@", 1)[0]
    slash = name.rfind("/")
    colon = name.rfind(":")
    return name[:colon] if colon > slash else name


def strings_in(value: object, limit: int = 100_000) -> set[str]:
    """Every string anywhere in parsed JSON (keys excluded): an order is compared as a whole, so no field is missed."""
    found: set[str] = set()
    stack = [value]
    while stack and limit > 0:
        item = stack.pop()
        limit -= 1
        if isinstance(item, str):
            found.add(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def parse_images(raw: object) -> list[dict]:
    """`docker image inspect` output → [{id, refs, tokens, repos, created, tagged, size}], every value checked.

    refs: the tags and repo digests (what an order names). tokens: everything an
    image can be called by (its id, its refs, the digest part of each repo
    digest). An entry without a well-formed id is dropped: nothing is ever
    removed by a name it was not listed under.
    """
    images = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("Id"), str) or not IMAGE_ID.fullmatch(item["Id"]):
            continue
        tags = [t for t in (item.get("RepoTags") or []) if isinstance(t, str) and t and not t.startswith("<none>")]
        digests = [d for d in (item.get("RepoDigests") or []) if isinstance(d, str) and d and not d.startswith("<none>")]
        refs = sorted(set(tags) | set(digests))
        tokens = {item["Id"], *refs, *(d.split("@", 1)[1] for d in digests if "@" in d)}
        created = parse_time(item.get("Created"))
        meta = item.get("Metadata")
        tagged = parse_time(meta.get("LastTagTime")) if isinstance(meta, dict) else None
        size = item.get("Size")
        images.append({
            "id": item["Id"],
            "refs": refs,
            "tokens": tokens,
            "repos": sorted({repository_of(r) for r in refs}),
            "created": created,
            "tagged": tagged,
            "size": size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else 0,
        })
    return images


def newest_by_repository(images: list[dict], keep: int = NEWEST_KEEP) -> set[str]:
    """The ids of the `keep` newest images of each repository (by build time, then id). A dangling image has no repository."""
    groups: dict[str, list[dict]] = {}
    for image in images:
        for repo in image["repos"]:
            groups.setdefault(repo, []).append(image)
    safe: set[str] = set()
    for members in groups.values():
        members.sort(key=lambda i: (i["created"] or 0.0, i["id"]), reverse=True)
        safe.update(i["id"] for i in members[:keep])
    return safe


def plan_images(images: list[dict], protected: dict[str, set[str]], now: float) -> dict:
    """What happens to each image. {remove: [image], keep: {id: why}}. Pure.

    `protected` maps a reason (containers, orders, release, compose, caddy) to
    the names that reason protects. The first reason that holds is the one
    reported; any one is enough to keep an image.
    """
    newest = newest_by_repository(images)
    remove, keep = [], {}
    for image in images:
        why = next((reason for reason, names in protected.items() if image["tokens"] & names), None)
        if why is None and image["id"] in newest:
            why = f"one of the newest {NEWEST_KEEP} of its repository"
        if why is None:
            built, tagged = image["created"], image["tagged"]
            stamp = max(t for t in (built, tagged) if t is not None) if (built or tagged) else None
            if stamp is None:
                why = "its age is not known"
            elif now - stamp < MIN_AGE_SECONDS:
                why = "younger than 48 h"
        if why is None:
            remove.append(image)
        else:
            keep[image["id"]] = why
    return {"remove": remove, "keep": keep}


def disk_state(used_percent: float) -> str:
    return "full" if used_percent >= FULL_PERCENT else "warn" if used_percent >= WARN_PERCENT else "ok"


# ---------------------------------------------------------------------------
# Pre-update copies: the one rule, called by release.py too
# ---------------------------------------------------------------------------


def preupdate_copies(folder: Path) -> list[dict]:
    """The zips in the folder as {path, name, world|None, mtime, bytes}, oldest first. Links and others are not ours."""
    found = []
    for path in folder.glob("*.zip") if folder.is_dir() else []:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            info = path.stat()
        except OSError:
            continue
        match = PREUPDATE_ZIP.fullmatch(path.name)
        found.append({"path": path, "name": path.name, "world": match["world"] if match else None,
                      "mtime": info.st_mtime, "bytes": info.st_size})
    return sorted(found, key=lambda c: (c["mtime"], c["name"]))


def plan_preupdate(copies: list[dict], now: float, keep_seconds: int = PREUPDATE_KEEP_SECONDS,
                   cap: int = PREUPDATE_CAP_BYTES) -> dict:
    """Which copies go. {old: [copy], over: [copy], left: bytes, capped: bool}. Pure.

    old: past the age limit (whatever it is, as release.py always did). over: still
    too many bytes, oldest first, never the newest copy of a world. capped: the cap
    could not be met because only those newest copies are left.
    """
    old = [c for c in copies if now - c["mtime"] > keep_seconds]
    gone = {c["name"] for c in old}
    rest = [c for c in copies if c["name"] not in gone]
    total = sum(c["bytes"] for c in rest)
    newest: dict[str, str] = {}
    for c in rest:  # oldest first, so the last of each world wins
        if c["world"] is not None:
            newest[c["world"]] = c["name"]
    protected = set(newest.values())
    over = []
    for c in rest:
        if total <= cap:
            break
        if c["name"] in protected:
            continue
        over.append(c)
        total -= c["bytes"]
    return {"old": old, "over": over, "left": total, "capped": total > cap}


def prune_preupdate(folder: Path, now: float, dry_run: bool = False, keep_seconds: int = PREUPDATE_KEEP_SECONDS,
                    cap: int = PREUPDATE_CAP_BYTES) -> dict:
    """Apply the rule to the folder. {removed: [name], bytes: freed, left: bytes, capped: bool}. Never raises."""
    plan = plan_preupdate(preupdate_copies(folder), now, keep_seconds, cap)
    removed, freed = [], 0
    for c in plan["old"] + plan["over"]:
        try:
            if not dry_run:
                c["path"].unlink()
            removed.append(c["name"])
            freed += c["bytes"]
        except OSError:
            pass
    left = plan["left"] + sum(c["bytes"] for c in plan["over"] if c["name"] not in removed) \
        + sum(c["bytes"] for c in plan["old"] if c["name"] not in removed)
    return {"removed": removed, "bytes": freed, "left": left, "capped": plan["capped"]}


# ---------------------------------------------------------------------------
# Effects, all passed in through Env
# ---------------------------------------------------------------------------


class DockerError(Exception):
    """Docker could not answer: nothing is removed on a guess."""


class Env:
    """Everything the pass takes from outside. Tests replace each member."""

    def __init__(self, home: Path, docker_dir: str = DOCKER_DIR):
        self.home = home
        self.docker_dir = docker_dir
        self.now = time.time
        self.log = lambda line: print(line, file=sys.stderr)
        self.disk = self._disk  # () -> (total, free): bytes, free being what a normal process may still write
        self.images = self._images  # () -> parsed images; raises DockerError
        self.used = self._used  # () -> names of the images containers use (running or not); raises DockerError
        self.remove = self._remove  # (image id) -> (ok, why)
        self.container_log_bytes = self._container_log_bytes  # () -> int | None
        self.journal_bytes = self._journal_bytes  # () -> int | None

    @property
    def dir(self) -> Path:
        return self.home / CLEANUP_DIR

    def docker(self, *args: str) -> str:
        try:
            done = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=DOCKER_TIMEOUT,
                                  check=False, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as err:
            raise DockerError(f"docker {args[0]} could not run ({type(err).__name__})") from err
        if done.returncode != 0:
            raise DockerError(f"docker {args[0]} ended with {done.returncode}")
        return done.stdout

    def _disk(self) -> tuple[int, int]:
        path = self.docker_dir if os.path.isdir(self.docker_dir) else str(self.home)
        usage = shutil.disk_usage(path)
        stat = os.statvfs(path)
        used = (stat.f_blocks - stat.f_bfree) * stat.f_frsize  # df's own sum: used / (used + available)
        return used + usage.free, usage.free

    def _images(self) -> list[dict]:
        ids = sorted({line.strip() for line in self.docker("image", "ls", "-q", "--no-trunc").splitlines()
                      if IMAGE_ID.fullmatch(line.strip())})
        if not ids:
            return []
        try:
            return parse_images(json.loads(self.docker("image", "inspect", *ids)))
        except ValueError as err:
            raise DockerError("docker image inspect did not answer JSON") from err

    def _used(self) -> set[str]:
        ids = [line.strip() for line in self.docker("ps", "-aq", "--no-trunc").splitlines() if line.strip()]
        names: set[str] = set()
        if ids:
            for line in self.docker("container", "inspect", "--format", "{{.Image}} {{.Config.Image}}", *ids).splitlines():
                names.update(line.split())
        return names

    def _remove(self, image_id: str) -> tuple[bool, str]:
        if not IMAGE_ID.fullmatch(image_id):
            return False, "not an image id"
        try:
            self.docker("rmi", image_id)  # no -f: Docker itself refuses an image a container or another image needs
        except DockerError as err:
            return False, str(err)
        return True, "ok"

    def _container_log_bytes(self) -> int | None:
        total = 0
        try:
            for name in glob.glob(CONTAINER_LOGS):
                with contextlib.suppress(OSError):
                    total += os.lstat(name).st_size
        except OSError:
            return None
        return total

    def _journal_bytes(self) -> int | None:
        if not os.path.isdir(JOURNAL_DIR):
            return None
        total = 0
        for where, _dirs, names in os.walk(JOURNAL_DIR):
            for name in names:
                with contextlib.suppress(OSError):
                    total += os.lstat(os.path.join(where, name)).st_size
        return total


# ---------------------------------------------------------------------------
# What protects an image: read from the machine's own files
# ---------------------------------------------------------------------------


def protected_names(env: Env) -> dict[str, set[str]]:
    """reason → the names it protects. Raises OSError when the orders cannot be read (then no image is touched)."""
    orders = env.home / "orders"
    if not orders.is_dir():
        raise OSError("no orders folder")
    names: dict[str, set[str]] = {"used by an order": set(), "used by a container": set(), "release record": set(),
                                  "compose file": set(), "Caddy's": {R.CADDY_IMAGE}}
    for path in sorted(orders.glob("*.json")):
        try:
            order = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as err:
            raise OSError(f"an order cannot be read ({path.name})") from err
        names["used by an order"] |= strings_in(order)
    state = R.read_json(env.home / RELEASE_DIR / "state.json", {})
    if isinstance(state, dict):
        for key, field in (("target", "image"), ("applied", "image"), ("rolling", "previous"), ("rolling", "target")):
            part = state.get(key)
            value = part.get(field) if isinstance(part, dict) else None
            if isinstance(value, str):
                names["release record"].add(value)
    for name in ("docker-compose.yml", R.COMPOSE_APPLIED):
        compose = R.read_json(env.home / "run" / name, {})
        services = compose.get("services") if isinstance(compose, dict) else None
        for service in services.values() if isinstance(services, dict) else []:
            if isinstance(service, dict) and isinstance(service.get("image"), str):
                names["compose file"].add(service["image"])
    names["used by a container"] = set(env.used())
    return names


@contextlib.contextmanager
def release_not_running(env: Env):
    """Hold release.py's lock while images go, so no update can pull an image and not yet name it. Yields False if it is busy."""
    folder = env.home / RELEASE_DIR
    if not folder.is_dir():
        yield True
        return
    try:
        handle = open(folder / "lock", "a")
    except OSError:
        yield False
        return
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


def clean_images(env: Env, now: float, dry_run: bool) -> dict:
    """{removed: [{id, refs, bytes}], failed: [{id, why}], kept: n, skipped: why|None, upToBytes: n}. Never raises."""
    result = {"removed": [], "failed": [], "kept": 0, "skipped": None, "upToBytes": 0}
    try:
        images = env.images()
        protected = protected_names(env)
    except (DockerError, OSError) as err:
        result["skipped"] = f"images not touched: {err}"
        env.log(result["skipped"])
        return result
    plan = plan_images(images, protected, now)
    result["kept"] = len(plan["keep"])
    with release_not_running(env) as free:
        if not free and not dry_run:
            result["skipped"] = "images not touched: an update (release.py) is running"
            env.log(result["skipped"])
            return result
        for image in plan["remove"]:
            ok, why = (True, "dry run") if dry_run else env.remove(image["id"])
            if ok:
                result["removed"].append({"id": image["id"], "refs": image["refs"][:3], "bytes": image["size"]})
                result["upToBytes"] += image["size"]
            else:
                result["failed"].append({"id": image["id"], "why": why[:200]})
                env.log(f"{image['id'][:19]} not removed: {why}")
    return result


def run_pass(env: Env, dry_run: bool = False) -> dict:
    """One pass. Returns the state it writes (unless dry_run). Never raises for one item's trouble."""
    now = env.now()
    notes: list[str] = []
    try:
        total_before, free_before = env.disk()
    except OSError:
        total_before = free_before = 0
    try:
        copies = prune_preupdate(env.home / RELEASE_DIR / PRE_UPDATE_DIR, now, dry_run)
    except Exception as err:  # noqa: BLE001 — a bad folder never stops the images
        copies = {"removed": [], "bytes": 0, "left": 0, "capped": False}
        env.log(f"pre-update copies not pruned ({type(err).__name__})")
    images = clean_images(env, now, dry_run)
    try:
        total, free = env.disk()
    except OSError:
        total, free = total_before, free_before
    used = max(0, total - free)
    percent = round(100.0 * used / (used + free), 1) if used + free > 0 else 0.0
    state = disk_state(percent)
    freed = 0 if dry_run else max(0, free - free_before)
    logs = env.container_log_bytes()
    if images["removed"]:
        notes.append(("would remove " if dry_run else "removed ") + f"{len(images['removed'])} old image(s), up to "
                     f"{images['upToBytes'] // 2**20} MiB (shared layers make the real figure smaller)")
    if images["failed"]:
        notes.append(f"{len(images['failed'])} image(s) could not be removed")
    if images["skipped"]:
        notes.append(images["skipped"])
    if copies["removed"]:
        notes.append(("would remove " if dry_run else "removed ") + f"{len(copies['removed'])} pre-update copy(ies)")
    if copies["capped"]:
        notes.append("pre-update copies are over their cap, but only the newest of each world are left")
    if percent >= STOP_PERCENT:
        notes.append(f"still {percent} % used after cleaning: nothing else is deleted automatically; "
                     "look at /var/lib/docker/containers/*/*-json.log, nightly/ and the worlds' data")
    elif state != "ok":
        notes.append(f"{percent} % used")
    summary = {
        "at": R.utc(now),
        "usedPct": percent,
        "freeBytes": free,
        "totalBytes": total,
        "removedImages": len(images["removed"]),
        "freedBytes": freed,
        "removedUpToBytes": images["upToBytes"],
        "preupdateBytes": copies["left"],
        "preupdateRemoved": len(copies["removed"]),
        "containerLogBytes": logs,
        "journalBytes": env.journal_bytes(),
        "state": state,
        "note": "; ".join(notes)[:400] or "nothing to clean",
        "removed": images["removed"][:LIST_LIMIT],
        "failed": images["failed"][:LIST_LIMIT],
        "kept": images["kept"],
        "dryRun": dry_run,
    }
    if dry_run:
        summary["wouldRemoveCopies"] = copies["removed"][:LIST_LIMIT]
    else:
        env.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(env.dir, 0o755)  # the door (user fleetd) reads state.json
        R.write_json(env.dir / STATE_FILE, summary, mode=0o644)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Keep this machine's disk from filling: old engine images, pre-update copies.")
    ap.add_argument("--home", type=Path, default=R.HOME)
    ap.add_argument("--dry-run", action="store_true", help="print what would go, change nothing")
    args = ap.parse_args(argv)
    env = Env(args.home)
    if args.dry_run:
        print(json.dumps(run_pass(env, dry_run=True), indent=2))
        return 0
    env.dir.mkdir(parents=True, exist_ok=True)
    with open(env.dir / "lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another cleanup pass is running", file=sys.stderr)
            return 0
        summary = run_pass(env)
    print(json.dumps({k: v for k, v in summary.items() if k not in ("removed", "failed")}))
    return 1 if summary["state"] == "full" else 0


if __name__ == "__main__":
    sys.exit(main())
