# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""What the tests of a world brought back from its zip share: a throwaway Oracle, worlds and zips.

No binary fixture lives in git: every zip and every database is made by the
test that needs it, with the standard library. The Oracle's key is made at
random for each run and never kept; the machine's tests list its address as
the machine's Oracle, so a request signed with it is a real EIP-712
signature the door and the reconciler check like any other.
"""

from pathlib import Path
import hashlib
import io
import os
import sqlite3
import stat
import struct
import tempfile
import zipfile

from ecdsa import SECP256k1, SigningKey
from ecdsa.util import sigencode_string_canonize

IMAGE = "ghcr.io/numengames/numinia-hyperfy2:sha-0e2045370989d051e73f81c7c9d53121d5c374b3"


class Oracle:
    """A wallet made for one test run: it signs fleet requests as a browser wallet would (EIP-712)."""

    def __init__(self, walletsig, secret=None):
        self.W = walletsig
        secret = secret or 1 + int.from_bytes(os.urandom(32), "big") % (SECP256k1.order - 1)
        self.secret = secret
        self.key = SigningKey.from_secret_exponent(secret, curve=SECP256k1)
        point = self.key.get_verifying_key().to_string("raw")
        self.address = "0x" + walletsig.keccak256(point)[12:].hex()

    def sign(self, kind, message):
        digest = self.W.typed_digest(kind, message)
        rs = self.key.sign_digest_deterministic(digest, sigencode=sigencode_string_canonize)
        for v in (27, 28):
            signature = "0x" + rs.hex() + f"{v:02x}"
            if self.W.recover(digest, signature) == self.address:
                return signature
        raise AssertionError("no recovery byte gives this key back")


def import_message(data, world="plaza-copia", nonce=None, expires=1893456000, **over):
    """An ImportWorld for this zip: its exact size and SHA-256, for the machine open-1."""
    message = {
        "machine": "open-1",
        "world": world,
        "domain": f"{world}.numinia.com",
        "image": IMAGE,
        "memory": "2g",
        "cpus": "1.5",
        "maxUploadMb": 50,
        "zipBytes": len(data),
        "zipSha256": hashlib.sha256(data).hexdigest(),
        "nonce": nonce or os.urandom(16).hex(),
        "expires": expires,
    }
    message.update(over)
    return message


def world_db(path, blueprints=3, entities=5):
    """A world's database as the engine keeps it: its four tables, and a few rows in each."""
    db = sqlite3.connect(str(path))
    db.executescript(
        "create table config (key text primary key, value text);"
        "create table users (id text primary key, name text, rank integer, avatar text, createdAt text);"
        "create table blueprints (id text primary key, data text, createdAt text, updatedAt text);"
        "create table entities (id text primary key, data text, createdAt text, updatedAt text);"
    )
    db.execute("insert into config values ('settings', '{\"title\": \"Plaza\"}')")
    db.execute("insert into users values ('u1', 'Pablo', 2, null, '2026-10-12')")
    for i in range(blueprints):
        db.execute("insert into blueprints values (?, ?, '2026-10-12', '2026-10-12')",
                   (f"bp{i}", f'{{"model": "asset://{i:064x}.glb"}}'))
    for i in range(entities):
        db.execute("insert into entities values (?, ?, '2026-10-12', '2026-10-12')",
                   (f"e{i}", f'{{"blueprint": "bp{i % blueprints}", "position": [{i}, 0, 0]}}'))
    db.commit()
    db.close()


def db_bytes(**rows):
    """The bytes of a world's database, made in a temporary file."""
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "db.sqlite"
        world_db(path, **rows)
        return path.read_bytes()


def foreign_db_bytes():
    """A sound SQLite database that is not a world's: none of the engine's tables."""
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "db.sqlite"
        db = sqlite3.connect(str(path))
        db.execute("create table notes (id integer primary key, body text)")
        db.execute("insert into notes (body) values ('hello')")
        db.commit()
        db.close()
        return path.read_bytes()


def broken_db_bytes():
    """A world's database with one of its pages overwritten: SQLite's integrity check finds it."""
    data = bytearray(db_bytes(blueprints=40, entities=200))
    page = struct.unpack(">H", data[16:18])[0] or 65536
    # The last page holds rows of a table: scribble over its cell area.
    start = len(data) - page + 8
    data[start:start + 200] = b"\xff" * 200
    return bytes(data)


def make_zip(files, *, links=(), encrypted=(), devices=(), compression=zipfile.ZIP_DEFLATED):
    """A zip of (name, bytes) pairs, made here; `links` and `devices` are entries of another Unix type,
    `encrypted` names entries whose 'encrypted' flag is set in both headers."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compression) as z:
        for name, data in files:
            z.writestr(name, data)
        for name, target in links:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, target)
        for name in devices:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFCHR | 0o600) << 16
            z.writestr(info, b"")
    data = buf.getvalue()
    for name in encrypted:
        data = set_flag(data, name, 0x1)
    return data


def central_entries(data):
    """(offset, name) of each entry of a zip's central directory, read from its end record."""
    end = data.rfind(b"PK\x05\x06")
    at = struct.unpack_from("<L", data, end + 16)[0]
    found = []
    while data[at:at + 4] == b"PK\x01\x02":
        name_len, extra_len, comment_len = struct.unpack_from("<HHH", data, at + 28)
        found.append((at, data[at + 46:at + 46 + name_len].decode()))
        at += 46 + name_len + extra_len + comment_len
    return found


def set_flag(data, name, bit):
    """The zip with a general-purpose flag bit set on one entry, in its local and its central header.

    zipfile clears the flags it does not use when it writes, so the bit is
    set on the bytes afterwards."""
    raw = bytearray(data)
    for at, found in central_entries(data):
        if found == name:
            flags = struct.unpack_from("<H", raw, at + 8)[0]
            struct.pack_into("<H", raw, at + 8, flags | bit)
            local = struct.unpack_from("<L", raw, at + 42)[0]
            flags = struct.unpack_from("<H", raw, local + 6)[0]
            struct.pack_into("<H", raw, local + 6, flags | bit)
    return bytes(raw)


def zip64(files, *, count=None):
    """A zip whose end carries ZIP64 records (as zipfile writes past 65 535 entries), here for a few.

    `count` puts another number of entries in the ZIP64 record, the one a
    reader believes.
    """
    from unittest import mock

    buf = io.BytesIO()
    with mock.patch.object(zipfile, "ZIP_FILECOUNT_LIMIT", 0), zipfile.ZipFile(buf, "w") as z:
        for name, data in files:
            z.writestr(name, data)
    data = bytearray(buf.getvalue())
    record = data.rfind(b"PK\x06\x06")
    assert record >= 0, "zipfile wrote no ZIP64 record"
    if count is not None:
        struct.pack_into("<QQ", data, record + 24, count, count)
    return bytes(data)
