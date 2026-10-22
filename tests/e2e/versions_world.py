# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT
"""Test-only helper of versions_e2e.mjs: the machine's side of the version requests, on a temp fleet home.

Subcommands (environment: MACHINE_DIR, DOOR_ROOT; the output is one JSON line):

  seed          a Hyperfy world `plaza` (order, keys, a real database, an asset, storage.json), running
  zip <path>    a small Hyperfy world zip (what an Oracle would upload); prints its size and SHA-256
  run           ONE real run of the reconciler (reconcile.run_once: requests, plan, apply, switches, copies)
                with Docker faked: a container set kept in fake-docker.json that `docker compose up`
                makes equal to the compose file. `hold` keeps a container from going (the switch waits).
  hold on|off   the fake Docker refuses to let the world's container go (or lets it)
  look          what is on disk: data/plaza, versions/, the journal, the copies
"""
import argparse
import contextlib
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ["MACHINE_DIR"])
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # tests/: fleet_zips
import reconcile as R  # noqa: E402
import versions as V  # noqa: E402
import fleet_zips as Z  # noqa: E402

root = Path(os.environ["DOOR_ROOT"])
home, etc = root / "srv", root / "etc"
FAKE = root / "fake-docker.json"
KEYS = "JWT_SECRET=x\nADMIN_CODE=abcd1234efgh\n"
ASSET = "a" * 64 + ".glb"


def fake_state():
    return json.loads(FAKE.read_text()) if FAKE.exists() else {"containers": [], "ups": 0, "hold": False}


def save(state):
    FAKE.write_text(json.dumps(state))


def seed():
    order = {"id": "plaza", "card": "plaza", "state": "running", "server": "test-1", "domain": "plaza.numinia.com",
             "image": Z.IMAGE, "limits": {"memory": "2g", "cpus": "1.5", "maxUploadMb": 50},
             "createdAt": "2026-10-01T00:00:00Z"}
    (home / "orders" / "plaza.json").write_text(json.dumps(order))
    (home / "env" / "plaza.env").write_text(KEYS)
    (etc / "codes" / "plaza").write_text("abcd1234efgh\n")
    data = home / "data" / "plaza"
    (data / "assets").mkdir(parents=True)
    Z.world_db(data / "db.sqlite", blueprints=2, entities=3)
    (data / "assets" / ASSET).write_bytes(b"statue bytes")
    (data / "storage.json").write_bytes(b'{"from": "created"}')
    print(json.dumps({"seeded": "plaza"}))


def make_zip(path):
    data = Z.make_zip([("world/db.sqlite", Z.db_bytes(blueprints=1, entities=2)),
                       ("world/assets/" + "b" * 64 + ".glb", b"a zipped statue"),
                       ("world/storage.json", b'{"from": "zip"}')])
    Path(path).write_bytes(data)
    print(json.dumps({"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}))


def run():
    """One run_once, Docker faked. Reports what the fake Docker was asked and what it holds after."""
    state = fake_state()
    slept = []
    ups_before = state["ups"]

    def sh(*cmd):
        if cmd[4:5] != ("up",):
            return
        names = set(json.loads(Path(cmd[3]).read_text())["services"]) - {R.CADDY_SERVICE}
        if "--no-deps" in cmd:
            state["containers"] = sorted(set(state["containers"]) | {cmd[-1]})
        else:
            # A held container cannot be taken down: it stays when the book no longer names it.
            keep = {c for c in state["containers"] if state["hold"] and c not in names}
            state["containers"] = sorted(names | keep)
            state["ups"] += 1

    original = R.apply
    args = argparse.Namespace(home=home, codes_dir=etc / "codes", admin_code_file=etc / "admin-code",
                              signers_file=etc / "signers")
    with mock.patch.object(R, "sh", sh), \
            mock.patch.object(V, "docker_gone", lambda wid: wid not in state["containers"]), \
            mock.patch.object(R, "apply", lambda p, h, code, codes_dir=None: original(p, h, code, sleep=slept.append, codes_dir=codes_dir)), \
            mock.patch.object(time, "sleep", slept.append):
        with contextlib.redirect_stdout(sys.stderr):  # the run's own summary line is not this helper's answer
            applied = R.run_once(args, "test-1", home / "orders", None, "test-1.numinia.com", set())
    save(state)
    print(json.dumps({"applied": applied, "containers": state["containers"], "ups": state["ups"] - ups_before,
                      "slept": slept}))


def hold(on):
    state = fake_state()
    state["hold"] = on
    save(state)
    print(json.dumps({"hold": on}))


def look():
    def ls(path):
        return sorted(p.name for p in path.iterdir()) if path.is_dir() else None

    def ino(path):
        return os.lstat(path).st_ino if path.exists() else None

    versions = home / "versions" / "plaza"
    print(json.dumps({
        "data": ls(home / "data" / "plaza"),
        "dataStorage": (home / "data" / "plaza" / "storage.json").read_text() if (home / "data" / "plaza" / "storage.json").exists() else None,
        "versions": ls(versions),
        "versionStorage": {p.name: (p / "storage.json").read_text() for p in sorted(versions.iterdir()) if (p / "storage.json").exists()} if versions.is_dir() else {},
        "assetLinked": {p.name: ino(p / "assets" / ASSET) == ino(home / "data" / "plaza" / "assets" / ASSET)
                        for p in sorted(versions.iterdir())} if versions.is_dir() else {},
        "inoData": ino(home / "data" / "plaza"),
        "inoVersions": {p.name: ino(p) for p in sorted(versions.iterdir())} if versions.is_dir() else {},
        "meta": json.loads((home / "versions" / "plaza.json").read_text()) if (home / "versions" / "plaza.json").exists() else None,
        "journal": R.read_switching(home),
        "copies": ls(home / "copies"),
        "order": json.loads((home / "orders" / "plaza.json").read_text()),
        "env": (home / "env" / "plaza.env").read_text() == KEYS,
        "uploads": ls(home / "uploads"),
        "staging": ls(home / "staging"),
    }))


if __name__ == "__main__":
    command, *rest = sys.argv[1:]
    {"seed": seed, "zip": lambda: make_zip(rest[0]), "run": run, "hold": lambda: hold(rest[0] == "on"), "look": look}[command]()
