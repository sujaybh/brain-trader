#!/usr/bin/env bash
# Thesis Trader: install the system-level services and timers. Idempotent; safe to re-run.
#
# What this does (needs sudo):
#   1. Creates system user+group `thesis` (no login, no home) if missing, and adds the project
#      owner (anomander) to the `thesis` group.
#   2. Grants `thesis` traverse-only access (ACL u:thesis:x) to the directories above the project
#      (e.g. /home/anomander is 750) so the broker can reach the code and the venv's python.
#      It cannot list or read anything else there.
#   3. mkdir -p /etc/thesis-trader; copies config/alpaca.env.example to
#      /etc/thesis-trader/alpaca.env only if that file does not exist yet (never overwrites keys);
#      chown thesis:thesis + chmod 600.
#   4. Makes data/ owned by group `thesis`, setgid, group-writable, so the broker (thesis) and the
#      tools/scheduler/web (anomander) share the SQLite DB.
#   5. Renders every scheduler/systemd/*.service|*.timer with scheduler/render-units.sh (system
#      flavor: __ROOT__ = this checkout, User= the checkout owner) into /etc/systemd/system/,
#      daemon-reload, enables+starts thesis-broker and thesis-web and every timer (tick,
#      nightly, review); restarts broker/web only if their unit file changed.
# Undo with scheduler/uninstall.sh.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OWNER="$(stat -c %U "$ROOT")"
echo "Project root: $ROOT (owner: $OWNER)"
OWNER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"
if [[ -f "$OWNER_HOME/.config/systemd/user/thesis-broker.service" ]]; then
  echo "WARNING: user units from install-user.sh exist for $OWNER. Disable them first, or two brokers" >&2
  echo "         and two schedulers will run: sudo -u $OWNER scheduler/uninstall.sh --user" >&2
fi


# 1. user + group
if ! getent group thesis >/dev/null; then
  groupadd --system thesis && echo "created group thesis"
fi
if ! id thesis >/dev/null 2>&1; then
  useradd --system --gid thesis --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin thesis
  echo "created system user thesis"
fi
if ! id -nG "$OWNER" | tr ' ' '\n' | grep -qx thesis; then
  usermod -aG thesis "$OWNER" && echo "added $OWNER to group thesis (log out/in for interactive shells)"
fi

# 2. traverse-only ACL on every ancestor the thesis user cannot already enter
PY_REAL="$(readlink -f "$ROOT/.venv/bin/python")"
for target in "$ROOT" "$PY_REAL"; do
  d="$(dirname "$target")"
  while [[ "$d" != "/" ]]; do
    if ! sudo -u thesis test -x "$d" 2>/dev/null; then
      setfacl -m u:thesis:x "$d" && echo "ACL u:thesis:x on $d"
    fi
    d="$(dirname "$d")"
  done
done
sudo -u thesis test -r "$ROOT/broker/__main__.py" 2>/dev/null || \
  echo "WARNING: thesis user still cannot read $ROOT/broker/__main__.py; check permissions." >&2

# 3. secrets
mkdir -p /etc/thesis-trader
chmod 755 /etc/thesis-trader
if [[ ! -f /etc/thesis-trader/alpaca.env ]]; then
  cp "$ROOT/config/alpaca.env.example" /etc/thesis-trader/alpaca.env
  echo "created /etc/thesis-trader/alpaca.env from the example (fill in keys)"
fi
chown thesis:thesis /etc/thesis-trader/alpaca.env
chmod 600 /etc/thesis-trader/alpaca.env

# 4. shared data dir
mkdir -p "$ROOT/data"
chgrp -R thesis "$ROOT/data"
chmod 2775 "$ROOT/data"
find "$ROOT/data" -type f -exec chmod g+rw {} +

# 5. units
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
"$ROOT/scheduler/render-units.sh" system "$TMP" "$OWNER"
CHANGED=()
TIMERS=()
for f in "$TMP"/*; do
  u="$(basename "$f")"
  [[ "$u" == *.timer ]] && TIMERS+=("$u")
  if ! cmp -s "$f" "/etc/systemd/system/$u"; then
    install -m 644 "$f" "/etc/systemd/system/$u"
    CHANGED+=("$u")
  fi
done
echo "units updated: ${CHANGED[*]:-none}"
systemctl daemon-reload
systemctl enable --now thesis-broker.service
systemctl enable --now "${TIMERS[@]}"
systemctl enable --now thesis-web.service
for svc in thesis-broker.service thesis-web.service; do
  if printf '%s\n' "${CHANGED[@]}" | grep -qx "$svc"; then
    systemctl try-restart "$svc" && echo "restarted $svc (unit changed)"
  fi
done

cat <<NEXT

Installed. Next steps:
  1. Put your Alpaca PAPER keys in /etc/thesis-trader/alpaca.env:
       sudoedit /etc/thesis-trader/alpaca.env
  2. Make sure $ROOT/config/trader.env exists (copy trader.env.example; set BROKER_TOKEN, NTFY_TOPIC).
  3. Restart the broker:      sudo systemctl restart thesis-broker
     Check it:                 journalctl -u thesis-broker -n 50
  4. See the schedule:         systemctl list-timers 'thesis-*'
  5. Run a tick by hand:       sudo systemctl start thesis-tick.service && journalctl -u thesis-tick -n 50
  6. Log out and back in so your shell picks up the 'thesis' group (needed to write data/).
  7. Health check:             .venv/bin/python scheduler/doctor.py
NEXT
