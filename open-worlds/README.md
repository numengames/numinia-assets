<!--
SPDX-FileCopyrightText: 2026 Numen Games S.L.
SPDX-License-Identifier: CC-BY-4.0
-->

# open-worlds/ — the public worlds fleet

This folder holds the recipe the public fleet's machines are made from
([`machine/`](machine/README.md)) and, for one more step, the trial's first
orders. It sits in the resource depot only while the fleet is being
trialled (numinia-archive `ADR-069`). It moves to a repository of its own
when the depot gets heavy, when the trial passes eight worlds, or when the
trial ends.

A world is kept in three places, and each part lives in only one of them:

| Part | Where | Says |
|---|---|---|
| The card | numinia-archive, `objects/<card>.md` (`entity: world`) | what the world is, who it was made for, where its copies are |
| The order | on its machine, `/srv/fleet/orders/<id>.json` | where and how it runs |
| The keys | on its machine only, made there by the machine | never in git, encrypted or not |

An order never copies the card. It names the card and nothing more.

## Where the orders live now

Each machine keeps its own book. Oracles change it from the worlds room on
numinia.com — create, stop, start, download, delete — with a request their
wallet signs and the machine checks itself (see [The signed
requests](machine/README.md#the-signed-requests)). No pull request, no
merge, nobody at the machine. What each machine did is public at
`https://<alias>.numinia.com/fleet/v1/log`.

The orders still in this folder (`prueba.json`, `pablofm.json`) are the
ones the trial wrote here by pull request. A machine adopts the orders that
name it once, the first time the new bootstrap runs on it, and never reads
this folder again; they leave this folder once open-1 has adopted them.

## An order

```json
{
  "id": "example-world",
  "card": "example-world",
  "state": "running",
  "server": "open-1",
  "domain": "example.numen.games",
  "image": "ghcr.io/numengames/numinia-hyperfy2:sha-0123456",
  "limits": { "memory": "2g", "cpus": "1.5", "maxUploadMb": 50 }
}
```

| Field | Meaning |
|---|---|
| `id` | the file name without `.json`; lowercase letters, digits and hyphens |
| `card` | the world's card in numinia-archive's `objects/`, by file name |
| `state` | `running` or `stopped`. A deleted world loses its order; its card and any copy downloaded remain. |
| `server` | the machine's alias, never its address |
| `domain` | the address players open; it must resolve to the machine before the world starts |
| `image` | the engine image, pinned to one build: a `sha-` tag or a digest, never `latest` |
| `limits` | memory and CPU for the container, and the largest upload in MB |

On the machine an order also keeps who signed its creation (`createdBy`,
a wallet) or when it was adopted (`adoptedAt`); the door never shows the
wallet. What is never written in an order: an IP address, a key, a
password, a token, or a client's name. The tests in
`tests/test_open_worlds.py` refuse an order here that breaks these rules,
and the machine refuses one there. Two ids are reserved: `index` and
`caddy` (the machine's own front service).

## The index

`index.json` lists the ids of the orders still in this folder, sorted. The
worlds room read it while the book lived here; it leaves with them. Until
then it must match the folder (`tests/test_open_worlds.py` fails when it
does not). To regenerate it by hand, from the repository root:

```sh
python3 -c 'import json,pathlib; f=pathlib.Path("open-worlds"); ids=sorted(p.stem for p in f.glob("*.json") if p.name!="index.json"); (f/"index.json").write_text(json.dumps({"orders": ids}, indent=2)+"\n")'
```

## The machines

The machines are made from the recipe in [`machine/`](machine/README.md):
one line brings a fresh server up with its alias and its Oracles' wallets;
then a door takes their signed requests and a reconciler keeps the
containers equal to the book.
