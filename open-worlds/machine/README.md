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
| `bootstrap.sh` | turns a fresh Debian 12 VPS into a fleet machine, in one line |
| `reconcile.py` | every minute: what runs equals what the orders say |
| `../../tests/test_machine.py` | the tests, run by the depot's `python -m unittest` |

## Bring up a machine

1. Buy a VPS (OVH, France; Debian 12). Note its public IP — it goes in DNS,
   never in this repository.
2. Log in once and run, replacing `open-1` with the machine's alias:

   ```sh
   curl -fsSL https://raw.githubusercontent.com/numengames/numinia-assets/main/open-worlds/machine/bootstrap.sh \
     | sudo bash -s -- open-1
   ```

   It installs Docker, opens only ports 22, 80 and 443, turns on unattended
   upgrades, clones this repository to `/srv/fleet/book`, writes the alias to
   `/etc/fleet/alias` and starts a timer that runs `reconcile.py` every
   minute. Running it again is safe.

That is the whole link between the machine and the fleet: the alias.

The reconciler is copied to `/usr/local/sbin/fleet-reconcile` by the
bootstrap and run from there, never from the clone. A merge on this
repository changes orders, not the code that obeys them. To update the
reconciler on a machine, run the bootstrap line again.

## Bring up a world on it

1. Point the world's domain (an `A` record) at the machine's IP. Caddy cannot
   issue the certificate before the name resolves.
2. On the machine, write the world's keys — the only thing done by hand, and
   the only thing that never enters git:

   ```sh
   sudo install -m 600 /dev/null /srv/fleet/env/<id>.env
   printf 'JWT_SECRET=%s\nADMIN_CODE=%s\n' "$(openssl rand -hex 32)" "$(openssl rand -hex 4)" \
     | sudo tee /srv/fleet/env/<id>.env >/dev/null
   ```

   Optional lines: `LIVEKIT_WS_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`
   for voice; `SAVE_INTERVAL` (seconds, default 60).
3. Open a pull request with the order `open-worlds/<id>.json`, `"server"`
   equal to the alias, `"state": "running"`. When it merges, the machine
   pulls it within a minute and the world comes up at `https://<domain>`.

A world whose order says `running` but has no `env/<id>.env` is **not**
started and the reconciler says so in its log: a world with an empty admin
code would make every visitor an admin.

## Stop, start, close

Change the order's `state` and merge. `stopped` takes the container down and
leaves `/srv/fleet/data/<id>/` as it is; `running` brings it back. Deleting
the order file does the same as `stopped`: the folder stays until a person
removes it.

## Where things are on the machine

| Path | Holds |
|---|---|
| `/srv/fleet/book/` | the clone of this repository |
| `/srv/fleet/env/<id>.env` | the world's keys (mode 600, never in git) |
| `/srv/fleet/data/<id>/` | the world: `db.sqlite` and `assets/` |
| `/srv/fleet/run/` | the generated `docker-compose.yml`, `Caddyfile` and Caddy's state |
| `/srv/fleet/copies/` | the nightly copies (`MIS-158`, not yet) |
| `/etc/fleet/alias` | this machine's alias |

## Look

```sh
journalctl -u fleet-reconcile -n 20                     # what the last runs did
docker compose -f /srv/fleet/run/docker-compose.yml ps  # what is running
fleet-reconcile --dry-run                               # the plan, touching nothing
```

## What the machine refuses

- An order whose `server` is not its alias: not its business.
- An order whose image is not from `ghcr.io/numengames/` or whose domain is
  not one label under `numen.games`: refused and logged, even if merged.
- A `running` order with no `env/<id>.env` on the machine: not started, logged.

## Security, in one table

| If this is stolen… | …the thief gets | …and does not get |
|---|---|---|
| the machine | its worlds' data and keys | GitHub (no credential), the other machines, the console |
| the console (numinia.com) | the power to open pull requests | any machine (no key to them), any world's keys |
| a reviewer's account | the power to merge orders | code execution on machines (the reconciler is installed, not pulled); images outside the house's registry; domains outside the zone |

Log in to the machine with an SSH key, never a password (OVH offers the key
at creation). Docker itself runs as root; the engine runs as its own user
inside the container.

## Why a script and not Doco-CD

`MIS-156` named Doco-CD as the first candidate. The order is JSON, not a
compose file, so a git-to-compose tool would need a translation step anyway;
the translation *is* the reconciler, forty lines of pure functions with
tests. If the fleet grows past one compose file per machine, revisit.
