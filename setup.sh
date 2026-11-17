#!/usr/bin/env bash
# First-time setup on a fresh checkout. Idempotent: never overwrites an existing config file.
#   ./setup.sh            venv + requirements + config files + tests   (systemd / dev machines)
#   ./setup.sh --docker   config files only (the Docker image carries its own Python)
# Moving an existing install? Copy config/alpaca.env, config/trader.env and data/trader.db from
# the old machine instead of generating new ones (see "Moving to a server" in README.md).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
DOCKER=0
[[ "${1:-}" == "--docker" ]] && DOCKER=1

mkdir -p data
rand() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }

if [[ ! -f config/trader.env ]]; then
  cp config/trader.env.example config/trader.env
  sed -i "s|^BROKER_TOKEN=.*|BROKER_TOKEN=$(rand)|; s|^WEB_TOKEN=.*|WEB_TOKEN=$(rand)|; s|^NTFY_TOPIC=.*|NTFY_TOPIC=thesis-$(rand)|" config/trader.env
  chmod 600 config/trader.env
  echo "created config/trader.env with fresh tokens"
else
  echo "config/trader.env exists; left alone"
fi
if [[ ! -f config/alpaca.env ]]; then
  cp config/alpaca.env.example config/alpaca.env
  chmod 600 config/alpaca.env
  echo "created config/alpaca.env: paste your Alpaca PAPER keys into it"
else
  echo "config/alpaca.env exists; left alone"
fi

if (( ! DOCKER )); then
  [[ -x .venv/bin/python ]] || python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
  .venv/bin/python -m pytest -q
fi

val() { sed -n "s/^$1=//p" config/trader.env | tail -1; }
cat <<NEXT

Next:
  1. Put Alpaca PAPER keys in config/alpaca.env (if not there yet).
  2. Set WEB_URL in config/trader.env to the address your phone can reach (currently: $(val WEB_URL)).
  3. Subscribe the ntfy app to topic: $(val NTFY_TOPIC)
  4. Start it ONE way:   scheduler/install-user.sh    (systemd)
                    or   deploy/switch.sh docker      (Docker Compose; see deploy/README.md)
  5. Dashboard: <WEB_URL>/?token=$(val WEB_TOKEN)
NEXT
