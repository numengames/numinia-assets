# Changes

## Unreleased

- `open-worlds/machine/reconcile.py`: a world's domain may be one label under
  either house zone, `numinia.com` or `numen.games`, or a client's own domain
  listed by an Oracle in `/etc/fleet/domains` on the machine (exact host or
  `*.zone`). The order book's rule is unchanged; the machine's rail widens.

- `open-worlds/machine/`: the recipe of a fleet machine (numinia-archive
  `MIS-156`). `bootstrap.sh` turns a fresh Debian 12 VPS into a machine with
  one line and one argument, its alias; `reconcile.py` runs every minute and
  makes the containers equal to the orders whose `server` is that alias —
  `running` up, `stopped` or gone down, a world without keys on the machine
  never started. The machine pulls the public book and holds no GitHub
  credential. Scripts are AGPL-3.0-only; `tests/test_machine.py` covers the
  pure part and the two refusals of the bootstrap.

- `open-worlds/`: the public 3D worlds fleet's orders, for its trial
  (numinia-archive ADR-069). It holds a README that defines an order and no
  orders yet. `tests/test_open_worlds.py` refuses an order that is malformed,
  is not pinned to one engine build, or holds an IP address or a secret, and
  refuses any other kind of file in the folder. The orders are CC0, declared
  in `REUSE.toml`.
- CI takes the family shape (numinia-archive STD-015 § The family pipeline):
  `check.yml` becomes `ci.yml`, its job is literally named `build` (the one
  check the branch ruleset requires in every Numen repository), every step
  is commented, the token is read-only, and a `checklist` job reports the
  files the standards require without blocking while STD-015 is draft.
  Same two checks as before: `reuse lint` over every file and the tests that
  prove a missing declaration is refused.
- `.github/dependabot.yml` (GitHub Actions and pip, daily),
  `dependabot-auto-merge.yml` and `scorecard.yml` copied from the archive;
  `.github/CODEOWNERS` naming the licence map, the licence texts and the
  workflows.
- Import unchanged Avocado and Pot Vapor 02 originals, with primary Polygonal
  Mind CC0 evidence and explicit per-file notices.
- Link the canonical records and the complete review of 32 candidates.
- Add exact inventory, SHA-256, size, GLB container and VRM licence tests.
- No thumbnails, personal state, consumer changes or legacy repository cleanup.

## Foundation

- Establish the resource depot alongside numinia-archive, without importing
  media or duplicating the canonical catalogue.
- Declare licences by file and reserve the first migration for verified CC0.
- Check REUSE compliance in CI and exercise the missing-declaration gate
  against temporary, synthetic files.

Not included: ingestion, storage backends, download delivery, new resource
IDs or schemas, archive cleanup, and retirement of legacy repositories.
