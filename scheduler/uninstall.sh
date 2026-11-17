#!/usr/bin/env bash
# Thesis Trader: stop and remove the systemd units. Idempotent.
#
#   sudo scheduler/uninstall.sh [--purge-user]   system units from install.sh
#   scheduler/uninstall.sh --user                user units from install-user.sh (no sudo)
#
# Removes every unit that has a template in scheduler/systemd/ (broker, web, tick, nightly,
# review), plus thesis-first-order.* if present. Leaves alone: data/ (the DB and journal), the
# keys (/etc/thesis-trader/alpaca.env or config/alpaca.env), the `thesis` user/group and the
# traverse ACLs. --purge-user also removes the user, group and ACLs (keys file and data are
# never deleted by this script).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNITS=()
for f in "$ROOT"/scheduler/systemd/*.timer "$ROOT"/scheduler/systemd/*.service; do UNITS+=("$(basename "$f")"); done
UNITS+=(thesis-first-order.timer thesis-first-order.service)

if [[ "${1:-}" == "--user" ]]; then
  DIR="$HOME/.config/systemd/user"
  for u in "${UNITS[@]}"; do
    systemctl --user disable --now "$u" 2>/dev/null || true
    if [[ -e "$DIR/$u" || -L "$DIR/$u" ]]; then
      rm -f "$DIR/$u" && echo "removed $DIR/$u"
    fi
  done
  systemctl --user daemon-reload
  systemctl --user reset-failed 'thesis-*' 2>/dev/null || true
  echo "Done. Kept: $ROOT/data and $ROOT/config."
  exit 0
fi

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0 [--purge-user]   (or: $0 --user for user units)" >&2
  exit 1
fi

for u in "${UNITS[@]}"; do
  systemctl disable --now "$u" 2>/dev/null || true
  # install.sh writes regular files (rendered templates), not symlinks
  if [[ -e "/etc/systemd/system/$u" || -L "/etc/systemd/system/$u" ]]; then
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
