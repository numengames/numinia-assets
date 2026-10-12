<!--
SPDX-FileCopyrightText: 2026 Numen Games S.L.
SPDX-License-Identifier: CC-BY-4.0
-->

# open-worlds/machine/ — how a machine of the fleet is made

A machine of the public fleet is a small server that keeps its own book of
orders and keeps its containers equal to it. Oracles change the book from
the worlds room on numinia.com: each request is signed with the wallet the
Oracle signs in with, and the machine checks the signature itself. The
machine never writes to git and holds no GitHub credential; the console
holds no key to the machine. This folder is the whole recipe. It lives
beside the trial's first orders (numinia-archive `ADR-069`, `MIS-156`) and
moves with them.

| File | Does |
|---|---|
| `bootstrap.sh` | turns a fresh Debian 12 or Ubuntu (24.04 or later) VPS into a fleet machine, or updates one, in one line |
| `fleetd.py` | the door: takes an Oracle's signed request, shows the machine's public facts, hands out copies |
| `reconcile.py` | every minute and on each request: checks the requests again, changes the book, makes the containers match, deletes, zips copies |
| `walletsig.py` | who signed a request: EIP-712 typed data, recovered to a wallet address |
| `../../tests/test_machine.py`, `test_requests.py`, `test_fleetd.py`, `test_walletsig.py` | the tests, run by the depot's `python -m unittest` |

## Bring up a machine

1. Buy a VPS (OVH, France; Debian 12 or Ubuntu 24.04 or later). Note its
   public IP — it goes in DNS, never in this repository.
2. Log in. The user depends on the image: `debian` on Debian, `ubuntu` on
   Ubuntu. OVH's VPS order does not ask for an SSH key (its Public Cloud
   does): the first login uses the password OVH emails, which it makes you
   change; then copy your key in with `ssh-copy-id` and use the key from
   then on.
3. Run the line the worlds room shows under the machine (LAP › Mundos): it
   carries the machine's alias, your wallet (the address you sign in to
   numinia.com with; several Oracles go separated by commas) and the commit
   of this repository the room was built for:

   ```sh
   curl -fsSL https://raw.githubusercontent.com/numengames/numinia-assets/<commit>/open-worlds/machine/bootstrap.sh \
     | sudo FLEET_REF=<commit> bash -s -- open-1 0xYourWallet
   ```

   It installs Docker (from Docker's repository on Debian, from Ubuntu's own
   archive on Ubuntu; an existing Docker is kept) and `python3-ecdsa`, adds
   a 2 GB swap file, opens only ports 22, 80 and 443, turns on unattended
   upgrades, clones this repository to `/srv/fleet/book` and leaves it on
   exactly the commit in `FLEET_REF` (the recipe), creates the door's own
   user (`fleetd`), installs the programs, writes the alias, the machine's
   address for the console (`<alias>.numinia.com`), the Oracles' wallets
   and the folder for each world's admin code, adopts the machine's first
   orders from the public book (once), and starts the door and the
   reconciler. It ends by saying which commit it installed and by checking
   that the door and the reconciler are running: if they are not, it says
   so in capitals and ends with an error, because such a machine takes
   every signed request and does none of them. Running it again is safe,
   keeps every world's code, and keeps the wallets if you leave them out.
4. DNS: while the fleet has one machine, the wildcard `*.numinia.com` already
   points at it, which covers its address for the console and every world
   under numinia.com. A second machine needs an `A` record for
   `<alias>.numinia.com` (and its worlds' names).

The programs are copied to `/usr/local/lib/fleet/` by the bootstrap and run
from there, never from the clone. A request changes orders, not the code
that obeys them. To update a machine, run its line again.

**One commit, pinned.** `FLEET_REF` is a whole commit (40 hex characters):
the bootstrap fetches exactly that one, installs nothing if what it got is
another, and writes it to `/etc/fleet/recipe`; the door says it
(`/fleet/v1/machine` → `recipe`), so the room can tell a machine that runs
an older recipe. A later change to `main` reaches no machine until someone
hands out a line with its commit. Without `FLEET_REF` the bootstrap installs
`main`'s newest commit and says so: *NOT pinned*.

## Bring up a world

From the worlds room on numinia.com (LAP › Mundos › *+ Nuevo mundo*): its
name, the machine, the address, the engine and the limits, then *Create*.
The wallet asks you to sign one message — `CreateWorld`, with the world,
its address and the machine, which the wallet shows you before you sign.
The room sends it to the machine's door; the machine checks it, writes the
order, makes the world's keys, starts it and tells Caddy its address. That
is a reload of Caddy's whole configuration, which by default closes every
WebSocket it carries — every visitor of every world on the machine would
see "You have been disconnected". Each world's site therefore keeps its
connections open across a reload (`stream_close_delay 12h`). The world
comes up at `https://<domain>` within a minute, and the room shows the
world's own admin code: type `/admin <code>` in the world's chat. Nothing
is done on the machine by hand.

### The keys, made by the machine

Each world has its own admin code. The door makes it when the world is
created — 16 random hex characters — and shows it once to the Oracle who
signed; the inbox entry carries it to the reconciler, which writes the
world's key file `env/<id>.env` (mode 600, never in git): a JWT secret of
the world's own, 64 random hex characters nobody needs to read, and that
code. It also writes the code to `/etc/fleet/codes/<id>` (`root:fleetd`,
mode 640), where the door reads it to show it to a signed Oracle. The key
file is the truth — the engine reads it — and every run makes each code
file say what its key file gives. A world created under a name a world had
before gets new keys, never the old ones.

A key file can still be written by hand, with the optional
`LIVEKIT_WS_URL`, `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` lines for
voice; a change of code rewrites only its `ADMIN_CODE` line:

```sh
sudo install -m 600 /dev/null /srv/fleet/env/<id>.env
printf 'JWT_SECRET=%s\nADMIN_CODE=%s\n' "$(openssl rand -hex 32)" "$(openssl rand -hex 8)" \
  | sudo tee /srv/fleet/env/<id>.env >/dev/null
```

The machine's old shared code (`/etc/fleet/admin-code`, made once by the
bootstrap) is what the worlds keyed before each had its own still have,
until an Oracle changes theirs, and what an order adopted from the public
book is keyed with. A machine with no such code (no file, or anything in it
but 8 to 64 letters, digits, `-` or `_`) keys no adopted order: that world
is **not** started and the reconciler says so in its log, because a world
with an empty admin code would make every visitor an admin.

## Stop, start, download, delete, a world's code

The same way: one signed message each, from the world's card in the room.

- **Stop** takes the container down and leaves `/srv/fleet/data/<id>/` as
  it is; **start** brings it back. The engine writes what was placed,
  moved or edited in a world to its database every `SAVE_INTERVAL`
  seconds (10 here), and not when it is told to stop, so the reconciler
  waits for the world's next save before Docker stops it: a stop takes up
  to 12 seconds longer, and loses nothing.
- **Download** zips the world whole, after its next save: a consistent
  copy of its database (SQLite's online backup, safe while the world
  runs), its files, a compose
  file that runs it on `localhost:3000` with the same engine build, **fresh
  keys** (not the published world's), a manifest (`world.json`) with each
  file's size and SHA-256, and a short guide. The room gets a link to it
  that works for a day; a second download of the same world within ten
  minutes gets the same zip. This is the Alchemists' Tower's copy, made by
  the machine itself.
- **Delete** asks you, in the room, to type the world's name. Then the
  order goes, Docker removes the container, and the world's folder, keys
  and copies are deleted. It cannot be undone: download a copy first if
  you may want it back. Until the folder and keys are gone, the door
  refuses a new world under the same name or address (`still-deleting`):
  it would inherit them.
- **Show its code** gives the world's admin code to the Oracle who signed,
  and changes nothing; the log says who asked. **Change its code** makes a
  new one, which the door shows once to the Oracle who signed: the world
  keeps its JWT secret (everyone who has been in it keeps who they are)
  and is restarted alone, after its next save, so the engine reads the new
  code and whoever is inside reconnects in a few seconds. A stopped world
  takes its new code when it starts. A leaked code is closed with one
  signature. While a change of code, or the world's creation, waits in the
  inbox, the door answers `busy` to another show or change of that world.

## The signed requests

Each request is EIP-712 typed data (domain `Numinia Fleet`, version `1`) —
the standard way a wallet signs an off-chain message that a server checks,
as Snapshot does with votes. The wallet shows every field before signing.

| Request | Fields |
|---|---|
| `CreateWorld` | machine, world, domain, image, memory, cpus, maxUploadMb, nonce, expires |
| `DeleteWorld` | machine, world, domain, nonce, expires |
| `StopWorld`, `StartWorld`, `DownloadWorld` | machine, world, nonce, expires |
| `ShowWorldCode`, `RotateWorldCode` | machine, world, nonce, expires |
| `ShowAdminCode` | machine, nonce, expires — the machine's old shared code, for the room that still asks for it |

`nonce` is 128 random bits chosen by the browser; `expires` is at most
fifteen minutes ahead. The door refuses a request for another machine, an
expired one, one whose number it has seen, one from another site's page
(only numinia.com's room may send them from a browser; a script with no
`Origin` may, so the machine still works if the console is gone), one not
signed by a wallet listed in `/etc/fleet/signers`, and more than twenty
requests from one address in five minutes. Then the reconciler checks the
signature, the signer, the machine and the fields **again** before it
touches the book. What the machine did is public:
`https://<alias>.numinia.com/fleet/v1/worlds`, `/log`, `/machine`.
`/machine` says which commit of this repository the machine's programs came
from (`recipe`, `null` when the bootstrap that set it up wrote none), and
whether the machine keeps up: when the reconciler last ran and whether that
run ended well (`lastRun`, with the broken rule, never a value), and how
many requests wait in the inbox and since when (`waiting`). A time that
stops moving, or a request that waits more than a
minute or two, is a machine that has stopped obeying.

## Where things are on the machine

| Path | Holds |
|---|---|
| `/srv/fleet/orders/<id>.json` | the machine's book: one order per world |
| `/srv/fleet/inbox/` | signed requests the door took and the reconciler has not yet |
| `/srv/fleet/requests/<n>.json` | how each request went, as the door shows it |
| `/srv/fleet/copies/` | the copies, for a day (`<c>.zip` and `<c>.json`) |
| `/srv/fleet/data/<id>/` | the world: `db.sqlite` and `assets/` |
| `/srv/fleet/env/<id>.env` | the world's keys: its JWT secret and its admin code (mode 600, made by the machine, never in git) |
| `/srv/fleet/run/` | the generated `docker-compose.yml`, `Caddyfile` and Caddy's state; `Caddyfile.applied` is the list of sites Caddy last accepted and `docker-compose.applied.yml` the compose file Docker last accepted; `purge.json` and `copy-queue.json` hold deletes and copies in progress, `restart.json` the worlds whose code changed and that wait to be restarted; `last-run.json` says when the reconciler last ran and how it ended |
| `/srv/fleet/sock/fleetd.sock` | the door's socket; Caddy is the only way in to it |
| `/srv/fleet/log.jsonl` | what was done here: when, what, which world, a short wallet |
| `/srv/fleet/book/` | the clone of this repository, left on the commit the machine's line named: the recipe, and the orders adopted once |
| `/etc/fleet/alias` | this machine's alias |
| `/etc/fleet/recipe` | the commit the machine's programs were installed from |
| `/etc/fleet/signers` | the Oracles' wallets this machine obeys, one per line |
| `/etc/fleet/control-host` | the machine's address for the console, e.g. `open-1.numinia.com` |
| `/etc/fleet/codes/<id>` | each world's admin code, as its key file gives it (`root:fleetd`, mode 640: the door shows it to a signed Oracle) |
| `/etc/fleet/admin-code` | the machine's old shared code: the worlds keyed before each had its own keep it until it is changed, and an adopted order gets it (`root:fleetd`, mode 640) |
| `/etc/fleet/domains` | client domains this machine may serve (optional) |
| `/etc/fleet/console-origins` | other consoles allowed to send requests from a browser, one origin per line (optional; numinia.com always is) |
| `/var/lib/fleetd/nonces.json` | the request numbers the door already took |

## Look

```sh
journalctl -u fleet-reconcile -n 20                     # what the last runs did
journalctl -u fleetd -n 20                              # what the door accepted
docker compose -f /srv/fleet/run/docker-compose.yml ps  # what is running
fleet-reconcile --dry-run                               # the plan, touching nothing
curl https://open-1.numinia.com/fleet/v1/worlds         # what the console sees
```

## What the machine refuses

- A request that is not signed by an Oracle listed on the machine, is for
  another machine, is expired, or was seen before: refused by the door and
  again by the reconciler.
- An order whose image is not from `ghcr.io/numengames/` pinned to one build
  (`:sha-…` or `@sha256:…`; never `dev` or `latest`): refused and logged.
- An order whose domain is not one label under a house zone (`numinia.com`,
  `numen.games`) and is not listed in `/etc/fleet/domains` on the machine,
  or is the machine's own address: refused and logged. A client's own
  domain (`mundo.cliente.example`, or `*.cliente.example` for all of them)
  is written there by an Oracle when the client's DNS is set up; one line
  per domain, `#` for comments.
- An order any of whose fields does not match its strict pattern — `id` and
  the domain's labels are lowercase letters, digits and hyphens, `state` is
  `running` or `stopped`, `limits.memory` is like `512m` or `2g`,
  `limits.cpus` like `1.5`, `limits.maxUploadMb` a positive whole number —
  or whose `id` is `caddy` or `index`: refused and logged. Only checked
  values reach the compose file, and that file is built as data and written
  as JSON (which is YAML), so no field can add a setting to it. The log
  names the broken rule, never the value.
- A `running` order adopted from the public book, with no `env/<id>.env`,
  on a machine with no old shared code: not started, logged.

## Security, in one table

| If this is stolen… | …the thief gets | …and does not get |
|---|---|---|
| the machine | its worlds' data and keys, and the admin code of every world on it | GitHub (no credential), the other machines, the console, any Oracle's wallet |
| the console (numinia.com) | nothing on the machines: it holds no key and signs nothing. It could show an Oracle a false page and ask for a signature — the wallet shows exactly what is signed (the request, the world, its address, the machine) | any machine, any world's keys |
| an Oracle's wallet | create, stop, start, download and delete worlds on the machines that list it; show and change any of their worlds' admin codes | code execution on machines (the programs are installed, not pulled, and an order's fields are checked values, never configuration); images outside the house's registry or unpinned; domains outside the house zones or the machine's list |
| the door (`fleetd`) broken into | the book, the copies and every world's admin code, read; requests put in the inbox, and the code a world gets when an Oracle signs its creation or a change of its code (the door makes it) | anything signed: the reconciler checks every signature again, and the door has no root, no Docker, no network of its own, and writes only its inbox, its socket and its list of used numbers — never a key file |
| a signed request, intercepted | nothing more: it is good once, for one machine and one world, for fifteen minutes | anything else |
| a download link | that copy, for a day | the world's keys (the copy's are fresh), anything else |
| a world's admin code | admin of that world, from any browser, until an Oracle changes it with one signature | the other worlds (each has its own), the machine, its files, the other machines |
| a merge on this repository's `main` | nothing on any machine by itself: a machine installs only the commit its line names (`FLEET_REF`) and says which (`recipe`) | the machines, until someone hands out and runs a line with that commit |

Log in to the machine with an SSH key, never a password (after the first
login, see *Bring up a machine*). Docker itself runs as root, and so does
the engine inside its container (see *Debts*).

## Debts, on purpose

The fleet is a trial, not yet in alpha: these make a world appear, travel
and go with nothing done by hand, and are written here so that they are
paid, not forgotten.

- **An admin code is still a shared secret.** Each world has its own, and
  one signature changes it, but whoever holds it administers that world
  from any browser, and it is typed in a chat. What pays it: each world's
  administrators named in its order as wallets, proved by a signature
  (decided for the fleet, not built yet).
- **The machine's old shared code lives on.** The worlds keyed before each
  had its own keep it until an Oracle changes theirs, an order adopted from
  the public book is keyed with it, and `ShowAdminCode` still shows it.
  What pays it: changing the code of every world made before, then
  retiring `ShowAdminCode` once no room asks for it.
- **A pin is only as good as the commit it names.** A machine installs the
  commit its line carries; nothing yet requires a review before a merge on
  this repository's `main`. What pays it: a rule on `main` that does.
- **The engine runs as root in its container.** Its image creates a user it
  never switches to. What pays it: `user:` in the compose file and a `chown`
  of `data/<id>/`.
- **The book lives only on the machine.** A lost machine loses its book with
  its worlds; the public log tells what was done. What pays it: the nightly
  copy (`MIS-158`) takes the book with the worlds.
- **No nightly copy yet.** A copy is made when someone downloads one. What
  pays it: the nightly copy (`MIS-158`).
- **The machine waits for the engine's save, from outside.** The engine
  saves on a timer and not when it is stopped, so the machine passes a
  short `SAVE_INTERVAL` (10 s) and waits one interval before a stop or a
  copy. A world that dies without warning (a crash, a reboot, no memory
  left) still loses up to 10 seconds. What pays it: the engine saving when
  it is told to stop (`SIGTERM`), in its own repository.
- **The Oracles are listed by hand.** Adding or removing one is a re-run of
  the bootstrap line with the new list. What pays it: owners by wallet in
  the order, and a signed request that changes the list.
- **A download link is the key to its copy.** Whoever holds it can download
  that copy for a day, like a pre-signed link. What pays it, if needed: a
  signature at download time too.

## Why the machine and not a pull request

The book used to be `open-worlds/` in this repository, changed by pull
request and reviewed (`ADR-069`). A world could not be born without a
person merging, and nothing could reach a world's files to copy them. A
hosting console is, by the industry's convention, a control plane that
talks to an agent on each host (Hermes Cloud, Railway, Fly.io). Here the
agent is the door, and the console holds no key at all: the Oracle's
wallet signs each request and the machine checks it (EIP-712), which keeps
the rule that a stolen website cannot touch the machines.

## Why a script and not Doco-CD

`MIS-156` named Doco-CD as the first candidate. The order is JSON, not a
compose file, so a git-to-compose tool would need a translation step anyway;
the translation *is* the reconciler, pure functions with tests. If the fleet
grows past one compose file per machine, revisit.
