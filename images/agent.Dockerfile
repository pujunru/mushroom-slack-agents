# General agent worker: consumes pgmq and runs opencode v2.
#
# opencode is installed from npm as `@opencode/cli`, pinned to an exact
# version. Note that `ghcr.io/anomalyco/opencode:latest` still serves the v1
# line, so a floating tag is not a safe base here.

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY packages/mushroom-core /build/packages/mushroom-core
COPY services/agent /build/services/agent

RUN pip install --no-cache-dir /build/packages/mushroom-core \
 && pip install --no-cache-dir /build/services/agent


FROM python:3.12-slim AS runtime

# Pin opencode. Override at build time to move versions deliberately:
#   docker build --build-arg OPENCODE_VERSION=2.1.1 ...
ARG OPENCODE_VERSION=2.1.1
ARG NODE_MAJOR=22

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    WORKSPACE_ROOT=/workspace

# ripgrep is used by opencode for code search; git lets agents work in repos.
# libstdc++ is needed by opencode's compiled binary.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates curl git ripgrep libstdc++6 \
 && curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g "@opencode/cli@${OPENCODE_VERSION}" \
 && npm cache clean --force \
 && apt-get purge -y --auto-remove curl \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 mushroom \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin mushroom

COPY --from=builder /opt/venv /opt/venv
COPY db/migrations /app/db/migrations
COPY images/agent/cli.json /etc/mushroom/opencode-cli.json
COPY images/agent/opencode.json /etc/mushroom/opencode.json
COPY images/agent/entrypoint.sh /usr/local/bin/entrypoint.sh
# Skills opencode loads on demand. opencode.json lists /etc/mushroom/skills as
# a source; builtin/ ships with the image and custom/ is where the chart (or
# compose) mounts deployment-specific skills. Both live under one source so a
# missing mount never leaves opencode pointing at a nonexistent directory.
COPY images/agent/skills /etc/mushroom/skills/builtin

RUN chmod +x /usr/local/bin/entrypoint.sh \
 && mkdir -p /etc/mushroom/skills/custom \
 # The workspace is the agent's scratch space and opencode's HOME, so it must
 # be writable even with a read-only root filesystem (mount an emptyDir here).
 && mkdir -p /workspace \
 && chown -R 10001:10001 /workspace

WORKDIR /app
USER 10001:10001

# Metrics only; the worker serves no application traffic.
EXPOSE 9090

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "mushroom_agent.worker"]
