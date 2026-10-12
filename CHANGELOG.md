# Changes

## Unreleased

- `open-worlds/machine/`: each world has its own admin code, and a machine
  installs one exact commit of its recipe.
  - Every world on a machine shared one admin code, so whoever held it
    administered them all, a client's world and the house's alike. Now the
    door makes a code of the world's own when it is created (16 hex
    characters) and shows it once to the Oracle who signed; the reconciler
    writes it into the world's key file and into `/etc/fleet/codes/<id>`,
    from where the door shows it to a signed Oracle (`ShowWorldCode`). A
    signed `RotateWorldCode` makes a new one: the world keeps its JWT
    secret (everyone who has been in it keeps who they are) and is
    restarted alone, after its next save, so whoever is inside reconnects
    in a few seconds; a leaked code is closed with one signature. While a
    change of code or a creation waits, the door answers `busy` to another
    show or change of that world. The first run of this version copies the
    code each world already has, so `prueba` and `pablofm` keep the
    machine's old code until it is changed. `ShowAdminCode` (that old code)
    still works for the room that asks for it. A world created under a
    name a world had before gets new keys, never the old ones.
  - A re-run of the bootstrap installed whatever `main` held at that
    moment. The line now carries the commit (`FLEET_REF`, 40 hex
    characters): the clone is left on exactly that commit, a commit that is
    not the one asked for installs nothing, and without `FLEET_REF` it says
    NOT pinned. The commit is written to `/etc/fleet/recipe` and the door
    says it (`recipe` in `/fleet/v1/machine`), so the room can tell a
    machine that runs an older one. Proved against GitHub: a shallow clone
    fetches an older commit, `main` and the tip by their sha, and refuses
    one that does not exist. The bootstrap no longer prints an admin code.

- `open-worlds/machine/`: nothing is lost when a world stops, and nobody is
  thrown out when another world changes.
  - The engine writes what was placed, moved or edited in a world every
    `SAVE_INTERVAL` seconds and not when it is told to stop, so a Stop
    right after an edit lost up to a minute of it. The machine now passes
    10 seconds (it passed 60) and the reconciler waits for each world's
    next save before Docker stops or restarts it, and before it zips a
    copy of a running world. A stop takes up to 12 seconds longer. The
    first run of this version restarts `prueba` and `pablofm` once (their
    interval changes), after their save.
  - Every create, stop, start or delete reloads Caddy, and a reload closes
    every WebSocket Caddy carries: every visitor of every world on the
    machine saw "You have been disconnected". Each world's site now keeps
    its connections across a reload (`stream_close_delay 12h`). Proved with
    the real Caddy (2.11.7): without it, a reload that changes another site
    closes the connection (1001); with it, the connection stays and answers.
  - The bootstrap line (`curl … | sudo bash`) ended halfway without a word
    on 12 October: bash reads a piped script as it runs, and the Caddy
    reload the reconciler starts read the rest of the script as its input,
    so the timer and the watch on the inbox were never turned on again and
    the machine took requests it never did. Now the script runs from a
    function called on its last line (read whole first), no command the
    reconciler runs reads its input, and the script ends by checking that
    the door and the reconciler run — and says so, and fails, if not.
  - `/fleet/v1/machine` says whether the machine keeps up: when the
    reconciler last ran and how that run ended (`lastRun`, a rule, never a
    value) and how many requests wait and since when (`waiting`).
  - A world deleted a moment ago keeps its folder and keys until Docker
    takes its container down; a world created under its name or address in
    that time kept them. The door and the reconciler now refuse it
    (`still-deleting`) until the old one is gone.

- `open-worlds/machine/`: a world is created, stopped, started, downloaded
  and deleted from the worlds room on numinia.com with one signature, and
  nothing else — no pull request, no merge, no SSH. The Oracle's wallet
  signs each request (EIP-712 typed data, which the wallet shows field by
  field); a new door on each machine (`fleetd.py`, its own user, a Unix
  socket behind Caddy at `https://<alias>.numinia.com/fleet/v1/`) checks
  the shape, the machine, the fifteen-minute window, the signature against
  the Oracles' wallets listed on the machine (`/etc/fleet/signers`) and that
  the request is new, and the reconciler, as root, checks it all again
  before it changes the machine's own book (`/srv/fleet/orders/`).
  `walletsig.py` recovers the signer (Keccak-256 written out, since
  Python's `sha3_256` pads differently; `python3-ecdsa` for the curve;
  low-s only), proved against signatures made by viem, the browser's side.
  Download zips a world whole while it runs — a consistent copy of its
  database, its files, a compose file for `localhost:3000`, fresh keys, a
  manifest with each file's SHA-256 — kept a day, the Alchemists' Tower's
  copy made by the machine. Delete removes the order, waits for Docker to
  take the container down, then deletes the world's folder, keys and
  copies. The machine adopts, once, the orders the public book gave it, so
  `prueba` and `pablofm` keep running. The bootstrap takes the Oracles'
  wallets as its second argument. What the machine did is public at
  `/fleet/v1/log`. Proved on real containers: a viem-signed request through
  Caddy to the door, then create, download, delete.

- `open-worlds/machine/reconcile.py`: a world added after Caddy started got
  no address. Caddy reads its Caddyfile only when it starts, and
  `docker compose up` leaves a running Caddy alone when only that file's
  text changed — so on open-1 the first world, `prueba`, ran healthy for
  minutes while its address was refused, until Caddy was reloaded by hand.
  The reconciler now asks Caddy to reload whenever the list of sites
  differs from the last one Caddy accepted (`run/Caddyfile.applied`, written
  only after a reload that worked). A reload keeps open worlds connected; a
  failed one is reported and tried again the next minute; a machine whose
  Caddy started before this fix is put right on its first run.

- `open-worlds/machine/`: a world is born with nothing done by hand on the
  machine. The reconciler makes a world's keys the first time its order
  runs — a JWT secret of its own and the machine's admin code, which the
  bootstrap makes once (`/etc/fleet/admin-code`, mode 600) and shows at the
  end of every run. A key file that already exists is never touched, and a
  machine with no admin code still starts nothing (an empty admin code makes
  every visitor an admin). One code per machine is written in the README as
  a debt, with what pays it.
- `open-worlds/machine/reconcile.py`: the engine at the pinned build refused
  to boot (`[envs] SAVE_INTERVAL not set`, then `ASSETS_BASE_URL`), so no
  world could have come up. The compose file now passes both;
  `PUBLIC_ASSETS_URL`, a name this engine never reads, is gone. Checked by
  running the image with the generated settings: `/status` answers with the
  pinned commit and `"protected": true`.
- `open-worlds/machine/bootstrap.sh`: an earlier version wrote Docker's
  Debian source on Ubuntu machines; its 404 stopped every later run at the
  first `apt-get update`. The stray file is removed before any update. The
  first reconcile now runs before the timer starts and no longer aborts the
  script, so the summary and the admin code are always shown. The README no
  longer says the engine runs as its own user: its image runs as root (a
  debt, written down).

- `open-worlds/index.json`: the book's table of contents — the sorted ids
  of every order, and nothing else. The fleet console on numinia.com holds
  no GitHub key, and GitHub's API gives an address only 60 keyless reads an
  hour, so a morning of opening the room left it blind ("GitHub answered
  403"). Listing the folder was the one API read the console could not do
  without; with the index it reads the list and the orders as plain files,
  which have no such quota. `tests/test_open_worlds.py` fails when the index
  and the folder disagree, so a pull request that adds or closes an order
  must change the index in the same commit (the console does). The
  reconciler skips `index.json` and refuses `index` as a world's id, as it
  refuses `caddy`.

- `open-worlds/machine/reconcile.py`: an order is data, never configuration.
  Every field that reaches the compose file or the Caddyfile must match a
  strict, anchored pattern (`re.fullmatch`) or the order is refused: `id`,
  `state`, the domain (one label under a house zone, or listed on the
  machine), the image (`ghcr.io/numengames/…` pinned to `:sha-…` or
  `@sha256:…` — the "never `latest`" rule now holds on the machine too), and
  `limits.memory`, `limits.cpus`, `limits.maxUploadMb` (a positive whole
  number; `true` is not 1). The compose file is built as a dict and written
  as JSON, which is YAML, so no value can add a setting. Closes a hole a
  reviewer found: a domain carrying newlines put `privileged: true` and a
  mount of the host's `/` into the compose file. A malformed order is now
  refused alone instead of stopping the reconciler, and `caddy` is reserved.
- `open-worlds/machine/bootstrap.sh`: Ubuntu as well as Debian (Docker from
  Ubuntu's archive there; an existing Docker is kept; `docker compose` v2 is
  checked), and a 2 GB swap file, so a full machine slows down instead of
  the kernel killing a world. The README says which login user each image
  has and that OVH's VPS order asks for no SSH key.
- `tests/test_open_worlds.py`: the book's own test uses `fullmatch` too, so a
  trailing newline no longer passes, and `true` is not a valid upload limit.

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
