#!/usr/bin/env bash
# Install Thesis Trader as USER systemd units (no sudo). Everything runs as the current user,
# so the Alpaca keys in config/alpaca.env are readable by this user; the system-level
# install.sh (needs sudo) is the stronger setup where only the `thesis` user can read them.
#
# User units stop when you log out unless lingering is enabled; this script tries
# `loginctl enable-linger` and tells you if that needs sudo.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/scheduler/systemd"
DST="$HOME/.config/systemd/user"
mkdir -p "$DST" "$ROOT/data"

for u in thesis-broker.service thesis-web.service thesis-tick.service thesis-tick.timer \
         thesis-nightly.service thesis-nightly.timer; do
  # strip the per-user hardening that only applies to system units; point the broker at the
  # project's own keys file; install into the user default target.
  sed -E \
    -e '/^(User|Group|SupplementaryGroups|UMask|NoNewPrivileges|PrivateTmp|ProtectSystem|ReadWritePaths)=/d' \
    -e "s|^EnvironmentFile=/etc/thesis-trader/alpaca.env|EnvironmentFile=$ROOT/config/alpaca.env|" \
    -e 's/^WantedBy=multi-user.target/WantedBy=default.target/' \
    "$SRC/$u" > "$DST/$u"
done
chmod 600 "$ROOT/config/alpaca.env" 2>/dev/null || true

systemctl --user daemon-reload
systemctl --user enable --now thesis-broker.service thesis-web.service
systemctl --user enable --now thesis-tick.timer thesis-nightly.timer

if ! loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
  if loginctl enable-linger "$USER" 2>/dev/null; then
    echo "Lingering enabled: units keep running after logout."
  else
    echo "NOTE: could not enable lingering without sudo. Run: sudo loginctl enable-linger $USER"
    echo "      Until then the timers only run while you are logged in."
  fi
fi

echo
systemctl --user --no-pager status thesis-broker.service thesis-web.service | grep -E 'Loaded|Active' || true
systemctl --user list-timers --no-pager 'thesis-*' || true
echo
echo "Logs:   journalctl --user -u thesis-broker -f      (also thesis-web, thesis-tick, thesis-nightly)"
echo "Remove: systemctl --user disable --now thesis-broker thesis-web thesis-tick.timer thesis-nightly.timer"
