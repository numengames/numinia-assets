#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: AGPL-3.0-only
"""The fleet machine's updater: its Numinia Engine worlds follow the latest published engine, by themselves.

Runs every five minutes from a systemd timer (fleet-release.timer), installed
by the bootstrap and never run from the clone. Each pass:

1. Reads the target: the public release asset `imagen.json` of
   numengames/numinia-engine, release `latest`, fetched anonymously over
   https from that one address. The machine holds no GitHub credential. The
   image it names must come from ghcr.io/numengames/numinia-engine and be
   pinned by sha256 digest. Nothing else is believed.
2. Optionally reads a signed FleetRelease (release/fleet-release.json). If an
   Oracle of this machine signed it and its seq is the highest seen, it
   replaces the target: a digest pins the fleet there, an empty image PAUSES
   the fleet (the kill switch). No record means: follow latest.
3. Plans (`plan_rollout`, pure): worlds of another engine (Hyperfy), pinned
   worlds and worlds with another change under way are skipped; stopped
   worlds change all at once (nothing restarts); of the running ones the
   canary goes first and only ONE world changes per pass.
4. Executes (`run_pass`): copy of the world before (kept 14 days), `pull`
   once, the order changes through reconcile.update_world, the reconciler
   remakes that container only (the Caddyfile does not change, so Caddy is not
   reloaded), then the health check: container running, no restarts, and
   /status reports the target commit. If it fails the world goes back to its
   previous image, the machine HALTS there and the digest is written down as
   bad. A different target (or a higher-seq record) lifts the halt.

State: <home>/release/state.json. Everything outside (network, docker, clock)
is passed in, so the tests run with a fake docker, registry and clock.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import re
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import reconcile as R
import walletsig

RELEASE_URL = "https://github.com/numengames/numinia-engine/releases/latest/download/imagen.json"
RELEASE_HOSTS = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")
ENGINE = R.NUMINIA_ENGINE
REPOSITORY = R.REGISTRY + ENGINE
DIGEST_IMAGE = re.compile(re.escape(REPOSITORY) + r"@sha256:[0-9a-f]{64}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{7,40}")
RELEASE_DIR = "release"
STATE_FILE = "state.json"
OVERRIDE_FILE = "fleet-release.json"
CANARY_FILE = Path("/etc/fleet/canary")  # optional: the id of the world that updates first
PRE_UPDATE_DIR = "preupdate"
KEEP_SECONDS = 14 * 24 * 3600  # a copy made before an update stays this long
MAX_BYTES = 64 * 1024
HEALTH_SECONDS = 90
HEALTH_STEP = 5
ENGINE_PORT = R.ENGINE_PORT
BY = "fleet-release"


# ---------------------------------------------------------------------------
# The target: parsed and checked, no effects
# ---------------------------------------------------------------------------


def parse_release(raw: object) -> dict | None:
    """imagen.json → {image, commit} with the image by digest, or None when it is not believable."""
    if isinstance(raw, (bytes, str)):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    image, commit, digest = raw.get("image"), raw.get("commit"), raw.get("digest")
    if not isinstance(image, str) or not isinstance(commit, str) or not R.matches(COMMIT, commit.lower()):
        return None
    if not DIGEST_IMAGE.fullmatch(image):
        # A tag in `image` plus a separate `digest`: the digest is what counts.
        repository = re.split(r"[:@]", image, maxsplit=1)[0]
        if repository != REPOSITORY or not isinstance(digest, str) or not DIGEST.fullmatch(digest):
            return None
        image = f"{REPOSITORY}@{digest}"
    elif isinstance(digest, str) and digest != image.split("@", 1)[1]:
        return None  # two different digests in one file: believe neither
    if R.engine_of(image) != ENGINE:
        return None
    return {"image": image, "commit": commit.lower()}


def fetch_release(opener=None) -> bytes:
    """GET the release asset anonymously. Only https, only the release hosts, never a header of credentials."""
    class Guard(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not re.fullmatch(r"https://(?:%s)/.*" % "|".join(map(re.escape, RELEASE_HOSTS)), newurl):
                raise OSError("redirect away from the release hosts")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = opener or urllib.request.build_opener(Guard)
    request = urllib.request.Request(RELEASE_URL, headers={"User-Agent": "numinia-fleet-release"})
    with opener.open(request, timeout=20) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise OSError("release file too big")
    return data


def read_override(path: Path, signers: frozenset[str]) -> dict | None:
    """A signed FleetRelease from an Oracle of this machine: {seq, image, commit}, image '' meaning pause. Else None."""
    raw = R.read_json(path, None)
    if not isinstance(raw, dict) or not isinstance(raw.get("message"), dict):
        return None
    message = raw["message"]
    try:
        signer = walletsig.signer_of("FleetRelease", message, raw.get("signature"))
    except Exception:  # a malformed record is no record
        return None
    if signer is None or signer.lower() not in signers:
        return None
    seq, image = message.get("seq"), message.get("image")
    if message.get("engine") != ENGINE or isinstance(seq, bool) or not isinstance(seq, int):
        return None
    if image != "" and not (isinstance(image, str) and DIGEST_IMAGE.fullmatch(image)):
        return None
    commit = message.get("commit")
    if image and not (isinstance(commit, str) and R.matches(COMMIT, commit.lower())):
        return None
    return {"seq": seq, "image": image, "commit": (commit or "").lower()}


def choose_target(latest: dict | None, override: dict | None, seen_seq: int) -> tuple[dict | None, str]:
    """(target, why). Target None = change nothing this pass. A record below the highest seq seen is ignored."""
    if override is not None and override["seq"] >= seen_seq:
        if override["image"] == "":
            return None, "paused by a signed record"
        return {"image": override["image"], "commit": override["commit"]}, "pinned by a signed record"
    if latest is None:
        return None, "no release known"
    return latest, "latest"


# ---------------------------------------------------------------------------
# The plan: pure
# ---------------------------------------------------------------------------


def plan_rollout(orders: list[dict], target: dict | None, state: dict, busy: set[str] | frozenset[str] = frozenset(),
                 canary: str | None = None) -> dict:
    """What this pass does. {stopped: [ids], next: id|None, current: [...], skipped: {id: why}, note: str|None}.

    Pure: orders in, decision out. `stopped` change all at once; `next` is the
    single running world that changes now, the canary first, then by id.
    """
    plan = {"stopped": [], "next": None, "current": [], "skipped": {}, "note": None}
    if target is None:
        plan["note"] = "no target"
        return plan
    halted = state.get("halted")
    if isinstance(halted, dict) and halted.get("image") == target["image"]:
        plan["note"] = "halted on this image"
        return plan
    if target["image"] in (state.get("bad") or []):
        plan["note"] = "bad digest"
        return plan
    running = []
    for order in sorted(orders, key=lambda o: o.get("id", "")):
        wid = order.get("id")
        if R.engine_of(order.get("image")) != ENGINE:
            plan["skipped"][wid] = "other-engine"
        elif order.get("image") == target["image"]:
            plan["current"].append(wid)
        elif order.get("pin"):
            plan["skipped"][wid] = "pinned"
        elif wid in busy:
            plan["skipped"][wid] = "busy"
        elif order.get("state") == "stopped":
            plan["stopped"].append(wid)
        else:
            running.append(wid)
    if canary in running:
        running.remove(canary)
        running.insert(0, canary)
    plan["next"] = running[0] if running else None
    return plan


# ---------------------------------------------------------------------------
# Effects, all passed in through Env
# ---------------------------------------------------------------------------


class Env:
    """Everything the pass does to the outside world. Tests replace each member."""

    def __init__(self, home: Path, signers_file: Path = R.SIGNERS_FILE):
        self.home = home
        self.signers_file = signers_file
        self.now = time.time
        self.sleep = time.sleep
        self.log = lambda line: print(line, file=sys.stderr)
        self.fetch = fetch_release  # () -> bytes
        self.pull = R.pull_image  # (image) -> None, raises on failure
        self.backup = self._backup  # (order) -> Path, raises on failure
        self.apply = self._apply  # () -> bool: the reconciler took the book
        self.health = self._health  # (order, target) -> (ok, reason)
        self.busy = self._busy  # () -> set of world ids with another change under way
        self.canary = self._canary  # () -> id | None

    @property
    def dir(self) -> Path:
        return self.home / RELEASE_DIR

    def _busy(self) -> set[str]:
        return set(R.read_updates(self.home)) | set(R.read_restarts(self.home))

    def _canary(self) -> str | None:
        try:
            name = CANARY_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return name if R.matches(R.SLUG, name) else None

    def _apply(self) -> bool:
        # Through systemd: the oneshot never runs twice at once, so this waits
        # for a run already going and never races the timer.
        return subprocess.run(["systemctl", "start", "fleet-reconcile.service"], stdin=subprocess.DEVNULL).returncode == 0

    def _backup(self, order: dict) -> Path:
        status = R.build_engine_copy(self.home, order, uuid.uuid4().hex, self.now())
        copies = self.home / "copies"
        dest = self.dir / PRE_UPDATE_DIR
        dest.mkdir(parents=True, exist_ok=True)
        zip_path = copies / f"{status['copy']}.zip"
        target = dest / f"{order['id']}-{int(self.now())}.zip"
        zip_path.replace(target)
        (copies / f"{status['copy']}.json").unlink(missing_ok=True)
        return target

    def _health(self, order: dict, target: dict) -> tuple[bool, str]:
        compose = self.home / "run" / R.COMPOSE_APPLIED
        wid = order["id"]
        try:
            cid = subprocess.run(["docker", "compose", "-f", str(compose), "ps", "-q", wid], capture_output=True,
                                 text=True, check=True, stdin=subprocess.DEVNULL).stdout.strip()
            if not cid:
                return False, "no container"
            info = json.loads(subprocess.run(["docker", "inspect", cid], capture_output=True, text=True, check=True,
                                             stdin=subprocess.DEVNULL).stdout)[0]
            if not info["State"].get("Running") or info.get("RestartCount", 0) > 0:
                return False, "container not running or restarting"
            ip = next(iter(info["NetworkSettings"]["Networks"].values()))["IPAddress"]
            with urllib.request.urlopen(f"http://{ip}:{ENGINE_PORT}/status", timeout=5) as r:
                body = json.loads(r.read(MAX_BYTES))
        except (OSError, ValueError, KeyError, IndexError, StopIteration, subprocess.CalledProcessError):
            return False, "/status did not answer"
        return commit_matches(body, target["commit"])


def commit_matches(body: object, commit: str) -> tuple[bool, str]:
    got = body.get("commit") if isinstance(body, dict) else None
    if not isinstance(got, str) or not got:
        return False, "/status has no commit"
    got = got.lower()
    if got.startswith(commit) or commit.startswith(got):
        return True, "ok"
    return False, f"/status runs {got[:12]}, not {commit[:12]}"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def read_state(env: Env) -> dict:
    raw = R.read_json(env.dir / STATE_FILE, {})
    state = raw if isinstance(raw, dict) else {}
    state.setdefault("bad", [])
    state.setdefault("seq", 0)
    return state


def write_state(env: Env, state: dict) -> None:
    R.write_json(env.dir / STATE_FILE, state)


def prune_backups(env: Env) -> None:
    folder = env.dir / PRE_UPDATE_DIR
    for path in folder.glob("*.zip") if folder.is_dir() else []:
        try:
            if env.now() - path.stat().st_mtime > KEEP_SECONDS:
                path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


def set_image(env: Env, wid: str, image: str, **extra) -> str | None:
    """Change one order's image via the reconciler's own update_world (engine-mismatch and shape checks apply)."""
    orders = env.home / "orders"
    error = R.update_world({"world": wid, "by": BY}, {"image": image}, orders, env.now(), pull=lambda _i: None)
    if error is None and extra:
        path = orders / f"{wid}.json"
        order = R.read_order(path)
        if order is not None:
            for key, value in extra.items():
                if value is None:
                    order.pop(key, None)
                else:
                    order[key] = value
            R.write_json(path, order)
    return error


def wait_healthy(env: Env, order: dict, target: dict) -> tuple[bool, str]:
    waited, why = 0, "no check yet"
    while True:
        ok, why = env.health(order, target)
        if ok:
            return True, why
        if waited >= HEALTH_SECONDS:
            return False, why
        env.sleep(HEALTH_STEP)
        waited += HEALTH_STEP


def roll_back(env: Env, state: dict, wid: str, previous: str, target: dict, why: str) -> None:
    """The world goes back to its previous image, the digest is marked bad, the machine halts."""
    env.log(f"{wid}: {why}; going back to {previous}")
    set_image(env, wid, previous, updateError=why, previousImage=None)
    applied = env.apply()
    state["bad"] = sorted(set(state["bad"]) | {target["image"]})
    state["halted"] = {"image": target["image"], "world": wid, "reason": why, "at": R.utc(env.now()),
                       "rolledBack": bool(applied)}
    state["rolling"] = None
    write_state(env, state)


def update_running(env: Env, state: dict, wid: str, target: dict) -> str:
    order = R.read_order(env.home / "orders" / f"{wid}.json")
    if order is None:
        return "gone"
    try:
        env.backup(order)
    except Exception:  # no copy, no change
        env.log(f"{wid}: no copy could be made; the world is not touched")
        return "no-backup"
    previous = order["image"]
    state["rolling"] = {"world": wid, "previous": previous, "target": target["image"], "commit": target["commit"]}
    write_state(env, state)
    error = set_image(env, wid, target["image"], previousImage=previous, updateError=None)
    if error:
        state["rolling"] = None
        write_state(env, state)
        env.log(f"{wid}: {error}")
        return "refused"
    if not env.apply():
        roll_back(env, state, wid, previous, target, "docker did not take the book")
        return "rolled-back"
    ok, why = wait_healthy(env, {**order, "image": target["image"]}, target)
    if not ok:
        roll_back(env, state, wid, previous, target, why)
        return "rolled-back"
    state["rolling"] = None
    write_state(env, state)
    env.log(f"{wid}: now on {target['commit'][:12]}")
    return "updated"


def finish_interrupted(env: Env, state: dict, target: dict | None) -> bool:
    """A pass that died between the order and the health check: judge it now. True when it halted the machine."""
    rolling = state.get("rolling")
    if not isinstance(rolling, dict):
        return False
    wid = rolling.get("world")
    if not isinstance(wid, str) or not isinstance(rolling.get("previous"), str):
        state["rolling"] = None
        return False
    order = R.read_order(env.home / "orders" / f"{wid}.json") if isinstance(wid, str) else None
    chosen = {"image": rolling.get("target"), "commit": rolling.get("commit", "")}
    if order is None or order.get("image") != chosen["image"]:
        state["rolling"] = None
        write_state(env, state)
        return False
    ok, why = wait_healthy(env, order, chosen)
    if ok:
        state["rolling"] = None
        write_state(env, state)
        return False
    roll_back(env, state, wid, rolling["previous"], chosen, why)
    return True


def run_pass(env: Env) -> dict:
    """One pass. Returns what happened: {action, world(s), note}. Never raises on a bad network."""
    env.dir.mkdir(parents=True, exist_ok=True)
    state = read_state(env)
    signers = R.read_signers(env.signers_file)
    latest = None
    try:
        latest = parse_release(env.fetch())
    except Exception:
        env.log("the release file could not be fetched; nothing changes")
    override = read_override(env.dir / OVERRIDE_FILE, signers)
    if override is not None and override["seq"] > state["seq"]:
        state["seq"] = override["seq"]
        state["halted"] = None  # a newer signed record lifts a halt
    target, why = choose_target(latest, override, state["seq"])
    if finish_interrupted(env, state, target):
        return {"action": "halted", "note": "interrupted update judged bad"}
    halted = state.get("halted")
    if isinstance(halted, dict) and target is not None and halted.get("image") != target["image"]:
        state["halted"] = None  # a different build lifts the halt
    state["target"] = target and {**target, "why": why}
    orders = [o for o in R.read_orders(env.home / "orders") if R.matches(R.SLUG, o.get("id"))]
    plan = plan_rollout(orders, target, state, env.busy(), env.canary())
    result = {"action": "none", "stopped": [], "world": None, "note": plan["note"] or why, "plan": plan}
    if target is not None and (plan["stopped"] or plan["next"]):
        try:
            env.pull(target["image"])
        except (subprocess.CalledProcessError, OSError):
            env.log("the image could not be fetched here; no order is touched")
            write_state(env, state)
            result["note"] = "pull failed"
            return result
        for wid in plan["stopped"]:
            before = R.read_order(env.home / "orders" / f"{wid}.json") or {}
            if set_image(env, wid, target["image"], previousImage=before.get("image")) is None:
                result["stopped"].append(wid)
        if result["stopped"]:
            env.apply()
        if plan["next"]:
            result["world"] = plan["next"]
            result["action"] = update_running(env, state, plan["next"], target)
        elif result["stopped"]:
            result["action"] = "updated-stopped"
    if state.get("halted"):
        result["action"] = result["action"] if result["action"] == "rolled-back" else "halted"
    elif target is not None and not plan["next"] and not plan["stopped"] and not state.get("rolling"):
        pending = [w for w, r in plan["skipped"].items() if r == "busy"]
        if not pending:
            state["applied"] = {"image": target["image"], "commit": target["commit"], "state": "converged",
                                "at": R.utc(env.now())}
    write_state(env, state)
    prune_backups(env)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Move this machine's Numinia Engine worlds to the latest published engine.")
    ap.add_argument("--home", type=Path, default=R.HOME)
    ap.add_argument("--signers-file", type=Path, default=R.SIGNERS_FILE)
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    args = ap.parse_args(argv)
    env = Env(args.home, args.signers_file)
    env.dir.mkdir(parents=True, exist_ok=True)
    with open(env.dir / "lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another pass is running", file=sys.stderr)
            return 0
        if args.dry_run:
            state = read_state(env)
            target, why = choose_target(parse_release(env.fetch()), read_override(env.dir / OVERRIDE_FILE,
                                        R.read_signers(args.signers_file)), state["seq"])
            orders = R.read_orders(args.home / "orders")
            print(json.dumps({"target": target, "why": why, "plan": plan_rollout(orders, target, state, env.busy(), env.canary())}, indent=2))
            return 0
        result = run_pass(env)
    print(json.dumps({k: v for k, v in result.items() if k != "plan"}))
    return 1 if result["action"] in ("rolled-back", "halted") else 0


if __name__ == "__main__":
    sys.exit(main())
