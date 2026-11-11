# syntax=docker/dockerfile:1
# Build context is the REPOSITORY ROOT (the shared package lives in packages/):
#   docker build -f services/edge_gateway/Dockerfile -t edge-gateway .

# ---- build stage
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /src
COPY packages/telemetry-common ./packages/telemetry-common
COPY services/edge_gateway ./services/edge_gateway
RUN pip install "./packages/telemetry-common[can]" ./services/edge_gateway

# ---- runtime stage
FROM python:3.12-slim AS runtime
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data/buffer \
    && chown -R app:app /data
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GW_BUFFER_PATH=/data/buffer/gateway.sqlite3
# The store-and-forward buffer must outlive the container, otherwise unsent
# telemetry is lost on restart. Mount a volume here.
VOLUME ["/data"]
USER app
# Configuration comes from GW_* environment variables at run time (see .env.example).
# Certificates for AWS IoT Core are mounted read-only at run time, never copied in.
CMD ["python", "-m", "edge_gateway"]
