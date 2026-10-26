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
   a copy is queued (download), a world gets a new admin code (change of
   code), a world moves to another build of its engine (update), or a
   world comes back from its zip (import; see below). Each request ends
   with a result the door shows to whoever asked.
2. Containers. From the orders, render a docker-compose file and a
   Caddyfile and ask Docker to make the containers match; when the list of
   sites changed, ask Caddy to read it again (a reload; each world's site
   keeps its visitors' connections open through it). The machine's own
   address (/etc/fleet/control-host) is always one of the sites: it is how
   the console on numinia.com reaches the door. Before Docker stops or
   restarts a running world, the reconciler waits for that world's next
   save, since Hyperfy writes its objects to disk only on a timer and
   not when it is told to stop (the Numinia Engine saves when it is told
   to stop). A world whose admin code changed is restarted alone, after
   that save, so the engine reads its new code. Two engines run here, each
   known by its image (engine_of): Hyperfy and the Numinia Engine, whose
   zips are made and read by its own image's tools.
3. After. A deleted world's folder, keys, code and copies are removed once
   its container is gone; queued copies are zipped, after the world's next
   save; copies older than a day go. The run ends by writing when it ran
   and how it ended (run/last-run.json), which the door shows.

Each world has its own admin code. The door makes it when the world is
created or when an Oracle changes it (16 hex characters) and carries it to
this file in the signed request's inbox entry; this file writes it into the
world's key file (/srv/fleet/env/<id>.env, beside a JWT secret of the
world's own, which a change of code keeps) and into /etc/fleet/codes/<id>,
where the door reads it to show it to a signed Oracle. The key file is the
truth — the engine reads it — and every run makes each code file equal to
it, which is how the worlds keyed before this version keep the code they
have. A running order with no key file and no request that made one (an
order adopted from the public book) is keyed with the machine's legacy
code (/etc/fleet/admin-code, made once by bootstrap.sh); a machine with no
such code keys nothing, and that world is skipped and reported — no world
ever starts with an empty admin code.

A world comes back from its zip with one signed ImportWorld, which names the
zip's exact size and SHA-256. The door keeps an upload slot for it, takes
the zip into /srv/fleet/uploads/ and only then drops the request in the
inbox. This file copies that zip into a file of its own while it checks its
fingerprint, reads its index against the caps below (size, entries, ratio,
paths, links, encryption) before anything is written outside the upload
folder, finds the world's folder in it, unpacks only the database and the
world's files into a root-only staging folder, counting what it writes,
checks the database with SQLite's own integrity check and the engine's
tables, and only then moves it to /srv/fleet/data/<id>/, makes the world's
keys (never the zip's) and writes its order — stopped: an imported world is
started by an Oracle, never by its import. A refusal names its rule and
leaves nothing behind.

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
import calendar
import errno
import grp
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib
from pathlib import Path

import walletsig

HOME = Path("/srv/fleet")  # orders/, data/, env/, run/, inbox/, requests/, copies/, uploads/, staging/ live here
ALIAS_FILE = Path("/etc/fleet/alias")
# The machine's legacy code, made once by bootstrap.sh: the worlds keyed before
# each world had its own got it, an order adopted from the public book still
# does, and ShowAdminCode shows it.
ADMIN_CODE_FILE = Path("/etc/fleet/admin-code")
# Each world's own admin code, one file per world (`codes/<id>`, root:fleetd
# 640): written here, read by the door to show it to a signed Oracle.
CODES_DIR = Path("/etc/fleet/codes")
SIGNERS_FILE = Path("/etc/fleet/signers")  # the Oracles' wallets this machine obeys, one per line
HOUSE_FILE_NAME = "signers.house"  # beside SIGNERS_FILE: the wallets of the house's signed list (signers.py)
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
# The worlds whose admin code changed and that must be restarted to read it:
# written before their key file changes, cleared once Docker restarted them.
RESTART_QUEUE = "restart.json"
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

# A world brought back from its zip (ImportWorld): the caps its zip must
# keep, each with the rule a refusal names. The worlds room repeats them to
# refuse early, in the browser; these are the law.
ZIP_MAX = 2 * 1024**3  # the zip itself, 2 GiB: zip-too-big
FILE_MAX = 1024**3  # one entry unpacked, 1 GiB: file-too-big
UNPACKED_MAX = 4 * 1024**3  # every entry unpacked, together, 4 GiB: unpacked-too-big
ENTRIES_MAX = 20_000  # entries in the zip: too-many-files
RATIO_MAX = 100  # unpacked / packed, for an entry of RATIO_FROM bytes or more: ratio
RATIO_FROM = 1024**2
SLOT_SECONDS = 3600  # an upload slot lives an hour; its zip must have arrived by then: slot-expired
RESERVE = 1024**3  # free disk always left after an import, 1 GiB: no-room
# The zip's index (its central directory), which zipfile holds whole in
# memory: 20 000 entries take a few MB; more is a bomb aimed at the memory.
INDEX_MAX = 64 * 1024**2
MANIFEST_MAX = 32 * 1024**2  # the fleet copy's world.json, read whole only below this
CHUNK = 1024**2  # every zip is read and written in pieces of this size
# What a world's folder holds, as the engine keeps it (numinia-hyperfy2
# src/server: the database, AssetsLocal's assets/, CollectionsLocal's
# collections/, Storage's storage.json). Only these are unpacked.
WORLD_FILES = frozenset({ENGINE_DB, "storage.json"})
WORLD_FOLDERS = ("assets/", "collections/")
ENGINE_TABLES = ("config", "users", "blueprints", "entities")
IMPORTING = "import.json"  # run/: the world being unpacked right now, for the door to show
IMPORTS = ("ImportWorld", "ImportVersion")  # the requests that bring a zip: its upload slot, its marker in IMPORTING
UPDATING = "updating.json"  # run/: the worlds moved to a new build, until Docker has started it
SWITCHING = "switching.json"  # run/: the worlds whose published version is being switched, until the folders have moved

# Every pattern is used with re.fullmatch: `$` alone would let a trailing
# newline through, and a newline is how text turns into a new setting.
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
HOST = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
IMAGE = re.compile(
    r"ghcr\.io/numengames/[a-z0-9]+(?:[._/-][a-z0-9]+)*"  # the only registry an engine may come from
    r"(?::sha-[0-9a-f]{7,40}|@sha256:[0-9a-f]{64})"  # pinned to one build, never a moving tag
)
# Why a world is held on a build: one line of text, no control characters.
REASON = re.compile(r"[^\x00-\x1f\x7f]{1,200}")
MEMORY = re.compile(r"[0-9]+(?:\.[0-9]+)?[mg]")
CPUS = re.compile(r"[0-9]+(?:\.[0-9]+)?")
STATES = ("running", "stopped")
# The engines a machine runs, each known by its image's repository in the
# house's registry. A new request must name one of them (unknown-engine); an
# order already in a book with any other repository of the registry runs the
# way every order ran before engines were told apart: Hyperfy's.
REGISTRY = "ghcr.io/numengames/"
HYPERFY = "numinia-hyperfy2"
NUMINIA_ENGINE = "numinia-engine"
ENGINES = (HYPERFY, NUMINIA_ENGINE)
# The Numinia Engine's image (numinia-engine `Dockerfile`): it runs as its own
# user, never root, listens on one port and keeps the world in one folder.
ENGINE_UID = ENGINE_GID = 10001
ENGINE_USER = f"{ENGINE_UID}:{ENGINE_GID}"
ENGINE_PORT = 8790
ENGINE_FOLDER = "/mundo"
# Paths of an engine world's site that stay private: Caddy answers 404.
ENGINE_PRIVATE = ("/metrics*", "/sesiones*")
# The engine writes what it holds when it is told to stop (SIGTERM) and exits
# within 10 s; Docker waits this long before it kills it.
ENGINE_STOP_GRACE = "15s"
# The engine's own tools for a world's zip (`copia`, `comprobar`, `importar`),
# each run in a container of the world's image, with no network: the longest one may take.
ENGINE_TOOLS = ("copia", "comprobar", "importar")
TOOL_SECONDS = 1800
# numinia.com's own public key for the signed world pass (numinia-engine
# packages/server/src/pase.ts `leerClave`: raw 32 bytes base64url, or this
# whole document). Every Numinia Engine world gets it, the same, as
# MOTOR_PASE_CLAVE — without it the engine ignores every pass and "Entrar"
# silently makes everyone a guest. numinia.com re-publishes it at
# /.well-known/world-pass-key.json if it ever rotates.
WORLD_PASS_KEY = json.dumps({"alg": "Ed25519", "kid": "24673ddb", "publicKey": "tK59bXebbNZxn9wxiHkyOFMAiSHCYfm1rRKWYRez2O0"})
# The three tallas fleet-pricing/ESTUDIO-precio-de-los-mundos.md §5.1 prices
# (Pequeño, Mediano, Grande), each an order's memory and cpus tope, with the
# people cap the machine sets for it — a little over what the talla promises
# the client (20/50/100), so the machine turns away whoever is over the cap
# before the world itself gets slow. An order whose memory and cpus match
# neither tier (today's one legacy tariff, still 2g/1.5) carries no cap yet.
TALLAS = (
    ("0.75g", "0.25", 25),  # Pequeño: up to 20 people
    ("1.5g", "0.5", 60),  # Mediano: up to 50 people
    ("2g", "1", 120),  # Grande: up to 100 people
)


def fit_cpus(cpus: str, host: int | None = None) -> str:
    """An order's cpus tope as Docker will take it on this machine: never more than the machine has.

    Docker refuses a container whose cpus tope is over the machine's own count
    ("range of CPUs is from 0.01 to 2.00"), and one refused service stops
    `docker compose up` for every world in the book: no world starts, stops,
    updates or is deleted until that order changes. So a tope larger than the
    machine runs at the machine's size, and the order itself keeps what was
    asked (a bigger machine would give it all).
    """
    host = (os.cpu_count() or 1) if host is None else host
    return str(host) if float(cpus) > host else cpus


def people_cap(limits: dict) -> int | None:
    """The people cap an order's talla (its memory and cpus tope) sets; None for a talla not in TALLAS yet."""
    for memory, cpus, cap in TALLAS:
        if limits.get("memory") == memory and limits.get("cpus") == cpus:
            return cap
    return None
# What may be written into a world's key file. No `=`, `$`, quote, space or
# newline can pass: in an env file any of them could add or bend a setting.
ADMIN_CODE = re.compile(r"[A-Za-z0-9_-]{8,64}")
# A line of a key file that sets the admin code, in any form an env file
# allows (`export`, spaces around `=`, no value at all): a change of code
# replaces every such line with one plain `ADMIN_CODE=<code>`.
ADMIN_CODE_LINE = re.compile(r"[ \t]*(?:export[ \t]+)?ADMIN_CODE[ \t]*(?:=.*)?")
JWT = re.compile(r"[0-9a-f]{64}")
SWITCH_FIELDS = {"nonce", "by", "from", "to", "inoData", "inoTo"}  # what a job in run/switching.json holds
NONCE = re.compile(r"[0-9a-f]{32}")  # a request's number: 128 random bits, chosen by the browser
NONCE_ZIP = re.compile(r"[0-9a-f]{32}\.zip")  # staging/<nonce>.zip: the zip a version's duplicate is making
COPY_ID = re.compile(r"[0-9a-f]{32}")  # a copy's number: 128 random bits, chosen by the door
ADDRESS = re.compile(r"0x[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")  # a zip's fingerprint, as the browser computed and the Oracle signed it
STAMP = re.compile(r"[0-9]{8}-[0-9]{6}")  # a nightly copy's stamp: YYYYMMDD-HHMMSS, UTC (nightly.py names its zips by it)
VERSION = re.compile(r"v[1-9][0-9]{0,3}")  # a world's version: v1, v2, … never reused within a world
# A name inside a zip that starts with a drive (C:, c:\…): a Windows path, never a world's.
DRIVE = re.compile(r"[A-Za-z]:.*", re.DOTALL)
# The shape every string field of a signed request must have.
FIELD_RULES = {
    "machine": SLUG,
    "world": SLUG,
    "domain": HOST,
    "image": IMAGE,
    "reason": REASON,
    "memory": MEMORY,
    "cpus": CPUS,
    "nonce": NONCE,
    "zipSha256": SHA256,
    "version": VERSION,
    "stamp": STAMP,
    "replace": VERSION,  # an ImportVersion may also leave it empty: request_problem
}
# The requests about one of a world's versions, and the one that brings a zip in as a version.
VERSION_REQUESTS = ("DuplicateVersion", "PublishVersion", "DeleteVersion", "DownloadVersion")


def matches(pattern: re.Pattern[str], value: object) -> bool:
    """True only for a string the whole of which the pattern matches."""
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def engine_of(image: object) -> str | None:
    """The engine an image runs, by its repository: numinia-hyperfy2 or numinia-engine; None for any other."""
    if not isinstance(image, str) or not matches(IMAGE, image):
        return None
    repository = re.split(r"[:@]", image, maxsplit=1)[0]
    name = repository[len(REGISTRY):]
    return name if name in ENGINES else None


def runs_engine(order: dict) -> bool:
    """Whether an order runs the Numinia Engine; every other order runs as Hyperfy, as all did before."""
    return engine_of(order.get("image")) == NUMINIA_ENGINE


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


def read_wallets(path: Path) -> set[str]:
    """The lowercase 0x addresses of one file, one per line; `#` starts a comment and anything else is ignored."""
    if not path.is_file():
        return set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return set()
    found = set()
    for line in lines:
        entry = line.split("#", 1)[0].strip().lower()
        if matches(ADDRESS, entry):
            found.add(entry)
    return found


def read_signers(path: Path = SIGNERS_FILE, house: Path | None = None) -> frozenset[str]:
    """The wallets this machine obeys: the union of the wallets put there by hand (`path`) and the house's signed
    list (`house`, by default `signers.house` beside `path`, written by signers.py). Each file may be missing or
    broken without costing the other's wallets."""
    house = path.with_name(HOUSE_FILE_NAME) if house is None else house
    return frozenset(read_wallets(path) | read_wallets(house))


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
    if "org" in order and not matches(SLUG, order.get("org")):
        return "org is not lowercase letters, digits and hyphens"
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
        **({"org": order["org"]} if "org" in order else {}),
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
    """An admin code kept in a file — the machine's legacy one, or a world's own — or None.

    None when the file is missing or holds anything but a plain code. With no
    legacy code this machine keys no adopted order: a world with no key file
    then stays down rather than start with an empty admin code (everyone an
    admin).
    """
    if not path.is_file():
        return None
    code = path.read_text(encoding="utf-8").strip()
    return code if matches(ADMIN_CODE, code) else None


def make_code() -> str:
    """A world's own admin code: 16 hex characters, 64 random bits from the system.

    What `secrets.token_hex(8)` returns, made the way this file makes every
    other key (`os.urandom`, then hex).
    """
    return os.urandom(8).hex()


def world_env(admin_code: str, jwt: str) -> str:
    """A world's key file: its own JWT secret and its admin code, both plain."""
    if not matches(ADMIN_CODE, admin_code) or not matches(JWT, jwt):
        raise ValueError("a key is not a plain code")
    return f"JWT_SECRET={jwt}\nADMIN_CODE={admin_code}\n"


def env_lines(text: str) -> list[str]:
    """A key file's lines, split as Docker reads an env file: on newlines only."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def env_code(text: str) -> str | None:
    """The admin code a world's key file gives the engine, or None.

    Only one plain `ADMIN_CODE=<code>` line counts: none, several, or one in
    any other form is no code the machine can vouch for.
    """
    found = [line for line in env_lines(text) if ADMIN_CODE_LINE.fullmatch(line)]
    if len(found) != 1 or not found[0].startswith("ADMIN_CODE="):
        return None
    code = found[0][len("ADMIN_CODE="):]
    return code if matches(ADMIN_CODE, code) else None


def with_code(text: str, admin_code: str) -> str:
    """A world's key file with `admin_code` as its admin code, every other line kept as it is.

    Its JWT secret stays — everyone who has been in the world keeps who they
    are — and so do lines added by hand (voice). Every line that sets the
    admin code, in any form, becomes one plain line where the first was.
    """
    if not matches(ADMIN_CODE, admin_code):
        raise ValueError("an admin code is not a plain code")
    lines, placed = [], False
    for line in env_lines(text):
        if ADMIN_CODE_LINE.fullmatch(line):
            if not placed:
                lines.append(f"ADMIN_CODE={admin_code}")
                placed = True
            continue
        lines.append(line)
    if not placed:
        lines.append(f"ADMIN_CODE={admin_code}")
    return "\n".join(lines) + "\n"


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
        if runs_engine(o):
            services[wid] = engine_service(o, home)
            continue
        services[wid] = {
            "image": o["image"],
            "restart": "unless-stopped",
            "env_file": f"{home}/env/{wid}.env",
            "environment": engine_environment(f"https://{domain}", f"wss://{domain}", limits["maxUploadMb"]),
            "volumes": [f"{home}/data/{wid}:/app/world"],
            "deploy": {"resources": {"limits": {"memory": limits["memory"], "cpus": fit_cpus(limits["cpus"])}}},
        }
    return {"services": services}


def engine_service(o: dict, home: Path) -> dict:
    """A Numinia Engine world's service: its own user, its keys from its key file, its folder at /mundo.

    The engine reads JWT_SECRET and ADMIN_CODE from the key file (its
    MOTOR_SECRET and MOTOR_ADMIN fall back to them) and needs no setting of
    its own beyond the pass: its image says its port. No ports here either:
    Caddy reaches it on the machine's own network. MOTOR_PASE_CLAVE and
    MOTOR_PASE_MUNDO are numinia.com's world pass (H3b): without them the
    engine ignores every pass and "Entrar" silently makes everyone a guest.
    MOTOR_PASE_ORG follows only when the order names an organization — no
    signed request sets `org` yet (see open-worlds/machine/README.md).
    MOTOR_TOPE_PERSONAS follows only when the order's talla (its memory and
    cpus tope) is one of the three in fleet-pricing/ESTUDIO-precio-de-los-
    mundos.md §5.1: the people cap the machine sets for that talla, for the
    engine to read and impose over the owner's own saved setting (a later,
    numinia-engine change — not made here).
    """
    wid, limits = o["id"], o["limits"]
    cap = people_cap(limits)
    return {
        "image": o["image"],
        "restart": "unless-stopped",
        "user": ENGINE_USER,
        "env_file": f"{home}/env/{wid}.env",
        "environment": {
            "MOTOR_PASE_CLAVE": WORLD_PASS_KEY,
            "MOTOR_PASE_MUNDO": wid,
            **({"MOTOR_PASE_ORG": o["org"]} if "org" in o else {}),
            **({"MOTOR_TOPE_PERSONAS": str(cap)} if cap is not None else {}),
        },
        "volumes": [f"{home}/data/{wid}:{ENGINE_FOLDER}"],
        "stop_grace_period": ENGINE_STOP_GRACE,
        "deploy": {"resources": {"limits": {"memory": limits["memory"], "cpus": fit_cpus(limits["cpus"])}}},
    }


def render_compose(running: list[dict], home: Path) -> str:
    """The compose file as JSON — valid YAML that no field can restructure."""
    return json.dumps(compose_dict(running, home), indent=2) + "\n"


def world_site(o: dict) -> str:
    """A world's site: HTTPS at its address, its connections kept open across a reload."""
    if runs_engine(o):
        return engine_site(o)
    return (f"{o['domain']} {{\n\treverse_proxy {o['id']}:3000 {{\n"
            f"\t\tstream_close_delay {STREAM_CLOSE_DELAY}\n\t}}\n}}")


def engine_site(o: dict) -> str:
    """A Numinia Engine world's site: compressed, its metrics and sessions private, its connections kept across a reload."""
    private = "".join(f"\trespond {path} 404\n" for path in ENGINE_PRIVATE)
    return (f"{o['domain']} {{\n\tencode zstd gzip\n{private}"
            f"\treverse_proxy {o['id']}:{ENGINE_PORT} {{\n\t\tstream_close_delay {STREAM_CLOSE_DELAY}\n\t}}\n}}")


def door_site(control_host: str) -> str:
    """The door's site: a world's zip in on its own route, up to the import cap; everything else 64 KB at most.

    Caddy enforces both: a body over its route's cap is cut at the edge,
    before the door reads it. Only `/fleet/v1/uploads/*` (PUT of a slot's
    zip) takes the big one; signed requests stay small.
    """
    door = f"\t\treverse_proxy unix/{DOOR_SOCKET_INSIDE}\n"
    return (
        f"{control_host} {{\n"
        f"\thandle /fleet/v1/uploads/* {{\n\t\trequest_body {{\n\t\t\tmax_size {ZIP_MAX}\n\t\t}}\n{door}\t}}\n"
        f"\thandle {{\n\t\trequest_body {{\n\t\t\tmax_size 64KB\n\t\t}}\n{door}\t}}\n"
        f"}}"
    )


def render_caddyfile(running: list[dict], control_host: str | None = None) -> str:
    """One site per running world, plus the door's; Caddy issues each certificate."""
    blocks = ["# Generated by open-worlds/machine/reconcile.py — do not edit, change the order."]
    if control_host is not None:
        # The door: small signed requests in, facts and copies out, and a world's zip in.
        blocks.append(door_site(control_host))
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

    `admin_code` is the machine's legacy code (main reads it from
    /etc/fleet/admin-code). A world created through the door already has its
    key file, with its own code; a running world that has none (an order
    adopted from the public book) is listed in `new_keys` and started when
    there is a legacy code, and stays down, listed as `keyless`, when there
    is not.
    """
    if allowed is None:
        allowed = read_allowed_domains()
    mine = select_orders(read_orders(orders_dir), alias, allowed, control_host)
    ready, keyless = split_by_keys(mine, home / "env")
    # A world whose versions are being switched is left out of the book: Docker
    # takes its container down, and nothing starts it on a folder in motion.
    switching = sorted(read_switching(home))
    ready = [o for o in ready if o["id"] not in switching]
    keyless = [o for o in keyless if o["id"] not in switching]
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
        "switching": switching,
        # The running worlds of the Numinia Engine: their folders are the engine user's.
        "engine_worlds": [o["id"] for o in running if runs_engine(o)],
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


def save_waits(
    before: object, after: dict, keep: set[str] | frozenset[str], restart: set[str] | frozenset[str] = frozenset()
) -> dict[str, int]:
    """The running worlds Docker is about to stop or restart, each with the wait for its next save.

    The engine writes what was placed, moved or edited in a world to disk every
    SAVE_INTERVAL seconds, and not when it is told to stop: a world stopped
    right after an edit would lose it. `before` is the compose file the
    running containers came from, `after` the one about to be applied, `keep`
    the worlds that still have an order, or are being switched — a deleted world needs no save —
    and `restart` the worlds restarted even though their service is the same
    (their admin code changed).
    """
    old = before.get("services") if isinstance(before, dict) else None
    new = after.get("services", {})
    waits = {}
    for name, service in (old.items() if isinstance(old, dict) else []):
        if name == CADDY_SERVICE or name not in keep or not isinstance(service, dict):
            continue
        if new.get(name) == service and name not in restart:
            continue
        if engine_of(service.get("image")) == NUMINIA_ENGINE:
            # The Numinia Engine writes what it holds when it is told to stop.
            waits[name] = SAVE_MARGIN
            continue
        environment = service.get("environment")
        waits[name] = save_pause(environment.get("SAVE_INTERVAL") if isinstance(environment, dict) else None)
    return waits


def restarting(before: object, after: dict, queued: list[str]) -> list[str]:
    """The queued worlds to restart now: those running before this run and after it.

    A queued world that is stopped, deleted or started in this run needs no
    restart: its container goes, or is made anew from its new key file.
    """
    old = before.get("services") if isinstance(before, dict) else None
    old = old if isinstance(old, dict) else {}
    new = after.get("services", {})
    return sorted(w for w in set(queued) if w != CADDY_SERVICE and isinstance(old.get(w), dict) and w in new)


def read_restarts(home: Path) -> list[str]:
    """The worlds waiting to be restarted for their new admin code (run/restart.json)."""
    raw = read_json(home / "run" / RESTART_QUEUE, [])
    return sorted({w for w in raw if matches(SLUG, w)}) if isinstance(raw, list) else []


def queue_restart(home: Path, wid: str) -> None:
    """Remember that a world must be restarted to read its new admin code.

    Written before its key file changes: a run that stops halfway still
    restarts it on the next one, so an old code never outlives its change.
    """
    write_json(home / "run" / RESTART_QUEUE, sorted(set(read_restarts(home)) | {wid}))


def sh(*cmd: str) -> None:
    # Never the caller's input: the bootstrap is piped into bash (`curl … | bash`),
    # and a child that reads stdin (`docker compose exec` does by default)
    # swallowed the rest of the script, which then ended halfway without a word.
    subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL)


def engine_folder(path: Path) -> None:
    """A Numinia Engine world's folder: made when missing, the engine user's (10001:10001), mode 700.

    The engine runs as that user, never as root, and reads and writes the
    world there; nobody else on the machine but root reads it. A link where
    the folder should be is left alone, never followed.
    """
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        return
    if os.geteuid() == 0:
        os.chown(path, ENGINE_UID, ENGINE_GID, follow_symlinks=False)
    os.chmod(path, 0o700)


def apply(p: dict, home: Path, admin_code: str | None = None, sleep=time.sleep,
          codes_dir: Path | None = None) -> None:
    """Make Docker and Caddy match the plan; restart, alone, each world whose admin code changed.

    `codes_dir` given, a world keyed here with the legacy code gets its code
    file at once (else the next run's sync_codes writes it).
    """
    if p["new_keys"]:
        if admin_code is None or not matches(ADMIN_CODE, admin_code):
            raise ValueError("worlds to key but no admin code on this machine")
        for wid in p["new_keys"]:
            if make_keys(home / "env", wid, admin_code) and codes_dir is not None:
                try:
                    write_code(codes_dir, wid, admin_code)
                except OSError:
                    print(f"{wid}: its code file could not be written; the next run tries again", file=sys.stderr)
    run = home / "run"
    run.mkdir(parents=True, exist_ok=True)
    engines = set(p.get("engine_worlds", ()))
    for wid in p["running"]:
        if wid in engines:
            engine_folder(home / "data" / wid)
        else:
            (home / "data" / wid).mkdir(parents=True, exist_ok=True)
    compose = run / "docker-compose.yml"
    compose_applied = run / COMPOSE_APPLIED
    # What the running containers came from: the copy Docker last accepted, or,
    # on a machine set up before that copy existed, the file the last run wrote.
    before = read_json(compose_applied, None) if compose_applied.exists() else read_json(compose, None)
    after = json.loads(p["compose"])
    queued = read_restarts(home)
    # The engine reads its key file only when its container starts: a world
    # whose admin code changed is restarted, after its next save like a stop.
    restart = restarting(before, after, queued)
    # A world being switched is not in `running`, yet Docker is about to stop it: it is kept, so it saves first.
    waits = save_waits(before, after, set(p["running"]) | set(p["stopped"]) | set(p.get("switching", ())),
                       set(restart))
    if waits:
        pause = max(waits.values())
        print(f"waiting {pause} s for {', '.join(sorted(waits))} to save before Docker stops or restarts them",
              file=sys.stderr)
        sleep(pause)
    compose.write_text(p["compose"], encoding="utf-8")
    (run / "Caddyfile").write_text(p["caddyfile"], encoding="utf-8")
    if restart:
        # Those worlds alone, and before the general `up` below, which then
        # finds them already current and restarts nobody a second time.
        sh("docker", "compose", "-f", str(compose), "up", "-d", "--force-recreate", "--no-deps", *restart)
        print(f"restarted {', '.join(restart)}: each reads its new admin code", file=sys.stderr)
    if queued:
        # Restarted now, or stopped, deleted or started anew in this run.
        (run / RESTART_QUEUE).unlink(missing_ok=True)
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


def write_file(path: Path, text: str, mode: int = 0o644, group: int | None = None) -> None:
    """Write a file whole and move it into place, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    if group is not None:
        os.chown(tmp, 0, group)
    os.replace(tmp, path)


def write_json(path: Path, data: object, mode: int = 0o644, group: int | None = None) -> None:
    """Write JSON whole and move it into place, so a reader never sees half a file."""
    write_file(path, json.dumps(data, indent=2) + "\n", mode, group)


def read_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Each world's own admin code: its key file, and the code file the door shows
# ---------------------------------------------------------------------------


def write_code(codes_dir: Path, wid: str, admin_code: str) -> None:
    """A world's code file, as the door reads it: the plain code, root:fleetd, mode 640."""
    if not matches(SLUG, wid) or not matches(ADMIN_CODE, admin_code):
        raise ValueError("not a world id, or not a plain code")
    group = door_gid()
    if not codes_dir.is_dir():
        # bootstrap.sh makes it; made here the same way if it is missing.
        codes_dir.mkdir(mode=0o750, parents=True)
        if group is not None:
            os.chown(codes_dir, 0, group)
    write_file(codes_dir / wid, admin_code + "\n", mode=0o640, group=group)


def give_code(home: Path, codes_dir: Path, wid: str, admin_code: str, fresh: bool = False) -> None:
    """Make `admin_code` a world's admin code: its key file, then its code file.

    The key file first: it is the truth, the file the engine reads; if it
    cannot be written nothing has changed. `fresh`, for a world just
    created: a new key file with a JWT secret of its own, in place of
    anything a world gone before it left under its name. Otherwise a world
    with a key file keeps every line of it but the admin code (with_code) —
    its JWT secret above all, so everyone who has been in the world keeps
    who they are — and a world with none gets one. A code file that cannot
    be written is written by the next run (sync_codes). The engine reads the
    new code when its container next starts.
    """
    if not matches(SLUG, wid) or not matches(ADMIN_CODE, admin_code):
        raise ValueError("not a world id, or not a plain code")
    env_dir = home / "env"
    path = env_dir / f"{wid}.env"
    if fresh:
        env_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_file(path, world_env(admin_code, os.urandom(32).hex()), mode=0o600)
    elif not make_keys(env_dir, wid, admin_code):
        write_file(path, with_code(path.read_text(encoding="utf-8"), admin_code), mode=0o600)
    try:
        write_code(codes_dir, wid, admin_code)
    except OSError:
        print(f"{wid}: its code file could not be written; the next run writes it from its key file",
              file=sys.stderr)


def sync_codes(home: Path, codes_dir: Path) -> list[str]:
    """Make each world's code file say the admin code its key file gives; the worlds whose file changed.

    The key file is the truth: the engine reads it. On the first run of this
    version this copies into codes/ the code each world already has — the
    machine's legacy one, for the worlds keyed before each had its own — so
    nobody's code changes until an Oracle changes it. A key file that gives
    no plain code leaves its world with no code file (the door then answers
    `no-admin-code`, and a signed change of code puts it right), and a code
    file with no key file goes. Never writes a key file.
    """
    env_dir = home / "env"
    given: dict[str, str | None] = {}
    unread = set()
    for path in sorted(env_dir.glob("*.env")) if env_dir.is_dir() else []:
        wid = path.name[: -len(".env")]
        if not matches(SLUG, wid):
            continue
        try:
            given[wid] = env_code(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            unread.add(wid)  # left as it is: a file it cannot read proves nothing
    changed = []
    for wid, admin_code in sorted(given.items()):
        target = codes_dir / wid
        if admin_code is None:
            if target.is_file() or target.is_symlink():
                target.unlink()
                changed.append(wid)
        elif read_admin_code(target) != admin_code:
            write_code(codes_dir, wid, admin_code)
            changed.append(wid)
    for path in sorted(codes_dir.iterdir()) if codes_dir.is_dir() else []:
        if matches(SLUG, path.name) and path.name not in given and path.name not in unread:
            if path.is_file() or path.is_symlink():
                path.unlink()
                changed.append(path.name)
    return sorted(changed)


# ---------------------------------------------------------------------------
# Signed requests: checked again, then applied to the book
# ---------------------------------------------------------------------------


# Signed records the machine fetches by itself: they are never requests to the door or the inbox.
RECORDS = ("FleetRelease", "FleetSigners")


def request_problem(kind: object, message: object) -> str | None:
    """Which rule a request's typed message breaks; None when its shape is right.

    The door and this file share it: a request is the same thing to both.
    """
    if kind not in walletsig.ACTIONS or kind in RECORDS:
        return "type is not a request this machine knows"
    fields = walletsig.ACTIONS[kind]
    if not isinstance(message, dict) or set(message) != {name for name, _ in fields}:
        return "message does not have exactly the fields of its type"
    for name, kind_of in fields:
        value = message[name]
        if kind == "ImportVersion" and name == "replace":
            # Empty when the world has one version; else the unpublished one it takes the place of.
            if value != "" and not matches(VERSION, value):
                return "replace is neither empty nor a version"
        elif kind == "PinWorld" and name == "image":
            # The same rail as an update (the house registry, one build: a sha- tag or a digest);
            # an empty image releases the pin.
            if value != "" and not matches(IMAGE, value):
                return "image is neither empty nor a build of the house registry"
        elif kind_of == "string":
            if not matches(FIELD_RULES[name], value):
                return f"{name} does not have its shape"
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return f"{name} is not a whole number"
    if "maxUploadMb" in message and not 1 <= message["maxUploadMb"] <= MAX_UPLOAD_MB:
        return "maxUploadMb is out of range"
    # A zip has at least one byte; one over the cap is refused by name
    # (zip-too-big), after the signature, by the door and again here.
    if "zipBytes" in message and message["zipBytes"] < 1:
        return "zipBytes is out of range"
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
    if kind in VERSION_REQUESTS and matches(VERSION, message.get("version")):
        entry["version"] = message["version"]
    if kind == "DownloadNightly" and matches(STAMP, message.get("stamp")):
        entry["stamp"] = message["stamp"]
    problem = request_problem(kind, message)
    if problem:
        return entry, None, problem
    if message["machine"] != alias:
        return entry, None, "the request names another machine"
    # An import reaches the inbox only once its zip has arrived, which may
    # be up to a slot's hour after the door took it: it has that hour more.
    late = GRACE + (SLOT_SECONDS if kind in ("ImportWorld", "ImportVersion") else 0)
    if message["expires"] < now - late:
        return entry, None, "the request expired"
    signer = walletsig.signer_of(kind, message, raw.get("signature"))
    if signer is None:
        return entry, None, "the signature does not hold"
    if signer not in signers:
        return entry, None, "the signer is not an Oracle of this machine"
    entry["by"] = signer
    if kind in ("DownloadWorld", "DownloadVersion", "DownloadNightly"):
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
    if kind in ("ShowAdminCode", "ShowWorldCode"):
        return None  # the door showed the code; this is the record of it, and nothing changes
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
        if engine_of(message["image"]) is None:
            return "unknown-engine"
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
    # DownloadWorld: the copy is made after the containers. RotateWorldCode:
    # the book stays as it is; the new code is given right after (key_world).
    return None


def pull_image(image: str) -> None:
    """Fetch an engine build onto the machine before an order names it (a failed fetch raises)."""
    sh("docker", "pull", image)


def update_world(entry: dict, message: dict, orders: Path, now: float, pull=None) -> str | None:
    """Move a world to another build of its own engine: the broken rule, or None when its order names the new image.

    The image must be of the world's own engine (engine-mismatch), one this
    machine knows (unknown-engine); it is fetched before the order changes,
    so a build the registry does not have never reaches the compose file —
    where it would stop Docker from taking the book for every world. Its
    container is made anew by the next `docker compose up`, after the
    world's save, like any change to its service. Any build of the same
    engine is allowed, an older one too: that is how an update is undone.
    """
    path = orders / f"{entry['world']}.json"
    current = read_order(path)
    if current is None:
        return "no world with this name on this machine"
    engine, now_runs = engine_of(message["image"]), engine_of(current.get("image"))
    if engine is None or now_runs is None:
        return "unknown-engine"
    if engine != now_runs:
        return "engine-mismatch"
    if current.get("image") != message["image"]:
        try:
            (pull or pull_image)(message["image"])
        except (subprocess.CalledProcessError, OSError):
            return "the image could not be fetched here"
    write_json(path, {**current, "image": message["image"], "updatedAt": utc(now), "updatedBy": entry["by"]})
    return None


def pin_world(entry: dict, message: dict, orders: Path, now: float, pull=None) -> str | None:
    """Hold a world on one build (a digest) or, with an empty image, let it follow the fleet again.

    Holding moves the world to that build exactly like an update (same
    rules, pull first) and writes `pin` on its order, which release.py
    honours: it never moves a pinned world. Releasing only removes the pin;
    the next release pass moves the world. Any pin or release is a new
    signed request, so the reason of the last one is the one that stays.
    """
    path = orders / f"{entry['world']}.json"
    current = read_order(path)
    if current is None:
        return "no world with this name on this machine"
    if message["image"]:
        rule = update_world(entry, message, orders, now, pull)
        if rule is not None:
            return rule
        current = read_order(path) or current
        write_json(path, {**current, "pin": {"image": message["image"], "reason": message["reason"],
                                             "at": utc(now), "by": entry["by"]}})
        return None
    current.pop("pin", None)
    write_json(path, current)
    return None


def read_updates(home: Path) -> dict[str, dict]:
    """The worlds whose new build is being started (run/updating.json): id → the request that asked."""
    raw = read_json(home / "run" / UPDATING, {})
    found = {}
    for wid, job in (raw.items() if isinstance(raw, dict) else []):
        if matches(SLUG, wid) and isinstance(job, dict) and matches(NONCE, job.get("nonce")):
            found[wid] = job
    return found


def finish_updates(home: Path, now: float | None = None) -> list[str]:
    """Answer the updates whose new build Docker took: done. Only once the book was applied."""
    pending = read_updates(home)
    for wid, job in sorted(pending.items()):
        answer(home, {"nonce": job["nonce"], "type": job.get("type") if job.get("type") == "PinWorld" else "UpdateWorld", "world": wid, "by": job.get("by")}, "done", None, now)
    (home / "run" / UPDATING).unlink(missing_ok=True)
    return sorted(pending)


def read_switching(home: Path) -> dict[str, dict]:
    """The worlds whose published version is being switched (run/switching.json): id → the job.

    A job is `{nonce, by, from, to, inoData, inoTo}` (and `stuck: true` once a
    person has been told it cannot go on). Every field is checked; an entry
    that is not whole is ignored, as is a file that is not an object.
    """
    raw = read_json(home / "run" / SWITCHING, {})
    found = {}
    for wid, job in (raw.items() if isinstance(raw, dict) else []):
        if not matches(SLUG, wid) or not isinstance(job, dict) or set(job) - {"stuck"} != SWITCH_FIELDS:
            continue
        inodes = (job["inoData"], job["inoTo"])
        if (
            matches(NONCE, job["nonce"])
            and (job["by"] is None or matches(ADDRESS, job["by"]))
            and matches(VERSION, job["from"])
            and matches(VERSION, job["to"])
            and job["from"] != job["to"]
            and all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in inodes)
            and isinstance(job.get("stuck", False), bool)
        ):
            found[wid] = job
    return found


def write_switching(home: Path, jobs: dict[str, dict]) -> None:
    """Write the journal whole; no worlds left, no file."""
    path = home / "run" / SWITCHING
    if jobs:
        write_json(path, jobs)
    else:
        path.unlink(missing_ok=True)


def carried_code(raw: object, kind: object) -> tuple[str | None, str | None]:
    """The admin code the door made for a create, an import or a change of code: (code, None), or (None, the rule).

    A create from a door older than each world's own code carries none: the
    machine makes one (an import, which no such door takes, likewise). A
    change of code must carry the code the door already showed to the
    Oracle, and only a plain one is ever written.
    """
    if kind not in ("CreateWorld", "ImportWorld", "RotateWorldCode") or not isinstance(raw, dict):
        return None, None
    if "adminCode" not in raw:
        if kind != "RotateWorldCode":
            return make_code(), None
        return None, "the request carries no new admin code"
    if not matches(ADMIN_CODE, raw["adminCode"]):
        return None, "the admin code the door made is not a plain code"
    return raw["adminCode"], None


def key_world(home: Path, orders: Path, codes_dir: Path, entry: dict, admin_code: str) -> str | None:
    """Give a world just created, or whose code an Oracle changed, its admin code; the rule, or None.

    A change of code first queues the world's restart, so a run that stops
    halfway still restarts it and the old code cannot outlive the change. A
    create whose keys cannot be written is undone: no world without its code.
    """
    wid = entry["world"]
    created = entry["type"] == "CreateWorld"
    try:
        if not created:
            queue_restart(home, wid)
        give_code(home, codes_dir, wid, admin_code, fresh=created)
    except (OSError, ValueError):  # a full disk, a key file that is not text
        if created:
            for leftover in (orders / f"{wid}.json", codes_dir / wid, home / "env" / f"{wid}.env"):
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    print("a create whose keys could not be written could not be wholly undone", file=sys.stderr)
        return "the world's keys could not be written here"
    return None


def answer(home: Path, entry: dict, state: str, rule: str | None = None, now: float | None = None,
           detail: str | None = None) -> None:
    """The result the door shows for one request; a final one also goes to the machine's log.

    A failed one's log line names the rule it broke — a rule, never a value.
    `detail` is the entry of a zip a refused import names (its result says
    which file broke the rule, for whoever asked); it never reaches the log.
    """
    result = {"request": entry["nonce"], "type": entry["type"], "world": entry["world"], "state": state,
              "rule": rule, "at": utc(now)}
    if entry.get("version") is not None:
        result["version"] = entry["version"]
    if entry.get("stamp") is not None:
        result["stamp"] = entry["stamp"]  # a DownloadNightly's copy
    if entry.get("made") is not None:
        result["made"] = entry["made"]  # a DuplicateVersion's new version: the slot that appeared
    if detail is not None:
        result["entry"] = detail
    write_json(home / "requests" / f"{entry['nonce']}.json", result)
    if state == "working":
        return
    line = {
        "at": utc(now),
        "type": entry["type"],
        "world": entry["world"],
        "by": walletsig.short(entry["by"]) if entry.get("by") else None,
        "state": state,
    }
    if state == "failed":
        line["rule"] = rule
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
    *,
    codes_dir: Path,
    pull=None,
) -> list[dict]:
    """Every request waiting in the inbox, oldest first: checked again, applied, answered.

    Every request leaves the inbox, so the path unit that woke this run sleeps
    again. Create, stop, start, show and change of code are answered at once
    (the book and the key files are the truth; the containers follow them
    below). A create or a change of code writes the world's admin code the
    door made (codes_dir and its key file); a change of code also queues the
    world's restart. An import is unpacked here, whole, before the next
    request (import_world): its world is born stopped, so the containers have
    nothing to do for it. An update fetches its build and writes the order
    (update_world); a running world's waits in run/updating.json until Docker
    has started its new build (finish_updates). A delete waits in
    run/purge.json until its container is gone; a download waits in
    run/copy-queue.json. A DeleteVersion removes one unpublished version at
    once (versions.delete_version); a DuplicateVersion copies one into a new
    version before the next request (versions.duplicate_version; its result
    names the new version as `made`). A PublishVersion is checked and written
    to the journal run/switching.json (versions.publish_version) and answered
    `working`; finish_switches ends it once the world's container is gone.
    A DownloadVersion is checked at once (versions.download_version) and then
    waits in run/copy-queue.json like a DownloadWorld, its job naming the
    version. An ImportVersion is unpacked here, whole, before the next
    request, into a new unpublished version (versions.import_version; its
    result names the version it replaced as `version`, "" for none, and the
    new one as `made`); the live world is not touched.
    A DownloadNightly is checked at once (nightly.download_nightly: the
    copy must be a regular file in the world's nightly folder, which may
    outlive the world) and waits in the same queue, its job naming the
    stamp; make_copies copies that file.
    Every other request about a world the journal names (bar the records of
    a shown code) is refused `busy`. Returns the entries taken, for the summary.
    """
    now = time.time() if now is None else now
    inbox = home / "inbox"
    taken = []
    clear_imports(home, orders, now)
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
        admin_code = detail = None
        if rule is None:
            admin_code, rule = carried_code(raw, entry["type"])
        if rule is None:
            deleting = {w: job.get("domain") for w, job in purge.items() if isinstance(job, dict)}
            if entry["world"] in read_switching(home) and entry["type"] not in (
                    "ShowAdminCode", "ShowWorldCode", "DownloadNightly"):
                rule = "busy"  # its folders are moving; nothing else is done to it until they have
            elif entry["type"] == "ImportWorld":
                rule, detail = import_world(home, orders, codes_dir, entry, message, raw, admin_code, alias,
                                            allowed, control_host, now, deleting)
            elif entry["type"] == "UpdateWorld":
                rule = update_world(entry, message, orders, now, pull)
            elif entry["type"] == "PinWorld":
                rule = pin_world(entry, message, orders, now, pull)
            elif entry["type"] == "DeleteVersion":
                import versions  # here, not at the top: versions imports this file
                rule = versions.delete_version(home, orders, entry, message)
            elif entry["type"] == "DuplicateVersion":
                import versions  # here, not at the top: versions imports this file
                rule = versions.duplicate_version(home, orders, entry, message, now)
            elif entry["type"] == "PublishVersion":
                import versions  # here, not at the top: versions imports this file
                rule = versions.publish_version(home, orders, entry, message)
            elif entry["type"] == "DownloadVersion":
                import versions  # here, not at the top: versions imports this file
                rule = versions.download_version(home, orders, entry, message)
            elif entry["type"] == "ImportVersion":
                import versions  # here, not at the top: versions imports this file
                rule, detail = versions.import_version(home, orders, entry, message, raw, now)
            elif entry["type"] == "DownloadNightly":
                import nightly  # here, not at the top: nightly imports this file
                rule = nightly.download_nightly(home, message)
            else:
                rule = change_book(entry, message, orders, alias, allowed, control_host, now, deleting)
                if rule is None and admin_code is not None:
                    rule = key_world(home, orders, codes_dir, entry, admin_code)
        if entry["type"] in IMPORTS:
            # Its zip goes, whatever happened: taken, refused or never checked.
            forget_upload(home, entry["nonce"])
        if rule is not None:
            print(f"refuse one request: {rule}", file=sys.stderr)
            answer(home, entry, "failed", rule, now, detail)
            if entry["type"] in IMPORTS:
                (home / "run" / IMPORTING).unlink(missing_ok=True)
            continue
        taken.append(entry)
        if entry["type"] == "DeleteWorld":
            purge[entry["world"]] = {"nonce": entry["nonce"], "by": entry["by"], "domain": message["domain"]}
            answer(home, entry, "working", None, now)
        elif entry["type"] in ("DownloadWorld", "DownloadVersion", "DownloadNightly"):
            job = {"copy": entry["copy"], "world": entry["world"], "nonce": entry["nonce"], "by": entry["by"]}
            record = {"copy": entry["copy"], "world": entry["world"], "state": "preparing", "at": utc(now)}
            if entry["type"] == "DownloadNightly":
                # The job names the nightly copy to bring out; make_copies copies that file.
                job["nightly"] = entry["stamp"]
            elif entry["type"] == "DownloadVersion":
                # The job names the version it copies; the record the door serves says so too.
                job["version"] = record["version"] = entry["version"]
            queue.append(job)
            write_json(home / "copies" / f"{entry['copy']}.json", record, mode=0o640, group=door_gid())
            answer(home, entry, "working", None, now)
        elif entry["type"] == "PublishVersion":
            # Done once the folders have moved and the world is back (finish_switches); until then the door says switching.
            answer(home, entry, "working", None, now)
        elif entry["type"] in ("UpdateWorld", "PinWorld") and message["image"] and (
                read_order(orders / f"{entry['world']}.json") or {}).get("state") == "running":
            # Done once Docker has started the new build (finish_updates); until then the door says updating.
            updates = read_updates(home)
            updates[entry["world"]] = {"nonce": entry["nonce"], "by": entry["by"], "image": message["image"],
                                       "type": entry["type"]}
            write_json(home / "run" / UPDATING, updates)
            answer(home, entry, "working", None, now)
        else:
            answer(home, entry, "done", None, now)
            if entry["type"] in IMPORTS:
                # Shown as being unpacked until its answer is written.
                (home / "run" / IMPORTING).unlink(missing_ok=True)
    if purge:
        write_json(home / "run" / "purge.json", purge)
    if queue:
        write_json(home / "run" / "copy-queue.json", queue)
    return taken


# ---------------------------------------------------------------------------
# A world brought back from its zip (ImportWorld)
# ---------------------------------------------------------------------------

# What zipfile raises on a zip it cannot read: a broken index or entry, a
# method it does not know, data that ends early or fails its CRC.
UNREADABLE = (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, NotImplementedError, OSError, RuntimeError,
              ValueError, struct.error, zlib.error)


class Refused(Exception):
    """A zip the machine does not take: the rule it breaks and, when one entry broke it, that entry."""

    def __init__(self, rule: str, entry: str | None = None) -> None:
        super().__init__(rule)
        self.rule = rule
        self.entry = entry


def unpacking(zip_bytes: int) -> int:
    """The most a zip of this size is allowed to unpack to: RATIO_MAX times it, and never over UNPACKED_MAX."""
    return min(zip_bytes * RATIO_MAX, UNPACKED_MAX)


def reservation(zip_bytes: int) -> int:
    """The disk an import may take until it is done: its zip, and what that zip unpacks to.

    The reconciler's own copy of the zip (take_zip) fits inside it: a zip of
    ZIP_MAX or less is allowed to unpack to at least its own size.
    """
    return zip_bytes + unpacking(zip_bytes)


def epoch(text: object) -> int | None:
    """Seconds since 1970 for a time written by utc(); None for anything else."""
    if not isinstance(text, str):
        return None
    try:
        return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def shown(name: str) -> str:
    """An entry's name as a refusal shows it: printable characters only, 200 at most."""
    return "".join(c if c.isprintable() else "?" for c in name)[:200]


def forget_upload(home: Path, nonce: str) -> None:
    """The door's zip for this request, and any slot or part file left for it, go."""
    for name in (f"{nonce}.zip", f"{nonce}.json", f".{nonce}.zip.part"):
        try:
            (home / "uploads" / name).unlink(missing_ok=True)
        except OSError:
            print("an upload's leftover could not be removed; the next run tries again", file=sys.stderr)


def take_zip(path: Path, size: int, sha256: str, spool: Path):
    """The door's zip, copied into a file of the machine's own while its SHA-256 is computed.

    Refused (fingerprint) unless it is exactly the size and the SHA-256 the
    Oracle signed. The copy has no name: nobody but this process can open
    it, so the zip that was checked is the zip that is unpacked — the door's
    user, who owns the upload folder, cannot change it after the check.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise Refused("fingerprint") from None  # no zip, or a link where it should be
    with os.fdopen(fd, "rb") as source:
        found = os.fstat(source.fileno())
        if not stat.S_ISREG(found.st_mode) or found.st_size != size:
            raise Refused("fingerprint")
        copy = tempfile.TemporaryFile(dir=spool)
        try:
            digest, copied = hashlib.sha256(), 0
            for chunk in iter(lambda: source.read(CHUNK), b""):
                copied += len(chunk)
                if copied > size:
                    raise Refused("fingerprint")
                digest.update(chunk)
                copy.write(chunk)
            if copied != size or digest.hexdigest() != sha256:
                raise Refused("fingerprint")
            copy.seek(0)
        except BaseException:
            copy.close()
            raise
    return copy


def zip_index(f) -> tuple[int, int] | None:
    """How many entries a zip's index lists, and the index's size, read at the zip's end; None for no zip.

    Read before zipfile parses the index, which it keeps whole in memory: a
    zip that lists millions of entries is refused without being parsed.
    """
    f.seek(0, os.SEEK_END)
    size = f.tell()
    start = max(0, size - (22 + 0xFFFF))
    f.seek(start)
    tail = f.read(size - start)
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or len(tail) - at < 22:
        return None
    fields = struct.unpack("<4s4H2LH", tail[at:at + 22])
    entries, index = fields[4], fields[5]
    locator = start + at - 20
    if locator >= 0:
        # ZIP64: the real counts live in a record a locator points to.
        f.seek(locator)
        found = f.read(20)
        if found[:4] == b"PK\x06\x07":
            f.seek(struct.unpack("<Q", found[8:16])[0])
            record = f.read(56)
            if len(record) == 56 and record[:4] == b"PK\x06\x06":
                entries, index = struct.unpack("<QQ", record[32:48])
    return entries, index


def open_zip(f) -> zipfile.ZipFile:
    """The zip in this file, opened once its index is known to be a world's size."""
    found = zip_index(f)
    if found is None:
        raise Refused("not-a-zip")
    if found[0] > ENTRIES_MAX or found[1] > INDEX_MAX:
        raise Refused("too-many-files")
    f.seek(0)
    try:
        zipped = zipfile.ZipFile(f)
    except UNREADABLE:
        raise Refused("not-a-zip") from None
    if len(zipped.infolist()) > ENTRIES_MAX:  # its end said fewer than it holds
        zipped.close()
        raise Refused("too-many-files")
    return zipped


def safe_name(info: zipfile.ZipInfo) -> bool:
    """A name that stays inside the folder it is unpacked into: relative, `/` between parts, no `..`.

    No absolute path, no drive (C:), no backslash, no empty, `.` or `..`
    part: an honest zip tool never writes one, and each is a way out.
    """
    name = info.filename
    if not name or name.startswith("/") or "\\" in name or matches(DRIVE, name) or "\x00" in info.orig_filename:
        return False
    parts = (name[:-1] if name.endswith("/") else name).split("/")
    return all(part not in ("", ".", "..") for part in parts)


def plain_entry(info: zipfile.ZipInfo) -> bool:
    """A file or a folder: never a link, a device, a pipe or a socket (the Unix type in its attributes)."""
    kind = stat.S_IFMT(info.external_attr >> 16)
    return kind in ((0, stat.S_IFDIR) if info.filename.endswith("/") else (0, stat.S_IFREG))


def too_packed(info: zipfile.ZipInfo) -> bool:
    """An entry of RATIO_FROM bytes or more that grows over RATIO_MAX times when unpacked: a zip bomb."""
    return info.file_size >= RATIO_FROM and info.file_size > RATIO_MAX * info.compress_size


def world_root(names: list[str]) -> str | None:
    """Where the world's folder starts in the zip: 'world/', a single top folder, or '' (the zip's root).

    The first that holds db.sqlite: `world/` (the fleet's own copy and the
    Alchemists' Tower's backup), the one top folder that holds it (a zipped
    world folder), the zip's root. None: no world in it.
    """
    present = set(names)
    if f"world/{ENGINE_DB}" in present:
        return "world/"
    tops = {name.split("/", 1)[0] + "/" for name in names if "/" in name}
    holding = [top for top in tops if f"{top}{ENGINE_DB}" in present]
    if len(holding) == 1:
        return holding[0]
    return "" if ENGINE_DB in present else None


def place(relative: str) -> str:
    """What becomes of an entry inside the world's folder: a kept 'file' or 'folder', 'skip', or the rule it breaks."""
    if relative.endswith("/"):
        return "folder" if relative.startswith(WORLD_FOLDERS) else "unexpected"
    if relative in WORLD_FILES or relative.startswith(WORLD_FOLDERS):
        return "file"
    if relative == f"{ENGINE_DB}-shm":
        return "skip"  # SQLite's shared-memory index: rebuilt when the database opens
    if relative in (f"{ENGINE_DB}-wal", f"{ENGINE_DB}-journal"):
        return "db-open"  # zipped while the world ran: its database is half written
    return "unexpected"


def check_entries(infos: list[zipfile.ZipInfo]) -> int:
    """The rules every zip keeps, whichever engine it is for; what its entries unpack to, or Refused.

    Reads the index alone. In order, over every entry: encrypted, zip-slip,
    link, file-too-big, ratio; then unpacked-too-big for the declared total.
    """
    checks = (
        ("encrypted", lambda info: bool(info.flag_bits & 0x1)),
        ("zip-slip", lambda info: not safe_name(info)),
        ("link", lambda info: not plain_entry(info)),
        ("file-too-big", lambda info: info.file_size > FILE_MAX),
        ("ratio", too_packed),
    )
    for rule, broken in checks:
        for info in infos:
            if broken(info):
                raise Refused(rule, shown(info.filename))
    total = sum(info.file_size for info in infos)
    if total > UNPACKED_MAX:
        raise Refused("unpacked-too-big")
    return total


def inspect_zip(zipped: zipfile.ZipFile) -> dict:
    """What of this zip becomes the world, once every rule holds; Refused, naming the first rule broken.

    Reads the zip's index (and its world.json) and nothing else: no entry is
    unpacked and nothing is written while a zip is inspected. The rules, in
    order, over every entry, inside the world's folder or not: encrypted,
    zip-slip, link, file-too-big, ratio, then unpacked-too-big for the
    declared total (check_entries); then not-a-world, db-open and unexpected
    inside the world's folder; then the manifest.
    """
    infos = zipped.infolist()
    check_entries(infos)
    root = world_root([info.filename for info in infos])
    if root is None:
        raise Refused("not-a-world")
    places = [(info, place(info.filename[len(root):])) for info in infos
              if info.filename.startswith(root) and info.filename != root]
    for rule in ("db-open", "unexpected"):
        for info, where in places:
            if where == rule:
                raise Refused(rule, shown(info.filename))
    kept = [info for info, where in places if where in ("file", "folder")]
    return {"root": root, "kept": kept, "manifest": read_manifest(zipped, root, kept),
            "bytes": sum(info.file_size for info in kept)}


def read_manifest(zipped: zipfile.ZipFile, root: str, kept: list[zipfile.ZipInfo]) -> dict | None:
    """The size and SHA-256 of each file, as the fleet's own copy lists them in the world.json at its root.

    None when the zip has no such list (a Tower backup, a zipped folder):
    nothing is compared. A list that cannot be read is refused, and so is a
    kept file it does not list with the size the index declares, or a file it
    lists inside the world's folder that the zip does not hold (manifest).
    """
    try:
        info = zipped.getinfo("world.json")
    except KeyError:
        return None
    if info.file_size > MANIFEST_MAX:
        raise Refused("manifest", "world.json")
    try:
        text = zipped.read(info)
    except UNREADABLE:
        raise Refused("not-a-zip", "world.json") from None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        return None
    listed = {}
    for item in files:
        size = item.get("bytes") if isinstance(item, dict) else None
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str) or isinstance(size, bool)
                or not isinstance(size, int) or size < 0 or not matches(SHA256, item.get("sha256"))):
            raise Refused("manifest", "world.json")
        listed[item["path"]] = (size, item["sha256"])
    names = {info.filename for info in kept}
    for info in kept:
        if not info.filename.endswith("/") and listed.get(info.filename, (None,))[0] != info.file_size:
            raise Refused("manifest", shown(info.filename))
    for path in listed:
        if path.startswith(root) and place(path[len(root):]) == "file" and path not in names:
            raise Refused("manifest", shown(path))
    return listed


def unpack(zipped: zipfile.ZipFile, plan: dict, target: Path) -> None:
    """Write the kept entries under `target`, counting what is written and never trusting the index.

    Never through a link: every folder on the way is made here and every
    file is new (a name seen twice is refused). Over a cap while it writes:
    refused, whatever the index said; a file that is not what the manifest
    says: refused (manifest).
    """
    root, manifest = plan["root"], plan["manifest"]
    total = 0
    for info in plan["kept"]:
        parts = info.filename[len(root):].rstrip("/").split("/")
        folder = target
        for part in parts if info.filename.endswith("/") else parts[:-1]:
            folder = folder / part
            try:
                os.mkdir(folder, 0o755)
            except FileExistsError:
                if not stat.S_ISDIR(os.lstat(folder).st_mode):
                    raise Refused("unexpected", shown(info.filename)) from None
        if info.filename.endswith("/"):
            continue
        try:
            fd = os.open(folder / parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        except FileExistsError:
            raise Refused("unexpected", shown(info.filename)) from None
        digest, written = hashlib.sha256(), 0
        with os.fdopen(fd, "wb") as out:
            try:
                source = zipped.open(info)
            except UNREADABLE:
                raise Refused("not-a-zip", shown(info.filename)) from None
            with source:
                while True:
                    try:
                        chunk = source.read(CHUNK)
                    except UNREADABLE:
                        raise Refused("not-a-zip", shown(info.filename)) from None
                    if not chunk:
                        break
                    written += len(chunk)
                    total += len(chunk)
                    if written > FILE_MAX:
                        raise Refused("file-too-big", shown(info.filename))
                    if total > UNPACKED_MAX:
                        raise Refused("unpacked-too-big")
                    if manifest is not None:
                        digest.update(chunk)
                    out.write(chunk)
        if manifest is not None and manifest.get(info.filename) != (written, digest.hexdigest()):
            raise Refused("manifest", shown(info.filename))


def check_database(path: Path) -> None:
    """The world's database opens, passes SQLite's own integrity check, and has the engine's tables.

    Opened read-only and immutable (nothing beside it is created or read)
    with the schema untrusted: a hostile file is only ever read.
    """
    try:
        db = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            db.execute("PRAGMA trusted_schema = OFF")
            db.execute("PRAGMA cell_size_check = ON")
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise Refused("db-broken")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            db.close()
    except sqlite3.Error:
        raise Refused("db-broken") from None
    if not set(ENGINE_TABLES) <= tables:
        raise Refused("db-foreign")


# ---------------------------------------------------------------------------
# The Numinia Engine's own tools: its zip made and read by its image
# ---------------------------------------------------------------------------


def run_tool(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run one of an engine image's tools (subprocess.run; the tests put another in its place)."""
    return subprocess.run(cmd, check=False, **kwargs)


def engine_tool(image: str, tool: str, limits: dict, mount: str | None = None, stdin: bool = False) -> list[str]:
    """The `docker run` of one of the engine image's own tools (copia, comprobar, importar).

    The world's image, as the engine's own user, with no network, no
    capability, no new privilege and the world's limits; removed when it
    ends. `mount` is the one folder it sees, at /mundo. Every value was
    checked by its pattern before it got here.
    """
    if engine_of(image) != NUMINIA_ENGINE or tool not in ENGINE_TOOLS:
        raise ValueError("not a tool of the Numinia Engine")
    if not matches(MEMORY, limits.get("memory")) or not matches(CPUS, limits.get("cpus")):
        raise ValueError("limits are not plain")
    cmd = ["docker", "run", "--rm", "--network", "none", "--user", ENGINE_USER, "--cap-drop", "ALL",
           "--security-opt", "no-new-privileges", "--memory", limits["memory"], "--cpus", fit_cpus(limits["cpus"])]
    if stdin:
        cmd.append("-i")
    if mount is not None:
        cmd += ["-v", mount]
    return cmd + [image, tool]


def tool_line(done: subprocess.CompletedProcess) -> str:
    """The one line a tool gave for its answer: the last line it wrote on stderr, else on stdout; printable, 200 at most."""
    for stream in (done.stderr, done.stdout):
        text = stream.decode("utf-8", "replace") if isinstance(stream, bytes) else (stream or "")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            return shown(lines[-1])
    return "no reason given"


def engine_import(copy, target: Path, image: str, limits: dict) -> None:
    """Hand the machine's own copy of the zip to the engine: `comprobar`, then `importar` into the empty folder.

    The machine never re-implements the engine's rules for its zip: the
    image of the world's own engine says whether it reads the zip (exit 0)
    or not (exit 1 and one line why: engine-refused, that line to whoever
    asked, never to the log), then unpacks it. The generic rules
    (check_entries) and the fingerprint were kept before this runs.
    """
    if os.geteuid() == 0:
        os.chown(target, ENGINE_UID, ENGINE_GID, follow_symlinks=False)
    for tool, mount in (("comprobar", None), ("importar", f"{target}:{ENGINE_FOLDER}")):
        copy.seek(0)
        try:
            done = run_tool(engine_tool(image, tool, limits, mount, stdin=True), stdin=copy,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TOOL_SECONDS)
        except subprocess.TimeoutExpired:
            raise Refused("engine-refused", f"{tool}: it took longer than {TOOL_SECONDS} s") from None
        if done.returncode == 1:
            raise Refused("engine-refused", tool_line(done))
        if done.returncode != 0:
            raise Refused("the engine's tools could not run here")


def import_clash(home: Path, orders: Path, wid: str, domain: str, deleting: dict[str, str | None]) -> str | None:
    """Why a world cannot be born here under this name and address now (CreateWorld's rules); None when it can.

    A folder left under the name, with no order, counts as a world: an
    import never unpacks into what another world left.
    """
    path = orders / f"{wid}.json"
    if read_order(path) is not None or path.exists():
        return "exists"
    if wid in deleting or domain in deleting.values():
        return "still-deleting"
    if os.path.lexists(home / "data" / wid):
        return "exists"
    if any(o.get("domain") == domain for o in read_orders(orders)):
        return "address-taken"
    return None


def settle_import(home: Path, orders: Path, codes_dir: Path, staged: Path, order: dict, admin_code: str) -> None:
    """Move an unpacked, checked world into place: its folder, its keys, then its order — stopped.

    The order goes last: the world is in the book only once its data and its
    keys are. Fresh keys, as for a world just created: a JWT secret of its
    own and the code the door showed. What cannot be written is taken back.
    A Numinia Engine world's folder stays the engine user's, mode 700.
    """
    wid = order["id"]
    data = home / "data" / wid
    data.parent.mkdir(parents=True, exist_ok=True)
    # As a created world's folder; staging/ itself stays root's alone.
    os.chmod(staged, 0o700 if runs_engine(order) else 0o755)
    os.rename(staged, data)
    try:
        give_code(home, codes_dir, wid, admin_code, fresh=True)
        write_json(orders / f"{wid}.json", order)
    except (OSError, ValueError):
        shutil.rmtree(data, ignore_errors=True)
        for leftover in (orders / f"{wid}.json", codes_dir / wid, home / "env" / f"{wid}.env"):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                print("an import whose keys could not be written could not be wholly undone", file=sys.stderr)
        raise


def stage_import(home: Path, nonce: str, message: dict, image: str, limits: dict, target: Path) -> None:
    """Check an uploaded zip and unpack it into `target`, a new folder in staging/: the world's data, whole and checked.

    What an ImportWorld and an ImportVersion share, for the engine `image`
    runs. The zip becomes the machine's own copy, which must be the signed
    one (take_zip: fingerprint); its index must keep every rule
    (check_entries, and inspect_zip for a Hyperfy world) — nothing is
    written outside the upload folder until then; the disk must keep its
    reserve after the declared total (no-room). A Hyperfy world is unpacked
    (unpack) and its database checked (check_database); a Numinia Engine
    world is handed to its image's own tools (engine_import). The upload
    goes as soon as the machine has its own copy. Raises Refused, OSError or
    ValueError; the caller removes `target`, which may be half written.
    """
    staging = home / "staging"
    engine = engine_of(image)
    with take_zip(home / "uploads" / f"{nonce}.zip", message["zipBytes"], message["zipSha256"], staging) as copy:
        forget_upload(home, nonce)  # from here on only the machine's own copy is read
        with open_zip(copy) as zipped:
            if engine == NUMINIA_ENGINE:
                # The rules every zip keeps; the engine's own come next, from its image.
                plan = {"bytes": check_entries(zipped.infolist())}
            else:
                plan = inspect_zip(zipped)
            if shutil.disk_usage(staging).free < plan["bytes"] + RESERVE:
                raise Refused("no-room")
            os.mkdir(target, 0o700)
            if engine != NUMINIA_ENGINE:
                unpack(zipped, plan, target)
        if engine == NUMINIA_ENGINE:
            engine_import(copy, target, image, limits)
    if engine != NUMINIA_ENGINE:
        check_database(target / ENGINE_DB)


def import_world(
    home: Path,
    orders: Path,
    codes_dir: Path,
    entry: dict,
    message: dict,
    raw: dict,
    admin_code: str,
    alias: str,
    allowed: set[str] | frozenset[str],
    control_host: str | None,
    now: float,
    deleting: dict[str, str | None],
) -> tuple[str | None, str | None]:
    """Bring a world back from its uploaded zip, born stopped: (None, None), or (the rule, the entry or None).

    First what the door checked, again: the name and the address (exists,
    still-deleting, address-taken, refused) and the cap on the zip. Then the
    zip becomes the machine's own copy, which must be the signed one
    (take_zip: fingerprint); its index must keep every rule (inspect_zip) —
    nothing is written outside the upload folder until then; the disk must
    keep its reserve after the declared total (no-room); the world is
    unpacked into a root-only staging folder (unpack) and its database
    checked (check_database); only then is it moved to data/<id>/ with keys
    of its own and an order that says stopped (settle_import). Whatever
    fails, the staging folder goes and nothing stays.
    """
    wid, nonce = entry["world"], entry["nonce"]
    staging = home / "staging"
    target = staging / nonce
    engine = engine_of(message["image"])
    order = {
        "id": wid,
        "card": wid,
        "state": "stopped",
        "server": alias,
        "domain": message["domain"],
        "image": message["image"],
        "limits": {"memory": message["memory"], "cpus": message["cpus"], "maxUploadMb": message["maxUploadMb"]},
        "createdAt": utc(now),
        "createdBy": entry["by"],
    }
    try:
        rule = import_clash(home, orders, wid, message["domain"], deleting)
        if rule is None and engine is None:
            rule = "unknown-engine"
        if rule is None and refusal(order, allowed, control_host) is not None:
            rule = "refused"
        if rule is None and message["zipBytes"] > ZIP_MAX:
            rule = "zip-too-big"
        if rule is None and raw.get("zip") != f"{nonce}.zip":
            rule = "fingerprint"
        if rule is not None:
            return rule, None
        if not staging.is_dir():
            staging.mkdir(mode=0o700, parents=True)  # bootstrap.sh makes it; made here the same way if missing
        write_json(home / "run" / IMPORTING, {wid: {"nonce": nonce, "by": entry["by"], "message": message}})
        answer(home, entry, "working", None, now)
        stage_import(home, nonce, message, message["image"], order["limits"], target)
        settle_import(home, orders, codes_dir, target, order, admin_code)
    except Refused as refused:
        return refused.rule, refused.entry
    except (OSError, ValueError) as error:
        full = isinstance(error, OSError) and error.errno == errno.ENOSPC
        return ("no-room" if full else "the world could not be written here"), None
    finally:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
    return None, None


def clear_imports(home: Path, orders: Path, now: float) -> None:
    """What an import that stopped halfway left — its run died — goes before the inbox is read.

    Runs never overlap (systemd starts one at a time), so a world still
    marked as being unpacked, or a staging folder, at the start of a run is
    a leftover. Its request gets its answer: done if its order was written
    (the last step), failed otherwise. An ImportVersion's marker says so
    (`type`) and it is judged by its own last step (versions.clear_import).
    """
    marker = home / "run" / IMPORTING
    raw = read_json(marker, {})
    for wid, job in (raw.items() if isinstance(raw, dict) else []):
        if not matches(SLUG, wid) or not isinstance(job, dict) or not matches(NONCE, job.get("nonce")):
            continue
        result = read_json(home / "requests" / f"{job['nonce']}.json", {})
        if isinstance(result, dict) and result.get("state") in ("done", "failed"):
            continue
        entry = {"nonce": job["nonce"], "type": "ImportWorld", "world": wid, "by": job.get("by")}
        if job.get("type") == "ImportVersion":
            import versions  # here, not at the top: versions imports this file
            versions.clear_import(home, orders, entry, job, now)
        elif read_order(orders / f"{wid}.json") is not None:
            answer(home, entry, "done", None, now)
        else:
            answer(home, entry, "failed", "the run stopped in the middle of the import", now)
        forget_upload(home, job["nonce"])
    marker.unlink(missing_ok=True)
    staging = home / "staging"
    for path in sorted(staging.iterdir()) if staging.is_dir() else []:
        if matches(NONCE, path.name) and path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        elif matches(NONCE_ZIP, path.name) and (path.is_symlink() or path.is_file()):
            path.unlink(missing_ok=True)  # the zip a Numinia Engine duplicate was making (versions.copy_engine)


def prune_uploads(home: Path, now: float | None = None) -> list[str]:
    """Remove the upload slots past their hour, with their part files, and what no slot or request holds.

    The door removes an expired slot too, when an upload reaches it. A zip
    or a part file that nothing names any more (a door stopped halfway)
    goes once it has been still for a slot's hour. Returns the slots removed.
    """
    now = time.time() if now is None else now
    uploads = home / "uploads"
    if not uploads.is_dir():
        return []
    waiting = {p.name[: -len(".json")].rpartition("-")[2] for p in (home / "inbox").glob("*.json")}
    gone = []
    for path in sorted(uploads.iterdir()):
        name = path.name
        try:
            still = now - os.lstat(path).st_mtime > SLOT_SECONDS
        except OSError:
            continue
        doomed = False
        if name.endswith(".json") and matches(NONCE, name[: -len(".json")]):
            slot = read_json(path, None)
            ends = epoch(slot.get("expiresAt")) if isinstance(slot, dict) else None
            if ends is None or now > ends:
                forget_upload(home, name[: -len(".json")])
                gone.append(name[: -len(".json")])
        elif name.startswith(".") and name.endswith(".zip.part") and matches(NONCE, name[1: -len(".zip.part")]):
            doomed = still and not (uploads / f"{name[1: -len('.zip.part')]}.json").exists()
        elif name.endswith(".zip") and matches(NONCE, name[: -len(".zip")]):
            doomed = still and name[: -len(".zip")] not in waiting
        elif name.startswith(".") and name.endswith(".tmp"):
            doomed = still
        if doomed:
            try:
                path.unlink()
            except OSError:
                print("an upload's leftover could not be removed; the next run tries again", file=sys.stderr)
    return gone


# ---------------------------------------------------------------------------
# After the containers: deletes and copies
# ---------------------------------------------------------------------------


def purge_world(home: Path, wid: str, *, codes_dir: Path) -> None:
    """Remove a world's folder, every version, keys, code and copies. Only for a world with no order and no container."""
    if not matches(SLUG, wid):
        raise ValueError("not a world id")
    data_root = home / "data"
    folder = data_root / wid
    if folder.is_symlink():
        folder.unlink()
    elif folder.is_dir() and folder.resolve().parent == data_root.resolve():
        shutil.rmtree(folder)
    import versions  # here, not at the top: versions imports this file
    versions.purge_versions(home, wid)
    (home / "env" / f"{wid}.env").unlink(missing_ok=True)
    (codes_dir / wid).unlink(missing_ok=True)
    for status_path in (home / "copies").glob("*.json"):
        status = read_json(status_path, {})
        if isinstance(status, dict) and status.get("world") == wid:
            (home / "copies" / f"{status_path.stem}.zip").unlink(missing_ok=True)
            status_path.unlink(missing_ok=True)


def purge_deleted(home: Path, orders: Path, now: float | None = None, *, codes_dir: Path) -> list[str]:
    """Delete what the deleted worlds left, now that Docker took their containers down."""
    path = home / "run" / "purge.json"
    pending = read_json(path, {})
    done = []
    for wid, job in (pending.items() if isinstance(pending, dict) else []):
        if not matches(SLUG, wid) or not isinstance(job, dict) or not matches(NONCE, job.get("nonce")):
            continue
        if not (orders / f"{wid}.json").exists():  # ordered again since: its folder is the new world's
            purge_world(home, wid, codes_dir=codes_dir)
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


def copy_name(wid: str, version: str | None, now: float) -> str:
    """The file a copy is downloaded as: <id>-<UTC stamp>.zip, and <id>-<vid>-<UTC stamp>.zip for one version."""
    stamp = time.strftime("%Y%m%d-%H%M", time.gmtime(now))
    return f"{wid}-{stamp}.zip" if version is None else f"{wid}-{version}-{stamp}.zip"


def build_copy(home: Path, order: dict, copy_id: str, alias: str, now: float | None = None,
               folder: Path | None = None, version: str | None = None) -> dict:
    """Zip a world whole — its folder, a compose file, fresh keys, a manifest, a guide — and say so.

    The Alchemists' Tower's copy, on one machine: everything needed to run
    the world anywhere, with keys of its own, and the fingerprint of each file.
    `folder` and `version` make it the copy of one version: that version's
    folder (the world's own, data/<id>, when none is given) and a file named
    for it; the manifest and the copy's record say which version it is.
    """
    now = time.time() if now is None else now
    wid = order["id"]
    data = home / "data" / wid if folder is None else folder
    copies = home / "copies"
    copies.mkdir(parents=True, exist_ok=True)
    part = copies / f".{copy_id}.zip.part"
    filename = copy_name(wid, version, now)
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
        if version is not None:
            manifest["version"] = version
        zipped.writestr("world.json", json.dumps(manifest, indent=2) + "\n")
    return finish_copy(home, wid, copy_id, part, filename, now, version)


def finish_copy(home: Path, wid: str, copy_id: str, part: Path, filename: str, now: float,
                version: str | None = None) -> dict:
    """A copy's zip moved into place, readable by the door's group alone, and its state: ready.

    The record of a version's copy also says which version (`version`); a
    world's has no such field.
    """
    copies = home / "copies"
    final = copies / f"{copy_id}.zip"
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
    if version is not None:
        status["version"] = version
    write_json(copies / f"{copy_id}.json", status, mode=0o640, group=group)
    return status


def engine_copia(order: dict, data: Path, part: Path, mode: int = 0o640) -> None:
    """Run the engine's own `copia` on a world's folder (read-only) and leave its zip in `part`.

    The one place the machine asks an engine image for the world's zip: the
    download button (build_engine_copy) and the nightly copy (nightly.py)
    both come through here. `part` is created new (no symlink followed) with
    `mode`; on any failure (container error, not a zip, timeout) it is
    removed and the error is raised, so no half file is ever left.
    """
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as out:
            done = run_tool(engine_tool(order["image"], "copia", order["limits"], f"{data}:{ENGINE_FOLDER}:ro"),
                            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.PIPE, timeout=TOOL_SECONDS)
        if done.returncode != 0:
            raise OSError("the engine did not make the copy")
        if not zipfile.is_zipfile(part):
            raise OSError("the engine's copy is not a zip")
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def build_engine_copy(home: Path, order: dict, copy_id: str, now: float | None = None,
                      folder: Path | None = None, version: str | None = None) -> dict:
    """A Numinia Engine world's zip, made by the engine itself (`copia`): the machine never re-implements its rules.

    The world's own image runs with no network and the world's folder
    read-only; its zip — the world, its adventure and environment, how to run
    it at home, never its keys — comes out on stdout, straight into the
    copy's part file. The engine writes each snapshot whole, so this works
    with the world running or stopped. `folder` and `version` make it the
    copy of one version, as for build_copy; the folder is read-only either way.
    """
    now = time.time() if now is None else now
    wid = order["id"]
    data = home / "data" / wid if folder is None else folder
    copies = home / "copies"
    copies.mkdir(parents=True, exist_ok=True)
    part = copies / f".{copy_id}.zip.part"
    if data.is_symlink() or not data.is_dir():
        raise OSError("the world has no folder here")
    engine_copia(order, data, part)
    return finish_copy(home, wid, copy_id, part, copy_name(wid, version, now), now, version)


def made_at(status: object) -> int | None:
    made = status.get("madeAt") if isinstance(status, dict) else None
    return made if isinstance(made, int) and not isinstance(made, bool) else None


def fresh_copy(home: Path, wid: str, now: float, version: str | None = None) -> dict | None:
    """A ready copy of this world (of this version of it, when one is asked) young enough to hand out again, if there is one.

    A world's copy and a version's never stand in for each other: the record
    of a version's copy names it, a world's does not.
    """
    best = None
    for path in (home / "copies").glob("*.json"):
        status = read_json(path, {})
        made = made_at(status)
        if made is None or status.get("world") != wid or status.get("state") != "ready":
            continue
        if status.get("version") != version:
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
        if isinstance(job, dict) and "nightly" in job:
            continue  # a finished file on disk: nothing is waiting to be saved
        order = read_order(orders / f"{wid}.json") if matches(SLUG, wid) else None
        # The Numinia Engine's own copy needs no wait: it writes each snapshot whole.
        if order is None or order.get("state") != "running" or runs_engine(order):
            continue
        # A version nobody writes (every one but the published) needs none either.
        version = job.get("version")
        if version is not None:
            import versions  # here, not at the top: versions imports this file
            meta = versions.read_versions(home, order)
            if meta is None or version != meta["published"]:
                continue
        return save_pause(SAVE_INTERVAL)
    return 0


def record_run(home: Path, ok: bool, rule: str | None = None, now: float | None = None) -> None:
    """When the reconciler last ran and how it ended — a rule, never a value — for the door to show.

    A machine whose reconciler stopped running shows it here: the time stops moving.
    """
    write_json(home / "run" / LAST_RUN, {"at": utc(now), "ok": ok, "rule": rule})


def make_copies(home: Path, orders: Path, alias: str, now: float | None = None) -> list[str]:
    """Zip every queued copy; a world copied minutes ago gets the same zip again.

    A DownloadNightly's job names a nightly copy, which is copied as it is.
    """
    now = time.time() if now is None else now
    path = home / "run" / "copy-queue.json"
    queue = read_json(path, [])
    made = []
    group = door_gid()
    for job in queue if isinstance(queue, list) else []:
        if not isinstance(job, dict) or not matches(COPY_ID, job.get("copy")) or not matches(NONCE, job.get("nonce")):
            continue
        if "nightly" in job:
            # A DownloadNightly: a file already on the disk, whether or not the world is still here.
            if matches(SLUG, job.get("world")) and matches(STAMP, job["nightly"]):
                import nightly  # here, not at the top: nightly imports this file
                if nightly.make_copy(home, job, now):
                    made.append(job["copy"])
            continue
        version = job.get("version")  # a DownloadVersion's job names its version; a DownloadWorld's has none
        if version is not None and not matches(VERSION, version):
            continue
        entry = {"nonce": job["nonce"], "type": "DownloadWorld" if version is None else "DownloadVersion",
                 "world": job.get("world"), "by": job.get("by")}
        order = read_order(orders / f"{job.get('world')}.json") if matches(SLUG, job.get("world")) else None
        failed = {"copy": job["copy"], "world": entry["world"], "state": "failed", "at": utc(now)}
        folder = rule = None
        if version is not None:
            entry["version"] = failed["version"] = version
        if order is None:
            rule = "no world with this name on this machine" if version is None else "missing"
        elif version is not None:
            import versions  # here, not at the top: versions imports this file
            rule, folder = versions.copy_source(home, order, version)
        if rule is not None:
            write_json(home / "copies" / f"{job['copy']}.json", failed, mode=0o640, group=group)
            answer(home, entry, "failed", rule, now)
            continue
        again = fresh_copy(home, order["id"], now, version)
        try:
            if again is not None:
                os.link(home / "copies" / f"{again['copy']}.zip", home / "copies" / f"{job['copy']}.zip")
                write_json(home / "copies" / f"{job['copy']}.json", {**again, "copy": job["copy"]},
                           mode=0o640, group=group)
            elif runs_engine(order):
                build_engine_copy(home, order, job["copy"], now, folder, version)
            else:
                build_copy(home, order, job["copy"], alias, now, folder, version)
        except (OSError, sqlite3.Error, zipfile.BadZipFile, ValueError, subprocess.SubprocessError) as error:
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
    ap.add_argument("--admin-code-file", type=Path, default=ADMIN_CODE_FILE,
                    help="the machine's legacy admin code, for orders adopted from the public book")
    ap.add_argument("--codes-dir", type=Path, default=CODES_DIR,
                    help="each world's own admin code, one file per world, for the door to show")
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
            print(f"{wid}: order says running but it has no key file and this machine has no legacy admin code"
                  f" ({args.admin_code_file}) — not started", file=sys.stderr)
        print(json.dumps({k: p[k] for k in ("alias", "running", "stopped", "keyless", "new_keys", "switching")}, indent=2))
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
    """One run on the machine: codes, requests, containers, deletes, copies. True when Docker took the book."""
    # First: each world's code file says what its key file gives (on the first
    # run of this version, the code every world already has is copied in).
    try:
        synced = sync_codes(args.home, args.codes_dir)
    except OSError:
        synced = []
        print("the worlds' code files could not be made equal to their key files; the next run tries again",
              file=sys.stderr)
    take_requests(args.home, orders, alias, read_signers(args.signers_file), allowed, control_host,
                  codes_dir=args.codes_dir)
    # Upload slots past their hour, and what an upload stopped halfway left.
    expired = prune_uploads(args.home)
    p = plan(orders, args.home, alias, allowed, admin_code=admin_code, control_host=control_host)
    for wid in p["keyless"]:
        print(
            f"{wid}: order says running but it has no key file and this machine has no legacy admin code"
            f" ({args.admin_code_file}) — not started",
            file=sys.stderr,
        )
    applied = True
    try:
        apply(p, args.home, admin_code, codes_dir=args.codes_dir)
    except subprocess.CalledProcessError:
        applied = False
        print("docker did not take the book; the next run tries again", file=sys.stderr)
    deleted = purge_deleted(args.home, orders, codes_dir=args.codes_dir) if applied else []
    # A world moved to a new build is done once Docker took the book with it.
    updated = finish_updates(args.home) if applied else []
    # A world whose published version is being switched, now that Docker took its container down:
    # move the folders, then plan again so it comes back as its order says.
    import versions  # here, not at the top: versions imports this file
    switched = versions.finish_switches(args.home, orders) if applied else []
    if switched:
        p = plan(orders, args.home, alias, allowed, admin_code=admin_code, control_host=control_host)
        try:
            apply(p, args.home, admin_code, codes_dir=args.codes_dir)
        except subprocess.CalledProcessError:
            applied = False
            print("docker did not take the book after a switch; the next run tries again", file=sys.stderr)
    pause = copies_wait(args.home, orders)
    if pause:
        time.sleep(pause)
    copied = make_copies(args.home, orders, alias)
    prune_copies(args.home)
    import nightly  # here, not at the top: nightly imports this file
    nightly.ensure_index(args.home)  # the door lists the nightly copies from it; the nightly pass keeps it after this
    if synced:
        print(f"{alias}: code files made equal to their key files for {synced}")
    if expired:
        print(f"{alias}: {len(expired)} upload slot(s) past their hour removed")
    for wid in p["new_keys"]:
        print(f"{wid}: keys made on this machine (its own JWT secret, the machine's legacy admin code)")
    if updated:
        print(f"{alias}: moved to their new build {updated}")
    if switched:
        print(f"{alias}: published version switched for {switched}")
    print(f"{alias}: running {p['running']} stopped {p['stopped']} keyless {p['keyless']}"
          f" deleted {deleted} copies {len(copied)}")
    return applied


if __name__ == "__main__":
    sys.exit(main())
