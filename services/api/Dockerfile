# syntax=docker/dockerfile:1
# Build context is the REPOSITORY ROOT:
#   docker build -f services/api/Dockerfile -t telemetry-api .

FROM python:3.15-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /src
COPY packages/telemetry-common ./packages/telemetry-common
COPY services/api ./services/api
RUN pip install ./packages/telemetry-common "./services/api[server]"

FROM python:3.15-slim AS runtime
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER app
EXPOSE 8080
# AWS credentials / region come from the environment or the task/instance role -- never the image.
CMD ["uvicorn", "telemetry_api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
