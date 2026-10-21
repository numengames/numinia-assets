#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: AGPL-3.0-only
"""The fleet machine's by-hand switch for a world's conditions-of-entry door: `fleet-condiciones`.

    sudo fleet-condiciones <world> on      [--source PATH|URL] [--dry-run]
    sudo fleet-condiciones <world> status
    sudo fleet-condiciones <world> restart [--wait SECONDS]  [--dry-run]
    sudo fleet-condiciones <world> off     [--dry-run] [--force]

A Numinia Engine world reads `aventura/condiciones.json` once, when it starts.
Its door ("puerta") is OFF unless that file says `"puerta": true` (or the
container has MOTOR_CONDICIONES_PUERTA=si, which wins over the file). This
program puts that file in, takes it out and shows where the world stands. It
is installed by the bootstrap, never run from the clone.

Two steps, on purpose. `on` and `off` only change a file (harmless: the world
keeps running as it was) and say so; `restart` is the step that cuts service,
for that one world only (`docker restart run-<world>-1`, never the compose
file), and waits until the container is healthy and its log says which state
the door started in. `status` reads and changes nothing.

Rules it keeps:

* Only a world that exists here: an existing /srv/fleet/data/<world>, a
  service of that name in the generated compose file, running the Numinia
  Engine. Anything else is refused before anything is touched. The compose
  file is read, never written, and the machine's key files are never printed.
* `on` checks the JSON before it places it: it must parse, be a
  `condiciones@1` document with layers and say `"puerta": true`. The default
  source is the engine's test file at a pinned commit (placeholder text, not
  for real visitors), checked against its SHA-256. `--source` takes an
  absolute path or an https address.
* A file the world already had is never lost: it is copied to
  `condiciones.json.antes` first, a note `condiciones.json.puesta` records
  what was placed and whether there was a file before, and `off` puts the
  original back. Where there was none, `off` deletes only what `on` placed. A
  file nobody here placed and with no `.antes` is not touched without --force
  (it is moved aside, never deleted).
* The world's folder belongs to the engine's user (10001) and a world can
  plant links in it: a link where a file or folder should be is refused;
  files are written to a new name and moved into place whole, owned by 10001.
* Every run ends with one sentence saying what happened and what to do next.
  Exit codes: 0 done; 1 not root or unexpected; 2 wrong usage; 3 unknown world;
  4 the source is unusable; 5 the world's files are not as expected; 6 the
  restart failed; 7 the world is not (yet) as the file says it should be.
* `--dry-run` prints the plan and changes nothing (it does read the source).

Everything outside (docker, network, clock, ownership) is passed in, so the
tests run with no Docker and no root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import reconcile as R

# The engine's own placeholder conditions (numengames/numinia-engine,
# aventuras/condiciones-prueba/condiciones.json), at one commit so that what is
# placed is what was read, whatever `main` becomes. The texts are EXAMPLES.
ENGINE_COMMIT = "1b04bd1dd3b21cc4fa5e89b103a9e51cb6d59714"
DEFAULT_SOURCE = (
    "https://raw.githubusercontent.com/numengames/numinia-engine/"
    f"{ENGINE_COMMIT}/aventuras/condiciones-prueba/condiciones.json"
)
DEFAULT_SHA256 = "c7facdd23c653d1d62bc43be389808d312c5074c50523a99b1239f2ae642d3df"
MAX_BYTES = 256 * 1024
FORMAT = "condiciones@1"
FILE = "condiciones.json"
BEFORE = FILE + ".antes"  # the world's own file, kept when ours goes in
NOTE = FILE + ".puesta"  # what this program placed: {antes, sha256, source, at}
ASIDE = FILE + ".quitado"  # a file moved aside by `off`: ASIDE-<stamp>
SAVED = BEFORE + "-"  # a file moved aside by `on`: BEFORE-<stamp>
DOOR_ENV = "MOTOR_CONDICIONES_PUERTA"
CONTAINER = "run-{}-1"  # compose project `run` (the folder of the generated file), service = world
LOG_MARK = "[condiciones]"
COMPOSE_FILE = "docker-compose.yml"

OK, GENERAL, USAGE, UNKNOWN_WORLD, SOURCE, FILES, RESTART, NOT_AS_ASKED = 0, 1, 2, 3, 4, 5, 6, 7


class Refusal(Exception):
    """A run that stops, with the exit code and the sentence to say."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def default_run(args: list[str], timeout: int) -> tuple[int, str, str]:
    """Run a command with no stdin; (exit code, stdout, stderr). A missing program or a timeout is a failure, not a crash."""
    try:
        done = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "", f"{args[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{' '.join(args[:2])}: no answer in {timeout}s"
    return done.returncode, done.stdout, done.stderr


def default_fetch(url: str) -> bytes:
    """GET an https address, at most MAX_BYTES, 20 s. Raises OSError for anything that is not a plain answer."""
    request = urllib.request.Request(url, headers={"User-Agent": "fleet-condiciones"})
    try:
        with urllib.request.urlopen(request, timeout=20) as answer:  # noqa: S310 — https checked by the caller
            data = answer.read(MAX_BYTES + 1)
    except (urllib.error.URLError, ValueError) as error:  # HTTPError is a URLError
        raise OSError(str(getattr(error, "reason", error))) from error
    return data


@dataclass
class Env:
    home: Path = R.HOME
    run: Callable[[list[str], int], tuple[int, str, str]] = default_run
    fetch: Callable[[str], bytes] = default_fetch
    euid: Callable[[], int] = os.geteuid
    chown: Callable[[Path, int, int], None] = os.chown
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    out: list[str] = field(default_factory=list)
    echo: bool = True

    def say(self, line: str = "") -> None:
        self.out.append(line)
        if self.echo:
            print(line, flush=True)


@dataclass
class World:
    wid: str
    aventura: Path
    container: str
    service: dict

    @property
    def file(self) -> Path:
        return self.aventura / FILE


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stamp(env: Env) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(env.now()))


# ----------------------------------------------------------------- the world

def find_world(env: Env, name: str) -> World:
    """The world, or a refusal. Reads the compose file; writes nothing."""
    if not R.matches(R.SLUG, name) or name == R.CADDY_SERVICE:
        raise Refusal(UNKNOWN_WORLD, f"'{R.shown(name)}' is not a world name (lowercase letters, digits and hyphens). Nothing was touched.")
    data = env.home / "data" / name
    if data.is_symlink() or not data.is_dir():
        raise Refusal(UNKNOWN_WORLD, f"There is no world '{name}' on this machine ({data} does not exist). Nothing was touched.")
    compose = env.home / "run" / COMPOSE_FILE
    try:
        services = json.loads(compose.read_text(encoding="utf-8")).get("services", {})
    except (OSError, ValueError, AttributeError):
        raise Refusal(UNKNOWN_WORLD, f"Cannot read {compose} as the machine writes it (JSON). Nothing was touched.") from None
    service = services.get(name) if isinstance(services, dict) else None
    if not isinstance(service, dict):
        raise Refusal(UNKNOWN_WORLD, f"'{name}' has a folder but no container in {compose}: it is not a running world of this machine. Nothing was touched.")
    if R.engine_of(service.get("image")) != R.NUMINIA_ENGINE:
        raise Refusal(UNKNOWN_WORLD, f"'{name}' does not run the Numinia Engine: it has no conditions door. Nothing was touched.")
    return World(name, data / "aventura", CONTAINER.format(name), service)


def door_in_environment(env: Env, world: World) -> bool | None:
    """What MOTOR_CONDICIONES_PUERTA says for this world (the compose file's setting wins over the key file's), or None.

    Only this one key is looked at, and only its parsed meaning leaves this
    function: the key file holds secrets and is never printed.
    """
    raw: object = None
    settings = world.service.get("environment")
    if isinstance(settings, dict):
        raw = settings.get(DOOR_ENV)
    elif isinstance(settings, list):
        for item in settings:
            if isinstance(item, str) and item.startswith(DOOR_ENV + "="):
                raw = item.split("=", 1)[1]
    if raw is None:
        try:
            lines = (env.home / "env" / f"{world.wid}.env").read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            match = re.fullmatch(rf"[ \t]*(?:export[ \t]+)?{DOOR_ENV}[ \t]*=[ \t]*(.*?)[ \t]*", line)
            if match:
                raw = match.group(1).strip("\"'")
    value = str(raw).strip().lower() if raw is not None else ""
    if value in ("si", "sí", "1"):
        return True
    if value in ("no", "0"):
        return False
    return None  # not said, or something the engine ignores


# ------------------------------------------------------------------ the files

@dataclass
class State:
    current: bytes | None
    before: bytes | None
    note: dict | None


def read_regular(path: Path) -> bytes | None:
    """A regular file's bytes, None when absent; a link or anything else is refused (a world can plant links)."""
    if not path.is_symlink() and not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise Refusal(FILES, f"{path} is not a plain file (a link or something else). Not touching it; look at it by hand.")
    return path.read_bytes()


def read_state(world: World) -> State:
    if world.aventura.is_symlink():
        raise Refusal(FILES, f"{world.aventura} is a link. Not touching it; look at it by hand.")
    if not world.aventura.is_dir():
        raise Refusal(FILES, f"{world.aventura} does not exist: the world has not started yet (the engine makes that folder the first time). "
                             "Start it once, then run this again. Nothing was touched.")
    current = read_regular(world.file)
    before = read_regular(world.aventura / BEFORE)
    raw = read_regular(world.aventura / NOTE)
    note = None
    if raw is not None:
        try:
            note = json.loads(raw)
            ok = isinstance(note, dict) and isinstance(note.get("antes"), bool) and R.matches(R.SHA256, note.get("sha256"))
        except ValueError:
            ok = False
        if not ok:
            raise Refusal(FILES, f"{world.aventura / NOTE} is not what this program writes. Not touching it; look at it by hand.")
    return State(current, before, note)


def put(env: Env, path: Path, data: bytes) -> None:
    """Write a file whole under a new name and move it into place, owned by the engine's user."""
    tmp = path.with_name(f".{path.name}.fleet-tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        env.chown(tmp, R.ENGINE_UID, R.ENGINE_GID)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def layers(data: bytes) -> str:
    """The layers a conditions file declares, as the engine's own log says them: `casa 1.0 (6) → comunidad 1.0 (5)`."""
    try:
        doc = json.loads(data)
        return " → ".join(f"{c['capa']} {c['version']} ({len(c['puntos'])})" for c in doc["capas"])
    except (ValueError, KeyError, TypeError):
        return "unreadable"


def says_door_on(data: bytes) -> bool | None:
    """What the file says about the door: True/False for `puerta`, None when absent or not valid JSON."""
    try:
        doc = json.loads(data)
    except ValueError:
        return None
    return doc.get("puerta") if isinstance(doc, dict) and isinstance(doc.get("puerta"), bool) else None


def check_document(data: bytes, where: str) -> None:
    """Refuse a source that is not a usable conditions document that asks for the door."""
    if len(data) > MAX_BYTES:
        raise Refusal(SOURCE, f"The source is bigger than {MAX_BYTES // 1024} KiB ({where}). Nothing was changed.")
    try:
        doc = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise Refusal(SOURCE, f"The source is not valid JSON ({where}): {error}. Nothing was changed.") from None
    if not isinstance(doc, dict) or doc.get("formato") != FORMAT:
        raise Refusal(SOURCE, f"The source is not a '{FORMAT}' document ({where}). Nothing was changed.")
    capas = doc.get("capas")
    if not (isinstance(capas, list) and capas and all(isinstance(c, dict) and isinstance(c.get("capa"), str)
                                                       and isinstance(c.get("puntos"), list) and "version" in c for c in capas)):
        raise Refusal(SOURCE, f"The source has no usable layers ({where}). Nothing was changed.")
    if doc.get("puerta") is not True:
        raise Refusal(SOURCE, f'The source does not say "puerta": true ({where}): placing it would not turn the door on. Nothing was changed.')


def load_source(env: Env, source: str | None) -> tuple[bytes, str]:
    """The conditions file to place and where it came from: fetched or read, then checked. Changes nothing."""
    source = source or DEFAULT_SOURCE
    if source.startswith("https://"):
        try:
            data = env.fetch(source)
        except OSError as error:
            raise Refusal(SOURCE, f"Could not download the source ({source}): {error}. Nothing was changed.") from None
        if source == DEFAULT_SOURCE and sha(data) != DEFAULT_SHA256:
            raise Refusal(SOURCE, "The download is not the file this program pins (its SHA-256 differs). Nothing was changed.")
    elif re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", source):
        raise Refusal(SOURCE, f"The source '{source}' must be an absolute path or an https address. Nothing was changed.")
    else:
        path = Path(source)
        if not path.is_absolute():
            raise Refusal(SOURCE, f"The source '{source}' must be an absolute path (or an https address). Nothing was changed.")
        try:
            if not path.is_file():
                raise OSError("not a file")
            data = path.read_bytes()[: MAX_BYTES + 1]
        except OSError as error:
            raise Refusal(SOURCE, f"Could not read the source {source}: {error}. Nothing was changed.") from None
    check_document(data, source if source != DEFAULT_SOURCE else "the engine's test file, pinned")
    return data, source


# --------------------------------------------------------------------- docker

def inspect(env: Env, container: str) -> dict | None:
    code, out, _err = env.run(["docker", "inspect", container], 30)
    if code != 0:
        return None
    try:
        info = json.loads(out)[0]
        state = info.get("State") or {}
        return {"status": state.get("Status"), "health": (state.get("Health") or {}).get("Status"),
                "restarts": info.get("RestartCount"), "started": state.get("StartedAt")}
    except (ValueError, IndexError, AttributeError):
        return None


def door_log(env: Env, container: str, since: str | None = None) -> tuple[list[str], bool | None]:
    """The container's `[condiciones]` log lines (since a time, if given) and the door state the last `puerta` line says."""
    args = ["docker", "logs"] + (["--since", since] if since else []) + [container]
    code, out, err = env.run(args, 60)
    lines = [line.strip() for line in (out + "\n" + err).splitlines() if LOG_MARK in line]
    started: bool | None = None
    for line in lines:
        if f"{LOG_MARK} puerta ENCENDIDA" in line:
            started = True
        elif f"{LOG_MARK} puerta apagada" in line:
            started = False
    return (lines if code == 0 else []), (started if code == 0 else None)


def wanted(env: Env, world: World, state: State) -> tuple[bool, str]:
    """The door state the world should start in, and why: the environment wins over the file, as in the engine."""
    in_env = door_in_environment(env, world)
    if in_env is not None:
        return in_env, f"{DOOR_ENV}={'si' if in_env else 'no'} in the container's settings (it wins over the file)"
    on = state.current is not None and says_door_on(state.current) is True
    return on, ("the file says \"puerta\": true" if on else "the file is absent or does not ask for the door")


# ------------------------------------------------------------------- commands

def cmd_on(env: Env, world: World, source: str | None, dry: bool) -> int:
    data, label = load_source(env, source)
    state = read_state(world)
    new = sha(data)
    env.say(f"source: {label} — valid, puerta true, {layers(data)}, sha256 {new[:12]}…")
    current, before, note = state.current, state.before, state.note
    cur_sha = sha(current) if current is not None else None
    ours = note is not None and cur_sha == note["sha256"]
    kept_as = None  # name a file that is not ours is kept under, before it is replaced
    if current is None or cur_sha == new or ours:  # nothing there to keep, or it is this very file, or one placed here
        had_before = bool(note["antes"]) if note else before is not None
    elif note is None and before is None:
        had_before, kept_as = True, BEFORE
    elif note is None and before is not None and cur_sha == sha(before):
        had_before = True  # the original is back (after `off`)
    else:
        had_before, kept_as = (bool(note["antes"]) if note else True), SAVED + stamp(env)  # edited since, or not what `.antes` holds: keep it too
    if cur_sha == new and note is not None and note["sha256"] == new:
        plan = ["nothing to write: the file is already in place"]
    elif cur_sha == new:
        plan = ["nothing to rewrite: the file already holds exactly this"]
    else:
        plan = ([f"keep the existing {FILE} as {kept_as}"] if kept_as else []) + [f"write {world.file}"]
        if current is None:
            plan = [f"write {world.file} (there is no {FILE} now)"]
    plan.append(f"write {world.aventura / NOTE}: {'there was a file before (kept)' if had_before else 'there was none before'}")
    for step in plan:
        env.say(("would " if dry and not step.startswith("nothing") else "") + step)
    if dry:
        env.say("=> Dry run: nothing was changed, and nothing restarts. `on` only places the file; the restart is its own step.")
        return OK
    if cur_sha != new:
        if kept_as and current is not None:
            put(env, world.aventura / kept_as, current)
        put(env, world.file, data)
    put(env, world.aventura / NOTE, (json.dumps({"antes": had_before, "sha256": new, "fuente": label, "en": R.utc(env.now())}, indent=2) + "\n").encode())
    placed = read_regular(world.file)
    if placed is None or sha(placed) != new or says_door_on(placed) is not True:
        raise Refusal(FILES, f"After writing, {world.file} is not what was placed (or lost \"puerta\": true). Check it by hand.")
    env.say(f'check: {world.file} is in place and says "puerta": true ({layers(placed)}).')
    env.say(f"=> The file is in place, but the running world has NOT changed: it reads it only when it starts. "
            f"Check it with `sudo fleet-condiciones {world.wid} status`, and when you are ready to cut service for this one world: "
            f"`sudo fleet-condiciones {world.wid} restart`.")
    return OK


def move_aside(env: Env, world: World, data: bytes) -> str:
    name = f"{ASIDE}-{stamp(env)}"
    put(env, world.aventura / name, data)
    return name


def cmd_off(env: Env, world: World, dry: bool, force: bool) -> int:
    state = read_state(world)
    current, before, note = state.current, state.before, state.note
    cur_sha = sha(current) if current is not None else None
    ours = note is not None and cur_sha == note["sha256"]
    steps: list[str] = []
    restore: bytes | None = None  # put this back as the file
    remove = False  # or take the file away
    aside = False  # after keeping a copy of what is there, because it is not exactly what was placed
    if note is not None and note["antes"] or note is None and before is not None:
        if before is None:
            raise Refusal(FILES, f"The note says there was a file before, but {world.aventura / BEFORE} is missing: cannot restore it. Nothing was changed.")
        if cur_sha == sha(before):
            steps.append(f"nothing to restore: {FILE} already is the original")
        else:
            restore, aside = before, current is not None and not ours
            steps += [f"keep the {FILE} that is there (it is not what was placed) as {ASIDE}-…"] if aside else []
            steps.append(f"restore {world.file} from {BEFORE} (the {BEFORE} copy stays)")
    elif note is not None:  # there was none before: only what was placed goes
        if current is None:
            steps.append(f"nothing to delete: there is no {FILE}")
        else:
            remove, aside = True, not ours
            steps += [f"keep the {FILE} that is there (it is not exactly what was placed) as {ASIDE}-…"] if aside else []
            steps.append(f"delete {world.file} (there was none before)")
    elif current is not None:  # no note, no .antes: nobody says this file is ours
        if not force:
            raise Refusal(FILES, f"{world.file} exists but nothing records that it was placed here and there is no {BEFORE} to put back. "
                                 f"Not touching it. If it is a placeholder you want gone, run again with --force: it is moved aside ({ASIDE}-…), not deleted.")
        remove = aside = True
        steps.append(f"move {world.file} aside as {ASIDE}-… (--force; not deleted)")
    else:
        steps.append(f"nothing to do: there is no {FILE}")
    if note is not None:
        steps.append(f"delete the note {world.aventura / NOTE}")
    for step in steps:
        env.say(("would " if dry and not step.startswith("nothing") else "") + step)
    if dry:
        env.say("=> Dry run: nothing was changed, and nothing restarts. `off` only changes the file; the restart is its own step.")
        return OK
    if aside and current is not None:
        move_aside(env, world, current)
    if restore is not None:
        put(env, world.file, restore)
    elif remove and current is not None:
        world.file.unlink()
    (world.aventura / NOTE).unlink(missing_ok=True)
    after = read_regular(world.file)
    asks = after is not None and says_door_on(after) is True
    file_says = "no file" if after is None else ('"puerta": true' if asks else "no door asked")
    env.say(f"check: {world.file} now: {file_says}.")
    in_env = door_in_environment(env, world)
    warning = (f" WARNING: the container's own settings have {DOOR_ENV}={'si' if in_env else 'no'}, which wins over the file."
               if in_env is not None and in_env != asks else "")
    env.say(f"=> The file is back to how it was before ({file_says}), but the running world has NOT changed: it reads it only when it starts. "
            f"Check with `sudo fleet-condiciones {world.wid} status`, then `sudo fleet-condiciones {world.wid} restart` for this one world.{warning}")
    return OK


def describe_container(info: dict | None) -> str:
    if info is None:
        return "NOT FOUND (docker does not know it)"
    return f"{info['status']}, health {info['health'] or 'none'}, restarts {info['restarts']}, started {info['started']}"


def cmd_status(env: Env, world: World) -> int:
    state = read_state(world)
    env.say(f"world: {world.wid}   container: {world.container}")
    if state.current is None:
        env.say(f"file: {world.file} — absent")
    else:
        asks = says_door_on(state.current)
        env.say(f"file: {world.file} — present, puerta {'true' if asks is True else 'false' if asks is False else 'not said'}, "
                f"{layers(state.current)}, sha256 {sha(state.current)[:12]}…")
    placed = "placed by this program" + (", original kept" if state.note and state.note["antes"] else ", there was none before") if state.note else "no note"
    env.say(f"undo: {placed}; {BEFORE} {'present' if state.before is not None else 'absent'}")
    in_env = door_in_environment(env, world)
    env.say(f"environment: {DOOR_ENV} " + ("not set" if in_env is None else f"= {'si' if in_env else 'no'} (wins over the file)"))
    info = inspect(env, world.container)
    env.say(f"container: {describe_container(info)}")
    lines, running = door_log(env, world.container)
    for line in lines[-2:]:
        env.say(f"log: {line}")
    if not lines:
        env.say("log: no [condiciones] line")
    want, why = wanted(env, world, state)
    word = lambda on: "ON" if on else "OFF"  # noqa: E731
    if info is None or info["status"] != "running" or info["health"] not in (None, "healthy"):
        env.say(f"=> Not healthy: the container is {describe_container(info)}. Look at it with `docker logs {world.container}`; this program changed nothing.")
        return NOT_AS_ASKED
    if running is None:
        env.say("=> The running world's log has no door line (an old engine, or the log was rotated). "
                f"By {why} the door should be {word(want)}; restart the world to be sure: `sudo fleet-condiciones {world.wid} restart`.")
        return NOT_AS_ASKED
    if running != want:
        env.say(f"=> NOT the same: the world started with the door {word(running)}, but {why} ⇒ {word(want)}. "
                f"It reads the file only at start: `sudo fleet-condiciones {world.wid} restart` (this world only).")
        return NOT_AS_ASKED
    env.say(f"=> All consistent: the door is {word(running)} in the running world, as {why}. Nothing to do.")
    return OK


def cmd_restart(env: Env, world: World, dry: bool, wait: int) -> int:
    state = read_state(world)
    if inspect(env, world.container) is None:
        raise Refusal(UNKNOWN_WORLD, f"Docker has no container {world.container}. Nothing was touched.")
    want, why = wanted(env, world, state)
    env.say(f"this will restart ONE container, {world.container} (docker restart; never the compose file); its visitors are disconnected for a moment.")
    env.say(f"after it the door should be {'ON' if want else 'OFF'}: {why}.")
    if dry:
        env.say(f"would run: docker restart {world.container}; then wait up to {wait}s for healthy and read its log.")
        env.say("=> Dry run: nothing was restarted.")
        return OK
    code, _out, err = env.run(["docker", "restart", world.container], 180)
    if code != 0:
        env.say(f"=> The restart FAILED (docker exit {code}): {err.strip()[:300]}. Look: `docker ps -a --filter name={world.container}` and `docker logs {world.container}`.")
        return RESTART
    deadline = env.now() + wait
    while True:
        info = inspect(env, world.container)
        if info is not None and info["status"] == "running" and info["health"] in (None, "healthy"):
            break
        if info is None or info["status"] in ("exited", "dead", "restarting") or info["health"] == "unhealthy" or env.now() >= deadline:
            env.say(f"=> The container did not become healthy in {wait}s: {describe_container(info)}. Look: `docker logs {world.container}`.")
            return NOT_AS_ASKED
        env.sleep(2)
    lines, running = door_log(env, world.container, since=info["started"])
    for line in lines[-2:]:
        env.say(f"log: {line}")
    word = lambda on: "ON" if on else "OFF"  # noqa: E731
    if running is None:
        env.say(f"=> {world.container} is healthy, but its log shows no door line since the start: cannot tell which state it is in. "
                f"Check `docker logs {world.container} | grep condiciones`.")
        return NOT_AS_ASKED
    if running != want:
        env.say(f"=> {world.container} is healthy, but started with the door {word(running)} and it should be {word(want)} ({why}). Check `sudo fleet-condiciones {world.wid} status`.")
        return NOT_AS_ASKED
    env.say(f"=> Done: {world.container} restarted, is healthy, and its door is {word(running)} ({why}).")
    return OK


# ----------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="fleet-condiciones", description="Turn a world's conditions-of-entry door on or off, in two steps (file, then restart), or show where it stands.")
    ap.add_argument("world")
    ap.add_argument("action", choices=("on", "off", "status", "restart"))
    ap.add_argument("--source", help="on: the conditions file, an absolute path or an https address (default: the engine's test file, pinned)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and change nothing (on/off/restart)")
    ap.add_argument("--force", action="store_true", help="off: also move aside a file nobody recorded as placed here and that has no .antes")
    ap.add_argument("--wait", type=int, default=120, help="restart: seconds to wait for the container to be healthy (default 120)")
    ap.add_argument("--home", type=Path, default=R.HOME, help=argparse.SUPPRESS)
    return ap


def main(argv: list[str] | None = None, env: Env | None = None) -> int:
    args = build_parser().parse_args(argv)  # a wrong usage exits 2 here
    env = env or Env()
    env.home = args.home if env.home == R.HOME else env.home
    try:
        if env.euid() != 0:
            raise Refusal(GENERAL, "This must run as root (the world's folder is the engine's user's): `sudo fleet-condiciones …`. Nothing was touched.")
        if args.source and args.action != "on":
            raise Refusal(USAGE, "--source only belongs to `on`.")
        if args.wait < 1:
            raise Refusal(USAGE, "--wait must be a positive number of seconds.")
        world = find_world(env, args.world)
        if args.action == "on":
            return cmd_on(env, world, args.source, args.dry_run)
        if args.action == "off":
            return cmd_off(env, world, args.dry_run, args.force)
        if args.action == "restart":
            return cmd_restart(env, world, args.dry_run, args.wait)
        return cmd_status(env, world)
    except Refusal as stop:
        env.say(f"=> {stop.message}")
        return stop.code
    except PermissionError as error:
        env.say(f"=> Permission denied ({error.filename}). Run it with sudo. Nothing more was changed.")
        return GENERAL
    except OSError as error:
        env.say(f"=> Stopped by an error on disk ({type(error).__name__}: {error}). Check the world's files by hand before running again.")
        return GENERAL


if __name__ == "__main__":
    sys.exit(main())
