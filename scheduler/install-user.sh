#!/usr/bin/env bash
# Install Thesis Trader as USER systemd units (no sudo). Everything runs as the current user,
# so the Alpaca keys in config/alpaca.env are readable by this user; the system-level
# install.sh (needs sudo) is the stronger setup where only the `thesis` user can read them.
#
# Idempotent: re-run after pulling changes to the unit templates. Renders every template in
# scheduler/systemd/ with scheduler/render-units.sh (user flavor: no User=/hardening lines, no
# network-online.target, which the user manager does not have), daemon-reloads, enables the
# broker, web and all timers, and restarts the broker/web only if their unit file changed.
#
# User units stop when you log out unless lingering is enabled; this script tries
# `loginctl enable-linger` and tells you if that needs sudo.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DST="$HOME/.config/systemd/user"
mkdir -p "$DST" "$ROOT/data"

if [[ -f /etc/systemd/system/thesis-broker.service ]]; then
  echo "WARNING: system units from install.sh exist in /etc/systemd/system. Running both sets" >&2
  echo "         means two brokers and double DCA attempts. Remove one: sudo scheduler/uninstall.sh" >&2
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
"$ROOT/scheduler/render-units.sh" user "$TMP" "$(id -un)"
CHANGED=()
for f in "$TMP"/*; do
  u="$(basename "$f")"
  if ! cmp -s "$f" "$DST/$u"; then
    install -m 644 "$f" "$DST/$u"
    CHANGED+=("$u")
  fi
done
echo "units updated: ${CHANGED[*]:-none}"
chmod 600 "$ROOT/config/alpaca.env" 2>/dev/null || true

TIMERS=()
for f in "$TMP"/*.timer; do TIMERS+=("$(basename "$f")"); done

systemctl --user daemon-reload
systemctl --user enable --now thesis-broker.service thesis-web.service
systemctl --user enable --now "${TIMERS[@]}"
for svc in thesis-broker.service thesis-web.service; do
  if printf '%s\n' "${CHANGED[@]}" | grep -qx "$svc"; then
    systemctl --user try-restart "$svc" && echo "restarted $svc (unit changed)"
  fi
done

if ! loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
  if loginctl enable-linger "$USER" 2>/dev/null; then
    echo "Lingering enabled: units keep running after logout and start at boot."
  else
    echo "NOTE: could not enable lingering without sudo. Run: sudo loginctl enable-linger $USER"
    echo "      Until then the units only run while you are logged in."
  fi
fi

echo
systemctl --user --no-pager status thesis-broker.service thesis-web.service | grep -E 'Loaded|Active' || true
systemctl --user list-timers --no-pager 'thesis-*' || true
echo
echo "Check:  .venv/bin/python scheduler/doctor.py"
echo "Logs:   journalctl --user -u thesis-broker -f      (also thesis-web, thesis-tick, thesis-nightly, thesis-review)"
echo "Remove: scheduler/uninstall.sh --user"
