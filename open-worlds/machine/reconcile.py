#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: AGPL-3.0-only
"""The fleet machine's reconciler: what runs here equals this machine's book of orders.

Runs every minute from a systemd timer, and at once when a signed request
lands in the inbox (a systemd path unit). Three jobs, in this order:

1. Requests. fleetd.py, the machine's door, drops each request an Oracle
   signed into /srv/fleet/inbox/. This file checks every one AGAIN — the
   signature, that the signer is listed in /etc/fleet/signers, the machine,
   the fields — and then changes the book (/srv/fleet/orders/): an order is
   written (create), its state changes (stop, start) or it goes (delete),
   or a copy is queued (download). Each request ends with a result the door
   shows to whoever asked.
2. Containers. From the orders, render a docker-compose file and a
   Caddyfile and ask Docker to make the containers match; when the list of
   sites changed, ask Caddy to read it again (a reload; each world's site
   keeps its visitors' connections open through it). The machine's own
   address (/etc/fleet/control-host) is always one of the sites: it is how
   the console on numinia.com reaches the door. Before Docker stops or
   restarts a running world, the reconciler waits for that world's next
   save, since the engine writes its objects to disk only on a timer and
   not when it is told to stop.
3. After. A deleted world's folder, keys and copies are removed once its
   container is gone; queued copies are zipped, after the world's next save;
   copies older than a day go. The run ends by writing when it ran and how
   it ended (run/last-run.json), which the door shows.

A world's keys are made here, the first time its order runs: a JWT secret of
its own and the machine's admin code (/etc/fleet/admin-code, made once by
bootstrap.sh). A key file that already exists is never touched. A machine
with no admin code makes no keys, and a world with none is skipped and
reported — it never starts with an empty admin code.

The machine never writes to git and holds no GitHub credential. Its book is
its own; its first orders were adopted, once, from the public order book in
numinia-assets (`--adopt-book`).

Rails:

- this file is INSTALLED once by bootstrap.sh (to /usr/local/lib/fleet) and
  never run from the clone of the repository: a request changes orders, not
  code;
- a request counts only when an Oracle's wallet signed it, for this machine,
  recently and once — checked by the door and again here, so a door that
  was broken into still cannot invent one;
- an order is obeyed only if its image comes from the house's registry,
  pinned to one build, and its domain is one label under a house zone
  (numinia.com, numen.games) or a client domain an Oracle listed on this
  machine in /etc/fleet/domains;
- every field that reaches the compose file or the Caddyfile must match a
  strict, anchored pattern first, and the compose file is built as data and
  written as JSON (which is YAML), so no field can add a setting — a domain
  that smuggles `privileged: true` is refused, never rendered.

Pure functions first (select, render, take), the shell last (main). The
tests in tests/test_machine.py cover them.
"""

from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import walletsig

HOME = Path("/srv/fleet")  # orders/, data/, env/, run/, inbox/, requests/, copies/ live here
ALIAS_FILE = Path("/etc/fleet/alias")
ADMIN_CODE_FILE = Path("/etc/fleet/admin-code")  # made once by bootstrap.sh; every world keyed here gets it
SIGNERS_FILE = Path("/etc/fleet/signers")  # the Oracles' wallets this machine obeys, one per line
CONTROL_HOST_FILE = Path("/etc/fleet/control-host")  # the door's address, e.g. open-1.numinia.com
DOOR_GROUP = "fleetd"  # the door's group: it reads copies and results, never writes the book
# Seconds between two saves of a world's objects (what is placed, moved or
# edited in it); the engine refuses to boot without it. It is also the most a
# world can lose when it dies without warning, so it is short.
SAVE_INTERVAL = "10"
SAVE_MARGIN = 2  # seconds on top of a save interval, for that save to finish
# How long Caddy keeps a visitor's connection open across a reload. A reload
# replaces Caddy's whole configuration and, by default, closes every WebSocket
# it carries: without this, creating, stopping or starting any world would
# disconnect every visitor of every world on the machine.
STREAM_CLOSE_DELAY = "12h"
CADDY_IMAGE = "caddy:2-alpine"
CADDY_SERVICE = "caddy"  # a world may not take this name
CADDYFILE_INSIDE = "/etc/caddy/Caddyfile"  # where Caddy sees run/Caddyfile, mounted read-only
DOOR_SOCKET_INSIDE = "/run/fleetd/fleetd.sock"  # where Caddy sees the door's socket (home/sock)
# The sites Caddy is known to serve: a copy of the Caddyfile, written only
# after Caddy accepted a reload of exactly that text.
CADDY_APPLIED = "Caddyfile.applied"
# The compose file Docker last accepted: what the running containers were
# started from, so the next run knows which of them it is about to stop.
COMPOSE_APPLIED = "docker-compose.applied.yml"
LAST_RUN = "last-run.json"  # when the reconciler last ran and how it ended; the door shows it
INDEX_FILE = "index.json"  # the public book's table of contents; never an order
RESERVED_IDS = frozenset({CADDY_SERVICE, INDEX_FILE[: -len(".json")]})
HOUSE_ZONES = ("numinia.com", "numen.games")  # one label under either is always allowed
DOMAINS_FILE = Path("/etc/fleet/domains")  # a client's own domains, one per line, written by an Oracle
ADOPTED = ".adopted"  # marks a book that took its first orders from the public one
COPY_SECONDS = 24 * 3600  # a copy can be downloaded for a day, then it goes
FRESH_COPY_SECONDS = 600  # a copy younger than this is handed out again instead of zipping anew
GRACE = 3600  # a signed request this long past its expiry is refused even if the door took it
MAX_UPLOAD_MB = 10_000
ENGINE_DB = "db.sqlite"

# Every pattern is used with re.fullmatch: `$` alone would let a trailing
# newline through, and a newline is how text turns into a new setting.
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
HOST = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
IMAGE = re.compile(
    r"ghcr\.io/numengames/[a-z0-9]+(?:[._/-][a-z0-9]+)*"  # the only registry an engine may come from
    r"(?::sha-[0-9a-f]{7,40}|@sha256:[0-9a-f]{64})"  # pinned to one build, never a moving tag
)
MEMORY = re.compile(r"[0-9]+(?:\.[0-9]+)?[mg]")
CPUS = re.compile(r"[0-9]+(?:\.[0-9]+)?")
STATES = ("running", "stopped")
# What may be written into a world's key file. No `=`, `$`, quote, space or
# newline can pass: in an env file any of them could add or bend a setting.
ADMIN_CODE = re.compile(r"[A-Za-z0-9_-]{8,64}")
JWT = re.compile(r"[0-9a-f]{64}")
NONCE = re.compile(r"[0-9a-f]{32}")  # a request's number: 128 random bits, chosen by the browser
COPY_ID = re.compile(r"[0-9a-f]{32}")  # a copy's number: 128 random bits, chosen by the door
ADDRESS = re.compile(r"0x[0-9a-f]{40}")
# The shape every string field of a signed request must have.
FIELD_RULES = {
    "machine": SLUG,
    "world": SLUG,
    "domain": HOST,
    "image": IMAGE,
    "memory": MEMORY,
    "cpus": CPUS,
    "nonce": NONCE,
}


def matches(pattern: re.Pattern[str], value: object) -> bool:
    """True only for a string the whole of which the pattern matches."""
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def utc(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))


def read_allowed_domains(path: Path = DOMAINS_FILE) -> set[str]:
    """Client domains this machine may serve: exact hosts, or `*.example.com` for a zone.

    Lines that are not a host name (or `*.` and one) are ignored, so a typo
    on the machine can never widen what it serves.
    """
    if not path.is_file():
        return set()
    allowed = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.strip().lower()
        if not entry or entry.startswith("#"):
            continue
        if matches(HOST, entry.removeprefix("*.")):
            allowed.add(entry)
    return allowed


def read_signers(path: Path = SIGNERS_FILE) -> frozenset[str]:
    """The Oracles' wallets this machine obeys: lowercase 0x addresses, one per line; anything else is ignored."""
    if not path.is_file():
        return frozenset()
    found = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip().lower()
        if matches(ADDRESS, entry):
            found.add(entry)
    return frozenset(found)


def read_control_host(path: Path = CONTROL_HOST_FILE) -> str | None:
    """The door's own address, or None when the file is missing or holds anything but a host name."""
    if not path.is_file():
        return None
    host = path.read_text(encoding="utf-8").strip().lower()
    return host if matches(HOST, host) else None


def domain_allowed(domain: str, allowed: set[str] | frozenset[str] = frozenset()) -> bool:
    """One label under a house zone, or listed on this machine (exact or `*.zone`)."""
    if not matches(HOST, domain):
        return False
    label, _, parent = domain.partition(".")
    if parent in HOUSE_ZONES and matches(LABEL, label):
        return True
    return domain in allowed or f"*.{parent}" in allowed


def refusal(
    order: dict, allowed: set[str] | frozenset[str] = frozenset(), control_host: str | None = None
) -> str | None:
    """Why an order is refused even though it names this machine; None when it is fine.

    The reason names the rule, never the value: the log must stay free of
    whatever a malicious order might carry.
    """
    if not matches(SLUG, order.get("id")) or order.get("id") in RESERVED_IDS:
        return "id is not lowercase letters, digits and hyphens, or is reserved"
    if order.get("state") not in STATES:
        return f"state is not one of {'/'.join(STATES)}"
    if not matches(IMAGE, order.get("image")):
        return "image is not from ghcr.io/numengames/ pinned to a sha- tag or a digest"
    domain = order.get("domain")
    if not isinstance(domain, str) or not domain_allowed(domain.lower(), allowed):
        return f"domain is neither one label under {'/'.join(HOUSE_ZONES)} nor listed in {DOMAINS_FILE}"
    if control_host is not None and domain.lower() == control_host:
        return "domain is the machine's own address"
    limits = order.get("limits")
    if not isinstance(limits, dict):
        return "limits is not an object"
    if not matches(MEMORY, limits.get("memory")):
        return "limits.memory is not like 512m or 2g"
    if not matches(CPUS, limits.get("cpus")):
        return "limits.cpus is not a number such as 1.5"
    upload = limits.get("maxUploadMb")
    # bool is an int in Python; True must not pass as 1.
    if isinstance(upload, bool) or not isinstance(upload, int) or upload <= 0:
        return "limits.maxUploadMb is not a positive whole number"
    return None


def clean(order: dict) -> dict:
    """The order reduced to the fields the machine uses, each one already checked."""
    limits = order["limits"]
    return {
        "id": order["id"],
        "state": order["state"],
        "domain": order["domain"].lower(),
        "image": order["image"],
        "limits": {"memory": limits["memory"], "cpus": limits["cpus"], "maxUploadMb": limits["maxUploadMb"]},
    }


def read_order(path: Path) -> dict | None:
    """One order file as a dict, or None when it is missing, not JSON, or names another id."""
    try:
        order = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return order if isinstance(order, dict) and order.get("id") == path.stem else None


def read_orders(folder: Path) -> list[dict]:
    """Every order in the folder, as dicts, sorted by id. Malformed files are skipped loudly."""
    orders = []
    for path in sorted(folder.glob("*.json")):
        if path.name == INDEX_FILE:
            continue
        try:
            order = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            print(f"skip {path.name}: not JSON ({error.msg})", file=sys.stderr)
            continue
        if isinstance(order, dict) and order.get("id") == path.stem:
            orders.append(order)
        else:
            print(f"skip {path.name}: id does not match the file", file=sys.stderr)
    return orders


def select_orders(
    orders: list[dict],
    alias: str,
    allowed: set[str] | frozenset[str] = frozenset(),
    control_host: str | None = None,
) -> list[dict]:
    """The orders this machine obeys, cleaned: its alias, and every field checked."""
    mine = []
    for o in orders:
        if o.get("server") != alias:
            continue
        why = refusal(o, allowed, control_host)
        if why:
            print(f"refuse one order on this machine: {why}", file=sys.stderr)
            continue
        mine.append(clean(o))
    return mine


def split_by_keys(orders: list[dict], env_dir: Path) -> tuple[list[dict], list[dict]]:
    """Running orders with keys on this machine, and running orders without them."""
    ready, keyless = [], []
    for o in orders:
        if o["state"] != "running":
            continue
        (ready if (env_dir / f"{o['id']}.env").is_file() else keyless).append(o)
    return ready, keyless


def read_admin_code(path: Path = ADMIN_CODE_FILE) -> str | None:
    """The machine's admin code, or None when the file is missing or holds anything but a plain code.

    None means this machine makes no keys: a world with no key file then stays
    down rather than start with an empty admin code (everyone an admin).
    """
    if not path.is_file():
        return None
    code = path.read_text(encoding="utf-8").strip()
    return code if matches(ADMIN_CODE, code) else None


def world_env(admin_code: str, jwt: str) -> str:
    """A world's key file: its own JWT secret and the machine's admin code, both plain."""
    if not matches(ADMIN_CODE, admin_code) or not matches(JWT, jwt):
        raise ValueError("a key is not a plain code")
    return f"JWT_SECRET={jwt}\nADMIN_CODE={admin_code}\n"


def make_keys(env_dir: Path, wid: str, admin_code: str) -> bool:
    """Write a world's key file once (mode 600). False when one is already there: it is never touched."""
    env_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(env_dir / f"{wid}.env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(world_env(admin_code, os.urandom(32).hex()))
    return True


def engine_environment(origin: str, ws_origin: str, upload_mb: int) -> dict[str, str]:
    """Everything the engine's `check envs` block demands at boot (numinia-hyperfy2
    src/server/index.js at the pinned build), except JWT_SECRET, which comes from
    the world's key file."""
    return {
        "WORLD": "world",
        "PORT": "3000",
        "SAVE_INTERVAL": SAVE_INTERVAL,
        "ASSETS": "local",
        "ASSETS_BASE_URL": f"{origin}/assets",
        "DB_URI": "local",
        "COLLECTIONS": "local",
        "PUBLIC_WS_URL": f"{ws_origin}/ws",
        "PUBLIC_API_URL": f"{origin}/api",
        "PUBLIC_MAX_UPLOAD_SIZE": str(upload_mb),
    }


def compose_dict(running: list[dict], home: Path) -> dict:
    """One Caddy, one service per running world. Only Caddy publishes ports."""
    services: dict[str, dict] = {
        CADDY_SERVICE: {
            "image": CADDY_IMAGE,
            "restart": "unless-stopped",
            "ports": ["80:80", "443:443"],
            "volumes": [
                f"{home}/run/Caddyfile:{CADDYFILE_INSIDE}:ro",
                f"{home}/run/caddy-data:/data",
                f"{home}/run/caddy-config:/config",
                # The door's socket: Caddy is the only way in to it.
                f"{home}/sock:{Path(DOOR_SOCKET_INSIDE).parent}",
            ],
        }
    }
    for o in running:
        wid, domain, limits = o["id"], o["domain"], o["limits"]
        services[wid] = {
            "image": o["image"],
            "restart": "unless-stopped",
            "env_file": f"{home}/env/{wid}.env",
            "environment": engine_environment(f"https://{domain}", f"wss://{domain}", limits["maxUploadMb"]),
            "volumes": [f"{home}/data/{wid}:/app/world"],
            "deploy": {"resources": {"limits": {"memory": limits["memory"], "cpus": limits["cpus"]}}},
        }
    return {"services": services}


def render_compose(running: list[dict], home: Path) -> str:
    """The compose file as JSON — valid YAML that no field can restructure."""
    return json.dumps(compose_dict(running, home), indent=2) + "\n"


def world_site(o: dict) -> str:
    """A world's site: HTTPS at its address, its connections kept open across a reload."""
    return (f"{o['domain']} {{\n\treverse_proxy {o['id']}:3000 {{\n"
            f"\t\tstream_close_delay {STREAM_CLOSE_DELAY}\n\t}}\n}}")


def render_caddyfile(running: list[dict], control_host: str | None = None) -> str:
    """One site per running world, plus the door's; Caddy issues each certificate."""
    blocks = ["# Generated by open-worlds/machine/reconcile.py — do not edit, change the order."]
    if control_host is not None:
        # The door: small signed requests in, facts and copies out.
        blocks.append(
            f"{control_host} {{\n\trequest_body {{\n\t\tmax_size 64KB\n\t}}\n"
            f"\treverse_proxy unix/{DOOR_SOCKET_INSIDE}\n}}"
        )
    for o in running:
        blocks.append(world_site(o))
    return "\n\n".join(blocks) + "\n"


def plan(
    orders_dir: Path,
    home: Path,
    alias: str,
    allowed: set[str] | None = None,
    admin_code: str | None = None,
    control_host: str | None = None,
) -> dict:
    """Everything the shell part needs, computed without touching Docker.

    `admin_code` is the machine's own (main reads it from /etc/fleet/admin-code).
    With it, a running world that has no key file is listed in `new_keys` and
    started; without it, that world stays down and is listed as `keyless`.
    """
    if allowed is None:
        allowed = read_allowed_domains()
    mine = select_orders(read_orders(orders_dir), alias, allowed, control_host)
    ready, keyless = split_by_keys(mine, home / "env")
    new_keys = keyless if matches(ADMIN_CODE, admin_code) else []
    if new_keys:
        keyless = []
    running = sorted(ready + new_keys, key=lambda o: o["id"])
    stopped = [o["id"] for o in mine if o["state"] == "stopped"]
    return {
        "alias": alias,
        "running": [o["id"] for o in running],
        "stopped": stopped,
        "keyless": [o["id"] for o in keyless],
        "new_keys": [o["id"] for o in new_keys],
        "compose": render_compose(running, home),
        "caddyfile": render_caddyfile(running, control_host),
    }


def caddy_needs_reload(caddyfile: str, applied: Path) -> bool:
    """Whether Caddy must be told to read its sites again.

    Caddy reads its Caddyfile when it starts and never again on its own, and
    `docker compose up` leaves a running Caddy alone when only the file's text
    changed — so a world added after Caddy started got no site at all. True
    until Caddy accepted a reload of exactly this text: a failed reload is
    tried again on the next run, and a machine whose Caddy started before this
    check existed is put right on its first run.
    """
    try:
        return applied.read_text(encoding="utf-8") != caddyfile
    except FileNotFoundError:
        return True


def save_pause(interval: object) -> int:
    """Seconds to wait for a world's next save, from the SAVE_INTERVAL it was started with."""
    seconds = int(interval) if isinstance(interval, str) and interval.isdigit() else 60
    return seconds + SAVE_MARGIN


def save_waits(before: object, after: dict, keep: set[str] | frozenset[str]) -> dict[str, int]:
    """The running worlds Docker is about to stop or restart, each with the wait for its next save.

    The engine writes what was placed, moved or edited in a world to disk every
    SAVE_INTERVAL seconds, and not when it is told to stop: a world stopped
    right after an edit would lose it. `before` is the compose file the
    running containers came from, `after` the one about to be applied, `keep`
    the worlds that still have an order — a deleted world needs no save.
    """
    old = before.get("services") if isinstance(before, dict) else None
    new = after.get("services", {})
    waits = {}
    for name, service in (old.items() if isinstance(old, dict) else []):
        if name == CADDY_SERVICE or name not in keep or not isinstance(service, dict):
            continue
        if new.get(name) == service:
            continue
        environment = service.get("environment")
        waits[name] = save_pause(environment.get("SAVE_INTERVAL") if isinstance(environment, dict) else None)
    return waits


def sh(*cmd: str) -> None:
    # Never the caller's input: the bootstrap is piped into bash (`curl … | bash`),
    # and a child that reads stdin (`docker compose exec` does by default)
    # swallowed the rest of the script, which then ended halfway without a word.
    subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL)


def apply(p: dict, home: Path, admin_code: str | None = None, sleep=time.sleep) -> None:
    if p["new_keys"]:
        if admin_code is None or not matches(ADMIN_CODE, admin_code):
            raise ValueError("worlds to key but no admin code on this machine")
        for wid in p["new_keys"]:
            make_keys(home / "env", wid, admin_code)
    run = home / "run"
    run.mkdir(parents=True, exist_ok=True)
    for wid in p["running"]:
        (home / "data" / wid).mkdir(parents=True, exist_ok=True)
    compose = run / "docker-compose.yml"
    compose_applied = run / COMPOSE_APPLIED
    # What the running containers came from: the copy Docker last accepted, or,
    # on a machine set up before that copy existed, the file the last run wrote.
    before = read_json(compose_applied, None) if compose_applied.exists() else read_json(compose, None)
    waits = save_waits(before, json.loads(p["compose"]), set(p["running"]) | set(p["stopped"]))
    if waits:
        pause = max(waits.values())
        print(f"waiting {pause} s for {', '.join(sorted(waits))} to save before Docker stops or restarts them",
              file=sys.stderr)
        sleep(pause)
    compose.write_text(p["compose"], encoding="utf-8")
    (run / "Caddyfile").write_text(p["caddyfile"], encoding="utf-8")
    sh("docker", "compose", "-f", str(compose), "up", "-d", "--remove-orphans")
    compose_applied.write_text(p["compose"], encoding="utf-8")
    applied = run / CADDY_APPLIED
    if caddy_needs_reload(p["caddyfile"], applied):
        # A reload, not a restart; each world's site keeps its connections (STREAM_CLOSE_DELAY).
        try:
            sh("docker", "compose", "-f", str(compose), "exec", "-T", CADDY_SERVICE,
               "caddy", "reload", "--config", CADDYFILE_INSIDE, "--adapter", "caddyfile")
        except subprocess.CalledProcessError:
            print("caddy did not take the new list of sites; the next run tries again", file=sys.stderr)
        else:
            applied.write_text(p["caddyfile"], encoding="utf-8")


# ---------------------------------------------------------------------------
# Files the door reads: written whole, then moved into place
# ---------------------------------------------------------------------------


def door_gid() -> int | None:
    """The door's group, when this runs as root on a machine that has it."""
    if os.geteuid() != 0:
        return None
    try:
        return grp.getgrnam(DOOR_GROUP).gr_gid
    except KeyError:
        return None


def write_json(path: Path, data: object, mode: int = 0o644, group: int | None = None) -> None:
    """Write JSON whole and move it into place, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, mode)
    if group is not None:
        os.chown(tmp, 0, group)
    os.replace(tmp, path)


def read_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Signed requests: checked again, then applied to the book
# ---------------------------------------------------------------------------


def request_problem(kind: object, message: object) -> str | None:
    """Which rule a request's typed message breaks; None when its shape is right.

    The door and this file share it: a request is the same thing to both.
    """
    if kind not in walletsig.ACTIONS:
        return "type is not a request this machine knows"
    fields = walletsig.ACTIONS[kind]
    if not isinstance(message, dict) or set(message) != {name for name, _ in fields}:
        return "message does not have exactly the fields of its type"
    for name, kind_of in fields:
        value = message[name]
        if kind_of == "string":
            if not matches(FIELD_RULES[name], value):
                return f"{name} does not have its shape"
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return f"{name} is not a whole number"
    if "maxUploadMb" in message and not 1 <= message["maxUploadMb"] <= MAX_UPLOAD_MB:
        return "maxUploadMb is out of range"
    if message["expires"] >= 2**63:
        return "expires is out of range"
    return None


def check_request(
    raw: object, alias: str, signers: frozenset[str], now: float
) -> tuple[dict | None, dict | None, str | None]:
    """A request from the inbox, checked again: (entry, message, None) or (entry or None, None, the rule).

    No entry means there is nobody to answer: the request carried no usable number.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("message"), dict):
        return None, None, "not a request"
    kind, message = raw.get("type"), raw["message"]
    if not matches(NONCE, message.get("nonce")):
        return None, None, "no request number"
    entry = {
        "nonce": message["nonce"],
        "type": kind if kind in walletsig.ACTIONS else None,
        "world": message.get("world") if matches(SLUG, message.get("world")) else None,
        "by": None,
        "copy": None,
    }
    problem = request_problem(kind, message)
    if problem:
        return entry, None, problem
    if message["machine"] != alias:
        return entry, None, "the request names another machine"
    if message["expires"] < now - GRACE:
        return entry, None, "the request expired"
    signer = walletsig.signer_of(kind, message, raw.get("signature"))
    if signer is None:
        return entry, None, "the signature does not hold"
    if signer not in signers:
        return entry, None, "the signer is not an Oracle of this machine"
    entry["by"] = signer
    if kind == "DownloadWorld":
        if not matches(COPY_ID, raw.get("copy")):
            return entry, None, "no copy number"
        entry["copy"] = raw["copy"]
    return entry, message, None


def change_book(
    entry: dict,
    message: dict,
    orders: Path,
    alias: str,
    allowed: set[str] | frozenset[str],
    control_host: str | None,
    now: float,
    deleting: dict[str, str | None] | None = None,
) -> str | None:
    """Change the book for one checked request; the broken rule, or None when done.

    `deleting` holds the worlds whose delete was taken but whose folder and
    keys are not gone yet (id → address): a world born under that name or
    address now would keep the old one's data, so its creation waits.
    """
    kind, wid = entry["type"], entry["world"]
    if kind == "ShowAdminCode":
        return None
    path = orders / f"{wid}.json"
    current = read_order(path)
    if kind == "CreateWorld":
        if current is not None or path.exists():
            return "a world with this name already exists here"
        domain = message["domain"]
        deleting = deleting or {}
        if wid in deleting or domain in deleting.values():
            return "a world with this name or address is still being deleted here"
        if any(o.get("domain") == domain for o in read_orders(orders)):
            return "another world already has this address"
        order = {
            "id": wid,
            "card": wid,
            "state": "running",
            "server": alias,
            "domain": domain,
            "image": message["image"],
            "limits": {"memory": message["memory"], "cpus": message["cpus"], "maxUploadMb": message["maxUploadMb"]},
            "createdAt": utc(now),
            "createdBy": entry["by"],
        }
        why = refusal(order, allowed, control_host)
        if why:
            return why
        write_json(path, order)
        return None
    if current is None:
        return "no world with this name on this machine"
    if kind == "DeleteWorld":
        if current.get("domain") != message["domain"]:
            return "the address does not match the world's"
        path.unlink()
        return None
    if kind in ("StopWorld", "StartWorld"):
        write_json(path, {**current, "state": "stopped" if kind == "StopWorld" else "running"})
        return None
    return None  # DownloadWorld: the copy is made after the containers


def answer(home: Path, entry: dict, state: str, rule: str | None = None, now: float | None = None) -> None:
    """The result the door shows for one request; a final one also goes to the machine's log."""
    write_json(
        home / "requests" / f"{entry['nonce']}.json",
        {"request": entry["nonce"], "type": entry["type"], "world": entry["world"], "state": state,
         "rule": rule, "at": utc(now)},
    )
    if state == "working":
        return
    line = {
        "at": utc(now),
        "type": entry["type"],
        "world": entry["world"],
        "by": walletsig.short(entry["by"]) if entry.get("by") else None,
        "state": state,
    }
    with open(home / "log.jsonl", "a", encoding="utf-8") as log:
        log.write(json.dumps(line) + "\n")


def take_requests(
    home: Path,
    orders: Path,
    alias: str,
    signers: frozenset[str],
    allowed: set[str] | frozenset[str] = frozenset(),
    control_host: str | None = None,
    now: float | None = None,
) -> list[dict]:
    """Every request waiting in the inbox, oldest first: checked again, applied, answered.

    Every request leaves the inbox, so the path unit that woke this run sleeps
    again. Create, stop, start and show are answered at once (the book is the
    truth; the containers follow it below). A delete waits in run/purge.json
    until its container is gone; a download waits in run/copy-queue.json.
    Returns the entries taken, for the summary.
    """
    now = time.time() if now is None else now
    inbox = home / "inbox"
    taken = []
    purge = read_json(home / "run" / "purge.json", {})
    queue = read_json(home / "run" / "copy-queue.json", [])
    purge = purge if isinstance(purge, dict) else {}
    queue = queue if isinstance(queue, list) else []
    for path in sorted(inbox.glob("*.json")) if inbox.is_dir() else []:
        raw = read_json(path, None)
        path.unlink(missing_ok=True)
        entry, message, rule = check_request(raw, alias, signers, now)
        if entry is None:
            print(f"drop one request from the inbox: {rule}", file=sys.stderr)
            continue
        if (home / "requests" / f"{entry['nonce']}.json").exists():
            print("drop one request: its number was used before", file=sys.stderr)
            continue
        if rule is None:
            deleting = {w: job.get("domain") for w, job in purge.items() if isinstance(job, dict)}
            rule = change_book(entry, message, orders, alias, allowed, control_host, now, deleting)
        if rule is not None:
            print(f"refuse one request: {rule}", file=sys.stderr)
            answer(home, entry, "failed", rule, now)
            continue
        taken.append(entry)
        if entry["type"] == "DeleteWorld":
            purge[entry["world"]] = {"nonce": entry["nonce"], "by": entry["by"], "domain": message["domain"]}
            answer(home, entry, "working", None, now)
        elif entry["type"] == "DownloadWorld":
            queue.append({"copy": entry["copy"], "world": entry["world"], "nonce": entry["nonce"], "by": entry["by"]})
            write_json(home / "copies" / f"{entry['copy']}.json",
                       {"copy": entry["copy"], "world": entry["world"], "state": "preparing", "at": utc(now)},
                       mode=0o640, group=door_gid())
            answer(home, entry, "working", None, now)
        else:
            answer(home, entry, "done", None, now)
    if purge:
        write_json(home / "run" / "purge.json", purge)
    if queue:
        write_json(home / "run" / "copy-queue.json", queue)
    return taken


# ---------------------------------------------------------------------------
# After the containers: deletes and copies
# ---------------------------------------------------------------------------


def purge_world(home: Path, wid: str) -> None:
    """Remove a world's folder, keys and copies. Only for a world with no order and no container."""
    if not matches(SLUG, wid):
        raise ValueError("not a world id")
    data_root = home / "data"
    folder = data_root / wid
    if folder.is_symlink():
        folder.unlink()
    elif folder.is_dir() and folder.resolve().parent == data_root.resolve():
        shutil.rmtree(folder)
    (home / "env" / f"{wid}.env").unlink(missing_ok=True)
    for status_path in (home / "copies").glob("*.json"):
        status = read_json(status_path, {})
        if isinstance(status, dict) and status.get("world") == wid:
            (home / "copies" / f"{status_path.stem}.zip").unlink(missing_ok=True)
            status_path.unlink(missing_ok=True)


def purge_deleted(home: Path, orders: Path, now: float | None = None) -> list[str]:
    """Delete what the deleted worlds left, now that Docker took their containers down."""
    path = home / "run" / "purge.json"
    pending = read_json(path, {})
    done = []
    for wid, job in (pending.items() if isinstance(pending, dict) else []):
        if not matches(SLUG, wid) or not isinstance(job, dict) or not matches(NONCE, job.get("nonce")):
            continue
        if not (orders / f"{wid}.json").exists():  # ordered again since: its folder is the new world's
            purge_world(home, wid)
        answer(home, {"nonce": job["nonce"], "type": "DeleteWorld", "world": wid, "by": job.get("by")},
               "done", None, now)
        done.append(wid)
    path.unlink(missing_ok=True)
    return done


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_sqlite(source: Path, target: Path) -> None:
    """A consistent copy of a database the engine may be writing to (SQLite's online backup)."""
    target.unlink(missing_ok=True)
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(target))
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()


def copy_compose(order: dict) -> str:
    """The world on one's own computer: the same engine build, on localhost:3000."""
    service = {
        "image": order["image"],
        "restart": "unless-stopped",
        "ports": ["3000:3000"],
        "env_file": ".env",
        "environment": engine_environment("http://localhost:3000", "ws://localhost:3000",
                                          order["limits"]["maxUploadMb"]),
        "volumes": ["./world:/app/world"],
    }
    return json.dumps({"services": {"world": service}}, indent=2) + "\n"


def copy_readme(order: dict, alias: str, when: str) -> str:
    wid, domain = order["id"], order["domain"]
    return f"""# {wid}

Copia completa del mundo `{wid}` (https://{domain}), hecha el {when} en la máquina `{alias}`.

## Abrirlo en tu ordenador

1. Instala Docker (Docker Desktop en Windows o macOS).
2. Descomprime este zip y abre una terminal en su carpeta.
3. Escribe `docker compose up -d`.
4. Abre http://localhost:3000 en el navegador.
5. Para administrarlo, escribe en el chat del mundo `/admin` seguido del código `ADMIN_CODE`
   que está en el archivo `.env`. Las llaves de esta copia son nuevas: no son las del mundo publicado.

## Qué hay dentro

| Archivo | Qué es |
|---|---|
| `world/` | el mundo: su base de datos (`db.sqlite`) y sus archivos (`assets/`) |
| `docker-compose.yml` | cómo arrancarlo, con el motor fijado a una versión |
| `.env` | las llaves de esta copia |
| `world.json` | de dónde viene la copia y la huella (sha256) de cada archivo |

---

# {wid} (English)

A complete copy of the world `{wid}` (https://{domain}), made on {when} on the machine `{alias}`.
To open it: install Docker, unzip, run `docker compose up -d` in this folder and open
http://localhost:3000. To administer it, type `/admin` and the `ADMIN_CODE` from `.env` in the
world's chat. This copy's keys are new; they are not the published world's.
"""


def build_copy(home: Path, order: dict, copy_id: str, alias: str, now: float | None = None) -> dict:
    """Zip a world whole — its folder, a compose file, fresh keys, a manifest, a guide — and say so.

    The Alchemists' Tower's copy, on one machine: everything needed to run
    the world anywhere, with keys of its own, and the fingerprint of each file.
    """
    now = time.time() if now is None else now
    wid = order["id"]
    data = home / "data" / wid
    copies = home / "copies"
    copies.mkdir(parents=True, exist_ok=True)
    part = copies / f".{copy_id}.zip.part"
    final = copies / f"{copy_id}.zip"
    filename = f"{wid}-{time.strftime('%Y%m%d-%H%M', time.gmtime(now))}.zip"
    files = []

    def add(zipped: zipfile.ZipFile, source: Path, name: str) -> None:
        zipped.write(source, name)
        files.append({"path": name, "bytes": source.stat().st_size, "sha256": file_digest(source)})

    with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        database = data / ENGINE_DB
        if database.is_file():
            snapshot = copies / f".{copy_id}.sqlite.tmp"
            try:
                snapshot_sqlite(database, snapshot)
                add(zipped, snapshot, f"world/{ENGINE_DB}")
            finally:
                snapshot.unlink(missing_ok=True)
        skip = {ENGINE_DB, f"{ENGINE_DB}-wal", f"{ENGINE_DB}-shm", f"{ENGINE_DB}-journal"}
        if data.is_dir():
            for folder, dirs, names in os.walk(data, followlinks=False):
                dirs.sort()
                for name in sorted(names):
                    source = Path(folder) / name
                    relative = source.relative_to(data).as_posix()
                    if relative in skip or source.is_symlink() or not source.is_file():
                        continue
                    add(zipped, source, f"world/{relative}")
        zipped.writestr("docker-compose.yml", copy_compose(order))
        zipped.writestr(".env", world_env(os.urandom(6).hex(), os.urandom(32).hex()))
        zipped.writestr("README.md", copy_readme(order, alias, utc(now)))
        manifest = {
            "format": "numinia-world-copy/1",
            "world": wid,
            "domain": order["domain"],
            "machine": alias,
            "image": order["image"],
            "limits": order["limits"],
            "copiedAt": utc(now),
            "files": files,
        }
        zipped.writestr("world.json", json.dumps(manifest, indent=2) + "\n")
    os.chmod(part, 0o640)
    group = door_gid()
    if group is not None:
        os.chown(part, 0, group)
    os.replace(part, final)
    status = {
        "copy": copy_id,
        "world": wid,
        "state": "ready",
        "bytes": final.stat().st_size,
        "sha256": file_digest(final),
        "filename": filename,
        "at": utc(now),
        "madeAt": int(now),
        "expiresAt": utc(now + COPY_SECONDS),
    }
    write_json(copies / f"{copy_id}.json", status, mode=0o640, group=group)
    return status


def made_at(status: object) -> int | None:
    made = status.get("madeAt") if isinstance(status, dict) else None
    return made if isinstance(made, int) and not isinstance(made, bool) else None


def fresh_copy(home: Path, wid: str, now: float) -> dict | None:
    """A ready copy of this world young enough to hand out again, if there is one."""
    best = None
    for path in (home / "copies").glob("*.json"):
        status = read_json(path, {})
        made = made_at(status)
        if made is None or status.get("world") != wid or status.get("state") != "ready":
            continue
        if now - made <= FRESH_COPY_SECONDS and (home / "copies" / f"{path.stem}.zip").is_file():
            if best is None or made > best[0]:
                best = (made, status)
    return best[1] if best else None


def copies_wait(home: Path, orders: Path) -> int:
    """Seconds to wait before zipping the queued copies: a running world's next save, or none.

    The copy reads the world's database, and what was placed or moved in the
    last seconds is still only in the engine's memory until its next save.
    """
    queue = read_json(home / "run" / "copy-queue.json", [])
    for job in queue if isinstance(queue, list) else []:
        wid = job.get("world") if isinstance(job, dict) else None
        order = read_order(orders / f"{wid}.json") if matches(SLUG, wid) else None
        if order is not None and order.get("state") == "running":
            return save_pause(SAVE_INTERVAL)
    return 0


def record_run(home: Path, ok: bool, rule: str | None = None, now: float | None = None) -> None:
    """When the reconciler last ran and how it ended — a rule, never a value — for the door to show.

    A machine whose reconciler stopped running shows it here: the time stops moving.
    """
    write_json(home / "run" / LAST_RUN, {"at": utc(now), "ok": ok, "rule": rule})


def make_copies(home: Path, orders: Path, alias: str, now: float | None = None) -> list[str]:
    """Zip every queued copy; a world copied minutes ago gets the same zip again."""
    now = time.time() if now is None else now
    path = home / "run" / "copy-queue.json"
    queue = read_json(path, [])
    made = []
    group = door_gid()
    for job in queue if isinstance(queue, list) else []:
        if not isinstance(job, dict) or not matches(COPY_ID, job.get("copy")) or not matches(NONCE, job.get("nonce")):
            continue
        entry = {"nonce": job["nonce"], "type": "DownloadWorld", "world": job.get("world"), "by": job.get("by")}
        order = read_order(orders / f"{job.get('world')}.json") if matches(SLUG, job.get("world")) else None
        failed = {"copy": job["copy"], "world": entry["world"], "state": "failed", "at": utc(now)}
        if order is None:
            write_json(home / "copies" / f"{job['copy']}.json", failed, mode=0o640, group=group)
            answer(home, entry, "failed", "no world with this name on this machine", now)
            continue
        again = fresh_copy(home, order["id"], now)
        try:
            if again is not None:
                os.link(home / "copies" / f"{again['copy']}.zip", home / "copies" / f"{job['copy']}.zip")
                write_json(home / "copies" / f"{job['copy']}.json", {**again, "copy": job["copy"]},
                           mode=0o640, group=group)
            else:
                build_copy(home, order, job["copy"], alias, now)
        except (OSError, sqlite3.Error, zipfile.BadZipFile) as error:
            print(f"the copy of {order['id']} failed ({type(error).__name__})", file=sys.stderr)
            write_json(home / "copies" / f"{job['copy']}.json", failed, mode=0o640, group=group)
            answer(home, entry, "failed", "the copy could not be made", now)
            continue
        answer(home, entry, "done", None, now)
        made.append(job["copy"])
    path.unlink(missing_ok=True)
    return made


def prune_copies(home: Path, now: float | None = None) -> list[str]:
    """Remove copies older than a day, and any half-written leftover."""
    now = time.time() if now is None else now
    gone = []
    copies = home / "copies"
    for path in list(copies.glob("*.json")) + list(copies.glob(".*.part")) + list(copies.glob(".*.tmp")):
        made = made_at(read_json(path, {})) if path.suffix == ".json" else None
        if now - (made if made is not None else path.stat().st_mtime) <= COPY_SECONDS:
            continue
        if path.suffix == ".json":
            (copies / f"{path.stem}.zip").unlink(missing_ok=True)
            gone.append(path.stem)
        path.unlink(missing_ok=True)
    return gone


def adopt_book(book: Path, orders: Path, alias: str) -> list[str]:
    """Copy this machine's orders from a clone of the public order book, once.

    The machine used to read numinia-assets `open-worlds/`; its book is now
    its own. The worlds the public book gave it keep running: each valid
    order naming this machine is copied in, unless the book already has it.
    """
    marker = orders / ADOPTED
    if marker.exists():
        return []
    orders.mkdir(parents=True, exist_ok=True)
    adopted = []
    for order in read_orders(book / "open-worlds"):
        if order.get("server") != alias or refusal(order, read_allowed_domains()) is not None:
            continue
        target = orders / f"{order['id']}.json"
        if target.exists():
            continue
        card = order.get("card") if matches(SLUG, order.get("card")) else order["id"]
        write_json(target, {**clean(order), "card": card, "server": alias, "adoptedAt": utc()})
        adopted.append(order["id"])
    marker.write_text(utc() + "\n", encoding="utf-8")
    return adopted


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Make this machine's containers match its book of orders.")
    ap.add_argument("--home", type=Path, default=HOME)
    ap.add_argument("--orders", type=Path, default=None, help="defaults to <home>/orders")
    ap.add_argument("--alias", default=None, help="defaults to /etc/fleet/alias")
    ap.add_argument("--admin-code-file", type=Path, default=ADMIN_CODE_FILE, help="the machine's admin code")
    ap.add_argument("--signers-file", type=Path, default=SIGNERS_FILE, help="the Oracles' wallets")
    ap.add_argument("--control-host-file", type=Path, default=CONTROL_HOST_FILE, help="the door's address")
    ap.add_argument("--adopt-book", type=Path, default=None, metavar="CLONE",
                    help="copy this machine's orders from a clone of numinia-assets, once, and stop")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, touch nothing")
    args = ap.parse_args(argv)
    alias = args.alias or ALIAS_FILE.read_text(encoding="utf-8").strip()
    orders = args.orders or args.home / "orders"
    if args.adopt_book is not None:
        adopted = adopt_book(args.adopt_book, orders, alias)
        print(f"{alias}: adopted {adopted} from the public order book" if adopted else f"{alias}: nothing to adopt")
        return 0
    admin_code = read_admin_code(args.admin_code_file)
    control_host = read_control_host(args.control_host_file)
    allowed = read_allowed_domains()
    if args.dry_run:
        p = plan(orders, args.home, alias, allowed, admin_code=admin_code, control_host=control_host)
        for wid in p["keyless"]:
            print(f"{wid}: order says running but it has no key file and this machine has no admin code"
                  f" ({args.admin_code_file}) — not started", file=sys.stderr)
        print(json.dumps({k: p[k] for k in ("alias", "running", "stopped", "keyless", "new_keys")}, indent=2))
        print(p["compose"])
        print(p["caddyfile"])
        return 0
    try:
        applied = run_once(args, alias, orders, admin_code, control_host, allowed)
    except Exception:
        record_run(args.home, False, "the run stopped on an unexpected error")
        raise
    record_run(args.home, applied, None if applied else "docker did not take the book")
    return 0 if applied else 1


def run_once(args: argparse.Namespace, alias: str, orders: Path, admin_code: str | None,
             control_host: str | None, allowed: set[str]) -> bool:
    """One run on the machine: requests, containers, deletes, copies. True when Docker took the book."""
    take_requests(args.home, orders, alias, read_signers(args.signers_file), allowed, control_host)
    p = plan(orders, args.home, alias, allowed, admin_code=admin_code, control_host=control_host)
    for wid in p["keyless"]:
        print(
            f"{wid}: order says running but it has no key file and this machine has no admin code"
            f" ({args.admin_code_file}) — not started",
            file=sys.stderr,
        )
    applied = True
    try:
        apply(p, args.home, admin_code)
    except subprocess.CalledProcessError:
        applied = False
        print("docker did not take the book; the next run tries again", file=sys.stderr)
    deleted = purge_deleted(args.home, orders) if applied else []
    pause = copies_wait(args.home, orders)
    if pause:
        time.sleep(pause)
    copied = make_copies(args.home, orders, alias)
    prune_copies(args.home)
    for wid in p["new_keys"]:
        print(f"{wid}: keys made on this machine (its own JWT secret, the machine's admin code)")
    print(f"{alias}: running {p['running']} stopped {p['stopped']} keyless {p['keyless']}"
          f" deleted {deleted} copies {len(copied)}")
    return applied


if __name__ == "__main__":
    sys.exit(main())
