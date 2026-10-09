<!--
SPDX-FileCopyrightText: 2026 Numen Games S.L.
SPDX-License-Identifier: CC-BY-4.0
-->

# open-worlds/ — the public worlds fleet's orders

This folder holds one **order** for each public 3D world the house runs.
The servers of the public fleet read this folder and nothing else. It sits
in the resource depot only while the fleet is being trialled (numinia-archive
`ADR-069`). It moves to a repository of its own when the depot gets heavy,
when the trial passes eight worlds, or when the trial ends.

A world is kept in three places, and each part lives in only one of them:

| Part | Where | Says |
|---|---|---|
| The card | numinia-archive, `objects/<card>.md` (`entity: world`) | what the world is, who it was made for, where its copies are |
| The order | here, `open-worlds/<id>.json` | where and how it runs |
| The keys | on its server only | never in git, encrypted or not |

An order never copies the card. It names the card and nothing more.

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
| `state` | `running` or `stopped`. A world that is closed for good loses its order; its card and copies remain. |
| `server` | the server's alias, never its address |
| `domain` | the address players open; it must resolve to the server before the world starts |
| `image` | the engine image, pinned to one build: a `sha-` tag or a digest, never `latest` |
| `limits` | memory and CPU for the container, and the largest upload in MB |

What is never written here: an IP address, a key, a password, a token, or
a client's name. The tests in `tests/test_open_worlds.py` refuse an order
that breaks these rules.

## Changing an order

By pull request, reviewed by the house's usual reviewers. The fleet console
on numinia.com only opens pull requests. No server and no automation writes
here.
