# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT
"""Test-only helper of nightly_restore_e2e.mjs: ONE real nightly pass, with the engine's `copia` run natively.

The only thing replaced is `docker run ... <image> copia`: the command the machine builds (reconcile.engine_tool)
is checked for what the sandbox promises (no network, the folder read-only, the tool `copia`) and then the engine's
real tool runs on that same folder with node (`entrada.ts copia`, MOTOR_MUNDO=<the mounted folder>), its zip going to
the same stdout file. Environment: MACHINE_DIR, ENGINE_DIR, NODE. Argument: the fleet home. Prints the pass's JSON.
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.environ["MACHINE_DIR"])
import nightly as N  # noqa: E402
import reconcile as R  # noqa: E402


def native(cmd, **kwargs):
    if cmd[-1] != "copia":
        raise AssertionError(f"only `copia` is run natively here, not {cmd[-1]}")
    if cmd[cmd.index("--network") + 1] != "none":
        raise AssertionError("the sandbox lost --network none")
    mount = cmd[cmd.index("-v") + 1]
    if not mount.endswith(":/mundo:ro"):
        raise AssertionError(f"the world's folder is not mounted read-only: {mount}")
    folder = mount[: -len(":/mundo:ro")]
    entry = Path(os.environ["ENGINE_DIR"]) / "packages" / "server" / "src" / "entrada.ts"
    return subprocess.run([os.environ["NODE"], str(entry), "copia"], check=False, stdin=subprocess.DEVNULL,
                          stdout=kwargs["stdout"], stderr=subprocess.PIPE, timeout=120,
                          env={**os.environ, "MOTOR_MUNDO": folder})


R.run_tool = native
sys.exit(N.main(["--home", sys.argv[1]]))
