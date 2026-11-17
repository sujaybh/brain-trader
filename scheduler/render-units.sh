#!/usr/bin/env bash
# Render every unit template in scheduler/systemd/ for one install flavor into DEST.
# Used by install.sh, install-user.sh and doctor.py, so what is checked is what gets installed.
#
#   scheduler/render-units.sh system|user DEST [OWNER]
#
# Placeholders: __ROOT__ (this checkout), __HOME__ (OWNER's home). OWNER defaults to the owner
# of the checkout. Writes only into DEST; changes nothing else.
#   system: User=anomander -> User=OWNER; everything else as written.
#   user:   drop User/Group/hardening lines (not valid or not needed in the user manager), read
#           the keys from config/alpaca.env, WantedBy=default.target, and drop
#           network-online.target / ollama.service (they exist only in the system manager).
set -euo pipefail
FLAVOR="${1:?usage: $0 system|user DEST [OWNER]}"
DEST="${2:?usage: $0 system|user DEST [OWNER]}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OWNER="${3:-$(stat -c %U "$ROOT")}"
HOMEDIR="$(getent passwd "$OWNER" | cut -d: -f6)"
HOMEDIR="${HOMEDIR:-$HOME}"
mkdir -p "$DEST"

shopt -s nullglob
for src in "$ROOT"/scheduler/systemd/*.service "$ROOT"/scheduler/systemd/*.timer; do
  u="$(basename "$src")"
  case "$FLAVOR" in
    system)
      sed -e "s|__ROOT__|$ROOT|g" -e "s|__HOME__|$HOMEDIR|g" -e "s|^User=anomander$|User=$OWNER|" \
        "$src" > "$DEST/$u"
      ;;
    user)
      sed -E \
        -e "s|__ROOT__|$ROOT|g" -e "s|__HOME__|$HOMEDIR|g" \
        -e '/^(User|Group|SupplementaryGroups|UMask|NoNewPrivileges|PrivateTmp|ProtectSystem|ReadWritePaths)=/d' \
        -e "s|^EnvironmentFile=/etc/thesis-trader/alpaca.env|EnvironmentFile=$ROOT/config/alpaca.env|" \
        -e 's/^WantedBy=multi-user.target/WantedBy=default.target/' \
        -e '/^(After|Wants|Requires)=/ s/ *(network-online\.target|ollama\.service)//g' \
        -e 's/^(After|Wants|Requires)= +/\1=/' \
        -e '/^(After|Wants|Requires)=$/d' \
        "$src" > "$DEST/$u"
      ;;
    *) echo "flavor must be system or user" >&2; exit 2 ;;
  esac
done
