#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: AGPL-3.0-only
"""The fleet machine's door: Oracles' signed requests in, public facts and copies out.

The worlds room on numinia.com talks to each machine through this door, at
the machine's own address (https://<alias>.numinia.com/fleet/v1/…), which
Caddy forwards to this process's socket. Nothing else reaches it.

What it does, and nothing more:

- POST /fleet/v1/actions — a request an Oracle signed with their wallet
  (EIP-712, see walletsig.py): create, stop, start, delete or download a
  world, bring one back from its zip, move one to another build of its
  engine, show a world's admin code or change it, or show the machine's
  legacy code. It checks the shape, the machine, the time window, the
  signature, that the signer is one of the Oracles listed on this machine,
  and that the request number is new; then it drops the request into
  /srv/fleet/inbox/ and answers 202. The reconciler
  (root) checks it again and does the work. A world's admin code is the one
  secret the door carries: it reads it from /etc/fleet/codes/<id> to show
  it, and makes a new one for a world just created, brought back or whose
  code an Oracle changes — written into the inbox entry for the
  reconciler, which puts it in the world's key file. The door never writes
  a key file and never logs a code.
- An import (ImportWorld) names its zip's exact size and SHA-256. The door
  first keeps an upload slot for it (/srv/fleet/uploads/<n>.json, an hour),
  if the disk has room for the zip and what it unpacks to; then
  PUT /fleet/v1/uploads/<n> streams the zip in, and only the zip that is
  exactly that size and that SHA-256 turns the slot into a request in the
  inbox. Another zip is refused and the slot kept, so the browser can send
  it again without a new signature.
- GET /fleet/v1/machine, /worlds, /log — public facts: the machine, the
  commit of the recipe it installed, and whether its reconciler keeps up
  (when it last ran, how it ended, how many requests wait and since when),
  whether it takes zips and up to what size, which engines it runs and
  that it takes updates, its worlds and their phase (an import shows its
  upload's progress), the last things done here. No wallet in full, no
  key, no code.
- GET /fleet/v1/requests/<n> — how a request went.
- GET /fleet/v1/copies/<c> and /copies/<c>/file — a copy's state and the
  zip. The copy's number (128 random bits, given only to the Oracle who
  signed the download) is the key to it, like a pre-signed link; it lasts
  a day.

It runs as its own user (fleetd), with no Docker, no root, no network of its
own (a Unix socket only) and write access to the inbox and the upload folder
alone. Broken into, it still cannot sign for an Oracle: the reconciler
checks every signature again, and a zip's fingerprint, and reads the zip
from a copy of its own. Python's standard library only, besides
python3-ecdsa for the signature.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import shutil
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import reconcile as R
import walletsig as W

STATE = Path("/var/lib/fleetd")  # the request numbers already used
SOCKET = R.HOME / "sock" / "fleetd.sock"
ORIGINS_FILE = Path("/etc/fleet/console-origins")  # extra consoles (a staging room), one origin per line
# The commit of numinia-assets the machine's programs were installed from,
# written by bootstrap.sh: the room compares it with the one it hands out.
RECIPE_FILE = Path("/etc/fleet/recipe")
RECIPE = re.compile(r"[0-9a-f]{40}")
CONSOLE_ORIGINS = frozenset({"https://numinia.com", "https://www.numinia.com"})
WINDOW = 15 * 60  # a signed request is good for at most fifteen minutes
MAX_BODY = 16 * 1024
LOG_LINES = 50
PHASES = {"DeleteWorld": "deleting", "StopWorld": "stopping", "StartWorld": "starting", "UpdateWorld": "updating"}
BIRTHS = ("CreateWorld", "ImportWorld")  # the requests that make a world that is not here yet
# The requests that change a world that is here: one at a time (busy).
CHANGES = ("RotateWorldCode", "UpdateWorld", "StopWorld", "StartWorld")
LENGTH = re.compile(r"[0-9]{1,20}")  # a Content-Length, digits only
PROGRESS_SECONDS = 1.0  # an upload's slot says how far it got at most this often


class Nonces:
    """Request numbers already used, kept until they expire, so a signed request counts once."""

    def __init__(self, path: Path) -> None:
        self.path = path
        raw = R.read_json(path, {})
        self.seen = {
            k: v for k, v in (raw.items() if isinstance(raw, dict) else [])
            if R.matches(R.NONCE, k) and isinstance(v, int)
        }

    def claim(self, nonce: str, expires: int, now: float) -> bool:
        self.seen = {k: v for k, v in self.seen.items() if v >= now}
        if nonce in self.seen:
            return False
        self.seen[nonce] = expires
        R.write_json(self.path, self.seen, mode=0o600)
        return True


class Limiter:
    """At most `per_client` requests per address and `overall` in all within `window` seconds."""

    def __init__(self, per_client: int = 20, overall: int = 200, window: float = 300) -> None:
        self.per_client, self.overall, self.window = per_client, overall, window
        self.calls: dict[str, collections.deque[float]] = collections.defaultdict(collections.deque)
        self.all: collections.deque[float] = collections.deque()

    def allow(self, client: str, now: float) -> bool:
        for calls in (self.calls[client], self.all):
            while calls and now - calls[0] > self.window:
                calls.popleft()
        if len(self.calls[client]) >= self.per_client or len(self.all) >= self.overall:
            return False
        self.calls[client].append(now)
        self.all.append(now)
        return True


def uploaded(slot: dict) -> int:
    """How many bytes of its zip a slot says have arrived: a whole number from 0 to the zip's size."""
    got = slot.get("bytes")
    if isinstance(got, bool) or not isinstance(got, int):
        return 0
    return max(0, min(got, slot["message"]["zipBytes"]))


class Door:
    """Everything the door knows and does, without HTTP — what the tests drive."""

    def __init__(
        self,
        home: Path = R.HOME,
        alias: str | None = None,
        signers_file: Path = R.SIGNERS_FILE,
        admin_code_file: Path = R.ADMIN_CODE_FILE,
        codes_dir: Path = R.CODES_DIR,
        recipe_file: Path = RECIPE_FILE,
        control_host_file: Path = R.CONTROL_HOST_FILE,
        domains_file: Path = R.DOMAINS_FILE,
        origins_file: Path = ORIGINS_FILE,
        state: Path = STATE,
        clock=time.time,
        disk_usage=shutil.disk_usage,
    ) -> None:
        self.home = home
        self.alias = alias or R.ALIAS_FILE.read_text(encoding="utf-8").strip()
        self.signers_file = signers_file
        self.admin_code_file = admin_code_file
        self.codes_dir = codes_dir
        self.recipe_file = recipe_file
        self.control_host_file = control_host_file
        self.domains_file = domains_file
        self.origins_file = origins_file
        self.clock = clock
        self.disk_usage = disk_usage
        self.lock = threading.Lock()
        self.nonces = Nonces(state / "nonces.json")
        self.limiter = Limiter()
        self.streaming: set[str] = set()  # the slots a PUT is writing into right now

    # --- what the door reads -------------------------------------------------

    @property
    def orders(self) -> Path:
        return self.home / "orders"

    @property
    def uploads(self) -> Path:
        return self.home / "uploads"

    def inbox(self) -> list[dict]:
        """The requests the reconciler has not taken yet."""
        waiting = []
        for path in sorted((self.home / "inbox").glob("*.json")):
            raw = R.read_json(path, None)
            if isinstance(raw, dict) and isinstance(raw.get("message"), dict):
                waiting.append(raw)
        return waiting

    def read_slot(self, nonce: str) -> dict | None:
        """An upload slot as the door wrote it — an ImportWorld waiting for its zip — whether or not it expired."""
        if not R.matches(R.NONCE, nonce):
            return None
        raw = R.read_json(self.uploads / f"{nonce}.json", None)
        if not isinstance(raw, dict) or raw.get("type") != "ImportWorld" or R.epoch(raw.get("expiresAt")) is None:
            return None
        message = raw.get("message")
        if not isinstance(message, dict) or message.get("nonce") != nonce:
            return None
        return raw if R.request_problem("ImportWorld", message) is None else None

    def expired(self, slot: dict, now: float) -> bool:
        ends = R.epoch(slot.get("expiresAt"))
        return ends is None or now > ends

    def slots(self) -> dict[str, dict]:
        """The open upload slots, by request number."""
        now = self.clock()
        found = {}
        for path in sorted(self.uploads.glob("*.json")) if self.uploads.is_dir() else []:
            slot = self.read_slot(path.name[: -len(".json")])
            if slot is not None and not self.expired(slot, now):
                found[path.name[: -len(".json")]] = slot
        return found

    def importing(self) -> dict[str, dict]:
        """The world the reconciler is unpacking right now (run/import.json): id → the message it signed."""
        raw = R.read_json(self.home / "run" / R.IMPORTING, {})
        found = {}
        for wid, job in (raw.items() if isinstance(raw, dict) else []):
            message = job.get("message") if isinstance(job, dict) else None
            if R.matches(R.SLUG, wid) and R.request_problem("ImportWorld", message) is None:
                found[wid] = message
        return found

    def origin_allowed(self, origin: str) -> bool:
        extra = set()
        if self.origins_file.is_file():
            extra = {line.strip() for line in self.origins_file.read_text(encoding="utf-8").splitlines()}
        return origin in CONSOLE_ORIGINS or origin in extra

    def machine(self) -> dict:
        """The machine's public facts: who it is, its recipe, whether its reconciler is keeping up, what zips it takes.

        Also which engines it runs and that it moves a world to another build
        of its engine (UpdateWorld): the room offers each only to a door that says so.
        """
        return {
            "machine": self.alias,
            "door": "fleetd/1",
            "oracles": len(R.read_signers(self.signers_file)),
            "zones": list(R.HOUSE_ZONES),
            "recipe": self.recipe(),
            "lastRun": self.last_run(),
            "waiting": self.waiting(),
            # The room offers to bring a world back from a zip only to a door that says this.
            "imports": {"zipMaxBytes": R.ZIP_MAX},
            "engines": list(R.ENGINES),
            "updates": True,
            "at": R.utc(self.clock()),
        }

    def recipe(self) -> str | None:
        """The commit the machine's programs were installed from, or None when it is not known.

        bootstrap.sh writes it; a machine set up before that, or a file that
        holds anything but a full commit, says nothing rather than something false.
        """
        try:
            ref = self.recipe_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        return ref if R.matches(RECIPE, ref) else None

    def last_run(self) -> dict | None:
        """When the reconciler last ran and how it ended: a time that stops moving means it stopped."""
        raw = R.read_json(self.home / "run" / R.LAST_RUN, None)
        if not isinstance(raw, dict) or not isinstance(raw.get("at"), str) or not isinstance(raw.get("ok"), bool):
            return None
        rule = raw.get("rule") if isinstance(raw.get("rule"), str) else None
        return {"at": raw["at"][:32], "ok": raw["ok"], "rule": rule[:200] if rule else None}

    def waiting(self) -> dict:
        """How many requests the reconciler has not taken yet, and since when the oldest waits."""
        inbox = self.inbox()
        times = sorted(r["receivedAt"] for r in inbox if isinstance(r.get("receivedAt"), str))
        return {"requests": len(inbox), "since": times[0][:32] if times else None}

    def purging(self) -> dict[str, str | None]:
        """Worlds whose delete was taken but whose folder and keys are not gone yet: id → address."""
        purge = R.read_json(self.home / "run" / "purge.json", {})
        found = {}
        for wid, job in (purge.items() if isinstance(purge, dict) else []):
            if R.matches(R.SLUG, wid) and isinstance(job, dict):
                found[wid] = job.get("domain") if R.matches(R.HOST, job.get("domain")) else None
        return found

    def worlds(self) -> dict:
        """This machine's worlds and what is happening to each: no wallet, no key.

        A world not in the book yet shows how it is coming: `creating`, or for
        an import `uploading` (its zip arriving: `bytes` of `of`) and then
        `importing` (waiting in the inbox, or being unpacked).
        """
        allowed = R.read_allowed_domains(self.domains_file)
        pending = {r["message"].get("world"): r for r in self.inbox()}
        updating = R.read_updates(self.home)
        listed = []
        for order in R.select_orders(R.read_orders(self.orders), self.alias, allowed):
            kind = pending.get(order["id"], {}).get("type")
            # A world moved to a new build says so until Docker has started it.
            settled = "updating" if order["id"] in updating else order["state"]
            listed.append({**order, "phase": PHASES.get(kind, settled)})
        known = {world["id"] for world in listed}

        def coming(message: dict, phase: str, **more: object) -> None:
            if message["world"] in known:
                return
            known.add(message["world"])
            listed.append({
                "id": message["world"],
                "state": "running" if phase == "creating" else "stopped",  # an import is born stopped
                "domain": message["domain"],
                "image": message["image"],
                "limits": {"memory": message["memory"], "cpus": message["cpus"],
                           "maxUploadMb": message["maxUploadMb"]},
                "phase": phase,
                **more,
            })

        for raw in pending.values():
            if R.request_problem(raw.get("type"), raw["message"]) is not None:
                continue
            if raw["type"] == "CreateWorld":
                coming(raw["message"], "creating")
            elif raw["type"] == "ImportWorld":
                coming(raw["message"], "importing")
        for message in self.importing().values():
            coming(message, "importing")
        for slot in self.slots().values():
            coming(slot["message"], "uploading", bytes=uploaded(slot), of=slot["message"]["zipBytes"])
        for wid, domain in self.purging().items():
            if wid not in known:
                listed.append({"id": wid, "domain": domain, "phase": "deleting"})
        return {"machine": self.alias, "worlds": sorted(listed, key=lambda w: w["id"]), "at": R.utc(self.clock())}

    def log(self) -> dict:
        path = self.home / "log.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()[-LOG_LINES:] if path.is_file() else []
        entries = []
        for line in reversed(lines):
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return {"machine": self.alias, "entries": entries}

    def request(self, nonce: str) -> dict | None:
        if not R.matches(R.NONCE, nonce):
            return None
        if any(r["message"].get("nonce") == nonce for r in self.inbox()):
            return {"request": nonce, "state": "pending"}
        slot = self.read_slot(nonce)
        if slot is not None and not self.expired(slot, self.clock()):
            # An import whose zip is still on its way.
            return {"request": nonce, "state": "uploading", "bytes": uploaded(slot), "of": slot["message"]["zipBytes"]}
        result = R.read_json(self.home / "requests" / f"{nonce}.json", None)
        return result if isinstance(result, dict) else None

    def copy(self, copy_id: str) -> dict | None:
        if not R.matches(R.COPY_ID, copy_id):
            return None
        status = R.read_json(self.home / "copies" / f"{copy_id}.json", None)
        if isinstance(status, dict):
            return {k: status[k] for k in ("copy", "world", "state", "bytes", "sha256", "filename", "expiresAt")
                    if k in status}
        if any(r.get("copy") == copy_id for r in self.inbox()):
            return {"copy": copy_id, "state": "preparing"}
        return None

    def copy_file(self, copy_id: str) -> tuple[Path, str] | None:
        status = self.copy(copy_id)
        path = self.home / "copies" / f"{copy_id}.zip"
        if status is None or status.get("state") != "ready" or not path.is_file():
            return None
        return path, status["filename"]

    # --- the one thing the door does: take a signed request -------------------

    def clash(self, kind: str, message: dict) -> str | None:
        """Why the machine's book cannot take this request now; None when it can.

        A world on its way counts as here: a create or an import waiting in
        the inbox, an import's open upload slot, an import being unpacked.
        Each holds its name and its address until it lands or goes.
        """
        if kind == "ShowAdminCode":
            return None
        wid = message["world"]
        current = R.read_order(self.orders / f"{wid}.json")
        waiting = [r for r in self.inbox() if r["message"].get("world") == wid]
        slots = list(self.slots().values())
        importing = self.importing()
        coming = wid in importing or any(s["message"]["world"] == wid for s in slots)
        if kind in ("ShowWorldCode", "RotateWorldCode"):
            # A world's own code: shown or changed only for a world that is
            # here, settled, and not already changing its code — else the
            # Oracle would be shown a code about to stop working. A change of
            # code also waits for a new build to start (one change at a time).
            if wid in self.purging() or any(r.get("type") == "DeleteWorld" for r in waiting):
                return "still-deleting"
            changing = (*BIRTHS, "RotateWorldCode") + (("UpdateWorld",) if kind == "RotateWorldCode" else ())
            if coming or any(r.get("type") in changing for r in waiting):
                return "busy"
            if kind == "RotateWorldCode" and wid in R.read_updates(self.home):
                return "busy"
            return "missing" if current is None else None
        if kind == "UpdateWorld":
            # Another build of the world's own engine, for a world that is
            # here and settled: one change at a time.
            if wid in self.purging() or any(r.get("type") == "DeleteWorld" for r in waiting):
                return "still-deleting"
            if coming or any(r.get("type") in (*BIRTHS, *CHANGES) for r in waiting):
                return "busy"
            if wid in R.read_updates(self.home):
                return "busy"
            if current is None:
                return "missing"
            engine, now_runs = R.engine_of(message["image"]), R.engine_of(current.get("image"))
            if engine is None or now_runs is None:
                return "unknown-engine"
            return "engine-mismatch" if engine != now_runs else None
        if kind in BIRTHS:
            if current is not None or coming or any(r.get("type") in BIRTHS for r in waiting):
                return "exists"
            domain = message["domain"]
            # A world deleted a moment ago keeps its folder and keys until its
            # container is gone; one born now under its name or address would
            # inherit them.
            deleting = self.purging()
            if wid in deleting or domain in deleting.values():
                return "still-deleting"
            taken = {o.get("domain") for o in R.read_orders(self.orders)}
            taken |= {r["message"].get("domain") for r in self.inbox() if r.get("type") in BIRTHS}
            taken |= {s["message"]["domain"] for s in slots} | {m["domain"] for m in importing.values()}
            if domain in taken:
                return "address-taken"
            # Only an engine this machine runs: numinia-hyperfy2 or numinia-engine.
            if R.engine_of(message["image"]) is None:
                return "unknown-engine"
            draft = {
                "id": wid, "state": "running" if kind == "CreateWorld" else "stopped", "domain": domain,
                "image": message["image"],
                "limits": {"memory": message["memory"], "cpus": message["cpus"],
                           "maxUploadMb": message["maxUploadMb"]},
            }
            control = R.read_control_host(self.control_host_file)
            if R.refusal(draft, R.read_allowed_domains(self.domains_file), control) is not None:
                return "refused"
            return None
        if current is None:
            return "missing"
        if kind == "DeleteWorld" and current.get("domain") != message["domain"]:
            return "address-mismatch"
        return None

    def room_for(self, zip_bytes: int) -> bool:
        """Whether the disk keeps its reserve after this zip, what it unpacks to, and every import already let in.

        An open slot holds room for its whole zip and its unpacking; an
        import whose zip has arrived (in the inbox, or being unpacked) for
        its unpacking still to come.
        """
        try:
            free = self.disk_usage(self.home).free
        except OSError:
            return False
        promised = sum(R.reservation(s["message"]["zipBytes"]) for s in self.slots().values())
        promised += sum(R.unpacking(r["message"]["zipBytes"]) for r in self.inbox()
                        if r.get("type") == "ImportWorld" and R.request_problem("ImportWorld", r["message"]) is None)
        promised += sum(R.unpacking(m["zipBytes"]) for m in self.importing().values())
        return free >= R.reservation(zip_bytes) + R.RESERVE + promised

    def accept(self, body: bytes, client: str = "-") -> tuple[int, dict]:
        now = self.clock()
        if not self.limiter.allow(client, now):
            return 429, {"error": "slow-down"}
        if len(body) > MAX_BODY:
            return 413, {"error": "too-large"}
        try:
            request = json.loads(body)
        except ValueError:
            return 400, {"error": "not-json"}
        if not isinstance(request, dict) or set(request) != {"type", "message", "signature"}:
            return 400, {"error": "shape"}
        kind, message = request["type"], request["message"]
        problem = R.request_problem(kind, message)
        if problem:
            return 400, {"error": "invalid", "rule": problem}
        if message["machine"] != self.alias:
            return 400, {"error": "other-machine"}
        if not now < message["expires"] <= now + WINDOW:
            return 400, {"error": "expired"}
        signer = W.signer_of(kind, message, request["signature"])
        if signer is None:
            return 401, {"error": "bad-signature"}
        if signer not in R.read_signers(self.signers_file):
            return 403, {"error": "not-an-oracle-here"}
        with self.lock:
            nonce = message["nonce"]
            used = (self.home / "requests" / f"{nonce}.json").exists() or (
                self.uploads / f"{nonce}.json").exists() or any(
                r["message"].get("nonce") == nonce for r in self.inbox()
            )
            if used or not self.nonces.claim(nonce, message["expires"], now):
                return 409, {"error": "replayed"}
            clash = self.clash(kind, message)
            if clash:
                return 409, {"error": clash}
            if kind == "ImportWorld":
                if message["zipBytes"] > R.ZIP_MAX:
                    return 413, {"error": "zip-too-big"}
                if not self.room_for(message["zipBytes"]):
                    return 507, {"error": "no-room"}
            reply: dict = {"request": nonce}
            dropped = {"type": kind, "message": message, "signature": request["signature"],
                       "signer": signer, "receivedAt": R.utc(now)}
            if kind == "DownloadWorld":
                reply["copy"] = dropped["copy"] = os.urandom(16).hex()
            if kind in ("ShowAdminCode", "ShowWorldCode"):
                # The machine's legacy code, or this one world's own.
                kept = self.admin_code_file if kind == "ShowAdminCode" else self.codes_dir / message["world"]
                code = R.read_admin_code(kept)
                if code is None:
                    return 503, {"error": "no-admin-code"}
                reply["adminCode"] = code
            elif kind in (*BIRTHS, "RotateWorldCode"):
                # A new code of the world's own, made here and shown once to the
                # Oracle who signed; the inbox entry carries it to the
                # reconciler, which writes it into the world's key file.
                reply["adminCode"] = dropped["adminCode"] = R.make_code()
            if kind == "ImportWorld":
                # Not in the inbox yet: a slot that waits an hour for the zip
                # it names, which only then becomes the request.
                slot = {**dropped, "expiresAt": R.utc(now + R.SLOT_SECONDS), "bytes": 0}
                R.write_json(self.uploads / f"{nonce}.json", slot, mode=0o640)
                reply["upload"] = f"uploads/{nonce}"
            else:
                name = f"{int(now * 1000):013d}-{nonce}.json"
                R.write_json(self.home / "inbox" / name, dropped, mode=0o640)
        target = f" {message['world']}" if "world" in message else ""
        print(f"accepted {kind}{target} from {W.short(signer)}", file=sys.stderr)
        return 202, reply

    # --- an import's zip, streamed into its slot -------------------------------

    def upload(self, nonce: str, length: str | None, body, client: str = "-") -> tuple[int, dict]:
        """Take a slot's zip from `body`: kept only when it is exactly the size and the SHA-256 signed for it.

        Counted once by the rate limiter, never per piece. No slot: missing;
        a slot past its hour: slot-expired (and it goes); a length that is
        not the signed size: wrong-size, before a byte is read; another
        upload into the same slot: busy.
        """
        now = self.clock()
        if not self.limiter.allow(client, now):
            return 429, {"error": "slow-down"}
        slot = self.read_slot(nonce)
        if slot is None:
            return 404, {"error": "missing"}
        if self.expired(slot, now):
            self.drop_slot(nonce)
            return 410, {"error": "slot-expired"}
        if not R.matches(LENGTH, length):
            return 411, {"error": "length"}
        size = slot["message"]["zipBytes"]
        if int(length) != size:
            return 400, {"error": "wrong-size"}
        with self.lock:
            if nonce in self.streaming:
                return 409, {"error": "busy"}
            self.streaming.add(nonce)
        try:
            return self.stream(nonce, slot, body)
        finally:
            with self.lock:
                self.streaming.discard(nonce)

    def drop_slot(self, nonce: str) -> None:
        """A slot past its hour goes, with whatever part of its zip had arrived."""
        for name in (f"{nonce}.json", f".{nonce}.zip.part"):
            (self.uploads / name).unlink(missing_ok=True)

    def progress(self, nonce: str, received: int) -> bool:
        """Write how far a slot's upload got; False when the slot went or its hour is over."""
        slot = self.read_slot(nonce)
        if slot is None or self.expired(slot, self.clock()):
            return False
        R.write_json(self.uploads / f"{nonce}.json", {**slot, "bytes": received}, mode=0o640)
        return True

    def stream(self, nonce: str, slot: dict, body) -> tuple[int, dict]:
        """Write the zip into the slot's part file in pieces, hashing as it goes, and judge it at the end."""
        size, signed = slot["message"]["zipBytes"], slot["message"]["zipSha256"]
        part = self.uploads / f".{nonce}.zip.part"
        digest, received, said = hashlib.sha256(), 0, self.clock()
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o640)
        with os.fdopen(fd, "wb") as out:
            while received < size:
                try:
                    chunk = body.read(min(R.CHUNK, size - received))
                except (OSError, ValueError):  # a timeout, or a connection that went
                    break
                if not chunk:
                    break
                try:
                    out.write(chunk)
                except OSError:  # the disk is full after all
                    self.give_up(nonce)
                    return 507, {"error": "no-room"}
                digest.update(chunk)
                received += len(chunk)
                if self.clock() - said >= PROGRESS_SECONDS:
                    said = self.clock()
                    if not self.progress(nonce, received):
                        self.drop_slot(nonce)
                        return 410, {"error": "slot-expired"}
        world = slot["message"]["world"]
        if received < size:
            self.give_up(nonce)
            print(f"the zip for {world} stopped halfway: cut", file=sys.stderr)
            return 400, {"error": "cut"}
        if digest.hexdigest() != signed:
            # Not the zip the Oracle signed. The slot stays: the browser may
            # send the right one again, without a new signature, while it lives.
            self.give_up(nonce)
            print(f"the zip for {world} is not the one signed: fingerprint", file=sys.stderr)
            return 400, {"error": "fingerprint"}
        with self.lock:
            current = self.read_slot(nonce)
            if current is None or self.expired(current, self.clock()):
                self.drop_slot(nonce)
                return 410, {"error": "slot-expired"}
            # The zip is the one signed: the slot becomes the request the
            # reconciler takes. The inbox entry first, then the slot goes,
            # so the world is never shown as neither.
            os.replace(part, self.uploads / f"{nonce}.zip")
            entry = {**current, "bytes": size, "zip": f"{nonce}.zip"}
            R.write_json(self.home / "inbox" / f"{int(self.clock() * 1000):013d}-{nonce}.json", entry, mode=0o640)
            (self.uploads / f"{nonce}.json").unlink(missing_ok=True)
        print(f"took the zip for {world}: it waits for the reconciler", file=sys.stderr)
        return 202, {"request": nonce}

    def give_up(self, nonce: str) -> None:
        """An upload that did not end in the signed zip: its part goes, the slot stays and says nothing arrived."""
        (self.uploads / f".{nonce}.zip.part").unlink(missing_ok=True)
        self.progress(nonce, 0)


# ---------------------------------------------------------------------------
# HTTP, over the socket Caddy forwards to
# ---------------------------------------------------------------------------

PREFIX = "/fleet/v1/"


class Handler(BaseHTTPRequestHandler):
    door: Door
    server_version = "fleetd/1"
    sys_version = ""
    timeout = 30

    def client(self) -> str:
        # Caddy sets X-Forwarded-For to the address it saw (it drops what a client sends).
        return self.headers.get("X-Forwarded-For", "").split(",")[0].strip()[:64] or "-"

    def address_string(self) -> str:
        return self.client()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — the base class names it so
        # Quiet: paths carry copy numbers, which are keys. The door logs what it accepts.
        return

    def cors(self, write: bool) -> None:
        origin = self.headers.get("Origin")
        if not write:
            self.send_header("Access-Control-Allow-Origin", "*")
        elif origin is not None and self.door.origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def reply(self, status: int, payload: dict, write: bool = False) -> None:
        body = (json.dumps(payload) + "\n").encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.cors(write)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return  # whoever asked has gone (an upload cut on their side): nobody to answer

    def route(self) -> list[str]:
        path = self.path.split("?", 1)[0]
        return path[len(PREFIX):].split("/") if path.startswith(PREFIX) else []

    def writes(self) -> bool:
        """Whether this path takes something from a browser: a signed request, or an import's zip."""
        parts = self.route()
        return parts == ["actions"] or (len(parts) == 2 and parts[0] == "uploads")

    def do_OPTIONS(self) -> None:  # noqa: N802 — the base class's naming
        # The preflight before a POST or a PUT: only the consoles may send requests and zips.
        self.send_response(204)
        self.cors(write=self.writes())
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parts = self.route()
        door = self.door
        found: dict | None = None
        if parts == ["machine"]:
            found = door.machine()
        elif parts == ["worlds"]:
            found = door.worlds()
        elif parts == ["log"]:
            found = door.log()
        elif len(parts) == 2 and parts[0] == "requests":
            found = door.request(parts[1])
        elif len(parts) == 2 and parts[0] == "copies":
            found = door.copy(parts[1])
        elif len(parts) == 3 and parts[0] == "copies" and parts[2] == "file":
            return self.send_copy(parts[1])
        if found is None:
            return self.reply(404, {"error": "not-found"})
        return self.reply(200, found)

    def send_copy(self, copy_id: str) -> None:
        located = self.door.copy_file(copy_id)
        if located is None:
            return self.reply(404, {"error": "not-found"})
        path, filename = located
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.cors(write=False)
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    def do_POST(self) -> None:  # noqa: N802
        if self.route() != ["actions"]:
            return self.reply(404, {"error": "not-found"}, write=True)
        origin = self.headers.get("Origin")
        if origin is not None and not self.door.origin_allowed(origin):
            return self.reply(403, {"error": "origin"}, write=True)
        length = self.headers.get("Content-Length", "")
        if not length.isdigit():
            return self.reply(411, {"error": "length"}, write=True)
        if int(length) > MAX_BODY:
            return self.reply(413, {"error": "too-large"}, write=True)
        status, payload = self.door.accept(self.rfile.read(int(length)), self.client())
        return self.reply(status, payload, write=True)

    def do_PUT(self) -> None:  # noqa: N802
        # An import's zip, into the slot its signed request opened.
        parts = self.route()
        if len(parts) != 2 or parts[0] != "uploads":
            return self.reply(404, {"error": "not-found"}, write=True)
        origin = self.headers.get("Origin")
        if origin is not None and not self.door.origin_allowed(origin):
            return self.reply(403, {"error": "origin"}, write=True)
        status, payload = self.door.upload(parts[1], self.headers.get("Content-Length"), self.rfile, self.client())
        return self.reply(status, payload, write=True)


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def make_server(door: Door, socket_path: Path) -> Server:
    socket_path.unlink(missing_ok=True)  # a socket left by the last run
    handler = type("DoorHandler", (Handler,), {"door": door})
    server = Server(str(socket_path), handler)
    # Caddy, in its container, must be able to connect; only it and root see the folder.
    os.chmod(socket_path, 0o666)
    return server


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The fleet machine's door: signed requests in, facts out.")
    ap.add_argument("--home", type=Path, default=R.HOME)
    ap.add_argument("--socket", type=Path, default=SOCKET)
    ap.add_argument("--state", type=Path, default=STATE)
    args = ap.parse_args(argv)
    door = Door(home=args.home, state=args.state)
    server = make_server(door, args.socket)
    print(f"{door.alias}: the door is open on {args.socket}", file=sys.stderr)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
