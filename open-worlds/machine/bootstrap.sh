#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: AGPL-3.0-only
#
# Turn a fresh Debian 12 or Ubuntu (24.04 or later) VPS into a machine of the
# public worlds fleet, or bring one up to date.
#
#   curl -fsSL https://raw.githubusercontent.com/numengames/numinia-assets/main/open-worlds/machine/bootstrap.sh \
#     | sudo bash -s -- open-1 0xYourWallet
#
# The first argument is the machine's alias. The second is the wallet of the
# Oracle (or several, separated by commas) who may act on this machine from
# the worlds room on numinia.com: each request is signed with that wallet and
# the machine checks the signature itself. Leave it out on a re-run to keep
# the list the machine already has.
#
# What it does, in order: Docker, a 2 GB swap file, the firewall (22, 80, 443
# only), unattended upgrades, a clone of this repository (the recipe), the
# door's own user, the programs, the alias, the machine's address for the
# console (<alias>.numinia.com), the Oracles' wallets, the machine's admin
# code, its first orders (adopted once from the public order book), systemd
# units, and a first run. It ends by showing the admin code. Nothing here
# needs a GitHub credential.
#
# The programs are INSTALLED to /usr/local/lib/fleet from this same commit and
# run from there, never from the clone. To update them, run this script again.
#
# Re-running it is safe; every step checks before it acts.

set -euo pipefail

ALIAS="${1:-}"
SIGNERS="${2:-}"
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
# python3-ecdsa checks the Oracles' signatures (the door and the reconciler).
apt-get install -y -q ca-certificates curl git python3 python3-ecdsa ufw unattended-upgrades sqlite3 zip

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

echo "== unattended upgrades"
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "== the recipe at $BOOK_DIR"
mkdir -p "$HOME_DIR"/{data,env,run,orders,requests}
chmod 700 "$HOME_DIR/env"
if [[ -d "$BOOK_DIR/.git" ]]; then
  git -C "$BOOK_DIR" pull --ff-only --quiet
else
  git clone --quiet --depth 1 "$BOOK_URL" "$BOOK_DIR"
fi

echo "== the door's own user: fleetd (no shell, no home, no Docker)"
if ! id -u fleetd >/dev/null 2>&1; then
  useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin fleetd
fi
mkdir -p "$HOME_DIR"/{inbox,sock,copies}
# The door writes requests into the inbox and its socket into sock/; it reads
# copies; it writes nothing else.
chown fleetd:fleetd "$HOME_DIR/inbox" "$HOME_DIR/sock"
chmod 750 "$HOME_DIR/inbox"
chmod 755 "$HOME_DIR/sock"
chown root:fleetd "$HOME_DIR/copies"
chmod 750 "$HOME_DIR/copies"

echo "== the programs, installed once"
# A machine set up by an earlier version runs its reconciler every minute:
# pause it while the programs change, so it never runs half-installed code
# against a book not yet adopted. The end of this script starts it again.
systemctl stop fleet-reconcile.timer fleet-reconcile.path 2>/dev/null || true
install -d -m 0755 "$LIB_DIR"
for program in reconcile.py walletsig.py fleetd.py; do
  install -m 0644 "$BOOK_DIR/open-worlds/machine/$program" "$LIB_DIR/$program"
done
wrapper() {  # wrapper <command> <program>: a command that runs the installed copy
  local tmp
  tmp="$(mktemp)"
  printf '#!/bin/sh\nexec /usr/bin/python3 %s/%s "$@"\n' "$LIB_DIR" "$2" > "$tmp"
  install -m 0755 "$tmp" "/usr/local/sbin/$1"
  rm -f "$tmp"
}
wrapper fleet-reconcile reconcile.py
wrapper fleetd fleetd.py

echo "== alias: $ALIAS"
mkdir -p /etc/fleet
echo "$ALIAS" > /etc/fleet/alias

echo "== the machine's address for the console"
# The worlds room reaches this machine's door here. While the fleet has one
# machine, the wildcard record *.numinia.com already points at it.
if [[ ! -s /etc/fleet/control-host ]]; then
  echo "$ALIAS.$CONTROL_ZONE" > /etc/fleet/control-host
fi

echo "== the Oracles who may act on this machine"
if [[ -n "$SIGNERS" ]]; then
  echo "$SIGNERS" | tr ',' '\n' | tr 'A-F' 'a-f' > /etc/fleet/signers
elif [[ ! -f /etc/fleet/signers ]]; then
  echo "# One Oracle wallet per line (0x…). Set it with: bootstrap.sh <alias> <wallet>" > /etc/fleet/signers
fi
chmod 644 /etc/fleet/signers

echo "== the machine's admin code, made once"
# Every world this machine keys gets it: type /admin <code> in a world's chat.
# One code for all the machine's worlds is a debt of the trial (see README).
# The door shows it to a signed Oracle, so its group may read it.
if [[ ! -s /etc/fleet/admin-code ]]; then
  (umask 077 && python3 -c 'import secrets; print(secrets.token_hex(6))' > /etc/fleet/admin-code)
fi
chown root:fleetd /etc/fleet/admin-code
chmod 640 /etc/fleet/admin-code

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
ReadWritePaths=$HOME_DIR/inbox $HOME_DIR/sock

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable fleetd.service
systemctl restart fleetd.service

echo "== first run"
# Before the timer starts, so the two never race on a fresh machine. A failed
# first run does not stop the script: the timer tries again every minute, and
# the summary below (with the admin code) is still shown.
if ! /usr/local/sbin/fleet-reconcile; then
  echo "the first run did not finish; the timer tries again every minute (journalctl -u fleet-reconcile -n 20)" >&2
fi
systemctl enable --now fleet-reconcile.timer fleet-reconcile.path

ORACLES="$(grep -c '^0x' /etc/fleet/signers || true)"
cat <<EOF

Done. This machine is "$ALIAS".
  its door:          https://$(cat /etc/fleet/control-host)/fleet/v1/machine
  Oracles here:      $ORACLES wallet(s) in /etc/fleet/signers
  its orders:        $HOME_DIR/orders/<id>.json, written by the worlds room through the door
  a world's folder:  $HOME_DIR/data/<id>/
  what is running:   docker compose -f $HOME_DIR/run/docker-compose.yml ps
  the reconciler:    journalctl -u fleet-reconcile -n 20
  the door:          journalctl -u fleetd -n 20
  update it:         run this script again (the clone's copy is never executed)

The admin code of every world on this machine (the worlds room shows it to an Oracle too;
type /admin <code> in a world's chat):

    $(cat /etc/fleet/admin-code)

Keep it somewhere safe and never in git. To see it again: sudo cat /etc/fleet/admin-code
EOF
