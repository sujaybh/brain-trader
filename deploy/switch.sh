#!/usr/bin/env bash
# Run Thesis Trader EITHER under systemd OR under Docker Compose on this machine, never both
# (two brokers/schedulers on one data/trader.db means double DCA orders).
#
#   deploy/switch.sh status    read-only: which of the two is active (safe any time)
#   deploy/switch.sh docker    stop + disable the systemd units, then build and start the stack
#   deploy/switch.sh systemd   stop the stack (data/ is kept), then (re)install the systemd units
#
# Env: COMPOSE_FILES="deploy/docker-compose.yml deploy/docker-compose.traefik.yml" adds the
# Traefik overlay (default: the base file only; also read from COMPOSE_FILES in deploy/.env).
# User units are handled without sudo; if only SYSTEM units (/etc/systemd/system) exist the
# script uses `sudo systemctl` for them.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
ENVFILE="$ROOT/deploy/.env"

UNITS=(thesis-broker.service thesis-web.service thesis-tick.timer thesis-nightly.timer thesis-review.timer)
ALL_UNITS=("${UNITS[@]}" thesis-tick.service thesis-nightly.service thesis-review.service)

# Value of KEY from deploy/.env (empty if unset). Does not source the file.
envval() { [[ -f "$ENVFILE" ]] && sed -n "s/^$1=//p" "$ENVFILE" | tail -1 | tr -d "\"'" || true; }

PROJECT="${COMPOSE_PROJECT_NAME:-$(envval COMPOSE_PROJECT_NAME)}"
PROJECT="${PROJECT:-thesis}"
FILES="${COMPOSE_FILES:-$(envval COMPOSE_FILES)}"
FILES="${FILES:-deploy/docker-compose.yml}"
COMPOSE=(docker compose)
for f in $FILES; do COMPOSE+=(-f "$f"); done

# Which systemd flavor holds the units: user, system, both, none.
flavor() {
  local u=0 s=0
  [[ -f "$HOME/.config/systemd/user/thesis-broker.service" ]] && u=1
  [[ -f /etc/systemd/system/thesis-broker.service ]] && s=1
  if ((u && s)); then echo both; elif ((u)); then echo user; elif ((s)); then echo system; else echo none; fi
}

sd_state() { # <flavor> <unit> -> active|inactive|...  (read-only)
  if [[ "$1" == user ]]; then systemctl --user is-active "$2" 2>/dev/null || true
  else systemctl is-active "$2" 2>/dev/null || true; fi
}

systemd_active_units() { # lists active thesis units of the given flavor
  local fl="$1" u
  for u in "${UNITS[@]}"; do
    [[ "$(sd_state "$fl" "$u")" == active ]] && echo "$u" || true
  done
}

docker_running() { # count of running containers in the compose project
  command -v docker >/dev/null 2>&1 || { echo 0; return; }
  docker ps -q --filter "label=com.docker.compose.project=$PROJECT" 2>/dev/null | wc -l | tr -d ' '
}

do_status() {
  local fl d active="" u
  fl="$(flavor)"
  d="$(docker_running)"
  echo "project dir : $ROOT"
  echo "systemd     : units installed = $fl"
  case "$fl" in
    user|both) active+="$(systemd_active_units user | tr '\n' ' ')" ;;
  esac
  case "$fl" in
    system|both) active+="$(systemd_active_units system | tr '\n' ' ')" ;;
  esac
  echo "              active units    = ${active:-none}"
  echo "docker      : running containers in project '$PROJECT' = $d"
  if command -v docker >/dev/null 2>&1; then
    docker ps --filter "label=com.docker.compose.project=$PROJECT" --format '              {{.Names}}  {{.Status}}' 2>/dev/null || true
  else
    echo "              (docker not installed)"
  fi
  if [[ -n "${active// /}" && "$d" -gt 0 ]]; then
    echo "RESULT      : BOTH are running. Stop one NOW: deploy/switch.sh docker  (or systemd)"; return 1
  elif [[ "$d" -gt 0 ]]; then echo "RESULT      : docker"
  elif [[ -n "${active// /}" ]]; then echo "RESULT      : systemd"
  else echo "RESULT      : neither is running"; fi
}

prepare_docker() {
  command -v docker >/dev/null || { echo "docker not found" >&2; exit 1; }
  mkdir -p "$ROOT/data"
  [[ -f "$ROOT/config/trader.env" && -f "$ROOT/config/alpaca.env" ]] \
    || { echo "config/trader.env and config/alpaca.env are required (see config/*.example)" >&2; exit 1; }
  [[ -f "$ROOT/config/limits.json" ]] || { echo "config/limits.json is required" >&2; exit 1; }
  if [[ ! -f "$ENVFILE" ]]; then
    cp "$ROOT/deploy/.env.example" "$ENVFILE"
    echo "created deploy/.env from the example (edit WEB_BIND / TRADER_HOST etc. if needed)"
  fi
  grep -q '^TRADER_UID=' "$ENVFILE" && sed -i "s/^TRADER_UID=.*/TRADER_UID=$(id -u)/" "$ENVFILE" || echo "TRADER_UID=$(id -u)" >> "$ENVFILE"
  grep -q '^TRADER_GID=' "$ENVFILE" && sed -i "s/^TRADER_GID=.*/TRADER_GID=$(id -g)/" "$ENVFILE" || echo "TRADER_GID=$(id -g)" >> "$ENVFILE"
  if [[ -e "$ROOT/data/trader.db" && ! -w "$ROOT/data/trader.db" ]]; then
    echo "data/trader.db is not writable by $(id -un); fix ownership first (e.g. sudo chown -R $(id -u):$(id -g) data)" >&2
    exit 1
  fi
}

do_docker() {
  prepare_docker
  echo "== building the image (systemd, if running, is untouched until this succeeds)"
  "${COMPOSE[@]}" build
  local fl
  fl="$(flavor)"
  echo "== stopping and disabling the systemd units (flavor: $fl)"
  if [[ "$fl" == user || "$fl" == both ]]; then
    systemctl --user disable --now "${UNITS[@]}" 2>&1 | sed 's/^/   /' || true
    systemctl --user stop "${ALL_UNITS[@]}" 2>/dev/null || true
  fi
  if [[ "$fl" == system || "$fl" == both ]]; then
    sudo systemctl disable --now "${UNITS[@]}" 2>&1 | sed 's/^/   /' || true
  fi
  echo "== starting the stack"
  "${COMPOSE[@]}" up -d
  echo
  do_status || true
  echo "Logs: ${COMPOSE[*]} logs -f      Check: .venv/bin/python scheduler/doctor.py"
}

do_systemd() {
  if command -v docker >/dev/null 2>&1 && [[ "$(docker_running)" -gt 0 || -n "$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" 2>/dev/null)" ]]; then
    echo "== stopping the compose stack (the data/ bind mount is kept)"
    docker compose -p "$PROJECT" down   # by project name: needs no compose files or env
  fi
  echo "== installing and starting the systemd units"
  if [[ "$(flavor)" == system ]]; then
    echo "Only system units exist; re-enable them with: sudo systemctl enable --now ${UNITS[*]}"
  else
    "$ROOT/scheduler/install-user.sh"
  fi
  echo
  do_status || true
}

case "${1:-}" in
  status) do_status ;;
  docker) do_docker ;;
  systemd) do_systemd ;;
  *) echo "usage: deploy/switch.sh status|docker|systemd" >&2; exit 2 ;;
esac
