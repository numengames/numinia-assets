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
| `fleetd.py` | the door: takes an Oracle's signed request and a world's zip, shows the machine's public facts, hands out copies |
| `reconcile.py` | every minute and on each request: checks the requests again, changes the book, makes the containers match, deletes, zips copies, brings worlds back from zips |
| `versions.py` | a world's versions: the metadata file, duplicate, delete, download, import and publish, and the journal that finishes a publication (called by `reconcile.py` and `fleetd.py`) |
| `walletsig.py` | who signed a request: EIP-712 typed data, recovered to a wallet address |
| `release.py` | every five minutes: the Numinia Engine worlds follow the latest published engine (canary, one world a pass, rollback, halt) |
| `signers.py` | inside that same five-minute pass: takes the house's signed list of Oracles and Archons (`/etc/fleet/signers.house`) |
| `nightly.py` | each night: a copy of every world, kept 14 days (a Numinia Engine world's is the engine's own export, so «Bring a world back» takes it) |
| `cleanup.py` | each night at 04:10: old engine images and pre-update copies get a limit, the logs are measured, and the door shows the disk (see *What keeps the disk from filling*) |
| `condiciones.py` | by hand: turns a world's conditions-of-entry door on or off (`fleet-condiciones <world> on\|off\|status\|restart`) |
| `recipe.py` | every ten minutes: the machine's own programs (this folder) follow `main` (canary, rest, GitHub's signature, rollback, halt) |
| `../../tests/test_machine.py`, `test_requests.py`, `test_fleetd.py`, `test_walletsig.py`, `test_imports.py`, `test_engines.py`, `test_release.py`, `test_nightly.py`, `test_cleanup.py`, `test_signers.py`, `test_recipe.py`, `test_condiciones.py`, `test_versions.py`, `test_duplicate.py`, `test_publish.py`, `test_version_imports.py` | the tests, run by the depot's `python -m unittest` |
| `../../tests/e2e/` | end to end, by hand (not in `unittest`): `versions_e2e.mjs` signs the version requests with viem against the real door and the real reconciler (needs node, viem and a python with `ecdsa`); `pin_e2e.mjs` does the same for a pin, behind Caddy; `nightly_restore_e2e.mjs` makes a nightly copy of a Numinia Engine world with the real `nightly.py` and the engine's real `copia`, then has the engine's `comprobar`/`importar` and the room's `inspectZip` take it (needs node 24 and checkouts of numinia-engine and numinia-web) |

## Bring up a machine

1. Buy a VPS (OVH, France; Debian 12 or Ubuntu 24.04 or later). Note its
   public IP — it goes in DNS, never in this repository.
2. Log in. The user depends on the image: `debian` on Debian, `ubuntu` on
   Ubuntu. OVH's VPS order does not ask for an SSH key (its Public Cloud
   does): the first login uses the password OVH emails, which it makes you
   change; then copy your key in with `ssh-copy-id` and use the key from
   then on. Do it before step 3: the line turns SSH passwords off once your
   key is in, and leaves them on, saying `SSH STILL TAKES PASSWORDS`, while
   it is not.
3. Run the line the worlds room shows under the machine (LAP › Mundos): it
   carries the machine's alias, your wallet (the address you sign in to
   numinia.com with; several Oracles go separated by commas) and installs
   `main`:

   ```sh
   curl -fsSL https://raw.githubusercontent.com/numengames/numinia-assets/main/open-worlds/machine/bootstrap.sh \
     | sudo bash -s -- open-1 0xYourWallet
   ```

   Pinning a commit by hand (`FLEET_REF=<commit>`) is still accepted; see
   below.

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
that obeys them. A machine then updates its programs by itself from `main`
(see *The recipe follows main* below): the line is run by hand once.

**One commit, pinned.** `FLEET_REF` is a whole commit (40 hex characters):
the bootstrap fetches exactly that one, installs nothing if what it got is
another, and writes it to `/etc/fleet/recipe`; the door says it
(`/fleet/v1/machine` → `recipe`, and `recipeUpdate` for the updater's
state, which is what the room shows). Without `FLEET_REF` the bootstrap installs `main`'s newest
commit and says so: *NOT pinned*. Either way, from then on the machine keeps
its recipe on `main` by itself (`recipe.py`).

**Where the signed release is fetched.** The bootstrap writes
`/etc/fleet/release-url` (`https://numinia.com/fleet/release.json`) only when
the machine has none; a value already there is kept on a re-run. To set or
change it, add `RELEASE_URL=<http(s) address>` next to `FLEET_REF` on the line
(it replaces the file; an address that is not http(s) is refused). The record
is verified against `/etc/fleet/signers`, so the address carries no authority.
See *Kill switch* below.

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

### Two engines, told apart by their image

A machine runs two engines, and knows which one an order runs by its
image's repository in the house's registry:

| Engine | Image | Its container | Its site |
|---|---|---|---|
| Hyperfy | `ghcr.io/numengames/numinia-hyperfy2` | Hyperfy's settings (`PORT 3000`, its database and assets), the world at `/app/world` | `reverse_proxy <id>:3000` |
| Numinia Engine | `ghcr.io/numengames/numinia-engine` | `user: 10001:10001` (the image's own user, never root), the key file and nothing else, the world at `/mundo`; `data/<id>/` is that user's, mode 700 | `encode zstd gzip`, 404 for `/metrics*` and `/sesiones*`, `reverse_proxy <id>:8790` |

Both keep their visitors connected across a reload
(`stream_close_delay 12h`) and read the same key file: the Numinia Engine
reads `JWT_SECRET` and `ADMIN_CODE` when it has no `MOTOR_SECRET` and
`MOTOR_ADMIN` of its own, and its `/dueño <code>` takes the world's admin
code. A new world, an import or an update must name one of the two
(`unknown-engine`); an order already in a book with another repository of
the registry runs as every order ran before engines were told apart. The
door says which engines the machine runs (`/fleet/v1/machine` →
`engines`), and the room offers only those. The Numinia Engine saves when
it is told to stop, so the machine waits for no save before stopping it.

Every Numinia Engine world also gets numinia.com's signed world pass
settings (H3b, numinia-engine `packages/server/src/pase.ts`):
`MOTOR_PASE_CLAVE` (numinia.com's public key, the same for every world) and
`MOTOR_PASE_MUNDO` (the world's own id) — without them the engine ignores
every pass and "Entrar" silently makes everyone a guest. `MOTOR_PASE_ORG`
follows when the order names an `org` (lowercase letters, digits and
hyphens): no signed request sets it yet, so today only an order edited by
hand on the machine carries one — wiring it into `CreateWorld` and the
room is its own change, across this repository, `walletsig.py`'s typed
data and numinia-web's signing UI.

A Numinia Engine world's zip is the engine's business: the machine never
re-implements its rules. It runs the world's own image, with no network,
no capability and the world's limits, as the engine's user:

```sh
docker run --rm --network none -v <data/id>:/mundo:ro <image> copia     > mundo.zip  # Download
docker run --rm --network none -i <image> comprobar                     < mundo.zip  # Import: exit 0 or 1 + one line
docker run --rm --network none -i -v <staging/n>:/mundo <image> importar < mundo.zip  # into an empty folder
```

### Another build of the engine: UpdateWorld

One signed `UpdateWorld` (machine, world, image) moves a world to another
build of its own engine — a newer one, or an older one to undo an update.
The door refuses it for a world that is not here (`missing`), an image of
another engine (`engine-mismatch`), one the machine does not run
(`unknown-engine`), and while any other change of that world waits
(`busy`) or it is being deleted (`still-deleting`). The reconciler checks
it again, fetches the build (`docker pull`; a build the registry does not
have never reaches the compose file, where it would stop Docker from
taking the book for every world), writes the order and lets the next
`docker compose up` make that one container anew, after the world's save
like a stop. The door shows the world `updating` until Docker has started
the new build; the log says who asked, with the short wallet. A stopped
world takes its new build when it starts. The door says it takes updates
(`/fleet/v1/machine` → `updates: true`).

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

## Bring a world back from its zip

A world's zip — the copy **Download** made, or an Alchemists' Tower
backup — comes back as a new world, always **stopped**: an Oracle starts
it when it is wanted, never the import. One signature and one upload:

1. The room asks the Oracle to sign an `ImportWorld`: CreateWorld's
   fields plus the zip's exact size and SHA-256 (`zipBytes`, `zipSha256`),
   so the machine takes that zip and no other. The door checks it like a
   creation (name, address, domain) and that the disk has room for the zip,
   what it may unpack to and its reserve (`no-room`); it opens an **upload
   slot** for an hour and answers with the world's own code, made like a
   creation's and shown once, and where to send the zip
   (`{request, upload, adminCode}`).
2. The browser sends the zip there: `PUT /fleet/v1/uploads/<n>`, the
   whole file, `Content-Length` its exact size. The door streams it into
   `/srv/fleet/uploads/`, hashing as it goes, and keeps it only if it is the
   one signed (`fingerprint` otherwise: the slot stays for the right zip,
   no new signature needed). Then, and only then, the request is in the
   inbox. `/fleet/v1/worlds` shows the world `uploading` (`bytes` of
   `of`), then `importing`; `/fleet/v1/requests/<n>` says the same.
3. The reconciler checks the signature, the name and the address again,
   and that the zip is the signed one; it reads the zip's index against the
   caps **before it writes anything** outside the upload folder; it finds
   the world's folder in the zip (`world/` as Download and the Tower make
   it, one top folder, or the zip's root), unpacks only the database and
   the world's files (`db.sqlite`, `assets/`, `collections/`,
   `storage.json`) into a folder of root's alone, counting every byte it
   writes, and asks SQLite whether the database is sound
   (`integrity_check`) and is a world's (the engine's tables). Only then
   is it moved to `/srv/fleet/data/<id>/`, with keys the machine makes —
   a JWT secret of its own and the code the door showed, **never the
   zip's** — and an order that says `stopped`. The zip is deleted either
   way.

Nothing else in the zip is used: its `.env`, its compose file, its guide,
its engine image stay in the zip. If the zip has the manifest Download
writes (`world.json`), every file must be the one it lists. A Numinia
Engine world's zip (an import signed with a `numinia-engine` image) keeps
the rules every zip keeps here — size, files, ratio, paths, links,
encryption, the fingerprint — and then goes to the engine's own
`comprobar` and `importar` (see *Two engines*): `engine-refused` names the
engine's one line to whoever asked. A refused zip
names its rule — `zip-too-big`, `file-too-big`, `unpacked-too-big`,
`too-many-files`, `ratio`, `zip-slip`, `link`, `encrypted`, `not-a-zip`,
`not-a-world`, `unexpected`, `db-open` (zipped while the world ran),
`db-broken`, `db-foreign`, `manifest`, `fingerprint`, `slot-expired`,
`no-room` — and the entry that broke it, to the Oracle who asked; the log
says the rule alone.

| Cap | Value | Rule |
|---|---|---|
| the zip | 2 GiB | `zip-too-big` (the door, and Caddy, which cuts a bigger body at the edge) |
| one file, unpacked | 1 GiB | `file-too-big` |
| every file, unpacked | 4 GiB | `unpacked-too-big` |
| files in the zip | 20 000 | `too-many-files` |
| unpacked / packed, for a file of 1 MiB or more | 100 | `ratio` |
| the upload slot | one hour | `slot-expired` |
| free disk left after an import | 1 GiB | `no-room` |

The caps live in `reconcile.py` and are the law; the room repeats them only
to refuse early. `/fleet/v1/machine` says the biggest zip the machine takes
(`imports.zipMaxBytes`): the room offers *Traer de un zip* only to a door
that says it.

## Versions

A world can keep **two versions** of itself: a published one, which is the
world people visit, and one that waits beside it. Pablo's flow: duplicate
the published one, publish the copy, touch it live, and keep the old one as
the net to fall back on.

- **Where they are.** The published version is where a world has always
  been, `/srv/fleet/data/<id>/`: nothing moved, no world's container changed
  when this arrived. The other lives at `/srv/fleet/versions/<id>/<vid>/`
  (`<vid>` is `v1`, `v2`, …, on the same disk, so a rename is instant).
  A name is **never reused** within a world: a download called `v3` can
  never be mistaken for an older `v3`. `versions/` is `root:fleetd` 750 (the
  door reads only the small metadata files); `versions/<id>/` is root's alone
  (700); a Numinia Engine version keeps the engine user's ownership, as in
  `data/`.
- **Metadata.** `versions/<id>.json` says which one is published and, for
  each version, when it was made, by which wallet, how (`created`, `zip` or
  `copy:<vid>`) and how many bytes (measured when the folder is made, and
  again for both when a publication swaps them). A world with no file has one
  version, `v1`, published; the file is written the first time an Oracle
  uses a version request. An order never holds version data. The room reads
  all of it from `/fleet/v1/worlds` (`versions`, `versionComing`) and from
  `/fleet/v1/machine` (`versions.max`: a door that does not say it gets no
  button).
- **The five requests** (each names the world, and a version except the
  import):
  - `DuplicateVersion` copies a version into a new one, unpublished, while
    the world runs or stopped. A Hyperfy world uses SQLite's backup for the
    database and hardlinks for the files that are named by their SHA-256 and
    never rewritten; a Numinia Engine world is copied by the engine's own
    `copia`, `comprobar` and `importar` tools, never by hardlinks (its
    files are rewritten in place). Made in `staging/`, renamed into place
    whole, listed last: nothing half-made is ever a version.
  - `PublishVersion` makes another version the published one (below).
  - `DeleteVersion` removes an unpublished version.
  - `DownloadVersion` is a download of any version: the file is named
    `<id>-<vid>-<date>.zip`; the unpublished one needs no wait for a save.
  - `ImportVersion` puts a zip in as a new version of a world that is here,
    through the same upload slot as an import. It is checked by **the
    world's own engine** (a zip of the other engine fails that engine's
    rules), and the live world is never touched. `replace` is empty when the
    world has one version; when it has two it must name the unpublished one,
    which goes only after the new one is whole.
- **Publishing takes the world down for a few seconds.** The request is
  written to a journal, `run/switching.json`, and answered `working`; the
  world shows the phase `switching`. The next plan leaves it out of the
  book, so Docker stops it after its next save. Only when Docker has taken
  the book and the container is gone does the machine move the folders —
  `data/<id>` to `versions/<id>/<old>`, then `versions/<id>/<new>` to
  `data/<id>` — and write the metadata. The same run then plans again and
  the world comes back as its order says (a stopped world stays stopped).
  The world's address, container, keys and admin code never change; the
  ranks and users live inside each version's data, so publishing brings that
  version's admins. People inside are put out for a few seconds and
  reconnect. If the machine dies halfway, the next run reads where the two
  folders (by inode) are and finishes the job; a picture no step explains
  leaves the world down, the request `failed` with `switch-stuck`, and the
  journal for a person — the machine never starts a world on the wrong
  folder. `release.py` counts a world in the journal as busy, so an engine
  update never meets a publication.
- **What it refuses**, by name. The door: `busy` (to every other change of a
  world while a `PublishVersion` waits or the journal names it; to a
  `PublishVersion` while the world is updating or restarting or has another
  version request waiting; to a second duplicate or import while one is being
  made), `missing`, `still-deleting`, `zip-too-big`, `no-room`. The
  reconciler: `no-version`, `published` (the published one is not deleted),
  `already-published`, `only-version` (a world always keeps one), `full` (two
  versions already, or every name used), `no-room` (the disk keeps its
  1 GiB reserve; a Numinia Engine duplicate needs room for a zip and a
  folder), `engine-refused`, `fingerprint`, `switch-stuck`.
- **Delete and copies.** `DeleteWorld` removes every version with the world.
  The nightly copy still copies only `data/<id>` (the published one), so it
  costs no more; it ignores `versions/` and skips a world whose folder is
  between the two renames of a publication.
- Not yet: visiting the unpublished version (it would need a second
  container and an address), names for versions, a nightly copy of both.

## The signed requests

Each request is EIP-712 typed data (domain `Numinia Fleet`, version `1`) —
the standard way a wallet signs an off-chain message that a server checks,
as Snapshot does with votes. The wallet shows every field before signing.

| Request | Fields |
|---|---|
| `CreateWorld` | machine, world, domain, image, memory, cpus, maxUploadMb, nonce, expires |
| `ImportWorld` | CreateWorld's fields, and zipBytes, zipSha256 before the nonce — then the zip itself, `PUT /fleet/v1/uploads/<nonce>` (not signed again: it must be the zip whose size and SHA-256 were signed) |
| `DeleteWorld` | machine, world, domain, nonce, expires |
| `StopWorld`, `StartWorld`, `DownloadWorld` | machine, world, nonce, expires |
| `ShowWorldCode`, `RotateWorldCode` | machine, world, nonce, expires |
| `UpdateWorld` | machine, world, image, nonce, expires — another build of the world's own engine |
| `PinWorld` | machine, world, image, reason, nonce, expires — hold a world on one build of its engine (`pin` in its order, which `release.py` honours); an empty image releases the pin |
| `ShowAdminCode` | machine, nonce, expires — the machine's old shared code, for the room that still asks for it |
| `DuplicateVersion`, `PublishVersion`, `DeleteVersion`, `DownloadVersion` | machine, world, version, nonce, expires — see *Versions* |
| `ImportVersion` | machine, world, zipBytes, zipSha256, replace, nonce, expires — then the zip, `PUT /fleet/v1/uploads/<nonce>` as for an import; `replace` is `""` or the unpublished version it takes the place of |

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
| `/srv/fleet/uploads/` | a world's zip on its way in: the upload slot (`<n>.json`, an hour), the zip arriving (`.<n>.zip.part`), the zip waiting for the reconciler (`<n>.zip`). The door's (`fleetd`, mode 750); root reads it and deletes each zip once taken |
| `/srv/fleet/staging/` | where the reconciler unpacks a zip and checks it before it becomes `data/<id>/` (root's alone, mode 700; empty between runs) |
| `/srv/fleet/versions/<id>/<vid>/` | a world's other version (root's alone, mode 700; a Numinia Engine one keeps user 10001). `/srv/fleet/versions/<id>.json` says which version is published and what each is (`root:fleetd`, mode 640: the door reads it) |
| `/srv/fleet/data/<id>/` | the world: `db.sqlite` and `assets/` (Hyperfy); `mundo.json`, `datos/`, `aventura/`, `entorno/`, `sesiones/` (the Numinia Engine's, user 10001, mode 700) |
| `/srv/fleet/env/<id>.env` | the world's keys: its JWT secret and its admin code (mode 600, made by the machine, never in git) |
| `/srv/fleet/run/` | the generated `docker-compose.yml`, `Caddyfile` and Caddy's state; `Caddyfile.applied` is the list of sites Caddy last accepted and `docker-compose.applied.yml` the compose file Docker last accepted; `purge.json` and `copy-queue.json` hold deletes and copies in progress, `restart.json` the worlds whose code changed and that wait to be restarted, `updating.json` the worlds moved to a new build until Docker has started it, `switching.json` the worlds whose published version is being switched; `last-run.json` says when the reconciler last ran and how it ended |
| `/srv/fleet/cleanup/state.json` | the disk as the daily cleanup last saw it: used percent, free, total, what it removed (the door shows a few of these numbers: `/fleet/v1/machine` → `disk`) |
| `/srv/fleet/release/preupdate/` | the copy of a world made before each engine update (`<id>-<unix time>.zip`; 14 days, at most 2 GiB, never the newest of a world by the cap) |
| `/etc/systemd/journald.conf.d/fleet.conf` | the journal's cap (`SystemMaxUse=200M`), written by the bootstrap |
| `/srv/fleet/sock/fleetd.sock` | the door's socket; Caddy is the only way in to it |
| `/srv/fleet/log.jsonl` | what was done here: when, what, which world, a short wallet |
| `/srv/fleet/book/` | the clone of this repository, left on the commit the machine's line named: the recipe, and the orders adopted once |
| `/etc/fleet/alias` | this machine's alias |
| `/etc/fleet/recipe` | the commit the machine's programs were installed from |
| `/etc/fleet/recipe-canary`, `recipe-pin`, `recipe-soak` | optional: this machine takes a new recipe first; `off` or a commit freezes it; minutes of rest before a non-canary takes one (2 h without it) |
| `/srv/fleet/recipe/` | the recipe updater's `state.json` (the door shows it), its partial clone (`git/`), the copies of the programs before each install (`previous/`, root's alone) and the last install's output (`install.log`, root's alone) |
| `/etc/fleet/signers` | the Oracles' wallets this machine obeys, one per line (put there by hand; the machine never rewrites it) |
| `/etc/fleet/signers.house` | the wallets of the house's signed list of Oracles and Archons, with its `seq` in a comment (written by `signers.py`; do not edit). The machine obeys the union of both files |
| `/etc/fleet/signers-url` | where `signers.py` fetches that list (`https://numinia.com/fleet/signers.json`; the bootstrap writes it only when there is none) |
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
- A zip that breaks a cap, leaves its folder (`../`, `/`, `C:`, `\`), holds
  a link or a device, is encrypted, is not a world's or is not the one
  signed: refused by rule before anything is written outside
  `/srv/fleet/uploads/`; the zip goes and nothing stays.

## Security, in one table

| If this is stolen… | …the thief gets | …and does not get |
|---|---|---|
| the machine | its worlds' data and keys, and the admin code of every world on it | GitHub (no credential), the other machines, the console, any Oracle's wallet |
| the console (numinia.com) | nothing on the machines: it holds no key and signs nothing. It could show an Oracle a false page and ask for a signature — the wallet shows exactly what is signed (the request, the world, its address, the machine) | any machine, any world's keys |
| an Oracle's wallet | create, stop, start, download and delete worlds on the machines that list it; bring worlds back from zips, stopped; show and change any of their worlds' admin codes | code execution on machines (the programs are installed, not pulled, and an order's fields are checked values, never configuration); images outside the house's registry or unpinned; domains outside the house zones or the machine's list |
| the door (`fleetd`) broken into | the book, the copies and every world's admin code, read; requests put in the inbox and zips in the upload folder, and the code a world gets when an Oracle signs its creation, its import or a change of its code (the door makes it) | anything signed: the reconciler checks every signature again, and the zip's size and SHA-256 against the signed ones; the door has no root, no Docker, no network of its own, and writes only its inbox, its upload folder, its socket and its list of used numbers — never a key file |
| a hostile zip, signed by an Oracle | nothing: refused by rule before anything is written outside `uploads/` — no path out of the world's folder, no link, no bomb past the caps, no key or compose file of its own | the machine, its other worlds. An imported world's scripts run like any world's, inside its container. A Numinia Engine zip reaches the engine's own tools only after those rules, in a container with no network, no capability, the world's limits and the engine's user, which sees one empty folder |
| a signed request, intercepted | nothing more: it is good once, for one machine and one world, for fifteen minutes | anything else |
| a download link | that copy, for a day | the world's keys (the copy's are fresh), anything else |
| a world's admin code | admin of that world, from any browser, until an Oracle changes it with one signature | the other worlds (each has its own), the machine, its files, the other machines |
| a merge on this repository's `main` that touches `open-worlds/machine/` | root on every machine, by design: the canary at once, the others after the rest (2 h). Only a commit GitHub itself signed (a pull request merged on github.com, which `main`'s protection makes go through review) is installed; a commit pushed from a laptop, even by an admin, is refused | a machine frozen with `/etc/fleet/recipe-pin` (`off`); a recipe that already failed its health check there |
| the machine's SSH password, guessed | nothing, once the line has run with a key in: the SSH door takes keys only (`/etc/ssh/sshd_config.d/00-fleet-keys-only.conf`) | the machine. Before that, `ubuntu` with a guessed password is the whole machine, and the door keeps taking guesses |

Log in to the machine with an SSH key, never a password (after the first
login, see *Bring up a machine*). The bootstrap keeps the rule itself: once
the person running the line logs in with a key, it turns SSH passwords off,
and it says `SSH STILL TAKES PASSWORDS` when it cannot. Docker itself runs as
root, and so does the engine inside its container (see *Debts*).

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
- **Hyperfy runs as root in its container.** Its image creates a user it
  never switches to. The Numinia Engine already runs as its own user
  (10001), its folder that user's. What pays it for Hyperfy: `user:` in the
  compose file and a `chown` of `data/<id>/`.
- **The book lives only on the machine.** A lost machine loses its book with
  its worlds; the public log tells what was done. What pays it: the nightly
  copy (`MIS-158`) also takes the book, and sends the copies off the machine.
- **The nightly copy stays on the machine.** `fleet-nightly.timer` copies every
  world each night and keeps 14 days (see below), but on the same disk: a lost
  machine loses them too, and they hold no keys. What pays it: sending them off
  the machine (`MIS-158`).
- **The machine waits for the engine's save, from outside.** The engine
  saves on a timer and not when it is stopped, so the machine passes a
  short `SAVE_INTERVAL` (10 s) and waits one interval before a stop or a
  copy. A world that dies without warning (a crash, a reboot, no memory
  left) still loses up to 10 seconds. What pays it: the engine saving when
  it is told to stop (`SIGTERM`), in its own repository.
- **The Oracles are listed by hand, plus the house's list.** A wallet put
  in `/etc/fleet/signers` is added or removed by re-running the bootstrap line;
  the house's own list of Oracles and Archons (below) needs no hand once the
  house wallet is set in `signers.py`. What pays the rest: owners by wallet in
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

## Automatic engine updates (`release.py`)

Numinia Engine worlds follow the latest published engine by themselves; no Oracle signs each version.
`fleet-release.timer` runs `/usr/local/sbin/fleet-release` every five minutes (installed by the bootstrap, never run from the clone).

- **Target**: the public asset `imagen.json` of the release `latest` of `numengames/numinia-engine`, fetched anonymously over https (no GitHub credential on the machine). Its image must be `ghcr.io/numengames/numinia-engine@sha256:…`.
- **Order**: stopped worlds change together (nothing restarts). Running worlds change **one per pass**, the canary first (`/etc/fleet/canary`, a world id; else the lowest id), then by id. Hyperfy worlds, pinned worlds (`pin` in the order) and worlds with another change under way (`busy`) are skipped.
- **Per world**: a copy before (kept 14 days and at most 2 GiB in `/srv/fleet/release/preupdate/`; `cleanup.py` owns that rule), `docker pull`, the order's image changes (`previousImage` is kept), the reconciler remakes only that container (the Caddyfile does not change: no Caddy reload), then health: container running without restarts and `/status` reporting the target commit (`commitHash`, as the engine answers it) within 90 s.
- **Failure**: the world goes back to `previousImage`, the digest is written to `bad`, and the machine **halts** (`halted` in `/srv/fleet/release/state.json`) until a different digest is published. `updateError` stays in the order.
- **Kill switch (optional)**: a signed `FleetRelease` placed at `/srv/fleet/release/fleet-release.json` (`{message, signature}`) from an Oracle in `/etc/fleet/signers`, with the highest `seq` seen, replaces the target: a digest pins the fleet there, an empty `image` pauses it. Without it the machine follows `latest`. The record can also be **fetched**: set `FLEET_RELEASE_URL` (or put one line in `/etc/fleet/release-url`; unset = off, the local file only; production: `https://numinia.com/fleet/release.json`, which answers `{message, signature}`, an `ETag` per `seq`, 304 when unchanged and 404 before anything is published). Each pass the machine asks with `If-None-Match`, verifies the answer against its own `/etc/fleet/signers` exactly as for the local file, and writes it to `fleet-release.json` only if its `seq` is above every `seq` it has seen (anti-rollback). A network error, 404, 5xx, garbage, a record over 64 KiB, a stranger's signature or an old `seq` change nothing and never override the local state. Pause (empty `image`) and go-back (a higher `seq` naming an older digest) travel the same way; `--dry-run` does not fetch.
- **Provenance (optional, OFF by default)**: with `FLEET_RELEASE_PROVENANCE` (or the one word in `/etc/fleet/provenance`) set to `api` or `gh`, `latest` is believed only if its image has a build attestation; a refused image is treated as not published (nothing changes, the reason is in `state.json` → `provenanceRefused` and the log). `api` fetches the public attestation of the digest from `api.github.com` (anonymous, no credential) and checks that it is SLSA provenance of THAT digest, built from THAT commit by `.github/workflows/publicar-motor.yml` on `main` of `numengames/numinia-engine`; it does **not** verify the sigstore signature. `gh` runs `gh attestation verify` (signature, repository, workflow, ref) and needs `gh` plus a `GH_TOKEN` on the machine; an unknown word counts as `gh`. A signed `FleetRelease` is not subject to the check (an Oracle decided). Turn it on only once releases come only from the workflow; a release published by hand has no attestation and would be refused.
- `fleet-release --dry-run` prints the target and the plan and changes nothing.

### What the door says about updates and pins

`GET /fleet/v1/machine` carries two optional fields (a room that does not know them ignores them): `listens: {pin: true}` (this door takes a `PinWorld`) and `release` = `{target, commit, halted, haltedReason, lastPass: {at, action}}`, read from `release/state.json`. `GET /fleet/v1/worlds` adds `pin: {image, reason, at}` and `updateError` to a world only when it has them. `PinWorld` takes the same rails as `UpdateWorld` (house registry, Oracle signer, nonce, expires, one change at a time: `busy`, `engine-mismatch`); pinning also moves the world to that build.

## The house's list of Oracles and Archons (`signers.py`)

Every machine obeys the wallets in `/etc/fleet/signers`, which someone typed when
the bootstrap line was run. On top of those it follows one list the house
publishes and signs, so a new Oracle or Archon is obeyed everywhere without
touching any machine (the customers' machines too, in the alpha).

- **Where it runs**: inside the five-minute pass of `release.py` (`fleet-release.timer`,
  already on every machine), before the pass reads the signers, even when the
  engine updates are halted. A machine gets `signers.py` and `/etc/fleet/signers-url`
  with the recipe update (`recipe.py` runs the new commit's bootstrap): nothing by hand,
  no new timer. A failure of `signers.py` is printed and never stops an engine update.
- **What it fetches**: `GET https://numinia.com/fleet/signers.json` →
  `{"message": {"signers": ["0x…", …], "seq": N, "issuedAt": N}, "signature": "0x…"}`
  (the address is in `/etc/fleet/signers-url`, or the env `FLEET_SIGNERS_URL`; it
  carries no authority). The signature is EIP-712 `FleetSigners`
  (`signers` = the wallets joined with `,`, `seq`, `issuedAt`; same domain as every
  other record), checked with `walletsig.py`.
- **Whom it believes**: only the one wallet `HOUSE_SIGNER` in `signers.py`. It is
  **empty today: the feature is off** and the pass does nothing (no request is made)
  until a reviewed change sets it.
- **What it accepts**: 1 to 200 wallets (`0x` + 40 lowercase hex, sorted, unique, exactly as
  signed); a `seq` above the last one this machine took (the record's own `seq` is kept
  in the file); an `issuedAt` at most 24 hours old and at most 5 minutes ahead.
- **What it never does**: leave a machine without signers. An empty, broken, unsigned,
  stale, old, future or foreign answer, or a network error, changes nothing and the
  previous list stays. It writes `/etc/fleet/signers.house` whole and atomically, and
  never touches `/etc/fleet/signers`.
- **What the machine obeys**: the union of both files (`reconcile.read_signers`). A wallet the
  house drops stops being obeyed at the next accepted list; a wallet added by hand stays.
- `fleet-release --dry-run` does not fetch. `python3 /usr/local/lib/fleet/signers.py`
  takes the list now and prints `off`, `stored`, `refused` or `failed`.

## Nightly copy (`nightly.py`)

`fleet-nightly.timer` runs `/usr/local/sbin/fleet-nightly` at 03:30 machine time (`Persistent=`, up to 10 minutes of random delay, low CPU and IO priority; installed by the bootstrap, never run from the clone).

- **What (a Hyperfy world)**: one zip per world per UTC day in `/srv/fleet/nightly/<world>/<world>-YYYYMMDD-HHMMSS.zip`: the world's folder (`db.sqlite` through SQLite's online backup, so it is consistent while the world runs), its order and a `world.json` manifest with the sha256 of each file. Never keys or admin codes. Folder and zips are root-only (0700/0600).
- **What (a Numinia Engine world)**: the engine's own export, the same zip the download button gives, made by the `copia` tool of the world's own image (`reconcile.engine_copia`: no network, the folder read-only, the world's limits; the machine never re-implements the engine's rules). It holds `mundo.json`, `datos/`, `aventura/`, `entorno/` and the files to run it at home; never keys, never who came in. Nothing of ours is added to it (the engine's importer refuses a file its manifest does not list). The order (image, domain, limits; never keys) goes beside it, in `<world>-YYYYMMDD-HHMMSS.order.json` (0600), and is kept and deleted together with its zip. **It can be brought back from the room's «Bring a world back»**: upload the zip; the order file is only for whoever restores it by hand. If `copia` fails the world's result is `failed: …` (nothing partial is left, the others still get theirs, the service ends failed). A copy of an engine world made before this, in the Hyperfy shape (no `mundo.json`), cannot be restored: it does not count as tonight's, a proper one is made, and the old ones age out as any other.
- **Only the published version**: `data/<id>`; a world's other versions (`versions/`) are not copied, and a world whose folder is between the two renames of a publication is skipped for that night.
- **Reads only**: nothing is written in a world's folder and no container is stopped or touched. The zip is built as a hidden `.part` and moved into place whole.
- **Idempotent**: a world that already has a copy dated today is left alone.
- **Kept 14 days**: older zips (and their order files) go, but the newest copy of a world is never deleted, and a world removed from the machine keeps its copies until they age out.
- **Low disk**: a world is copied only if at least 2 GiB stay free after its folder's size; otherwise it is skipped and logged (`journalctl -u fleet-nightly`), and nothing else is deleted to make room. One world failing does not stop the others (the service then ends failed).
- Result of the last pass: `/srv/fleet/nightly/state.json`. Different from the copy `release.py` makes before an update (`release/preupdate/`) and from the one a download makes (`copies/`, a day).

## What keeps the disk from filling (`cleanup.py`)

Before this, nothing removed an old engine image: every engine update leaves the
previous image on the disk for good (a machine went from 5 % to 85 % in eleven
days), and nothing said the disk was filling. `fleet-cleanup.timer` runs
`/usr/local/sbin/fleet-cleanup` at 04:10 machine time, after the nightly copy
(`Persistent=`, low CPU and IO priority; installed by the bootstrap, never run
from the clone). `fleet-cleanup --dry-run` prints what it would do and changes
nothing (no file, no state).

- **Old engine images.** Removed one by one by their exact id (`docker rmi <id>`; never
  `docker image prune -a`, never `-f`), and only an image that is ALL of: used by no container
  (running or stopped); not named by any order in `orders/` (its `image`, its `previousImage`, a
  pin: every string of the order is compared); not the `target`, `applied` or `rolling` image
  in `release/state.json` (the current one and the one to go back to); not in the compose files
  Docker last accepted; not Caddy's; not one of the **newest 2 local images of its repository**
  (so a rollback target exists even if every record is odd); and **older than 48 h**, counted
  from the later of its build and the last time it was tagged or pulled here. Dangling images
  follow the same rules, minus the repository one. If Docker cannot list images or containers, or an
  order cannot be read, no image is touched; while `release.py` holds its lock (an update is
  under way) none is either. A `docker rmi` that fails is reported (`failed` in the state) and
  never stops the others. The note says «up to N MiB»: images share layers, so the sum of their
  sizes is the most that can come back; `freedBytes` is what the disk really gained.
- **Pre-update copies** (`release/preupdate/`): 14 days, as before, **and** at most 2 GiB for the
  folder (`PREUPDATE_CAP_BYTES`). Over the cap the oldest go first, but the cap never deletes the
  newest copy of a world (if only those are left, the note says the folder is over its cap). The
  rule lives in `cleanup.py`; `release.py`'s `prune_backups` calls it after every pass, so there
  is one rule and not two.
- **Logs.** The journal: the bootstrap writes `/etc/systemd/journald.conf.d/fleet.conf`
  (`SystemMaxUse=200M`) and restarts `systemd-journald` only when that file changed (a restart
  loses nothing). Docker's container logs are **not capped yet**, on purpose: see below. The
  cleanup measures `/var/lib/docker/containers/*/*-json.log` and the journal (read only) and
  puts both sizes in its state (`containerLogBytes`, `journalBytes`), so a log that grows is seen.
- **The door shows the disk.** `/srv/fleet/cleanup/state.json` holds `{at, usedPct, freeBytes,
  totalBytes, removedImages, freedBytes, preupdateBytes, state, note, …}`, and `GET /fleet/v1/machine`
  carries `disk: {usedPct, freeBytes, totalBytes, state, at}` from it (all `null` until the first
  run; a room that does not know the field ignores it). `state` is `ok`, `warn` (80 % used or
  more) or `full` (90 % or more), measured after cleaning. At 85 % or more after cleaning it does
  **not** delete anything else: the note says so, and a person decides. In `full` the command
  ends with an error, so `systemctl --failed` shows it.
- **Never touched**: `data/`, `copies/`, `nightly/`, `env/`, the signers, `/etc/fleet`.

### Why Docker's logs are not capped here

The natural place is `logging: {driver: json-file, options: {max-size: 10m, max-file: 3}}` on each
world's service in the compose file `reconcile.py` renders. It is **not shipped**: Compose
decides whether to recreate a container by hashing the service's whole configuration, and
`logging` is part of it, so the next reconcile pass would run `docker compose up -d` and recreate
EVERY running world at once (and `save_waits` would first make the reconciler wait for each save). A
log driver's options can only be set when a container is created. `/etc/docker/daemon.json` would
restart `dockerd` (every world with it) and is not touched either. The safe route is to apply it
where a world is recreated anyway: when an engine update moves a world (`release.py`
sets its order's image), the order would also record that the world takes the log cap, and the
renderer would add `logging` only to the services whose order says so. That needs a new, reviewed order
field and a change in `reconcile.py`'s `select_orders`, and is the next step. A test pins that no service carries
a `logging` key today.

### How a machine that only updates itself gets this

`recipe.py` (every ten minutes) compares the id of the `open-worlds/machine/` folder: a new
program file changes that id, so it counts as a new recipe. The canary installs it at once, the
rest after the 2 h rest, and only if GitHub signed the commit. The install runs **that commit's own**
`bootstrap.sh` (fetched by `recipe.py`, run with the alias alone), and the bootstrap writes
`cleanup.py`, the `fleet-cleanup` command, the service and the timer, the journald drop-in, runs
`daemon-reload` and `enable --now` on the timers. So a new program and a new timer **do** reach a
machine that only self-updates; nothing is done by hand and `recipe.py` did not need a new rail.
The health check of the **old** updater (the one that is running) does not look at the new timer;
from the next recipe on, `recipe.py` also requires `fleet-cleanup.timer` to be active and waits
while `fleet-cleanup.service` runs. The first cleanup runs at the next 04:10 (not at install
time); `sudo systemctl start fleet-cleanup` runs it now.

## The conditions door, by hand (`condiciones.py`)

A Numinia Engine world can ask every visitor to accept its conditions of entry
before they move, speak or build (the engine's *puerta*). The door is **off**
by default and is read **once, when the world starts**: it is on when the
world's `aventura/condiciones.json` says `"puerta": true`, or when its container
has `MOTOR_CONDICIONES_PUERTA=si` (which wins over the file). `fleet-condiciones`
(`/usr/local/sbin/fleet-condiciones`, installed by the bootstrap and kept by the
recipe updater like the other commands; never run from the clone) puts that file
in, takes it out and says where a world stands. It needs `sudo`: the folder is
the engine user's (10001).

It works in two steps on purpose. **`on` and `off` only change a file** (the
world keeps running as it was, nobody notices); **`restart` is the step that cuts
service**, for that one container only, and it waits for `healthy` and for the
engine's own log line to say which state the door started in.

```sh
sudo fleet-condiciones alchemiststower status                  # read only: file, container, last [condiciones] log line
sudo fleet-condiciones alchemiststower on --dry-run            # the plan; it does read the source
sudo fleet-condiciones alchemiststower on                      # step 1: place the file (nothing restarts)
sudo fleet-condiciones alchemiststower status                  # look: file there, puerta true; the running world still OFF
sudo fleet-condiciones alchemiststower restart                 # step 2: docker restart run-alchemiststower-1, checked
sudo fleet-condiciones alchemiststower off                     # undo, step 1: put the file back as it was
sudo fleet-condiciones alchemiststower restart                 # undo, step 2
```

Expected output of `on` (after it, the file is in place and the world has not changed):

```text
source: https://raw.githubusercontent.com/numengames/numinia-engine/1b04bd1d…/aventuras/condiciones-prueba/condiciones.json — valid, puerta true, casa 1.0 (6) → comunidad 1.0 (5), sha256 c7facdd23c65…
write /srv/fleet/data/alchemiststower/aventura/condiciones.json (there is no condiciones.json now)
write /srv/fleet/data/alchemiststower/aventura/condiciones.json.puesta: there was none before
check: /srv/fleet/data/alchemiststower/aventura/condiciones.json is in place and says "puerta": true (casa 1.0 (6) → comunidad 1.0 (5)).
=> The file is in place, but the running world has NOT changed: it reads it only when it starts. Check it with `sudo fleet-condiciones alchemiststower status`, and when you are ready to cut service for this one world: `sudo fleet-condiciones alchemiststower restart`.
```

Expected output of `restart` (the last line is the sentence; the exit code is 0 only when it is true):

```text
this will restart ONE container, run-alchemiststower-1 (docker restart; never the compose file); its visitors are disconnected for a moment.
after it the door should be ON: the file says "puerta": true.
log: [condiciones] puerta ENCENDIDA: nadie se mueve, habla ni construye hasta aceptar las normas obligatorias
log: [condiciones] del mundo: casa 1.0 (6) → comunidad 1.0 (5)
=> Done: run-alchemiststower-1 restarted, is healthy, and its door is ON (the file says "puerta": true).
```

What it keeps to:

- **Only a world that exists here.** `<world>` must be a folder of `/srv/fleet/data/`, a service of the same name in the generated `/srv/fleet/run/docker-compose.yml` running the Numinia Engine; anything else is refused (exit 3) before a file is touched. The compose file is read, never written, and **no key file is ever printed** (`/srv/fleet/env/<world>.env` is read only to see whether it sets `MOTOR_CONDICIONES_PUERTA`, and only that answer is shown).
- **The source is checked before it is placed.** The default is the engine's test file at one pinned commit (`ENGINE_COMMIT` in `condiciones.py`) and its SHA-256; its texts are placeholders, not for real visitors. `--source /absolute/path.json` or `--source https://…` takes another one. It must parse, be a `condiciones@1` document with layers, and say `"puerta": true`; a failed download, bad JSON or a file that would not turn the door on stops the run with the world untouched (exit 4).
- **Nothing the world had is lost.** An existing `condiciones.json` is copied to `condiciones.json.antes` first and `condiciones.json.puesta` notes what was placed; `off` puts the original back (the `.antes` copy stays), and deletes the file only if there was none before. A file edited meanwhile is kept aside (`.antes-<time>`, `.quitado-<time>`), never overwritten. A file nobody recorded as placed here and with no `.antes` is left alone unless `off --force`, which moves it aside, not deletes it.
- **A world can plant links in its own folder**, so a link where `aventura/` or one of these files should be is refused (exit 5); files are written under a new name and moved into place whole, owned by 10001. A world whose `aventura/` does not exist yet (never started) is refused too: making it could stop the engine from seeding its default adventure.
- **`status` only reads**: the file, `puerta` in it, the container's state and health, the last `[condiciones]` lines of its log, and whether they agree. Exit 0 = the running world matches the file; 7 = it does not (restart pending, unhealthy, no door line in the log).
- **`--dry-run`** prints the plan (`on`, `off`, `restart`) and changes nothing.
- Messages are in English like the machine's other programs; the engine's own log lines stay in Spanish.

| Exit | Means |
|---|---|
| 0 | done / consistent |
| 1 | not root, or an unexpected error on disk |
| 2 | wrong usage |
| 3 | not a world of this machine (or not a Numinia Engine world, or no container) |
| 4 | the source is unusable (download, JSON, not asking for the door) |
| 5 | the world's files are not as expected (no `aventura/`, a link, nothing to restore from) |
| 6 | `docker restart` failed |
| 7 | the world is not (yet) as the file says: unhealthy, no door line, or started in the other state |

The file lives in the world's own folder, so it survives a restart, an engine
update (`release.py`) and a recreated container. It is not part of the world's
order, so it is not rebuilt if the world is deleted and made again.

## The recipe follows main (`recipe.py`)

The machine's own programs (everything in this folder, the bootstrap
included) follow `main` of this repository by themselves; nobody runs the
line again. `fleet-recipe.timer` runs `/usr/local/sbin/fleet-recipe` every
ten minutes (installed by the bootstrap, never run from the clone).

- **What it reads**: `main`'s newest commit, anonymously over https from
  github.com (`git`, a partial and shallow clone in `/srv/fleet/recipe/git`:
  commits and folders, never the content's files; no credential, the user's
  git config ignored, a two-minute cap per call, wiped above 64 MiB), and
  the id of its `open-worlds/machine/` folder. The same id as the installed
  recipe's (a merge that touched only content): noted, nothing installed.
- **Who takes it, when**: the machine with `/etc/fleet/recipe-canary` (any
  content) at once; every other one once that folder has been `main`'s for
  a rest, counted from when THIS machine first saw it: 2 h, or the minutes
  in `/etc/fleet/recipe-soak`. No coordination: a recipe that breaks the
  canary and is reverted within the rest never reaches the others, and a
  content merge does not restart the rest.
- **Trust**: only a commit signed by GitHub's own web-flow key (pinned in
  `recipe.py`, checked with `gpgv`): GitHub signs what it merges on
  github.com, never a push. With `main` protected, that is: only what a
  reviewed pull request merged.
- **Install**: a copy of the installed programs, commands and `fleet*`
  units (`/srv/fleet/recipe/previous/`, the last three), then that commit's
  `bootstrap.sh` with the alias alone and `FLEET_REF` (the Oracles' wallets,
  the release URL, the admin code, every world's code, Caddy and the worlds
  stay as they are; its output in `/srv/fleet/recipe/install.log`), then
  health: `/etc/fleet/recipe` names the commit, the door answers on its
  socket with it, the reconciler runs (cleanly, if its last run was clean),
  the door and every fleet timer are active, and the new `fleet-recipe
  --check` starts. It waits while a reconcile, release or nightly run is
  going.
- **Failure**: the copy goes back (units the new recipe added are disabled
  and removed), and the machine **halts** on that recipe (`halted` in
  `/srv/fleet/recipe/state.json`, with the commit and the reason) and never
  tries that folder again; a different recipe on `main` lifts the halt (to
  try the same one again by hand: `sudo rm /srv/fleet/recipe/state.json`). A
  bootstrap that stopped before it changed anything (no network for `apt`)
  is tried again on the next passes, three times, then halts too.
- **Stop switch**: `/etc/fleet/recipe-pin` holding `off` freezes the
  machine on what it runs (GitHub is not even asked); a whole commit holds
  it on that commit (installed with no rest; signed by GitHub all the
  same). Anything else in the file also freezes.
- **The door says it**: `GET /fleet/v1/machine` → `recipeUpdate` =
  `{installed, upToDate, mainCommit, checkedAt, target, targetWhy, restUntil,
  canary, pin, halted, haltedCommit, haltedReason, refused, lastPass}`;
  commits and short words only. `fleet-recipe --check` prints the same
  switches on the machine; `journalctl -u fleet-recipe` tells each pass.
- **If GitHub changes its web-flow key** (it did on 2024-01-16), every
  machine refuses the next recipe (`refused`: "signed by a key that is not
  GitHub's web-flow key") until the new key is in `recipe.py` and the line is
  run once by hand.
