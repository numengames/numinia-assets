#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: AGPL-3.0-only
"""The house's signed list of Oracles and Archons, followed by every machine.

Until now a machine obeyed only the wallets someone typed into
/etc/fleet/signers when the bootstrap line was run. The house now publishes
one list, signed, at https://numinia.com/fleet/signers.json:

    {"message": {"signers": ["0x…", …], "seq": N, "issuedAt": N}, "signature": "0x…"}

The signature is an EIP-712 `FleetSigners` record (walletsig.ACTIONS) whose
`signers` field is the wallets joined with ",". The machine believes it only
when the signer is the one wallet HOUSE_SIGNER below (empty = this feature is
off and the pass does nothing), and only when:

- the wallets are 1 to 200, each 0x + 40 lowercase hex, sorted and unique
  (the exact string the house signed);
- `seq` is above the last one this machine took (anti-replay: an old, valid
  record can never come back);
- `issuedAt` is at most 24 hours old and at most 5 minutes in the future.

What it takes is written to /etc/fleet/signers.house, whole and atomically.
A list that is empty, broken, unsigned, stale or from anyone else changes
nothing: a machine never loses its current signers to a bad answer. The
wallets in /etc/fleet/signers (put there by hand) are never touched; the
wallets the machine obeys are the union of the two files
(reconcile.read_signers). A wallet the house drops stops being obeyed at the
next pass; a wallet added by hand stays.

It runs inside the five-minute pass of release.py (fleet-release.timer, already
installed on every machine), so a machine gets it with the recipe update and
nothing by hand. Where it asks: /etc/fleet/signers-url or the env
FLEET_SIGNERS_URL, else DEFAULT_URL. Nothing here holds a secret.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import reconcile as R
import walletsig

# The house's wallet: the only signer of the list. EMPTY = the feature is off.
# Set in one reviewed change, after the house wallet exists.
HOUSE_SIGNER = ""

DEFAULT_URL = "https://numinia.com/fleet/signers.json"
URL_FILE = Path("/etc/fleet/signers-url")
MIN_SIGNERS = 1
MAX_SIGNERS = 200
MAX_AGE = 24 * 3600  # seconds the record may be old
MAX_AHEAD = 5 * 60  # seconds it may be in the future (clock drift)
MAX_BYTES = 64 * 1024
TIMEOUT = 15
SEQ_LINE = re.compile(r"#\s*seq\s+(\d+)\s*")


def house_file(signers_file: Path = R.SIGNERS_FILE) -> Path:
    return signers_file.with_name(R.HOUSE_FILE_NAME)


def last_seq(path: Path) -> int:
    """The seq of the list in the house file; 0 when there is none."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    for line in text.splitlines():
        found = SEQ_LINE.fullmatch(line)
        if found:
            return int(found.group(1))
    return 0


def whole_number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def verify(raw: object, now: float, seen_seq: int, house: str = HOUSE_SIGNER) -> tuple[dict | None, str]:
    """(record, why). The record is {signers, seq, issuedAt} only when every rule holds; else None and the rule broken."""
    if not house or not R.matches(R.ADDRESS, house):
        return None, "off"
    if not isinstance(raw, dict) or not isinstance(raw.get("message"), dict):
        return None, "not a record"
    message = raw["message"]
    if set(message) != {"signers", "seq", "issuedAt"}:
        return None, "wrong fields"
    wallets, seq, issued = message["signers"], message["seq"], message["issuedAt"]
    if not whole_number(seq) or not whole_number(issued):
        return None, "seq or issuedAt is not a whole number"
    if not isinstance(wallets, list) or not MIN_SIGNERS <= len(wallets) <= MAX_SIGNERS:
        return None, f"{MIN_SIGNERS} to {MAX_SIGNERS} wallets expected"
    if not all(R.matches(R.ADDRESS, w) for w in wallets):
        return None, "a wallet is not 0x and 40 lowercase hex"
    if wallets != sorted(set(wallets)):
        return None, "wallets are not sorted and unique"
    try:
        signer = walletsig.signer_of(
            "FleetSigners", {"signers": ",".join(wallets), "seq": seq, "issuedAt": issued}, raw.get("signature")
        )
    except Exception:  # noqa: BLE001 — a malformed record is no record
        return None, "signature does not hold"
    if signer is None or signer.lower() != house.lower():
        return None, "not signed by the house"
    if seq <= seen_seq:
        return None, "seq is not above the last one taken"
    if issued < now - MAX_AGE:
        return None, "too old"
    if issued > now + MAX_AHEAD:
        return None, "from the future"
    return {"signers": list(wallets), "seq": seq, "issuedAt": issued}, "ok"


def render(record: dict) -> str:
    head = [
        "# The house's signed list of Oracles and Archons, written by fleet-release (signers.py).",
        "# Do not edit: the next pass rewrites it. Wallets added by hand go in /etc/fleet/signers.",
        f"# seq {record['seq']}",
        f"# issuedAt {record['issuedAt']}",
    ]
    return "\n".join(head + record["signers"]) + "\n"


def signers_url(environ=None, path: Path = URL_FILE) -> str:
    """env FLEET_SIGNERS_URL, else the one line of /etc/fleet/signers-url, else DEFAULT_URL; only http(s) is taken."""
    environ = os.environ if environ is None else environ
    value = environ.get("FLEET_SIGNERS_URL")
    if value is None:
        try:
            value = path.read_text(encoding="utf-8")
        except OSError:
            return DEFAULT_URL
    value = value.strip()
    return value if re.fullmatch(r"https?://[^\s]+", value) else DEFAULT_URL


def fetch(url: str, opener=None) -> bytes:
    """GET the record. Raises on anything that is not a small 200."""

    class Guard(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not re.fullmatch(r"https?://[^\s]+", newurl):
                raise OSError("redirect to something that is not http(s)")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = opener or urllib.request.build_opener(Guard)
    request = urllib.request.Request(url, headers={"User-Agent": "numinia-fleet-signers", "Accept": "application/json"})
    with opener.open(request, timeout=TIMEOUT) as response:
        if response.status != 200:
            raise OSError(f"unexpected status {response.status}")
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise OSError("signed list too big")
    return data


def refresh(
    signers_file: Path = R.SIGNERS_FILE,
    now=time.time,
    get=fetch,
    url=signers_url,
    log=lambda line: print(line, file=sys.stderr),
    house: str | None = None,
) -> str:
    """One pass: fetch the house list and, if it verifies, write signers.house. Never raises, never removes
    anything: every failure leaves the file as it was. Returns off | stored | refused | failed."""
    house = HOUSE_SIGNER if house is None else house
    if not house:
        return "off"
    target = house_file(signers_file)
    try:
        raw = json.loads(get(url()).decode("utf-8"))
        record, why = verify(raw, now(), last_seq(target), house)
        if record is None:
            log(f"the house's signers list was not taken: {why}")
            return "refused"
        R.write_file(target, render(record), 0o644)
        log(f"the house's signers list (seq {record['seq']}, {len(record['signers'])} wallets) is in {target}")
        return "stored"
    except Exception as err:  # noqa: BLE001 — the network is never a reason to fail a pass
        log(f"the house's signers list could not be fetched ({type(err).__name__}); nothing changes")
        return "failed"


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Take the house's signed list of Oracles and Archons now.")
    ap.add_argument("--signers-file", type=Path, default=R.SIGNERS_FILE)
    args = ap.parse_args(argv)
    result = refresh(args.signers_file)
    print(result)
    return 0 if result in ("off", "stored") else 1


if __name__ == "__main__":
    sys.exit(main())
