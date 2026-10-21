#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: AGPL-3.0-only
"""The fleet machine's recipe updater: the machine's own programs follow main, by themselves.

release.py keeps the WORLDS on the latest engine; this keeps the MACHINE'S
PROGRAMS (fleetd, reconcile, release, nightly, condiciones, this file and bootstrap.sh
itself: everything under open-worlds/machine/) on main of
numengames/numinia-assets, so a merge reaches every machine without anyone
running the bootstrap line by hand again.

Runs every ten minutes from a systemd timer (fleet-recipe.timer), installed
by the bootstrap and never run from the clone. Each pass:

1. Reads the switch: /etc/fleet/recipe-pin holding `off` freezes the machine
   on what it runs; a whole commit holds it there (installs that one); no
   file means follow main. Anything else in the file also freezes (the
   strictest reading, never "follow").
2. Reads main's newest commit anonymously over https from github.com (git,
   partial and shallow: commits and folders, never the content's files; no
   credential, a capped time and a capped size on disk) and the id of its
   open-worlds/machine folder (a git tree id: the same id means the same
   bytes). The same folder as the installed recipe: only noted, nothing is
   installed (a merge that touches only content changes no machine).
3. A different folder is a new recipe. The canary machine
   (/etc/fleet/recipe-canary exists) takes it at once; every other machine
   only once that folder has been main's for a rest time, counted from
   when THIS machine first saw it (2 h by default; /etc/fleet/recipe-soak
   in minutes). No coordination between machines: a recipe that broke the
   canary is reverted on main within the rest, and the others never see it.
4. Trusts it only when the commit is signed by GitHub's own web-flow key,
   pinned below, checked with gpgv: GitHub signs the commits it makes
   itself (a pull request merged on github.com), never one pushed from a
   laptop. With main protected (a merge needs a review) that means: only
   what was merged through a reviewed pull request is ever installed.
5. Installs it: a copy of the programs and units installed now, then the
   bootstrap of THAT commit, run without arguments but the alias (so the
   Oracles' wallets, the codes, the admin code, the release URL, Caddy and
   the worlds stay as they are), then a health check: the door answers on
   its socket with the new recipe, the reconciler runs (cleanly, when it
   ran cleanly before), every timer is active and the new updater starts.
   If not: the copy goes back, the machine HALTS on that recipe
   (state.json `halted`, with the reason) and never retries it; a
   different recipe on main lifts the halt. A bootstrap that failed
   before it changed anything (no network for apt, say) is tried again on
   the next passes, three times, then halts too.

State: <home>/recipe/state.json, shown by the door (`/fleet/v1/machine` →
`recipeUpdate`). Everything outside (git, gpgv, systemctl, the bootstrap,
the clock) is passed in, so the tests run with fakes. Python's standard
library only, so a broken reconcile.py never stops the pass that undoes it.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPOSITORY_URL = "https://github.com/numengames/numinia-assets.git"
BRANCH = "main"
RECIPE_FOLDER = "open-worlds/machine"
BOOTSTRAP = RECIPE_FOLDER + "/bootstrap.sh"
HOME = Path("/srv/fleet")
RECIPE_DIR = "recipe"
STATE_FILE = "state.json"
INSTALLED_FILE = Path("/etc/fleet/recipe")  # written by bootstrap.sh: the commit the programs came from
ALIAS_FILE = Path("/etc/fleet/alias")
CANARY_FILE = Path("/etc/fleet/recipe-canary")  # exists: this machine takes a new recipe at once
PIN_FILE = Path("/etc/fleet/recipe-pin")  # `off` (frozen) or a whole commit (held there)
SOAK_FILE = Path("/etc/fleet/recipe-soak")  # minutes a new recipe rests on main before a non-canary takes it
SOAK_SECONDS = 2 * 3600
SOAK_MAX_MINUTES = 7 * 24 * 60
LIB_DIR = Path("/usr/local/lib/fleet")
SBIN_DIR = Path("/usr/local/sbin")
UNIT_DIR = Path("/etc/systemd/system")
SOCKET = HOME / "sock" / "fleetd.sock"
LAST_RUN = HOME / "run" / "last-run.json"  # the reconciler's own note (reconcile.LAST_RUN)
COMMIT = re.compile(r"[0-9a-f]{40}")
ALIAS = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
DOOR = "fleetd.service"
TIMERS = ("fleet-reconcile.timer", "fleet-reconcile.path", "fleet-release.timer", "fleet-nightly.timer",
          "fleet-recipe.timer")
OTHER_JOBS = ("fleet-reconcile.service", "fleet-release.service", "fleet-nightly.service")
GIT_TIMEOUT = 120
GIT_MAX_BYTES = 64 * 1024 * 1024  # the partial clone is wiped and made again above this
OBJECT_MAX = 256 * 1024  # a commit object or the bootstrap, at most
INSTALL_TIMEOUT = 30 * 60
LOG_KEEP = 64 * 1024
HEALTH_SECONDS = 60
HEALTH_STEP = 5
TRIES = 3  # a bootstrap that changed nothing is tried this many times before the machine halts
SEEN_KEEP = 30
BACKUPS_KEEP = 3
BY = "fleet-recipe"

# GitHub's web-flow key: the key github.com signs the commits it makes itself
# with (a pull request merged on the website, a squash, a revert button). Its
# public half, exactly as https://github.com/web-flow.gpg serves it
# (export-minimal), and its fingerprint, which is what is checked. The key
# that expired on 2024-01-16 (4AEE18F83AFDEB23) is left out on purpose.
WEB_FLOW_FINGERPRINTS = frozenset({"968479A1AFF927E37D1A566BB5690EEEBB952194"})
WEB_FLOW_KEY = """\
-----BEGIN PGP PUBLIC KEY BLOCK-----

mQINBGWmxXYBEACyN+4viFQM6QQoKr0A2W0rGdMobTJwOZso2QPpewbyBsuL3rNW
5OmHrWwXAhPKNqUIyOzdq8MoSxoTTuqLksoahixEL/X2nyhOBxR9GkYz/oI9R3nY
cLRaFQoSJoVfOt61opkLUzbWAehpbgT8EKln8JsENq0+0nDlWQi0h2Q9oGmqlgVz
skwmVZ8Leyv4Mg7hN6swyZ7moZfkkpD5+U7Z2XVurCzkSSfg4zb2lMRLJos2eCAc
749ECsX0t7OBftF+YqgjIXixXsm2RrUqvU47OkOtZeAhvAYenbC3pr9Fha5NxoBU
Ea+11MK9W6OcRhwvxVCUrMUR6FTSZyC//VpXTTtrRlOqpU5wGMbP3zpn9geqOXCl
8rF7+1gAPG/o+QFQTBsVEruwi4JWogiQuQyOwAIlFe/7dvaxWZGpv/yW2+L3guL7
xaHKFVGsayhlitQQ5Xa+P1iSgKSXDyReCbWotfqAempPySI25LHh3ScXI6NgdHSr
SBaFojwAfgxbPTEQ6adIsKHCQofrnLrNa3UOeGDGiOOK0aYV3jiEDGAouatkNf2q
85Eosj1f9laCqAH3YLJD7dcSne1iChK5qRTByMvIyeSD0NbNnVMFOGpXySyWtKb2
ldpu8AWBQJsJs9FmYBcWAGBA2pp+IxaCn6rBIHIsUVFRN8OVZKsEsBkWywARAQAB
tBtHaXRIdWIgPG5vcmVwbHlAZ2l0aHViLmNvbT6JAk4EEwEKADgWIQSWhHmhr/kn
430aVmu1aQ7uu5UhlAUCZabFdgIbAwULCQgHAgYVCgkICwIEFgIDAQIeAQIXgAAK
CRC1aQ7uu5UhlIMuEAClvVwC+Neoiq0AdixJZsagKHpx1QrMJWrtMRi4eXVTTaeX
+P1unhC/AmSO4Xxd3uRoejHvfWh4F0gitUJ8XKgiejnmuGcq7Dbt5OoO1JuXGlW2
BQ+MiGoYVw2B0sOhWDNrIBWOO/WL4LykcGnAtrRXwoS0Wx4MCydztXQY5lcnCWaW
8rvu7WmduoOikH4HI97rqN5896dc4iBKSx8LZf+46DRCCD/5SfACplBz4hs5zen8
TL8zd+zxjFrXbzota0jSDEGK9WGO4z55S2xScC6zv6v3Bj1OR8Bs5aodGtmamHZ7
sE9w0RJoCfNx+9cR/rE82SrOaBpVU7urLe4lg7zaaNhqDdNV8ymuXGmIJarDgrme
iB5bHS+dLFzLUkTgot4RFlPa9bFiJuJN6Tc9tMu5RJQ9l/zKmxDHIKWsAle5R65u
zEq04LugTQBdEorGxfQCsF2ga9ncKTDMiAThWTvZpOP3NJ/athZRmOBpG4B9iR6r
pRU8F/+MokG4fIMwnvtOhWQFiEzdTkJ7U5JAkPtTAmT3/mznwtPEU7DrFWSGAdqg
IMOlxNCBeGvjwLR0qGH7cB9qHDGNoDLkjaUFpu5tPv4/ivkQaHlHJxjT0ILM6jet
CAzKpKh48rm65tmrJX6KVpj0r2kKMscFf7s7XaPlCNCFds/YA+0puPbzJKWKfA==
=NRlX
-----END PGP PUBLIC KEY BLOCK-----
"""


class RemoteError(Exception):
    """GitHub could not be read this pass: nothing changes."""


# ---------------------------------------------------------------------------
# Small helpers (no reconcile.py: this pass must outlive a broken one)
# ---------------------------------------------------------------------------


def utc(now: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def read_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return default


def write_json(path: Path, data: object, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, mode)
    tmp.replace(path)


def read_line(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# The switches, parsed: pure
# ---------------------------------------------------------------------------


def parse_pin(text: str | None) -> str | None:
    """None (no file, or empty: follow main) | 'off' (frozen) | a whole commit (held there).

    Anything else freezes: a typo in a stop switch must never read as "go"."""
    if text is None:
        return None
    word = text.strip().lower()
    if not word:
        return None
    return word if COMMIT.fullmatch(word) else "off"


def parse_soak(text: str | None) -> int:
    """Seconds a new recipe rests on main before a non-canary machine takes it: the file holds minutes."""
    if text is None or not re.fullmatch(r"[0-9]{1,6}", text.strip()):
        return SOAK_SECONDS
    return min(int(text.strip()), SOAK_MAX_MINUTES) * 60


# ---------------------------------------------------------------------------
# GitHub's signature on a commit: checked with gpgv against the pinned key
# ---------------------------------------------------------------------------


def split_signed(commit_object: bytes) -> tuple[bytes, bytes] | None:
    """A raw commit object → (what was signed, the signature), as git verify-commit splits it. None: unsigned."""
    head, sep, message = commit_object.partition(b"\n\n")
    if not sep:
        return None
    kept, signature, inside = [], [], False
    for line in head.split(b"\n"):
        if line.startswith(b"gpgsig "):
            if signature:
                return None  # two signatures: believe neither
            inside = True
            signature.append(line[len(b"gpgsig "):])
        elif inside and line.startswith(b" "):
            signature.append(line[1:])
        else:
            inside = False
            kept.append(line)
    if not signature:
        return None
    return b"\n".join(kept) + b"\n\n" + message, b"\n".join(signature) + b"\n"


def dearmor(armored: str) -> bytes:
    """An ASCII-armored key block → its binary packets (what gpgv's --keyring reads)."""
    body = armored.split("\n\n", 1)[1].split("-----END", 1)[0]
    return base64.b64decode("".join(line for line in body.split() if not line.startswith("=")))


def signature_verdict(returncode: int, status: str, pinned: frozenset[str] = WEB_FLOW_FINGERPRINTS) -> tuple[bool, str]:
    """gpgv's machine-readable status lines → (ok, why). Good only with a VALIDSIG by a pinned primary key."""
    words = [line.split() for line in status.splitlines() if line.startswith("[GNUPG:] ")]
    kinds = {w[1] for w in words if len(w) > 1}
    for bad in ("BADSIG", "EXPKEYSIG", "REVKEYSIG", "EXPSIG", "ERRSIG", "NO_PUBKEY"):
        if bad in kinds:
            return False, {"NO_PUBKEY": "signed by a key that is not GitHub's web-flow key",
                           "ERRSIG": "signed by a key that is not GitHub's web-flow key"}.get(bad, f"signature refused ({bad})")
    for w in words:
        if len(w) >= 3 and w[1] == "VALIDSIG":
            primary = w[-1].upper()
            if returncode == 0 and (primary in pinned or w[2].upper() in pinned):
                return True, "signed by GitHub (web-flow)"
            return False, "signed by a key that is not GitHub's web-flow key"
    return False, "no valid signature"


def verify_commit(commit_object: bytes, run=subprocess.run, key: str = WEB_FLOW_KEY,
                  pinned: frozenset[str] = WEB_FLOW_FINGERPRINTS) -> tuple[bool, str]:
    """Is this commit signed by GitHub's web-flow key? Only the pinned key is in the keyring gpgv reads."""
    parts = split_signed(commit_object)
    if parts is None:
        return False, "the commit is not signed"
    payload, signature = parts
    with tempfile.TemporaryDirectory(prefix="fleet-recipe-") as tmp:
        folder = Path(tmp)
        (folder / "keyring.gpg").write_bytes(dearmor(key))
        (folder / "commit").write_bytes(payload)
        (folder / "commit.sig").write_bytes(signature)
        try:
            done = run(["gpgv", "--homedir", str(folder), "--keyring", str(folder / "keyring.gpg"), "--status-fd", "1",
                        str(folder / "commit.sig"), str(folder / "commit")],
                       capture_output=True, timeout=60, check=False, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return False, "gpgv could not run"
    status = done.stdout.decode("utf-8", "replace") if isinstance(done.stdout, bytes) else str(done.stdout)
    return signature_verdict(done.returncode, status, pinned)


# ---------------------------------------------------------------------------
# GitHub, read anonymously: a partial, shallow clone of commits and folders
# ---------------------------------------------------------------------------


class GitRemote:
    """main's newest commit, a commit's recipe folder id, its raw object, the bootstrap at it.

    A bare clone with no file contents (--filter=blob:none), one commit deep;
    the one file it ever reads (bootstrap.sh) is fetched when asked. No
    credential helper, no prompt, the user's and system git config ignored,
    only the URL's own protocol allowed."""

    def __init__(self, folder: Path, url: str = REPOSITORY_URL, branch: str = BRANCH, run=subprocess.run) -> None:
        self.folder = folder
        self.url = url
        self.branch = branch
        self.run = run
        scheme = url.split("://", 1)[0] if "://" in url else "file"
        self.config = ["-c", "credential.helper=", "-c", "protocol.allow=never", "-c", f"protocol.{scheme}.allow=always",
                       "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=30", "-c", "core.askPass=true"]

    def git_bytes(self, *args: str) -> bytes:
        environ = {"PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"), "HOME": str(self.folder),
                   "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                   "LC_ALL": "C"}
        try:
            done = self.run(["git", "-C", str(self.folder), *self.config, *args], capture_output=True,
                            timeout=GIT_TIMEOUT, check=False, env=environ, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as err:
            raise RemoteError(f"git {args[0]} could not run ({type(err).__name__})") from err
        if done.returncode != 0:
            raise RemoteError(f"git {args[0]} failed")
        return bytes(done.stdout)

    def git(self, *args: str) -> str:
        return self.git_bytes(*args).decode("utf-8", "replace").strip()

    def ready(self) -> None:
        """Make the empty partial clone once; wipe and remake it when it grew past its cap."""
        if self.folder.is_dir() and folder_size(self.folder) > GIT_MAX_BYTES:
            shutil.rmtree(self.folder, ignore_errors=True)
        if (self.folder / "HEAD").is_file():
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        self.git("init", "--quiet", "--bare")
        for key, value in (("remote.origin.url", self.url), ("remote.origin.promisor", "true"),
                           ("remote.origin.partialclonefilter", "blob:none"), ("core.repositoryformatversion", "1"),
                           ("extensions.partialClone", "origin")):
            self.git("config", key, value)

    def fetch(self, what: str) -> None:
        self.git("fetch", "--quiet", "--no-tags", "--depth", "1", "--filter=blob:none", "origin", what)

    def tip(self) -> str:
        """main's newest commit, as GitHub says it now."""
        self.ready()
        self.fetch(f"+refs/heads/{self.branch}:refs/heads/{self.branch}")
        commit = self.git("rev-parse", "--verify", f"refs/heads/{self.branch}^{{commit}}")
        if not COMMIT.fullmatch(commit):
            raise RemoteError("main is not a commit")
        return commit

    def have(self, commit: str) -> None:
        self.ready()
        try:
            self.git("cat-file", "-e", f"{commit}^{{commit}}")
        except RemoteError:
            self.fetch(commit)

    def tree(self, commit: str) -> str | None:
        """The id of the commit's open-worlds/machine folder (same id, same bytes); None when it has none."""
        self.have(commit)
        try:
            tree = self.git("rev-parse", "--verify", f"{commit}:{RECIPE_FOLDER}")
        except RemoteError:
            return None
        return tree if COMMIT.fullmatch(tree) else None

    def commit_object(self, commit: str) -> bytes:
        self.have(commit)
        if int(self.git("cat-file", "-s", commit) or 0) > OBJECT_MAX:
            raise RemoteError("commit too big")
        return self.git_bytes("cat-file", "commit", commit)

    def bootstrap(self, commit: str) -> bytes:
        self.have(commit)
        path = f"{commit}:{BOOTSTRAP}"
        if int(self.git("cat-file", "-s", path) or 0) > OBJECT_MAX:  # fetches the one file when it is missing
            raise RemoteError("bootstrap too big")
        return self.git_bytes("cat-file", "blob", path)


def folder_size(folder: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


# ---------------------------------------------------------------------------
# Effects, all passed in through Env
# ---------------------------------------------------------------------------


class Env:
    """Everything the pass does to the outside world. Tests replace each member."""

    def __init__(self, home: Path = HOME, lib_dir: Path = LIB_DIR, sbin_dir: Path = SBIN_DIR,
                 unit_dir: Path = UNIT_DIR, installed_file: Path = INSTALLED_FILE) -> None:
        self.home = home
        self.lib_dir, self.sbin_dir, self.unit_dir, self.installed_file = lib_dir, sbin_dir, unit_dir, installed_file
        self.now = time.time
        self.sleep = time.sleep
        self.log = lambda line: print(line, file=sys.stderr)
        self.run = subprocess.run
        self.remote = GitRemote(self.dir / "git")
        self.pin = lambda: parse_pin(read_line(PIN_FILE))
        self.canary = lambda: CANARY_FILE.exists()
        self.soak = lambda: parse_soak(read_line(SOAK_FILE))
        self.verify = lambda commit: verify_commit(self.remote.commit_object(commit))  # (commit) -> (ok, why)
        self.busy = self._busy  # () -> name of another fleet job running now, or None
        self.backup = self._backup  # () -> Path of the copy, raises on failure
        self.restore = self._restore  # (Path) -> bool
        self.bootstrap = self._bootstrap  # (commit) -> (ok, why)
        self.reconcile_was_ok = self._reconcile_was_ok  # () -> bool: the reconciler's last run ended well
        self.health = self._health  # (commit, reconcile_must_pass) -> (ok, why)

    @property
    def dir(self) -> Path:
        return self.home / RECIPE_DIR

    def installed(self) -> str | None:
        ref = read_line(self.installed_file)
        return ref if ref and COMMIT.fullmatch(ref) else None

    def systemctl(self, *args: str) -> int:
        try:
            return self.run(["systemctl", *args], capture_output=True, timeout=300, check=False,
                            stdin=subprocess.DEVNULL).returncode
        except (OSError, subprocess.SubprocessError):
            return 1

    def _busy(self) -> str | None:
        for unit in OTHER_JOBS:
            try:
                state = self.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=30,
                                 check=False, stdin=subprocess.DEVNULL).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                continue
            if state in ("activating", "deactivating", "reloading"):
                return unit
        return None

    # --- the copy of what is installed, and putting it back ---------------

    def managed(self) -> list[Path]:
        """What the bootstrap installs and a copy must hold: the programs, their commands, the fleet's units, the commit."""
        found = sorted(self.lib_dir.glob("*.py")) if self.lib_dir.is_dir() else []
        found += sorted(self.sbin_dir.glob("fleet*")) if self.sbin_dir.is_dir() else []
        found += sorted(p for p in self.unit_dir.glob("fleet*") if p.suffix in (".service", ".timer", ".path")) \
            if self.unit_dir.is_dir() else []
        if self.installed_file.is_file():
            found.append(self.installed_file)
        return found

    def _backup(self) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(self.now()))
        dest = self.dir / "previous" / stamp
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, mode=0o700)
        listing = []
        for path in self.managed():
            copy = dest / "files" / str(path).lstrip("/")
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, copy)
            listing.append(str(path))
        write_json(dest / "list.json", listing, mode=0o600)
        olds = sorted((self.dir / "previous").iterdir())
        for old in olds[:-BACKUPS_KEEP]:
            shutil.rmtree(old, ignore_errors=True)
        return dest

    def _restore(self, copy: Path) -> bool:
        """Put the copied programs, commands and units back exactly; remove what the new recipe added."""
        listing = read_json(copy / "list.json", None)
        if not isinstance(listing, list):
            return False
        keep = {Path(p) for p in listing if isinstance(p, str)}
        ok = True
        for path in self.managed():
            if path not in keep:
                if path.parent == self.unit_dir:
                    self.systemctl("disable", "--now", path.name)
                path.unlink(missing_ok=True)
        for path in sorted(keep):
            source = copy / "files" / str(path).lstrip("/")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f".{path.name}.restore")
                shutil.copy2(source, tmp)
                tmp.replace(path)
            except OSError:
                ok = False
        ok = self.systemctl("daemon-reload") == 0 and ok
        ok = self.systemctl("restart", DOOR) == 0 and ok
        timers = [p.name for p in sorted(keep) if p.parent == self.unit_dir and p.suffix in (".timer", ".path")]
        if timers:
            ok = self.systemctl("enable", "--now", *timers) == 0 and ok
        return ok

    # --- the bootstrap of the new commit -----------------------------------

    def _bootstrap(self, commit: str) -> tuple[bool, str]:
        alias = read_line(ALIAS_FILE)
        if not alias or not ALIAS.fullmatch(alias):
            return False, "this machine has no alias"
        try:
            text = self.remote.bootstrap(commit)
        except RemoteError as err:
            return False, str(err)
        script = self.dir / "bootstrap.sh"
        script.write_bytes(text)
        os.chmod(script, 0o700)
        # Only the alias: the bootstrap then keeps the Oracles' wallets, the release URL, the
        # admin code and every world's code. No SUDO_USER: its SSH step looks at root's keys
        # and, without one, only says so (it never turns passwords back on).
        environ = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root",
                   "LANG": "C.UTF-8", "DEBIAN_FRONTEND": "noninteractive", "FLEET_REF": commit}
        try:
            done = self.run(["bash", str(script), alias], capture_output=True, timeout=INSTALL_TIMEOUT, check=False,
                            env=environ, stdin=subprocess.DEVNULL)
            output = (done.stdout or b"") + (done.stderr or b"")
            code = done.returncode
        except subprocess.TimeoutExpired:
            output, code = b"", -1
        except OSError:
            return False, "the bootstrap could not run"
        log = self.dir / "install.log"
        log.write_bytes(output[-LOG_KEEP:])
        os.chmod(log, 0o600)
        if code == 0:
            return True, "ok"
        return False, "the bootstrap timed out" if code == -1 else f"the bootstrap ended with {code}"

    # --- health ------------------------------------------------------------

    def _reconcile_was_ok(self) -> bool:
        raw = read_json(LAST_RUN, None)
        return isinstance(raw, dict) and raw.get("ok") is True

    def door_recipe(self) -> str | None:
        """The recipe the door says it runs, asked over its own socket."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(5)
                sock.connect(str(self.home / "sock" / "fleetd.sock"))
                sock.sendall(b"GET /fleet/v1/machine HTTP/1.0\r\nHost: fleetd\r\n\r\n")
                data = b""
                while len(data) < 1 << 20:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            if not head.startswith(b"HTTP/1.") or b" 200 " not in head.split(b"\r\n", 1)[0] + b" ":
                return None
            recipe = json.loads(body).get("recipe")
            return recipe if isinstance(recipe, str) else None
        except (OSError, ValueError, AttributeError):
            return None

    def _health(self, commit: str, reconcile_must_pass: bool) -> tuple[bool, str]:
        if self.installed() != commit:
            return False, "the machine does not say it runs this recipe"
        waited, said = 0, None
        while True:
            said = self.door_recipe()
            if said == commit or waited >= HEALTH_SECONDS:
                break
            self.sleep(HEALTH_STEP)
            waited += HEALTH_STEP
        if said != commit:
            return False, "the door does not answer on its socket" if said is None else "the door runs another recipe"
        if self.systemctl("start", "fleet-reconcile.service") != 0 and reconcile_must_pass:
            return False, "the reconciler does not run cleanly"
        stopped = [u for u in (DOOR, *TIMERS) if self.systemctl("is-active", "--quiet", u) != 0]
        if stopped:
            return False, "not running: " + " ".join(stopped)
        try:
            check = self.run([str(self.sbin_dir / "fleet-recipe"), "--check"], capture_output=True, timeout=60,
                             check=False, stdin=subprocess.DEVNULL).returncode
        except (OSError, subprocess.SubprocessError):
            check = 1
        if check != 0:
            return False, "the new recipe updater does not start"
        return True, "ok"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def read_state(env: Env) -> dict:
    raw = read_json(env.dir / STATE_FILE, {})
    state = raw if isinstance(raw, dict) else {}
    if not isinstance(state.get("seen"), dict):
        state["seen"] = {}
    if not isinstance(state.get("bad"), list):
        state["bad"] = []
    return state


def write_state(env: Env, state: dict) -> None:
    write_json(env.dir / STATE_FILE, state)


def remember(state: dict, tree: str, now: float) -> float:
    """When this machine first saw `tree` as main's recipe; the oldest notes go."""
    seen = state["seen"]
    first = seen.get(tree)
    if not isinstance(first, (int, float)) or isinstance(first, bool):
        first = seen[tree] = now
    for old in sorted(seen, key=lambda t: seen[t] if isinstance(seen[t], (int, float)) else 0)[:-SEEN_KEEP]:
        seen.pop(old, None)
    return float(first)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


def halt(env: Env, state: dict, commit: str, tree: str | None, why: str, rolled_back: bool) -> None:
    state["halted"] = {"commit": commit, "tree": tree, "reason": why[:200], "at": utc(env.now()),
                       "rolledBack": rolled_back}
    if tree:
        state["bad"] = sorted(set(state["bad"]) | {tree})[-SEEN_KEEP:]
    state["installing"] = None
    state["tries"] = None
    write_state(env, state)
    env.log(f"recipe {commit[:12]}: {why}; the machine halts on it")


def roll_back(env: Env, state: dict, job: dict, why: str, final: bool = True) -> dict:
    """The copy goes back. final: the machine halts on that recipe; else it is tried again next pass."""
    previous = job.get("previous")
    copy = job.get("backup")
    restored = bool(copy) and env.restore(Path(copy))
    if restored and isinstance(previous, str):
        ok, after = env.health(previous, False)
        if not ok:
            env.log(f"after going back to {previous[:12]}: {after}")
    if final:
        halt(env, state, job["commit"], job.get("tree"), why, restored)
        return {"action": "rolled-back", "commit": job["commit"], "note": why}
    state["installing"] = None
    write_state(env, state)
    env.log(f"recipe {job['commit'][:12]}: {why}; nothing changed, it is tried again on the next pass")
    return {"action": "retry", "commit": job["commit"], "note": why}


def install(env: Env, state: dict, commit: str, tree: str | None, installed: str | None) -> dict:
    must_pass = env.reconcile_was_ok()
    try:
        copy = env.backup()
    except Exception as err:  # noqa: BLE001 — no copy, no change
        env.log(f"no copy of the installed recipe could be made ({type(err).__name__}); nothing is installed")
        return {"action": "no-backup", "commit": commit}
    job = {"commit": commit, "tree": tree, "previous": installed, "backup": str(copy), "at": utc(env.now()),
           "reconcileMustPass": must_pass}
    state["installing"] = job
    write_state(env, state)
    env.log(f"installing recipe {commit[:12]} (was {(installed or 'unknown')[:12]})")
    ok, why = env.bootstrap(commit)
    if not ok:
        tries = state.get("tries") if isinstance(state.get("tries"), dict) else {}
        count = (tries.get("count", 0) if tries.get("tree") == tree else 0) + 1
        if env.installed() == installed and count < TRIES:
            # It stopped before the programs: nothing new ran. Put the copy back and try again.
            state["tries"] = {"tree": tree, "count": count}
            return roll_back(env, state, job, why, final=False)
        return roll_back(env, state, job, why)
    ok, why = env.health(commit, must_pass)
    if not ok:
        return roll_back(env, state, job, why)
    state["installing"] = None
    state["tries"] = None
    state["installed"] = commit
    state["upToDate"] = True
    state["target"] = None
    state["lastInstall"] = {"commit": commit, "from": installed, "at": utc(env.now())}
    write_state(env, state)
    env.log(f"recipe {commit[:12]} installed and healthy")
    return {"action": "installed", "commit": commit}


def finish_interrupted(env: Env, state: dict) -> dict | None:
    """A pass that died between the bootstrap and the health check (a reboot, a kill): judge it now."""
    job = state.get("installing")
    if not isinstance(job, dict) or not isinstance(job.get("commit"), str) or not COMMIT.fullmatch(job["commit"]):
        state["installing"] = None
        return None
    ok, why = env.health(job["commit"], bool(job.get("reconcileMustPass")))
    if ok:
        state["installing"] = None
        state["lastInstall"] = {"commit": job["commit"], "from": job.get("previous"), "at": utc(env.now())}
        write_state(env, state)
        return None
    return roll_back(env, state, job, "an interrupted install: " + why)


def decide(env: Env, state: dict) -> dict:
    """The body of one pass, on `state` (written by the caller). Returns {action, commit, note}."""
    now = env.now()
    interrupted = finish_interrupted(env, state)
    if interrupted:
        return interrupted
    installed = env.installed()
    pin, canary, soak = env.pin(), bool(env.canary()), env.soak()
    state.update({"installed": installed, "pin": pin, "canary": canary, "soakSeconds": soak})
    if pin == "off":
        state["target"] = None
        return {"action": "frozen", "note": "frozen by /etc/fleet/recipe-pin"}
    if installed is None:
        return {"action": "none", "note": "no recipe installed here: run the bootstrap line once"}
    try:
        commit = pin or env.remote.tip()
        tree = env.remote.tree(commit)
        try:
            installed_tree = env.remote.tree(installed)
        except RemoteError:
            installed_tree = None  # a commit GitHub no longer has: whatever main has is newer
    except RemoteError as err:
        env.log(f"GitHub could not be read ({err}); nothing changes")
        return {"action": "none", "note": "github not reachable"}
    if tree is None:
        state["target"] = None
        return {"action": "none", "commit": commit, "note": "that commit has no recipe"}
    first = now if pin else remember(state, tree, now)
    state["checked"] = {"commit": commit, "tree": tree, "at": utc(now)}
    if tree == installed_tree:
        state.update({"target": None, "upToDate": True, "refused": None})
        return {"action": "same", "commit": commit}
    state["upToDate"] = False
    halted = state.get("halted") if isinstance(state.get("halted"), dict) else None
    if halted is not None and halted.get("tree") != tree:
        state["halted"] = halted = None  # a different recipe lifts the halt
    target = {"commit": commit, "tree": tree, "why": "pinned" if pin else ("canary" if canary else "rested")}
    state["target"] = target
    if halted is not None:
        return {"action": "halted", "commit": commit, "note": str(halted.get("reason"))}
    if not pin and tree in state["bad"]:
        return {"action": "halted", "commit": commit, "note": "this recipe failed here before"}
    if not pin and not canary and now < first + soak:
        target["why"] = "resting"
        target["restUntil"] = utc(first + soak)
        return {"action": "resting", "commit": commit, "note": f"until {target['restUntil']}"}
    try:
        ok, why = env.verify(commit)
    except RemoteError as err:
        env.log(f"the commit could not be read ({err}); nothing changes")
        return {"action": "none", "commit": commit, "note": "github not reachable"}
    if not ok:
        state["refused"] = {"commit": commit, "why": why, "at": utc(now)}
        env.log(f"recipe {commit[:12]} refused: {why}")
        return {"action": "refused", "commit": commit, "note": why}
    state["refused"] = None
    other = env.busy()
    if other:
        return {"action": "waiting", "commit": commit, "note": f"{other} is running"}
    return install(env, state, commit, tree, installed)


def run_pass(env: Env) -> dict:
    """One pass, and a note of when it ran and how it ended (the door shows it). Never raises on a bad network."""
    env.dir.mkdir(parents=True, exist_ok=True)
    state = read_state(env)
    result = decide(env, state)
    state["installed"] = env.installed()
    state["lastPass"] = {"at": utc(env.now()), "action": str(result.get("action"))[:40]}
    write_state(env, state)
    return result


def status(env: Env) -> dict:
    """What `--check` prints: the switches and the state, as this updater reads them."""
    state = read_state(env)
    return {"installed": env.installed(), "pin": env.pin(), "canary": bool(env.canary()), "soakSeconds": env.soak(),
            "target": state.get("target"), "halted": state.get("halted"), "lastPass": state.get("lastPass")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Keep this machine's fleet programs on main of numinia-assets.")
    ap.add_argument("--home", type=Path, default=HOME)
    ap.add_argument("--check", action="store_true", help="read the switches and the state, change nothing")
    args = ap.parse_args(argv)
    env = Env(args.home)
    if args.check:
        print(json.dumps(status(env), indent=2))
        return 0
    env.dir.mkdir(parents=True, exist_ok=True)
    os.chmod(env.dir, 0o755)  # the door (user fleetd) reads state.json; the copies inside are root's alone
    with open(env.dir / "lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another pass is running", file=sys.stderr)
            return 0
        result = run_pass(env)
    print(json.dumps(result))
    return 1 if result["action"] == "rolled-back" else 0  # a halt is said once, not every ten minutes


if __name__ == "__main__":
    sys.exit(main())
