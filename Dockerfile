# Build stage: produce a wheel for the oracle package so the runtime layer
# doesn't need build tooling.
FROM python:3.13-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --upgrade pip build && python -m build --wheel

# Runtime: OpenClaw base (verified live on ghcr.io, runs as user `node`,
# skills baked under /app/skills/<name>/SKILL.md).
FROM ghcr.io/openclaw/openclaw:latest

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
 && rm -rf /var/lib/apt/lists/*

# Isolate the oracle CLI in a venv on a well-known path so PATH setup doesn't
# depend on the base image's python layout.
ENV VIRTUAL_ENV=/opt/walchi
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
COPY --from=build /src/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# Drop the skill into OpenClaw's baked-in skills dir.
COPY --chown=node:node claw/walchi-oracle/SKILL.md /app/skills/walchi-oracle/SKILL.md

USER node
