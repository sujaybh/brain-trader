#!/usr/bin/env bash
# Thesis Trader: stop and remove the systemd units installed by install.sh. Idempotent.
# Leaves alone: data/ (the DB and journal), /etc/thesis-trader/alpaca.env (your keys), the
# `thesis` user/group, and the traverse ACLs. Pass --purge-user to also remove the user, group
# and ACLs (keys file and data are never deleted by this script).
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0 [--purge-user]" >&2
  exit 1
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNITS=(thesis-tick.timer thesis-nightly.timer thesis-tick.service thesis-nightly.service thesis-web.service thesis-broker.service)

for u in "${UNITS[@]}"; do
  systemctl disable --now "$u" 2>/dev/null || true
  if [[ -L "/etc/systemd/system/$u" ]]; then
    rm -f "/etc/systemd/system/$u" && echo "removed /etc/systemd/system/$u"
  fi
done
systemctl daemon-reload
systemctl reset-failed 'thesis-*' 2>/dev/null || true

if [[ "${1:-}" == "--purge-user" ]]; then
  PY_REAL="$(readlink -f "$ROOT/.venv/bin/python")"
  for target in "$ROOT" "$PY_REAL"; do
    d="$(dirname "$target")"
    while [[ "$d" != "/" ]]; do
      setfacl -x u:thesis "$d" 2>/dev/null || true
      d="$(dirname "$d")"
    done
  done
  id thesis >/dev/null 2>&1 && userdel thesis && echo "removed user thesis"
  getent group thesis >/dev/null && groupdel thesis && echo "removed group thesis"
fi
echo "Done. Kept: $ROOT/data and /etc/thesis-trader/alpaca.env (delete by hand if you really want)."
