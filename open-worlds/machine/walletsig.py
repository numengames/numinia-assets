#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: AGPL-3.0-only
"""Who signed a fleet request: EIP-712 typed data, recovered to a wallet address.

An Oracle asks a fleet machine for something (create, stop, start, delete or
download a world, bring one back from its zip, move one to another build of
its engine, show a world's admin code or change it, or show the machine's
legacy admin code) by signing a small typed message with the wallet they
already sign in with. The wallet shows exactly what is being signed; the
machine recovers the address that signed it and obeys only the addresses an
Oracle installed on it (/etc/fleet/signers).

This is EIP-712 (typed structured data), the way Ethereum wallets sign
off-chain messages that a server checks (Snapshot's votes, permits). The
types are flat — strings and uint256 only — so the encoding fits in a few
lines. The elliptic-curve part is python-ecdsa (Debian and Ubuntu ship it as
python3-ecdsa); Keccak-256 is written out below because the standard library
has only SHA-3, which pads differently. Nothing here holds a secret: it only
checks signatures, so timing does not matter.

The browser side builds the same messages (numinia-web, the worlds room);
tests/fleet_vectors.json holds digests and signatures made by viem, and
tests/test_walletsig.py checks this module against them.
"""

from __future__ import annotations

import re

DOMAIN = {"name": "Numinia Fleet", "version": "1"}

# Every request the machine understands, and its fields in signing order.
ACTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "CreateWorld": (
        ("machine", "string"),
        ("world", "string"),
        ("domain", "string"),
        ("image", "string"),
        ("memory", "string"),
        ("cpus", "string"),
        ("maxUploadMb", "uint256"),
        ("nonce", "string"),
        ("expires", "uint256"),
    ),
    "DeleteWorld": (
        ("machine", "string"),
        ("world", "string"),
        ("domain", "string"),
        ("nonce", "string"),
        ("expires", "uint256"),
    ),
    "StopWorld": (("machine", "string"), ("world", "string"), ("nonce", "string"), ("expires", "uint256")),
    "StartWorld": (("machine", "string"), ("world", "string"), ("nonce", "string"), ("expires", "uint256")),
    "DownloadWorld": (("machine", "string"), ("world", "string"), ("nonce", "string"), ("expires", "uint256")),
    # Legacy: the machine-wide code, kept while the room that asks for it is live.
    "ShowAdminCode": (("machine", "string"), ("nonce", "string"), ("expires", "uint256")),
    # One world's own admin code: shown, or changed for a new one the machine makes.
    "ShowWorldCode": (("machine", "string"), ("world", "string"), ("nonce", "string"), ("expires", "uint256")),
    "RotateWorldCode": (("machine", "string"), ("world", "string"), ("nonce", "string"), ("expires", "uint256")),
    # A world brought back from its zip: CreateWorld's fields, plus the zip's
    # exact size and SHA-256, so the machine takes that zip and no other.
    "ImportWorld": (
        ("machine", "string"),
        ("world", "string"),
        ("domain", "string"),
        ("image", "string"),
        ("memory", "string"),
        ("cpus", "string"),
        ("maxUploadMb", "uint256"),
        ("zipBytes", "uint256"),
        ("zipSha256", "string"),
        ("nonce", "string"),
        ("expires", "uint256"),
    ),
    # A world moved to another build of its own engine: the image is all that changes.
    "UpdateWorld": (
        ("machine", "string"),
        ("world", "string"),
        ("image", "string"),
        ("nonce", "string"),
        ("expires", "uint256"),
    ),
    # A version of an engine for the whole fleet, signed by an Oracle. No machine
    # and no nonce: `seq` only grows, so an old record can never be replayed.
    "FleetRelease": (
        ("engine", "string"),
        ("image", "string"),
        ("commit", "string"),
        ("seq", "uint256"),
        ("issuedAt", "uint256"),
    ),
    # The house's list of Oracles and Archons every machine follows: the wallets
    # (lowercase, sorted, unique, joined with ","), `seq` (= issuedAt) only grows.
    # Signed by the house's one wallet (signers.HOUSE_SIGNER), not by an Oracle.
    "FleetSigners": (
        ("signers", "string"),
        ("seq", "uint256"),
        ("issuedAt", "uint256"),
    ),
    # One world held on a build; an empty image releases it. UpdateWorld plus why.
    "PinWorld": (
        ("machine", "string"),
        ("world", "string"),
        ("image", "string"),
        ("reason", "string"),
        ("nonce", "string"),
        ("expires", "uint256"),
    ),
    # A world keeps up to two versions; one is published. Each of these names
    # the world and one of its versions (v1, v2, …). New types, never new
    # fields on old ones, so every older digest stays what it was.
    "DuplicateVersion": (
        ("machine", "string"),
        ("world", "string"),
        ("version", "string"),
        ("nonce", "string"),
        ("expires", "uint256"),
    ),
    "PublishVersion": (
        ("machine", "string"),
        ("world", "string"),
        ("version", "string"),
        ("nonce", "string"),
        ("expires", "uint256"),
    ),
    "DeleteVersion": (
        ("machine", "string"),
        ("world", "string"),
        ("version", "string"),
        ("nonce", "string"),
        ("expires", "uint256"),
    ),
    "DownloadVersion": (
        ("machine", "string"),
        ("world", "string"),
        ("version", "string"),
        ("nonce", "string"),
        ("expires", "uint256"),
    ),
    # A zip taken as a new version of an existing world: the zip's exact size
    # and SHA-256, and the unpublished version it replaces ("" when the world
    # has one version). The engine, limits and address are the world's own.
    "ImportVersion": (
        ("machine", "string"),
        ("world", "string"),
        ("zipBytes", "uint256"),
        ("zipSha256", "string"),
        ("replace", "string"),
        ("nonce", "string"),
        ("expires", "uint256"),
    ),
}

SIGNATURE = re.compile(r"0x[0-9a-fA-F]{130}")
ADDRESS = re.compile(r"0x[0-9a-f]{40}")
# The order of secp256k1's group (SEC 2, §2.4.1): r and s live below it.
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

# ---------------------------------------------------------------------------
# Keccak-256 (the original Keccak padding, as Ethereum uses it)
# ---------------------------------------------------------------------------

_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)
# Rotation offsets r[x][y] of the rho step.
_ROT = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)
_MASK = (1 << 64) - 1
_RATE = 136  # bytes: 1600-bit state, 512-bit capacity


def _rol(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (64 - shift))) & _MASK if shift else value


def _permute(lanes: list[int]) -> list[int]:
    """Keccak-f[1600]; lane (x, y) is lanes[x + 5 * y]."""
    a = lanes
    for rc in _RC:
        c = [a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        a = [a[i] ^ d[i % 5] for i in range(25)]
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rol(a[x + 5 * y], _ROT[x][y])
        a = [
            b[i] ^ (~b[(i % 5 + 1) % 5 + 5 * (i // 5)] & b[(i % 5 + 2) % 5 + 5 * (i // 5)])
            for i in range(25)
        ]
        a[0] ^= rc
    return a


def keccak256(data: bytes) -> bytes:
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % _RATE:
        padded.append(0x00)
    padded[-1] |= 0x80
    lanes = [0] * 25
    for start in range(0, len(padded), _RATE):
        block = padded[start : start + _RATE]
        for i in range(_RATE // 8):
            lanes[i] ^= int.from_bytes(block[8 * i : 8 * i + 8], "little")
        lanes = _permute(lanes)
    return b"".join(lane.to_bytes(8, "little") for lane in lanes[:4])


# ---------------------------------------------------------------------------
# EIP-712: the digest a wallet signs for one request
# ---------------------------------------------------------------------------

_DOMAIN_FIELDS = (("name", "string"), ("version", "string"))


def _encode_type(name: str, fields: tuple[tuple[str, str], ...]) -> bytes:
    return f"{name}({','.join(f'{kind} {field}' for field, kind in fields)})".encode()


def _hash_struct(name: str, fields: tuple[tuple[str, str], ...], values: dict) -> bytes:
    encoded = [keccak256(_encode_type(name, fields))]
    for field, kind in fields:
        value = values[field]
        if kind == "string":
            encoded.append(keccak256(value.encode("utf-8")))
        else:  # uint256
            encoded.append(value.to_bytes(32, "big"))
    return keccak256(b"".join(encoded))


def typed_digest(kind: str, message: dict) -> bytes:
    """The 32 bytes a wallet signs for this request (EIP-712 `\\x19\\x01` form).

    The caller has already checked that the message has exactly the fields
    of its kind, strings as strings and numbers as whole numbers ≥ 0.
    """
    separator = _hash_struct("EIP712Domain", _DOMAIN_FIELDS, DOMAIN)
    return keccak256(b"\x19\x01" + separator + _hash_struct(kind, ACTIONS[kind], message))


def recover(digest: bytes, signature: object) -> str | None:
    """The address (0x + 40 lowercase hex) that made this signature, or None.

    Refused: anything but 65 bytes in hex, a recovery byte other than
    27/28 (or 0/1), r or s out of range, and a high s (EIP-2: each signature
    has exactly one accepted form).
    """
    if not isinstance(signature, str) or SIGNATURE.fullmatch(signature) is None:
        return None
    raw = bytes.fromhex(signature[2:])
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:64], "big")
    v = raw[64] + 27 if raw[64] in (0, 1) else raw[64]
    if v not in (27, 28) or not 1 <= r < _N or not 1 <= s <= _N // 2:
        return None
    # Imported here, not at the top: the reconciler imports this module for
    # the request shapes too, and a machine missing python3-ecdsa must keep
    # its worlds running — only the signed requests stop being obeyed.
    from ecdsa import SECP256k1, VerifyingKey
    from ecdsa.util import sigdecode_string

    try:
        keys = VerifyingKey.from_public_key_recovery_with_digest(
            raw[:64], digest, SECP256k1, sigdecode=sigdecode_string
        )
    except Exception:  # noqa: BLE001 — any failure to recover means: not a signature
        return None
    # python-ecdsa lists the key for an even R first, then for an odd R:
    # exactly the order of Ethereum's recovery byte (27 even, 28 odd).
    point = keys[v - 27].to_string("raw")
    return "0x" + keccak256(point)[12:].hex()


def signer_of(kind: str, message: dict, signature: object) -> str | None:
    """Who signed this request; None when the signature does not hold."""
    return recover(typed_digest(kind, message), signature)


def short(address: str) -> str:
    """0x1234…abcd: enough to tell Oracles apart in a log, not a full identifier."""
    return f"{address[:6]}…{address[-4:]}" if ADDRESS.fullmatch(address) else "?"
