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
  world, or show the machine's admin code. It checks the shape, the
  machine, the time window, the signature, that the signer is one of the
  Oracles listed on this machine, and that the request number is new; then
  it drops the request into /srv/fleet/inbox/ and answers 202. The
  reconciler (root) checks it again and does the work.
- GET /fleet/v1/machine, /worlds, /log — public facts: the machine and
  whether its reconciler keeps up (when it last ran, how it ended, how many
  requests wait and since when), its worlds and their phase, the last
  things done here. No wallet in full, no key, no code.
- GET /fleet/v1/requests/<n> — how a request went.
- GET /fleet/v1/copies/<c> and /copies/<c>/file — a copy's state and the
  zip. The copy's number (128 random bits, given only to the Oracle who
  signed the download) is the key to it, like a pre-signed link; it lasts
  a day.

It runs as its own user (fleetd), with no Docker, no root, no network of its
own (a Unix socket only) and write access to the inbox alone. Broken into,
it still cannot sign for an Oracle: the reconciler checks every signature
again. Python's standard library only, besides python3-ecdsa for the
signature.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
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
CONSOLE_ORIGINS = frozenset({"https://numinia.com", "https://www.numinia.com"})
WINDOW = 15 * 60  # a signed request is good for at most fifteen minutes
MAX_BODY = 16 * 1024
LOG_LINES = 50
PHASES = {"DeleteWorld": "deleting", "StopWorld": "stopping", "StartWorld": "starting"}


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


class Door:
    """Everything the door knows and does, without HTTP — what the tests drive."""

    def __init__(
        self,
        home: Path = R.HOME,
        alias: str | None = None,
        signers_file: Path = R.SIGNERS_FILE,
        admin_code_file: Path = R.ADMIN_CODE_FILE,
        control_host_file: Path = R.CONTROL_HOST_FILE,
        domains_file: Path = R.DOMAINS_FILE,
        origins_file: Path = ORIGINS_FILE,
        state: Path = STATE,
        clock=time.time,
    ) -> None:
        self.home = home
        self.alias = alias or R.ALIAS_FILE.read_text(encoding="utf-8").strip()
        self.signers_file = signers_file
        self.admin_code_file = admin_code_file
        self.control_host_file = control_host_file
        self.domains_file = domains_file
        self.origins_file = origins_file
        self.clock = clock
        self.lock = threading.Lock()
        self.nonces = Nonces(state / "nonces.json")
        self.limiter = Limiter()

    # --- what the door reads -------------------------------------------------

    @property
    def orders(self) -> Path:
        return self.home / "orders"

    def inbox(self) -> list[dict]:
        """The requests the reconciler has not taken yet."""
        waiting = []
        for path in sorted((self.home / "inbox").glob("*.json")):
            raw = R.read_json(path, None)
            if isinstance(raw, dict) and isinstance(raw.get("message"), dict):
                waiting.append(raw)
        return waiting

    def origin_allowed(self, origin: str) -> bool:
        extra = set()
        if self.origins_file.is_file():
            extra = {line.strip() for line in self.origins_file.read_text(encoding="utf-8").splitlines()}
        return origin in CONSOLE_ORIGINS or origin in extra

    def machine(self) -> dict:
        """The machine's public facts: who it is, and whether its reconciler is keeping up."""
        return {
            "machine": self.alias,
            "door": "fleetd/1",
            "oracles": len(R.read_signers(self.signers_file)),
            "zones": list(R.HOUSE_ZONES),
            "lastRun": self.last_run(),
            "waiting": self.waiting(),
            "at": R.utc(self.clock()),
        }

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
        """This machine's worlds and what is happening to each: no wallet, no key."""
        allowed = R.read_allowed_domains(self.domains_file)
        pending = {r["message"].get("world"): r for r in self.inbox()}
        listed = []
        for order in R.select_orders(R.read_orders(self.orders), self.alias, allowed):
            kind = pending.get(order["id"], {}).get("type")
            listed.append({**order, "phase": PHASES.get(kind, order["state"])})
        known = {world["id"] for world in listed}
        for wid, raw in pending.items():
            message = raw["message"]
            if raw.get("type") == "CreateWorld" and wid not in known:
                listed.append({
                    "id": wid,
                    "state": "running",
                    "domain": message["domain"],
                    "image": message["image"],
                    "limits": {"memory": message["memory"], "cpus": message["cpus"],
                               "maxUploadMb": message["maxUploadMb"]},
                    "phase": "creating",
                })
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
        """Why the machine's book cannot take this request now; None when it can."""
        if kind == "ShowAdminCode":
            return None
        wid = message["world"]
        current = R.read_order(self.orders / f"{wid}.json")
        waiting = [r for r in self.inbox() if r["message"].get("world") == wid]
        if kind == "CreateWorld":
            if current is not None or any(r.get("type") == "CreateWorld" for r in waiting):
                return "exists"
            domain = message["domain"]
            # A world deleted a moment ago keeps its folder and keys until its
            # container is gone; one born now under its name or address would
            # inherit them.
            deleting = self.purging()
            if wid in deleting or domain in deleting.values():
                return "still-deleting"
            taken = {o.get("domain") for o in R.read_orders(self.orders)}
            taken |= {r["message"].get("domain") for r in self.inbox() if r.get("type") == "CreateWorld"}
            if domain in taken:
                return "address-taken"
            draft = {
                "id": wid, "state": "running", "domain": domain, "image": message["image"],
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
            used = (self.home / "requests" / f"{nonce}.json").exists() or any(
                r["message"].get("nonce") == nonce for r in self.inbox()
            )
            if used or not self.nonces.claim(nonce, message["expires"], now):
                return 409, {"error": "replayed"}
            clash = self.clash(kind, message)
            if clash:
                return 409, {"error": clash}
            reply: dict = {"request": nonce}
            dropped = {"type": kind, "message": message, "signature": request["signature"],
                       "signer": signer, "receivedAt": R.utc(now)}
            if kind == "DownloadWorld":
                reply["copy"] = dropped["copy"] = os.urandom(16).hex()
            if kind in ("ShowAdminCode", "CreateWorld"):
                code = R.read_admin_code(self.admin_code_file)
                if code is None and kind == "ShowAdminCode":
                    return 503, {"error": "no-admin-code"}
                if code is not None:
                    reply["adminCode"] = code
            name = f"{int(now * 1000):013d}-{nonce}.json"
            R.write_json(self.home / "inbox" / name, dropped, mode=0o640)
        target = f" {message['world']}" if "world" in message else ""
        print(f"accepted {kind}{target} from {W.short(signer)}", file=sys.stderr)
        return 202, reply


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
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.cors(write)
        self.end_headers()
        self.wfile.write(body)

    def route(self) -> list[str]:
        path = self.path.split("?", 1)[0]
        return path[len(PREFIX):].split("/") if path.startswith(PREFIX) else []

    def do_OPTIONS(self) -> None:  # noqa: N802 — the base class's naming
        # The preflight before a POST: only the consoles may send requests.
        self.send_response(204)
        self.cors(write=self.route() == ["actions"])
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
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
