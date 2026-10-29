#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: AGPL-3.0-only
"""A read token for one machine's vitals: what it is, and how the machine checks it.

An Oracle signs (EIP-712, walletsig.ACTIONS["FleetVitalsRead"]) a record naming ONE machine:

    {"message": {"machine": "<alias>", "issuedAt": N, "expires": N, "nonce": "…"}, "signature": "0x…"}

sent as `Authorization: Bearer <base64 of that JSON>`. The machine believes it only when:

- the machine named is this machine (a token for Bronte is worth nothing on any other);
- the signer is one of the wallets this machine already obeys (reconcile.read_signers);
- it was issued at most 5 minutes in the future and has not expired, and it lives at most 24 hours.

Nothing is stored: there is no list of tokens to steal. To end every token early, the signers change.
It only lets the door hand out the numbers fleet-vitals exports (cpu, memory, disk, network, and the
cpu and memory of each world's container); it cannot write, start, stop or delete anything.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from pathlib import Path

import walletsig as W

MAX_LIFE_S = 24 * 3600
SKEW_S = 300
MAX_TOKEN = 2048
NONCE = re.compile(r"[A-Za-z0-9_-]{8,64}")


def parse(header: str | None) -> dict | None:
    if not header or not header.startswith("Bearer ") or len(header) > MAX_TOKEN:
        return None
    try:
        raw = json.loads(base64.b64decode(header[7:].strip(), validate=True))
    except (binascii.Error, ValueError):
        return None
    if not isinstance(raw, dict) or set(raw) != {"message", "signature"} or not isinstance(raw["message"], dict):
        return None
    return raw


def check(header: str | None, alias: str, signers: set[str], now: float) -> tuple[bool, str]:
    raw = parse(header)
    if raw is None:
        return False, "no-token"
    m = raw["message"]
    if set(m) != {"machine", "issuedAt", "expires", "nonce"}:
        return False, "shape"
    if not all(isinstance(m[k], int) and not isinstance(m[k], bool) for k in ("issuedAt", "expires")):
        return False, "shape"
    if not isinstance(m["machine"], str) or not isinstance(m["nonce"], str) or not NONCE.fullmatch(m["nonce"]):
        return False, "shape"
    if m["machine"] != alias:
        return False, "other-machine"
    if m["issuedAt"] > now + SKEW_S:
        return False, "from-the-future"
    if m["expires"] <= now:
        return False, "expired"
    if m["expires"] - m["issuedAt"] > MAX_LIFE_S:
        return False, "too-long"
    signer = W.signer_of("FleetVitalsRead", m, raw["signature"])
    if signer is None:
        return False, "bad-signature"
    if signer not in signers:
        return False, "not-an-oracle-here"
    return True, "ok"


def served(directory: Path, which: str) -> dict | None:
    name = {"minute": "minute", "five": "five", "hour": "hour"}.get(which)
    if name is None:
        return None
    try:
        return json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
