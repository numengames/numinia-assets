#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: AGPL-3.0-only
#
# Turn a fresh Debian 12 VPS into a machine of the public worlds fleet.
#
#   curl -fsSL https://raw.githubusercontent.com/numengames/numinia-assets/main/open-worlds/machine/bootstrap.sh \
#     | sudo bash -s -- open-1
#
# The one argument is the machine's alias, the word orders use in `server`.
# What it does, in order: Docker, the firewall (22, 80, 443 only), unattended
# upgrades, a clone of the order book, the alias, and a systemd timer that runs
# reconcile.py every minute. It then runs the reconciler once. Nothing here
# needs a GitHub credential: the order book is public.
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

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q ca-certificates curl git python3 ufw unattended-upgrades sqlite3 zip

echo "== docker"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
systemctl enable --now docker

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

echo "== alias: $ALIAS"
mkdir -p /etc/fleet
echo "$ALIAS" > /etc/fleet/alias

echo "== systemd: reconcile every minute"
cat > /etc/systemd/system/fleet-reconcile.service <<EOF
[Unit]
Description=Fleet reconciler: containers match the order book
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $BOOK_DIR/open-worlds/machine/reconcile.py
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
systemctl enable --now fleet-reconcile.timer

echo "== first run"
python3 "$BOOK_DIR/open-worlds/machine/reconcile.py"

cat <<EOF

Done. This machine is "$ALIAS".
  orders it obeys:   $BOOK_DIR/open-worlds/*.json with "server": "$ALIAS"
  a world's keys:    $HOME_DIR/env/<id>.env   (JWT_SECRET, ADMIN_CODE; see machine/README.md)
  a world's folder:  $HOME_DIR/data/<id>/
  what is running:   docker compose -f $HOME_DIR/run/docker-compose.yml ps
  the reconciler:    journalctl -u fleet-reconcile -n 20
EOF
