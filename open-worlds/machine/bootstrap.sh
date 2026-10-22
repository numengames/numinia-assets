#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: AGPL-3.0-only
#
# Turn a fresh Debian 12 or Ubuntu (24.04 or later) VPS into a machine of the
# public worlds fleet, or bring one up to date. The worlds room on numinia.com
# hands out this line, with the commit of this repository it was built for:
#
#   curl -fsSL https://raw.githubusercontent.com/numengames/numinia-assets/<commit>/open-worlds/machine/bootstrap.sh \
#     | sudo FLEET_REF=<commit> bash -s -- open-1 0xYourWallet
#
# FLEET_REF pins the recipe: the programs are installed from exactly that
# commit (its 40 hex characters), the one this script was fetched from.
# Without FLEET_REF it installs main's newest commit, and says so: NOT pinned.
# After that the machine keeps its recipe on main by itself (recipe.py,
# fleet-recipe.timer): a canary at once, the others after a rest, a commit
# signed by GitHub only, back to the old programs and a halt if the new ones
# fail. /etc/fleet/recipe-pin (`off` or a commit) freezes it.
#
# RELEASE_URL (optional, the same way) is where the machine fetches the signed
# release record: https://numinia.com/fleet/release.json unless given. It is
# written to /etc/fleet/release-url only when that file is absent; an address
# already there stays, unless RELEASE_URL is given, which replaces it.
#
# The first argument is the machine's alias. The second is the wallet of the
# Oracle (or several, separated by commas) who may act on this machine from
# the worlds room on numinia.com: each request is signed with that wallet and
# the machine checks the signature itself. Leave it out on a re-run to keep
# the list the machine already has.
#
# What it does, in order: Docker, a 2 GB swap file, the firewall (22, 80, 443
# only), SSH by key only (once the person running it logs in with a key),
# unattended upgrades, a clone of this repository at the pinned commit
# (the recipe), the door's own user and its folders (the inbox, the upload
# folder for a world's zip), the programs, the alias, the machine's
# address for the console (<alias>.numinia.com), the Oracles' wallets, the
# machine's old shared admin code and the folder of each world's own, its
# first orders (adopted once from the public order book), systemd units, and
# a first run. It ends by saying which recipe it installed and by checking
# that the machine obeys. Nothing here needs a GitHub credential.
#
# The programs are INSTALLED to /usr/local/lib/fleet from this same commit and
# run from there, never from the clone. fleet-recipe.timer runs this script
# again, with the alias alone and FLEET_REF set, when main's recipe changes.
#
# Re-running it is safe; every step checks before it acts.
#
# Everything happens inside main(), called on the last line. Piped into bash,
# the script is read as it runs, and a program it starts that reads its input
# would swallow the rest of the script: bash then ends halfway, saying nothing.
# Wrapped in a function, the whole script is read before the first step runs.

set -euo pipefail

main() {
  ALIAS="${1:-}"
  SIGNERS="${2:-}"
  FLEET_REF="${FLEET_REF:-}"
  RELEASE_URL="${RELEASE_URL:-}"
  DEFAULT_RELEASE_URL="https://numinia.com/fleet/release.json"
  DEFAULT_SIGNERS_URL="https://numinia.com/fleet/signers.json"
  BOOK_URL="https://github.com/numengames/numinia-assets.git"
  HOME_DIR="/srv/fleet"
  BOOK_DIR="$HOME_DIR/book"
  LIB_DIR="/usr/local/lib/fleet"
  CONTROL_ZONE="numinia.com"

  if [[ -z "$ALIAS" || ! "$ALIAS" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]]; then
    echo "usage: bootstrap.sh <alias> [0xOracleWallet[,0x…]]   (alias: lowercase letters, digits and hyphens, e.g. open-1)" >&2
    exit 2
  fi
  if [[ -n "$SIGNERS" && ! "$SIGNERS" =~ ^0x[0-9a-fA-F]{40}(,0x[0-9a-fA-F]{40})*$ ]]; then
    echo "usage: bootstrap.sh <alias> [0xOracleWallet[,0x…]]   (a wallet is 0x and 40 hex characters)" >&2
    exit 2
  fi
  if [[ -n "$FLEET_REF" && ! "$FLEET_REF" =~ ^[0-9a-f]{40}$ ]]; then
    echo "FLEET_REF IS NOT A COMMIT: give the whole commit the worlds room shows (40 characters, 0-9 and a-f), or leave it out to install main" >&2
    exit 2
  fi
  if [[ -n "$RELEASE_URL" && ! "$RELEASE_URL" =~ ^https?://[^[:space:]]+$ ]]; then
    echo "RELEASE_URL IS NOT AN ADDRESS: give an http(s) address with no spaces, or leave it out to keep the machine's own" >&2
    exit 2
  fi
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "run as root (sudo)" >&2
    exit 2
  fi
  OS_ID="$(. /etc/os-release && echo "$ID")"
  OS_CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-}")"
  if [[ "$OS_ID" != debian && "$OS_ID" != ubuntu ]]; then
    echo "this recipe knows Debian and Ubuntu; this machine is $OS_ID" >&2
    exit 2
  fi

  echo "== packages"
  export DEBIAN_FRONTEND=noninteractive
  # An earlier version of this script wrote Docker's Debian source on Ubuntu
  # machines too. That source answers 404 for Ubuntu's release names, and under
  # `set -e` its error stopped every later run at the first update below. Ubuntu
  # takes Docker from its own archive, so the stray file goes before any update.
  if [[ "$OS_ID" == ubuntu ]] && grep -qs 'download.docker.com/linux/debian' /etc/apt/sources.list.d/docker.list; then
    rm -f /etc/apt/sources.list.d/docker.list
  fi
  apt-get update -q
  # python3-ecdsa checks the Oracles' signatures (the door and the reconciler);
  # gpgv checks GitHub's signature on a new recipe (recipe.py).
  apt-get install -y -q ca-certificates curl git gpgv python3 python3-ecdsa ufw unattended-upgrades sqlite3 zip

  echo "== docker"
  if ! command -v docker >/dev/null; then
    if [[ "$OS_ID" == ubuntu ]]; then
      # Ubuntu's own archive carries Docker and Compose v2, and knows its newest
      # release before Docker's repository does.
      apt-get install -y -q docker.io docker-compose-v2
    else
      install -m 0755 -d /etc/apt/keyrings
      curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
      chmod a+r /etc/apt/keyrings/docker.asc
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $OS_CODENAME stable" \
        > /etc/apt/sources.list.d/docker.list
      apt-get update -q
      apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
    fi
  fi
  systemctl enable --now docker
  if ! docker compose version >/dev/null 2>&1; then
    echo "docker is here but 'docker compose' (v2) is not; install docker-compose-v2 or docker-compose-plugin" >&2
    exit 1
  fi

  echo "== swap: 2 GB, so a full machine slows down instead of killing a world"
  if [[ -z "$(swapon --show=NAME --noheadings)" ]]; then
    if [[ ! -f /swapfile ]]; then
      fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
      chmod 600 /swapfile
      mkswap /swapfile >/dev/null
    fi
    swapon /swapfile
  fi
  if [[ -f /swapfile ]] && ! grep -q '^/swapfile ' /etc/fstab; then
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi

  echo "== firewall: 22, 80, 443"
  ufw --force reset >/dev/null
  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  ufw allow 22/tcp >/dev/null
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null

  echo "== ssh: a key, never a password"
  # The README's rule, kept by the machine itself. A fresh VPS still takes
  # passwords at the SSH door (OVH's first login uses the one it emails, and
  # cloud-init may allow them in 50-cloud-init.conf), so anyone may keep
  # guessing the password of a well-known user. Once the person running this
  # line can log in with a key, the door stops taking passwords. sshd keeps
  # the FIRST value it reads for a setting and reads its drop-in folder in name
  # order: this file is called 00-… so that no later file turns passwords back
  # on. Without a key nothing changes, and it says so: turning passwords off
  # then would lock that person out.
  local ssh_conf=/etc/ssh/sshd_config.d/00-fleet-keys-only.conf
  local ssh_user ssh_home ssh_tmp ssh_said
  ssh_user="${SUDO_USER:-root}"
  ssh_home="$(getent passwd "$ssh_user" | cut -d: -f6 || true)"
  if ! command -v sshd >/dev/null; then
    echo "no sshd on this machine: nothing to do"
  elif [[ -n "$ssh_home" ]] && ssh-keygen -l -f "$ssh_home/.ssh/authorized_keys" </dev/null >/dev/null 2>&1; then
    mkdir -p "$(dirname "$ssh_conf")"
    ssh_tmp="$(mktemp)"
    printf '# Written by the fleet bootstrap: log in with a key, never a password.\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n' > "$ssh_tmp"
    install -m 0644 "$ssh_tmp" "$ssh_conf"
    rm -f "$ssh_tmp"
    if ! sshd -t </dev/null; then
      rm -f "$ssh_conf"
      echo "!! SSH STILL TAKES PASSWORDS: sshd refused the new setting, so it was taken back out." >&2
    else
      if systemctl is-active --quiet ssh; then
        systemctl reload ssh || echo "!! sshd did not reload: the setting applies from its next start" >&2
      fi
      ssh_said="$(sshd -T </dev/null 2>/dev/null || true)"
      if grep -qx 'passwordauthentication no' <<<"$ssh_said"; then
        echo "keys only: $ssh_user logs in with a key, and the door takes no password"
      else
        echo "!! SSH STILL TAKES PASSWORDS: sshd does not read $ssh_conf (does /etc/ssh/sshd_config include sshd_config.d?)" >&2
      fi
    fi
  else
    echo "!! SSH STILL TAKES PASSWORDS: $ssh_user has no key in ${ssh_home:-its home}/.ssh/authorized_keys." >&2
    echo "!! Copy yours in (ssh-copy-id), log in with it, and run this line again with sudo." >&2
  fi

  echo "== unattended upgrades"
  dpkg-reconfigure -f noninteractive unattended-upgrades

  echo "== the recipe at $BOOK_DIR"
  mkdir -p "$HOME_DIR"/{data,env,run,orders,requests,staging}
  chmod 700 "$HOME_DIR/env"
  # Where the reconciler unpacks a world brought back from its zip, checked
  # before it becomes data/<id>/: root's alone.
  chmod 700 "$HOME_DIR/staging"
  if [[ ! -d "$BOOK_DIR/.git" ]]; then
    git clone --quiet --depth 1 "$BOOK_URL" "$BOOK_DIR"
  fi
  # Exactly the commit FLEET_REF names, or main's newest when there is none.
  # The clone is left on that commit, never on a branch that moves under it.
  git -C "$BOOK_DIR" fetch --quiet --depth 1 origin "${FLEET_REF:-main}"
  git -C "$BOOK_DIR" checkout --quiet --force --detach FETCH_HEAD
  RECIPE="$(git -C "$BOOK_DIR" rev-parse HEAD)"
  if [[ -n "$FLEET_REF" && "$RECIPE" != "$FLEET_REF" ]]; then
    echo "!! THE RECIPE IS NOT THE COMMIT ASKED FOR (got $RECIPE): nothing was installed" >&2
    exit 1
  fi
  if [[ -n "$FLEET_REF" ]]; then
    RECIPE_SAID="$RECIPE (pinned)"
  else
    RECIPE_SAID="$RECIPE (main, NOT pinned; from now on the machine follows main by itself)"
  fi
  echo "== recipe: $RECIPE_SAID"

  echo "== the door's own user: fleetd (no shell, no home, no Docker)"
  if ! id -u fleetd >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin fleetd
  fi
  mkdir -p "$HOME_DIR"/{inbox,sock,copies,uploads}
  # The door writes requests into the inbox, a world's zip and its upload
  # slot into uploads/, and its socket into sock/; it reads copies; it
  # writes nothing else. Root reads uploads/ (the reconciler takes each zip).
  chown fleetd:fleetd "$HOME_DIR/inbox" "$HOME_DIR/sock" "$HOME_DIR/uploads"
  chmod 750 "$HOME_DIR/inbox" "$HOME_DIR/uploads"
  chmod 755 "$HOME_DIR/sock"
  chown root:fleetd "$HOME_DIR/copies"
  chmod 750 "$HOME_DIR/copies"
  # A world's other versions wait in versions/<id>/<vid> (root's alone, 700);
  # the door reads only the small versions/<id>.json files beside them.
  mkdir -p "$HOME_DIR/versions"
  chown root:fleetd "$HOME_DIR/versions"
  chmod 750 "$HOME_DIR/versions"

  echo "== the programs, installed once"
  # A machine set up by an earlier version runs its reconciler every minute:
  # pause it while the programs change, so it never runs half-installed code
  # against a book not yet adopted. The end of this script starts it again.
  # fleet-recipe.timer is not paused: this script may be running under it.
  systemctl stop fleet-reconcile.timer fleet-reconcile.path fleet-release.timer fleet-nightly.timer 2>/dev/null || true
  install -d -m 0755 "$LIB_DIR"
  for program in reconcile.py walletsig.py fleetd.py release.py nightly.py recipe.py condiciones.py signers.py versions.py; do
    install -m 0644 "$BOOK_DIR/open-worlds/machine/$program" "$LIB_DIR/$program"
  done
  wrapper fleet-reconcile reconcile.py
  wrapper fleetd fleetd.py
  wrapper fleet-release release.py
  wrapper fleet-nightly nightly.py
  wrapper fleet-recipe recipe.py
  wrapper fleet-condiciones condiciones.py
  # Which commit the programs came from: the door shows it (/fleet/v1/machine
  # `recipe`), and the worlds room says when a machine runs an older one.
  mkdir -p /etc/fleet
  echo "$RECIPE" > /etc/fleet/recipe
  chmod 644 /etc/fleet/recipe

  echo "== alias: $ALIAS"
  mkdir -p /etc/fleet
  echo "$ALIAS" > /etc/fleet/alias

  echo "== the machine's address for the console"
  # The worlds room reaches this machine's door here. While the fleet has one
  # machine, the wildcard record *.numinia.com already points at it.
  if [[ ! -s /etc/fleet/control-host ]]; then
    echo "$ALIAS.$CONTROL_ZONE" > /etc/fleet/control-host
  fi

  echo "== where the machine fetches the signed release record"
  # release.py asks this address for the signed FleetRelease (pause, pin, go
  # back). Written only when the machine has none: a value already there stays,
  # so a re-run never undoes what someone set by hand. RELEASE_URL, given on the
  # line, replaces it on purpose. The record is checked against the Oracles
  # below, so the address itself is no secret and no authority.
  if [[ -n "$RELEASE_URL" ]]; then
    echo "$RELEASE_URL" > /etc/fleet/release-url
  elif [[ ! -e /etc/fleet/release-url ]]; then
    echo "$DEFAULT_RELEASE_URL" > /etc/fleet/release-url
  fi
  chmod 644 /etc/fleet/release-url

  echo "== where the machine fetches the house's signed list of Oracles and Archons"
  # signers.py (inside release.py's five-minute pass) asks this address for the list the
  # house signs; only HOUSE_SIGNER in signers.py can sign it, so the address carries no
  # authority. Written only when the machine has none: a value already there stays.
  if [[ ! -e /etc/fleet/signers-url ]]; then
    echo "$DEFAULT_SIGNERS_URL" > /etc/fleet/signers-url
  fi
  chmod 644 /etc/fleet/signers-url

  echo "== the Oracles who may act on this machine"
  if [[ -n "$SIGNERS" ]]; then
    echo "$SIGNERS" | tr ',' '\n' | tr 'A-F' 'a-f' > /etc/fleet/signers
  elif [[ ! -f /etc/fleet/signers ]]; then
    echo "# One Oracle wallet per line (0x…). Set it with: bootstrap.sh <alias> <wallet>" > /etc/fleet/signers
  fi
  chmod 644 /etc/fleet/signers

  echo "== the machine's old shared admin code, made once"
  # The worlds keyed before each world had its own code keep this one until
  # an Oracle changes theirs in the worlds room, and an order adopted from the
  # public book is keyed with it. The door shows it to a signed Oracle who
  # asks for it (ShowAdminCode), so its group may read it.
  if [[ ! -s /etc/fleet/admin-code ]]; then
    (umask 077 && python3 -c 'import secrets; print(secrets.token_hex(6))' > /etc/fleet/admin-code)
  fi
  chown root:fleetd /etc/fleet/admin-code
  chmod 640 /etc/fleet/admin-code

  echo "== each world's own admin code: /etc/fleet/codes"
  # One file per world, written by the reconciler: the code that world's key
  # file gives the engine. The door reads them to show a world's code to a
  # signed Oracle, so its group may read them, and nobody else.
  mkdir -p /etc/fleet/codes
  chown root:fleetd /etc/fleet/codes
  chmod 750 /etc/fleet/codes

  echo "== this machine's first orders, adopted once from the public order book"
  /usr/local/sbin/fleet-reconcile --adopt-book "$BOOK_DIR"

  echo "== systemd: the door, and the reconciler every minute and on each request"
  cat > /etc/systemd/system/fleet-reconcile.service <<EOF
[Unit]
Description=Fleet reconciler: containers match the machine's book of orders
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/fleet-reconcile
EOF
  cat > /etc/systemd/system/fleet-reconcile.timer <<EOF
[Unit]
Description=Run the fleet reconciler every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF
  cat > /etc/systemd/system/fleet-reconcile.path <<EOF
[Unit]
Description=Run the fleet reconciler as soon as a signed request arrives

[Path]
DirectoryNotEmpty=$HOME_DIR/inbox
Unit=fleet-reconcile.service

[Install]
WantedBy=paths.target
EOF
  cat > /etc/systemd/system/fleet-release.service <<EOF
[Unit]
Description=Fleet updater: Numinia Engine worlds follow the latest published engine, one at a time, with rollback
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/fleet-release
EOF
  cat > /etc/systemd/system/fleet-release.timer <<EOF
[Unit]
Description=Look for a newer engine every five minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF
  cat > /etc/systemd/system/fleet-nightly.service <<EOF
[Unit]
Description=Fleet nightly copy: every world, once a day, kept 14 days (reads the worlds, changes none)

[Service]
Type=oneshot
Nice=19
IOSchedulingClass=idle
ExecStart=/usr/local/sbin/fleet-nightly
EOF
  cat > /etc/systemd/system/fleet-nightly.timer <<EOF
[Unit]
Description=Copy every world each night at 03:30 (machine time)

[Timer]
OnCalendar=*-*-* 03:30:00
RandomizedDelaySec=10min
Persistent=true

[Install]
WantedBy=timers.target
EOF
  cat > /etc/systemd/system/fleet-recipe.service <<EOF
[Unit]
Description=Fleet recipe updater: this machine's programs follow main of numinia-assets (canary, rest, rollback, halt)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/fleet-recipe
TimeoutStartSec=45min
EOF
  cat > /etc/systemd/system/fleet-recipe.timer <<EOF
[Unit]
Description=Look for a new fleet recipe on main every ten minutes

[Timer]
OnBootSec=10min
OnUnitActiveSec=10min
RandomizedDelaySec=2min

[Install]
WantedBy=timers.target
EOF
  cat > /etc/systemd/system/fleetd.service <<EOF
[Unit]
Description=Fleet door: Oracles' signed requests in, public facts and copies out
After=network-online.target

[Service]
User=fleetd
Group=fleetd
ExecStart=/usr/local/sbin/fleetd
Restart=on-failure
RestartSec=2
UMask=0027
StateDirectory=fleetd
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_UNIX
ReadWritePaths=$HOME_DIR/inbox $HOME_DIR/sock $HOME_DIR/uploads

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable fleetd.service
  systemctl restart fleetd.service

  echo "== first run"
  # Before the timer starts, so the two never race on a fresh machine. A failed
  # first run does not stop the script: the timer tries again every minute, and
  # the summary below is still shown. A run that has to stop or restart a
  # world first waits for that world's next save. The first run of this
  # recipe copies the code each world already has into /etc/fleet/codes.
  if ! /usr/local/sbin/fleet-reconcile; then
    echo "the first run did not finish; the timer tries again every minute (journalctl -u fleet-reconcile -n 20)" >&2
  fi
  systemctl enable --now fleet-reconcile.timer fleet-reconcile.path fleet-release.timer fleet-nightly.timer fleet-recipe.timer

  ORACLES="$(grep -c '^0x' /etc/fleet/signers || true)"
  cat <<EOF

Done. This machine is "$ALIAS".
  its door:          https://$(cat /etc/fleet/control-host)/fleet/v1/machine
  its recipe:        $RECIPE_SAID
  Oracles here:      $ORACLES wallet(s) in /etc/fleet/signers
  its orders:        $HOME_DIR/orders/<id>.json, written by the worlds room through the door
  a world's folder:  $HOME_DIR/data/<id>/
  a zip coming in:   $HOME_DIR/uploads/ (its slot lives an hour), unpacked in $HOME_DIR/staging/ and checked first
  what is running:   docker compose -f $HOME_DIR/run/docker-compose.yml ps
  the reconciler:    journalctl -u fleet-reconcile -n 20
  the door:          journalctl -u fleetd -n 20
  its updates:       by itself from main (journalctl -u fleet-recipe -n 20); freeze: echo off > /etc/fleet/recipe-pin

Each world has its own admin code: the worlds room shows it, and changes it, on
that world's card; type /admin <code> in that world's chat. A world made before
each had its own keeps the machine's old code until it is changed there.
EOF

  echo
  echo "== does the machine obey?"
  # The units this script paused or started must be running again, or no
  # request signed in the worlds room will ever be done here.
  local unit stopped=""
  for unit in fleetd.service fleet-reconcile.timer fleet-reconcile.path fleet-release.timer fleet-nightly.timer fleet-recipe.timer; do
    if ! systemctl is-active --quiet "$unit"; then
      stopped="$stopped $unit"
    fi
  done
  if [[ -n "$stopped" ]]; then
    echo "!! NO: this machine will not obey the worlds room. Not running:$stopped" >&2
    echo "!! Run: sudo systemctl enable --now$stopped — then this script again, and tell whoever set it up." >&2
    exit 1
  fi
  echo "yes: the door is open and the reconciler runs every minute and on each request."
}

# wrapper <command> <program>: a command that runs the installed copy.
wrapper() {
  local tmp
  tmp="$(mktemp)"
  printf '#!/bin/sh\nexec /usr/bin/python3 %s/%s "$@"\n' "$LIB_DIR" "$2" > "$tmp"
  install -m 0755 "$tmp" "/usr/local/sbin/$1"
  rm -f "$tmp"
}

main "$@"
