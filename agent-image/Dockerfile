# Agent image: Hermes Agent (Nous Research) + our Bridge.
# Build: docker build -t personal-agent/agent:dev agent-image/
#
# Runs as uid 10001 on a read-only root filesystem; everything it writes lives
# in /data (HOME, HERMES_HOME, TMPDIR, workspace). Hermes's own chat connectors
# and cron are off; its API server listens on 127.0.0.1 only. The Bridge
# (0.0.0.0:8765) is the only way in.

ARG PYTHON_IMAGE=python:3.14-slim-trixie

FROM ${PYTHON_IMAGE} AS build
# Pin Hermes to an exact commit for reproducible images; bump deliberately.
ARG HERMES_REF=35272ce28b29bae90e9ecceff9def17ae791c012
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
# [mcp] = MCP client for the Platform MCP and user servers; aiohttp = the API server platform.
RUN pip install "hermes-agent[mcp] @ git+https://github.com/NousResearch/hermes-agent@${HERMES_REF}" "aiohttp==3.14.3"
COPY bridge /opt/bridge/bridge
# Precompile so nothing needs to write .pyc files at runtime (read-only rootfs).
RUN python -m compileall -q /opt/venv /opt/bridge

FROM ${PYTHON_IMAGE}
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tzdata git curl procps \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 10001 agent \
 && useradd -u 10001 -g 10001 -d /data/home -M -s /bin/bash agent \
 && mkdir -p /data \
 && chown 10001:10001 /data \
 && chmod 0700 /data
COPY --from=build /opt/venv /opt/venv
COPY --from=build /opt/bridge /opt/bridge
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/opt/bridge \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    HOME=/data/home \
    HERMES_HOME=/data/hermes \
    TMPDIR=/data/tmp \
    PLATFORM_FILE=/data/.platform/platform.json \
    BRIDGE_PORT=8765
USER 10001:10001
WORKDIR /data
EXPOSE 8765
ENTRYPOINT ["python", "-m", "bridge"]
