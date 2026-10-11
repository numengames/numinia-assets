#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: AGPL-3.0-only
"""The fleet machine's reconciler: what runs here equals what the order book says.

Runs every minute from a systemd timer. It pulls the order book, keeps the
orders whose `server` is this machine's alias, renders a docker-compose file
and a Caddyfile from them, and asks Docker to make the containers match.
When the list of sites changed, it asks Caddy to read it again (a reload:
open worlds stay connected); Caddy never re-reads the file on its own.
A world whose order says `stopped`, or whose order is gone, is taken down and
its folder on disk is left alone.

A world's keys are made here, the first time its order runs: a JWT secret of
its own and the machine's admin code (/etc/fleet/admin-code, made once by
bootstrap.sh). A key file that already exists is never touched. A machine
with no admin code makes no keys, and a world with none is skipped and
reported — it never starts with an empty admin code.

The machine never writes to git. It holds no GitHub credential: the order
book is public and is pulled like anyone would.

Three rails against a bad merge on the book:

- this file is INSTALLED once by bootstrap.sh (to /usr/local/sbin) and never
  run from the pulled tree, so a merged pull request changes orders, not code;
- an order is obeyed only if its image comes from the house's registry,
  pinned to one build, and its domain is one label under a house zone
  (numinia.com, numen.games) or a client domain an Oracle listed on this
  machine in /etc/fleet/domains;
- every field that reaches the compose file or the Caddyfile must match a
  strict, anchored pattern first, and the compose file is built as data and
  written as JSON (which is YAML), so no field can add a setting — a domain
  that smuggles `privileged: true` is refused, never rendered.

Pure functions first (select, render), the shell last (main). The tests in
tests/test_machine.py cover the pure part; the shell part is three commands.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BOOK = Path("/srv/fleet/book")  # the clone of numinia-assets
HOME = Path("/srv/fleet")  # data/, env/, run/ live here, outside the clone
ALIAS_FILE = Path("/etc/fleet/alias")
ADMIN_CODE_FILE = Path("/etc/fleet/admin-code")  # made once by bootstrap.sh; every world keyed here gets it
SAVE_INTERVAL = "60"  # seconds between two saves of a world; the engine refuses to boot without it
CADDY_IMAGE = "caddy:2-alpine"
CADDY_SERVICE = "caddy"  # a world may not take this name
CADDYFILE_INSIDE = "/etc/caddy/Caddyfile"  # where Caddy sees run/Caddyfile, mounted read-only
# The sites Caddy is known to serve: a copy of the Caddyfile, written only
# after Caddy accepted a reload of exactly that text.
CADDY_APPLIED = "Caddyfile.applied"
INDEX_FILE = "index.json"  # the book's table of contents (the console reads it); never an order
RESERVED_IDS = frozenset({CADDY_SERVICE, INDEX_FILE[: -len(".json")]})
HOUSE_ZONES = ("numinia.com", "numen.games")  # one label under either is always allowed
DOMAINS_FILE = Path("/etc/fleet/domains")  # a client's own domains, one per line, written by an Oracle

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


def matches(pattern: re.Pattern[str], value: object) -> bool:
    """True only for a string the whole of which the pattern matches."""
    return isinstance(value, str) and pattern.fullmatch(value) is not None


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


def domain_allowed(domain: str, allowed: set[str] | frozenset[str] = frozenset()) -> bool:
    """One label under a house zone, or listed on this machine (exact or `*.zone`)."""
    if not matches(HOST, domain):
        return False
    label, _, parent = domain.partition(".")
    if parent in HOUSE_ZONES and matches(LABEL, label):
        return True
    return domain in allowed or f"*.{parent}" in allowed


def refusal(order: dict, allowed: set[str] | frozenset[str] = frozenset()) -> str | None:
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


def select_orders(orders: list[dict], alias: str, allowed: set[str] | frozenset[str] = frozenset()) -> list[dict]:
    """The orders this machine obeys, cleaned: its alias, and every field checked."""
    mine = []
    for o in orders:
        if o.get("server") != alias:
            continue
        why = refusal(o, allowed)
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
            ],
        }
    }
    for o in running:
        wid, domain, limits = o["id"], o["domain"], o["limits"]
        services[wid] = {
            "image": o["image"],
            "restart": "unless-stopped",
            "env_file": f"{home}/env/{wid}.env",
            "environment": {
                # Everything the engine's `check envs` block demands at boot
                # (numinia-hyperfy2 src/server/index.js at the pinned build),
                # except JWT_SECRET, which comes from the world's key file.
                "WORLD": "world",
                "PORT": "3000",
                "SAVE_INTERVAL": SAVE_INTERVAL,
                "ASSETS": "local",
                "ASSETS_BASE_URL": f"https://{domain}/assets",
                "DB_URI": "local",
                "COLLECTIONS": "local",
                "PUBLIC_WS_URL": f"wss://{domain}/ws",
                "PUBLIC_API_URL": f"https://{domain}/api",
                "PUBLIC_MAX_UPLOAD_SIZE": str(limits["maxUploadMb"]),
            },
            "volumes": [f"{home}/data/{wid}:/app/world"],
            "deploy": {"resources": {"limits": {"memory": limits["memory"], "cpus": limits["cpus"]}}},
        }
    return {"services": services}


def render_compose(running: list[dict], home: Path) -> str:
    """The compose file as JSON — valid YAML that no field can restructure."""
    return json.dumps(compose_dict(running, home), indent=2) + "\n"


def render_caddyfile(running: list[dict]) -> str:
    """One site per running world; Caddy issues the certificate and forwards the WebSocket."""
    blocks = ["# Generated by open-worlds/machine/reconcile.py — do not edit, change the order."]
    for o in running:
        blocks.append(f"{o['domain']} {{\n\treverse_proxy {o['id']}:3000\n}}")
    return "\n\n".join(blocks) + "\n"


def plan(
    book: Path,
    home: Path,
    alias: str,
    allowed: set[str] | None = None,
    admin_code: str | None = None,
) -> dict:
    """Everything the shell part needs, computed without touching Docker.

    `admin_code` is the machine's own (main reads it from /etc/fleet/admin-code).
    With it, a running world that has no key file is listed in `new_keys` and
    started; without it, that world stays down and is listed as `keyless`.
    """
    if allowed is None:
        allowed = read_allowed_domains()
    mine = select_orders(read_orders(book / "open-worlds"), alias, allowed)
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
        "caddyfile": render_caddyfile(running),
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


def sh(*cmd: str) -> None:
    subprocess.run(cmd, check=True)


def apply(p: dict, home: Path, admin_code: str | None = None) -> None:
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
    compose.write_text(p["compose"], encoding="utf-8")
    (run / "Caddyfile").write_text(p["caddyfile"], encoding="utf-8")
    sh("docker", "compose", "-f", str(compose), "up", "-d", "--remove-orphans")
    applied = run / CADDY_APPLIED
    if caddy_needs_reload(p["caddyfile"], applied):
        # A reload, not a restart: the worlds already open stay connected.
        try:
            sh("docker", "compose", "-f", str(compose), "exec", "-T", CADDY_SERVICE,
               "caddy", "reload", "--config", CADDYFILE_INSIDE, "--adapter", "caddyfile")
        except subprocess.CalledProcessError:
            print("caddy did not take the new list of sites; the next run tries again", file=sys.stderr)
        else:
            applied.write_text(p["caddyfile"], encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Make this machine's containers match the order book.")
    ap.add_argument("--book", type=Path, default=BOOK)
    ap.add_argument("--home", type=Path, default=HOME)
    ap.add_argument("--alias", default=None, help="defaults to /etc/fleet/alias")
    ap.add_argument("--admin-code-file", type=Path, default=ADMIN_CODE_FILE, help="the machine's admin code")
    ap.add_argument("--no-pull", action="store_true", help="do not git pull the book first")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, touch nothing")
    args = ap.parse_args(argv)
    alias = args.alias or ALIAS_FILE.read_text(encoding="utf-8").strip()
    if not args.no_pull and not args.dry_run:
        sh("git", "-C", str(args.book), "pull", "--ff-only", "--quiet")
    admin_code = read_admin_code(args.admin_code_file)
    p = plan(args.book, args.home, alias, admin_code=admin_code)
    for wid in p["keyless"]:
        print(
            f"{wid}: order says running but it has no key file and this machine has no admin code"
            f" ({args.admin_code_file}) — not started",
            file=sys.stderr,
        )
    if args.dry_run:
        print(json.dumps({k: p[k] for k in ("alias", "running", "stopped", "keyless", "new_keys")}, indent=2))
        print(p["compose"])
        print(p["caddyfile"])
        return 0
    apply(p, args.home, admin_code)
    for wid in p["new_keys"]:
        print(f"{wid}: keys made on this machine (its own JWT secret, the machine's admin code)")
    print(f"{alias}: running {p['running']} stopped {p['stopped']} keyless {p['keyless']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
