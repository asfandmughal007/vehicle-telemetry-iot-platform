# syntax=docker/dockerfile:1
# Build context is the REPOSITORY ROOT (the shared package lives in packages/):
#   docker build -f services/simulator/Dockerfile -t vehicle-simulator .

# ---- build stage: install into a virtualenv, nothing from here but /opt/venv is shipped
FROM python:3.15-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /src
COPY packages/telemetry-common ./packages/telemetry-common
COPY services/simulator ./services/simulator
# One pip call so the local telemetry-common satisfies the simulator's dependency.
RUN pip install "./packages/telemetry-common[can]" ./services/simulator

# ---- runtime stage: no compiler, no pip cache, no root
FROM python:3.15-slim AS runtime
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER app
# Configuration comes from SIM_* environment variables at run time (see .env.example).
CMD ["python", "-m", "vehicle_simulator"]
