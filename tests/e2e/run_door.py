# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT
"""Local test door: the REAL fleetd.py (numinia-assets fleet/world-zips) on a unix socket in a temp home,
plus the REAL door_site() Caddyfile it renders (served over plain http on localhost). Test-only."""
import os, sys
from pathlib import Path
sys.path.insert(0, os.environ["MACHINE_DIR"])
import reconcile as R, fleetd as F

root = Path(os.environ["DOOR_ROOT"]); port = os.environ["DOOR_PORT"]
home = root / "srv"; etc = root / "etc"; sock = root / "fleetd.sock"
for d in ("inbox", "requests", "uploads", "orders", "run", "staging", "data", "env", "copies", "versions"):
    (home / d).mkdir(parents=True, exist_ok=True)
(etc / "codes").mkdir(parents=True, exist_ok=True); (root / "state").mkdir(exist_ok=True)
if not (etc / "signers").exists(): (etc / "signers").write_text("")
if "ZIP_MAX" in os.environ: R.ZIP_MAX = int(os.environ["ZIP_MAX"])
R.DOOR_SOCKET_INSIDE = str(sock)
(root / "Caddyfile").write_text("{\n\tadmin off\n\tauto_https off\n}\n" + R.door_site(f"http://127.0.0.1:{port}") + "\n")
door = F.Door(home=home, alias="test-1", signers_file=etc / "signers", admin_code_file=etc / "admin-code",
              codes_dir=etc / "codes", recipe_file=etc / "recipe", control_host_file=etc / "control-host",
              domains_file=etc / "domains", origins_file=etc / "origins", state=root / "state")
server = F.make_server(door, sock)
print("door up", flush=True)
server.serve_forever()
