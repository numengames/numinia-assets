#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: AGPL-3.0-only
#
# Turn a fresh Debian 12 or Ubuntu (24.04 or later) VPS into a machine of the
# public worlds fleet.
#
#   curl -fsSL https://raw.githubusercontent.com/numengames/numinia-assets/main/open-worlds/machine/bootstrap.sh \
#     | sudo bash -s -- open-1
#
# The one argument is the machine's alias, the word orders use in `server`.
# What it does, in order: Docker, a 2 GB swap file, the firewall (22, 80, 443
# only), unattended upgrades, a clone of the order book, the alias, the
# machine's admin code, and a systemd timer that runs the reconciler every
# minute. It then runs the reconciler once and shows the admin code. Nothing
# here needs a GitHub credential: the order book is public.
#
# The reconciler is INSTALLED to /usr/local/sbin from this same commit and run
# from there, never from the pulled clone: a merge on the book changes orders,
# not the code that obeys them. To update the reconciler, run this script again.
#
# Re-running it is safe; every step checks before it acts.

set -euo pipefail

ALIAS="${1:-}"
BOOK_URL="https://github.com/numengames/numinia-assets.git"
HOME_DIR="/srv/fleet"
BOOK_DIR="$HOME_DIR/book"

if [[ -z "$ALIAS" || ! "$ALIAS" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]]; then
  echo "usage: bootstrap.sh <alias>   (lowercase letters, digits and hyphens, e.g. open-1)" >&2
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
apt-get install -y -q ca-certificates curl git python3 ufw unattended-upgrades sqlite3 zip

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

echo "== the order book at $BOOK_DIR"
mkdir -p "$HOME_DIR"/{data,env,run,copies}
chmod 700 "$HOME_DIR/env"
if [[ -d "$BOOK_DIR/.git" ]]; then
  git -C "$BOOK_DIR" pull --ff-only --quiet
else
  git clone --quiet --depth 1 "$BOOK_URL" "$BOOK_DIR"
fi

echo "== the reconciler, installed once"
install -m 0755 "$BOOK_DIR/open-worlds/machine/reconcile.py" /usr/local/sbin/fleet-reconcile

echo "== alias: $ALIAS"
mkdir -p /etc/fleet
echo "$ALIAS" > /etc/fleet/alias

echo "== the machine's admin code, made once"
# Every world this machine keys gets it: type /admin <code> in a world's chat.
# One code for all the machine's worlds is a debt of the trial (see README).
if [[ ! -s /etc/fleet/admin-code ]]; then
  (umask 077 && python3 -c 'import secrets; print(secrets.token_hex(6))' > /etc/fleet/admin-code)
fi
chmod 600 /etc/fleet/admin-code

echo "== systemd: reconcile every minute"
cat > /etc/systemd/system/fleet-reconcile.service <<EOF
[Unit]
Description=Fleet reconciler: containers match the order book
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
systemctl daemon-reload

echo "== first run"
# Before the timer starts, so the two never race on a fresh machine. A failed
# first run does not stop the script: the timer tries again every minute, and
# the summary below (with the admin code) is still shown.
if ! /usr/local/sbin/fleet-reconcile; then
  echo "the first run did not finish; the timer tries again every minute (journalctl -u fleet-reconcile -n 20)" >&2
fi
systemctl enable --now fleet-reconcile.timer

cat <<EOF

Done. This machine is "$ALIAS".
  orders it obeys:   $BOOK_DIR/open-worlds/*.json with "server": "$ALIAS"
  a world's keys:    made by the machine in $HOME_DIR/env/<id>.env (see machine/README.md)
  a world's folder:  $HOME_DIR/data/<id>/
  what is running:   docker compose -f $HOME_DIR/run/docker-compose.yml ps
  the reconciler:    journalctl -u fleet-reconcile -n 20
  update it:         run this script again (the clone's copy is never executed)

The admin code of every world on this machine (type /admin <code> in a world's chat):

    $(cat /etc/fleet/admin-code)

Keep it somewhere safe and never in git. To see it again: sudo cat /etc/fleet/admin-code
EOF
