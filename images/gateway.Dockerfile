# Slack gateway: stateless FastAPI ingest.
#
# Multi-stage so the runtime image carries no build toolchain. Runs as a
# non-root user with no writable application directories, which lets the
# Kubernetes deployment set readOnlyRootFilesystem.

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Install into a virtualenv that is copied wholesale into the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY packages/mushroom-core /build/packages/mushroom-core
COPY services/gateway /build/services/gateway

RUN pip install --no-cache-dir /build/packages/mushroom-core \
 && pip install --no-cache-dir /build/services/gateway


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Unprivileged, fixed uid so a PodSecurityContext can pin runAsUser.
RUN groupadd --gid 10001 mushroom \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin mushroom

COPY --from=builder /opt/venv /opt/venv
COPY db/migrations /app/db/migrations

WORKDIR /app
USER 10001:10001

EXPOSE 8080

# Liveness is checked by Kubernetes; this keeps `docker run` honest too.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2).status==200 else 1)"

CMD ["python", "-m", "mushroom_gateway"]
