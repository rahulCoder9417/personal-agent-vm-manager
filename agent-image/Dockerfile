# Agent image: Hermes Agent (Nous Research) + Claude Code (Anthropic, official CLI) + our Bridge.
# Build: docker build -t personal-agent/agent:dev agent-image/
#
# Runs as uid 10001 on a read-only root filesystem; everything it writes lives
# in /data (HOME, HERMES_HOME, CLAUDE_CONFIG_DIR, TMPDIR, workspace). Hermes's own chat
# connectors and cron are off; its API server listens on 127.0.0.1 only. The Bridge
# (0.0.0.0:8765) is the only way in.
#
# Claude Code is installed with Anthropic's documented native installer at a pinned
# version and checked against Anthropic's GPG-signed release manifest. The binary is
# not modified. Its credentials (the user's own Claude sign-in) live in /data/claude.

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
# Hermes no longer builds wheels (its setup.py refuses outside Nix: the wheel would miss the
# bundled skills, locales and plugin manifests). It supports a source checkout with an editable
# install, so: the pinned source tree at /opt/hermes (read-only at runtime) + `pip install -e`.
# [mcp] = MCP client for the Platform MCP and user servers; aiohttp = the API server platform.
RUN git init -q /opt/hermes \
 && git -C /opt/hermes fetch -q --depth 1 https://github.com/NousResearch/hermes-agent "${HERMES_REF}" \
 && git -C /opt/hermes checkout -q FETCH_HEAD \
 && rm -rf /opt/hermes/.git
RUN pip install -e "/opt/hermes[mcp]" "aiohttp==3.14.3"
COPY bridge /opt/bridge/bridge
# Precompile so nothing needs to write .pyc files at runtime (read-only rootfs).
# (Hermes's tree also holds skill/template files that aren't importable modules: best effort.)
RUN python -m compileall -q /opt/venv /opt/bridge \
 && (python -m compileall -q /opt/hermes >/dev/null || true)

FROM ${PYTHON_IMAGE} AS claude
# Pin Claude Code to an exact release; bump deliberately (code.claude.com/docs/en/setup).
ARG CLAUDE_CODE_VERSION=2.1.283
# Anthropic's release signing key (code.claude.com/docs/en/setup#binary-integrity-and-code-signing).
ARG CLAUDE_CODE_KEY_FPR=31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && rm -rf /var/lib/apt/lists/*
ENV HOME=/opt/claude DISABLE_AUTOUPDATER=1
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}" \
 && BIN="$HOME/.local/share/claude/versions/${CLAUDE_CODE_VERSION}" && test -x "$BIN" \
 && export GNUPGHOME="$(mktemp -d)" \
 && curl -fsSL https://downloads.claude.ai/keys/claude-code.asc | gpg --batch --import \
 && gpg --batch --with-colons --fingerprint | grep -q "^fpr:::::::::${CLAUDE_CODE_KEY_FPR}:" \
 && REPO=https://downloads.claude.ai/claude-code-releases \
 && curl -fsSLo /tmp/manifest.json "$REPO/${CLAUDE_CODE_VERSION}/manifest.json" \
 && curl -fsSLo /tmp/manifest.json.sig "$REPO/${CLAUDE_CODE_VERSION}/manifest.json.sig" \
 && gpg --batch --verify /tmp/manifest.json.sig /tmp/manifest.json \
 && case "$(uname -m)" in x86_64) PLAT=linux-x64 ;; aarch64) PLAT=linux-arm64 ;; *) exit 1 ;; esac \
 && WANT="$(python -c 'import json,sys; print(json.load(open("/tmp/manifest.json"))["platforms"][sys.argv[1]]["checksum"])' "$PLAT")" \
 && echo "${WANT}  ${BIN}" | sha256sum -c - \
 && "$BIN" --version \
 && rm -rf "$GNUPGHOME" /tmp/manifest.json /tmp/manifest.json.sig

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
COPY --from=build /opt/hermes /opt/hermes
# The installed release tree as published (launcher symlink + versions/<version>), unmodified.
COPY --from=claude /opt/claude/.local /opt/claude/.local
RUN ln -s /opt/claude/.local/bin/claude /usr/local/bin/claude
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/opt/bridge \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    HOME=/data/home \
    HERMES_HOME=/data/hermes \
    TMPDIR=/data/tmp \
    PLATFORM_FILE=/data/.platform/platform.json \
    BRIDGE_PORT=8765 \
    CLAUDE_CONFIG_DIR=/data/claude \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_UPDATES=1 \
    GIT_TERMINAL_PROMPT=0
USER 10001:10001
WORKDIR /data
EXPOSE 8765
ENTRYPOINT ["python", "-m", "bridge"]
