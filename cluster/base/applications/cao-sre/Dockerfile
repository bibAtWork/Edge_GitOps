FROM node:22-bookworm-slim

ARG CAO_VERSION=2.5.0
ARG OPENCODE_VERSION=1.19.4

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv tmux ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/cao-venv \
    && /opt/cao-venv/bin/pip install --no-cache-dir "cli-agent-orchestrator==${CAO_VERSION}" \
    && npm install --global "opencode-ai@${OPENCODE_VERSION}" \
    && groupadd --gid 10001 cao \
    && useradd --uid 10001 --gid 10001 --home-dir /home/cao --create-home cao

ENV PATH="/opt/cao-venv/bin:${PATH}" \
    HOME=/home/cao \
    XDG_CONFIG_HOME=/home/cao/.config \
    XDG_DATA_HOME=/home/cao/.local/share \
    XDG_CACHE_HOME=/home/cao/.cache \
    PYTHONUNBUFFERED=1

USER 10001:10001
WORKDIR /home/cao
EXPOSE 9889
