# One-shot migration runner, used by the chart's pre-install/pre-upgrade hook.
FROM python:3.12-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY packages/mushroom-core /build/packages/mushroom-core
RUN pip install --no-cache-dir /build/packages/mushroom-core

COPY db/migrations /app/db/migrations
COPY scripts/migrate.py /app/migrate.py

RUN groupadd --gid 10001 mushroom \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin mushroom

WORKDIR /app
USER 10001:10001

CMD ["python", "/app/migrate.py"]
