# deploy/: run Thesis Trader with Docker Compose

A second way to run the same system, next to the systemd units in `scheduler/systemd/`.
**Run one or the other on a machine, never both** (two brokers or two schedulers on one
`data/trader.db` means double DCA orders). `deploy/switch.sh` enforces that.

| service | command | published | notes |
|---|---|---|---|
| `broker` | `python -m broker` | **nothing** (internal network only) | the only container with `config/alpaca.env`; `BROKER_HOST=0.0.0.0` applies inside the container; `BROKER_TOKEN` still required |
| `web` | `python -m web` | `${WEB_BIND:-127.0.0.1}:${WEB_PORT:-8010}` | `/healthz` healthcheck; waits for a healthy broker |
| `scheduler` | `scheduler/cron_loop.py` | nothing | tick Mon-Fri 09:30-16:00 /15 min, nightly Mon-Fri 18:30, review Sat 10:00 (New York) |

One image (`deploy/Dockerfile`, python:3.12-slim, non-root, `TZ=America/New_York`). It has **no Claude
CLI**: set `LLM_BACKEND=ollama` (the compose default) so chat and reviews use Ollama.

`config/limits.json` is mounted read-only. `data/` is a bind mount (default `./data`), so an existing
`trader.db` carries over; the containers run as `TRADER_UID:TRADER_GID` (filled in from your `id` by
`switch.sh`) so the DB stays writable by you.

## Laptop quick start (no Traefik)

```bash
cd /path/to/tracker
cp deploy/.env.example deploy/.env      # optional: edit WEB_BIND, WEB_PORT, ...
deploy/switch.sh docker                 # builds, disables the systemd units, starts the stack
deploy/switch.sh status                 # -> RESULT : docker
.venv/bin/python scheduler/doctor.py    # auto-detects docker mode
docker compose -f deploy/docker-compose.yml logs -f scheduler
```

Dashboard: `http://127.0.0.1:8010/?token=<WEB_TOKEN>`. Back to systemd: `deploy/switch.sh systemd`
(stops the stack, keeps `data/`, re-runs `scheduler/install-user.sh`). Rollback is the same command;
nothing in `data/` is changed by switching.

Without `switch.sh` (only if the systemd units are already off): `docker compose -f deploy/docker-compose.yml up -d --build`.

## Server quick start (Traefik + Tailscale)

Goal: reachable only from your tailnet. Traefik is optional; the overlay only adds labels to `web`.

1. Install Docker and Tailscale; clone the repo; copy `config/*.example` to `config/trader.env` and
   `config/alpaca.env` (paper keys), keep `config/limits.json`. Move your DB: copy `data/trader.db`
   (see below).
2. `cp deploy/.env.example deploy/.env` and set:
   - `TRADER_HOST` = hostname that resolves to the server **on the tailnet** (MagicDNS name, or a
     split-DNS record pointing at its tailnet IP);
   - `TRAEFIK_NETWORK` = the Docker network your Traefik is on (`docker network ls`);
   - `TRAEFIK_ENTRYPOINT` (`websecure`), `TRAEFIK_TLS` (`true`), `TRAEFIK_CERTRESOLVER` (your resolver, or empty);
   - `WEB_URL=https://<TRADER_HOST>`: **the URL the phone opens**. The ntfy Approve/Reject buttons
     and notification links are built from it.
3. Start: `COMPOSE_FILES="deploy/docker-compose.yml deploy/docker-compose.traefik.yml" deploy/switch.sh docker`
   (or `docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.traefik.yml up -d --build`).
4. Keep it private: bind Traefik's entrypoint to the tailnet IP (`ports: ["100.x.y.z:443:443"]` on the
   Traefik service) or firewall it to `tailscale0`, so the Host rule is only reachable over Tailscale.
   Without Traefik, use `WEB_BIND=<tailnet ip>` (`tailscale ip -4`), never `0.0.0.0` on a public host.

The overlay removes the host port publish from `web`, joins it to the external Traefik network, and sets
labels: router `Host(<TRADER_HOST>)`, entrypoint, TLS/certresolver, service port 8010.
Validate: `TRADER_HOST=x docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.traefik.yml config`.

## Basic auth

Set `WEB_BASIC_AUTH=user:password` (in `deploy/.env` or `config/trader.env`). Every web route then needs
HTTP Basic credentials **and** the existing `WEB_TOKEN` (query `?token=` or cookie). `/healthz` stays open
(returns `{"ok": true}`, nothing else). ntfy Approve/Reject buttons carry a matching
`headers.Authorization=Basic ...` so they keep working; the header is not written to the journal. Unset
= behaviour exactly as before. Changing the password: edit it and `docker compose ... up -d web scheduler`
(old notifications keep the old header and stop working, which is intended).

## Ollama

`LLM_BACKEND=ollama` is set in compose (override in `deploy/.env`). `OLLAMA_URL` / `OLLAMA_MODEL` come from
`config/trader.env`. `localhost` inside a container is the container, so the fallback defaults to
`OLLAMA_FALLBACK_URL=http://host.docker.internal:11434` (compose maps `host.docker.internal` to the Docker
host). Ollama on the host must listen beyond loopback for that (`OLLAMA_HOST=0.0.0.0`, firewall to
docker0/tailnet) or point the fallback at a tailnet address.

## Moving `data/trader.db`

Stop whatever writes it first (`deploy/switch.sh status` shows who), then copy the file **with its WAL**:
`sqlite3 data/trader.db "PRAGMA wal_checkpoint(TRUNCATE)"` and copy `data/trader.db` to the new machine's
`data/` (or `rsync -a data/ server:/path/data/`). Ownership must match `TRADER_UID`. **Only one machine
runs the system against the same Alpaca keys**: stop and disable the old one
(`deploy/switch.sh` on that machine, or `systemctl --user disable --now thesis-*.timer thesis-broker thesis-web`).
`client_order_id` de-duplication is per database, not per Alpaca account.

## Checks and troubleshooting

- `.venv/bin/python scheduler/doctor.py` (docker mode): containers healthy, scheduler heartbeat
  (`cron_alive`, < 5 min), and FAIL if systemd units are also active.
- `docker compose -f deploy/docker-compose.yml ps` / `logs scheduler`. Catch-up state lives in settings
  `cron_last:<job>`; force a run: `docker compose ... exec scheduler python scheduler/cron_loop.py --once --job tick`.
- `exec scheduler python scheduler/tick.py --dry-run` is safe; kill switch is still `data/HALT`.
- `limits.json` must exist before `up` (a missing bind source becomes an empty directory).
