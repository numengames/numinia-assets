<!--
SPDX-FileCopyrightText: 2026 Numen Games S.L.
SPDX-License-Identifier: CC-BY-4.0
-->

# open-worlds/machine/ — how a machine of the fleet is made

A machine of the public fleet is a small server that reads the orders in
`open-worlds/` and keeps its containers equal to them. It never writes to
git and holds no GitHub credential: the order book is public, it just pulls.
This folder is the whole recipe. It lives beside the orders during the trial
(numinia-archive `ADR-069`, `MIS-156`) and moves with them.

| File | Does |
|---|---|
| `bootstrap.sh` | turns a fresh Debian 12 or Ubuntu (24.04 or later) VPS into a fleet machine, in one line |
| `reconcile.py` | every minute: what runs equals what the orders say |
| `../../tests/test_machine.py` | the tests, run by the depot's `python -m unittest` |

## Bring up a machine

1. Buy a VPS (OVH, France; Debian 12 or Ubuntu 24.04 or later). Note its
   public IP — it goes in DNS, never in this repository.
2. Log in. The user depends on the image: `debian` on Debian, `ubuntu` on
   Ubuntu. OVH's VPS order does not ask for an SSH key (its Public Cloud
   does): the first login uses the password OVH emails, which it makes you
   change; then copy your key in with `ssh-copy-id` and use the key from
   then on.
3. Run, replacing `open-1` with the machine's alias:

   ```sh
   curl -fsSL https://raw.githubusercontent.com/numengames/numinia-assets/main/open-worlds/machine/bootstrap.sh \
     | sudo bash -s -- open-1
   ```

   It installs Docker (from Docker's repository on Debian, from Ubuntu's own
   archive on Ubuntu; an existing Docker is kept), adds a 2 GB swap file,
   opens only ports 22, 80 and 443, turns on unattended upgrades, clones this repository to `/srv/fleet/book`, writes the alias to
   `/etc/fleet/alias`, makes the machine's admin code (`/etc/fleet/admin-code`)
   and starts a timer that runs `reconcile.py` every minute. It ends by
   showing the admin code: keep it, it is the one you type to administer any
   world on this machine. Running it again is safe and keeps the same code.

That is the whole link between the machine and the fleet: the alias.

The reconciler is copied to `/usr/local/sbin/fleet-reconcile` by the
bootstrap and run from there, never from the clone. A merge on this
repository changes orders, not the code that obeys them. To update the
reconciler on a machine, run the bootstrap line again.

## Bring up a world on it

1. Point the world's domain (an `A` record) at the machine's IP. Caddy cannot
   issue the certificate before the name resolves. While the fleet has one
   machine, a wildcard record (`*.numinia.com` → the machine) covers every
   world at once. For a client's own domain, also add it to
   `/etc/fleet/domains` on the machine (see *What the machine refuses*).
2. Open a pull request with the order `open-worlds/<id>.json`, `"server"`
   equal to the alias, `"state": "running"`. When it merges, the machine
   pulls it within a minute, makes the world's keys, starts the world and
   tells Caddy about its address (a reload: the worlds already open stay
   connected), and the world comes up at `https://<domain>`. Nothing is done
   on the machine by hand.
3. To administer the world, open it and type `/admin <code>` in its chat,
   with the machine's admin code. The engine remembers the rank for that
   browser.

### The keys, made by the machine

The first time a `running` order has no `env/<id>.env`, the reconciler
writes one (mode 600, never in git): a JWT secret of the world's own, 64
random hex characters nobody needs to read, and the machine's admin code
from `/etc/fleet/admin-code`. A key file that already exists is never
touched, so a world can still be given keys of its own by hand, with the
optional `LIVEKIT_WS_URL`, `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` lines
for voice:

```sh
sudo install -m 600 /dev/null /srv/fleet/env/<id>.env
printf 'JWT_SECRET=%s\nADMIN_CODE=%s\n' "$(openssl rand -hex 32)" "$(openssl rand -hex 6)" \
  | sudo tee /srv/fleet/env/<id>.env >/dev/null
```

A machine with no admin code (no file, or anything in it but 8 to 64
letters, digits, `-` or `_`) makes no keys: a `running` order with no key
file is then **not** started and the reconciler says so in its log, because
a world with an empty admin code would make every visitor an admin.

## Stop, start, close

Change the order's `state` and merge. `stopped` takes the container down and
leaves `/srv/fleet/data/<id>/` as it is; `running` brings it back. Deleting
the order file does the same as `stopped`: the folder stays until a person
removes it.

## Where things are on the machine

| Path | Holds |
|---|---|
| `/srv/fleet/book/` | the clone of this repository |
| `/srv/fleet/env/<id>.env` | the world's keys (mode 600, made by the machine, never in git) |
| `/srv/fleet/data/<id>/` | the world: `db.sqlite` and `assets/` |
| `/srv/fleet/run/` | the generated `docker-compose.yml`, `Caddyfile` and Caddy's state; `Caddyfile.applied` is the list of sites Caddy last accepted |
| `/srv/fleet/copies/` | the nightly copies (`MIS-158`, not yet) |
| `/etc/fleet/alias` | this machine's alias |
| `/etc/fleet/admin-code` | the admin code every world keyed here gets (mode 600) |
| `/etc/fleet/domains` | client domains this machine may serve (optional) |

## Look

```sh
journalctl -u fleet-reconcile -n 20                     # what the last runs did
docker compose -f /srv/fleet/run/docker-compose.yml ps  # what is running
fleet-reconcile --dry-run                               # the plan, touching nothing
```

## What the machine refuses

- An order whose `server` is not its alias: not its business.
- An order whose image is not from `ghcr.io/numengames/` pinned to one build
  (`:sha-…` or `@sha256:…`; never `dev` or `latest`): refused and logged,
  even if merged.
- An order whose domain is not one label under a house zone (`numinia.com`,
  `numen.games`) and is not listed in `/etc/fleet/domains` on the machine:
  refused and logged. A client's own domain (`mundo.cliente.example`, or
  `*.cliente.example` for all of them) is written there by an Oracle when
  the client's DNS is set up; one line per domain, `#` for comments.
- An order any of whose fields does not match its strict pattern — `id` and
  the domain's labels are lowercase letters, digits and hyphens, `state` is
  `running` or `stopped`, `limits.memory` is like `512m` or `2g`,
  `limits.cpus` like `1.5`, `limits.maxUploadMb` a positive whole number —
  or whose `id` is `caddy` or `index`: refused and logged. `index.json`, the
  book's table of contents, is skipped before any order is read. Only checked values reach the
  compose file, and that file is built as data and written as JSON (which is
  YAML), so no field can add a setting to it. The log names the broken rule,
  never the value.
- A `running` order with no `env/<id>.env` on a machine with no admin code:
  not started, logged.

## Security, in one table

| If this is stolen… | …the thief gets | …and does not get |
|---|---|---|
| the machine | its worlds' data and keys, and the admin code of every world on it | GitHub (no credential), the other machines, the console |
| the console (numinia.com) | the power to open pull requests | any machine (no key to them), any world's keys |
| a reviewer's account | the power to merge orders: start, stop or add worlds that run the house's own engine images | code execution on machines (the reconciler is installed, not pulled, and an order's fields are checked values, never configuration); images outside the house's registry or unpinned; domains outside the house zones or the machine's list |
| the admin code | admin of every world on that machine, from any browser | the machine, its files, the other machines |

Log in to the machine with an SSH key, never a password (after the first
login, see *Bring up a machine*). Docker itself runs as root, and so does
the engine inside its container (see *Debts*).

## Debts, on purpose

The fleet is a trial, not yet in alpha: these make a world appear with
nothing done by hand, and are written here so that they are paid, not
forgotten.

- **One admin code per machine.** Whoever holds it administers every world
  on the machine; it is shown in a terminal and typed in a chat. What pays
  it: each world's administrators named in its order as wallets, proved by a
  signature (decided for the fleet, not built yet).
- **The engine runs as root in its container.** Its image creates a user it
  never switches to. What pays it: `user:` in the compose file and a `chown`
  of `data/<id>/`.
- **No copy of a world yet.** A lost machine is its worlds lost. What pays it:
  the nightly copy (`MIS-158`).

## Why a script and not Doco-CD

`MIS-156` named Doco-CD as the first candidate. The order is JSON, not a
compose file, so a git-to-compose tool would need a translation step anyway;
the translation *is* the reconciler, forty lines of pure functions with
tests. If the fleet grows past one compose file per machine, revisit.
