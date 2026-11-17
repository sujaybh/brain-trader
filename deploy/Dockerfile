# One image for every service (broker, web, scheduler); compose picks the command.
# Build context is the repo root:  docker compose -f deploy/docker-compose.yml build
# No Claude CLI in here: run the LLM features with LLM_BACKEND=ollama (see deploy/README.md).
FROM python:3.12-slim

ENV TZ=America/New_York \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first (cached until requirements.txt changes). pytest is not needed at runtime.
COPY requirements.txt .
RUN grep -v '^pytest' requirements.txt > /tmp/req.txt && pip install -r /tmp/req.txt && rm /tmp/req.txt

COPY broker ./broker
COPY scheduler ./scheduler
COPY strategies ./strategies
COPY tools ./tools
COPY trader ./trader
COPY web ./web
COPY docs ./docs
COPY CLAUDE.md ./CLAUDE.md
# Default limits; compose bind-mounts the real config/limits.json read-only over this.
COPY config/limits.json ./config/limits.json

# Non-root. The uid/gid should match the host user that owns ./data (compose also sets `user:`),
# so the bind-mounted trader.db stays writable by both the host and the containers.
ARG UID=1000
ARG GID=1000
RUN groupadd -g "${GID}" thesis \
 && useradd -u "${UID}" -g "${GID}" -M -d /tmp -s /usr/sbin/nologin thesis \
 && mkdir -p /app/data && chown -R "${UID}:${GID}" /app/data
USER thesis

CMD ["python", "-m", "web"]
