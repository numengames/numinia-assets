# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT
"""Test-only: one reconciler pass over the inbox of the test door (docker's pull is a stub), then print the taken requests."""
import json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ["MACHINE_DIR"])
import reconcile as R

root = Path(os.environ["DOOR_ROOT"]); home = root / "srv"; etc = root / "etc"
signers = frozenset(R.read_signers(etc / "signers"))
taken = R.take_requests(home, home / "orders", "test-1", signers, frozenset(), "test-1.numinia.com", None,
                        codes_dir=etc / "codes", pull=lambda image: None)
print(json.dumps([{"type": t["type"], "world": t["world"]} for t in taken]))
