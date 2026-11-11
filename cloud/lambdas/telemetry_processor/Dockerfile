# syntax=docker/dockerfile:1
# LOCAL DEVELOPMENT image: runs the Lambda's processing logic behind an MQTT subscription.
# (The Lambda itself is packaged by AWS CDK.)
# Build context is the REPOSITORY ROOT:
#   docker build -f cloud/lambdas/telemetry_processor/Dockerfile -t local-ingestor .

FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /src
COPY packages/telemetry-common ./packages/telemetry-common
COPY cloud/lambdas/telemetry_processor ./cloud/lambdas/telemetry_processor
# telemetry-common without the "can" extra: no CAN libraries are needed in the cloud.
RUN pip install ./packages/telemetry-common "./cloud/lambdas/telemetry_processor[local]"

FROM python:3.12-slim AS runtime
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER app
CMD ["python", "-m", "telemetry_processor.local_ingestor"]
