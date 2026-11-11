"""Wiring: build a processor (with real or local AWS clients) from configuration."""

from __future__ import annotations

from typing import Any

import boto3

from telemetry_processor.config import ProcessorConfig
from telemetry_processor.dlq import DeadLetterQueue
from telemetry_processor.processor import TelemetryProcessor
from telemetry_processor.repository import TelemetryRepository


def dynamodb_resource(config: ProcessorConfig) -> Any:
    """DynamoDB resource; ``endpoint_url`` points to DynamoDB Local in development."""
    return boto3.resource("dynamodb", endpoint_url=config.endpoint_url)


def build_processor(config: ProcessorConfig) -> TelemetryProcessor:
    repository = TelemetryRepository(
        dynamodb_resource(config), config.telemetry_table, config.vehicles_table
    )
    dlq = DeadLetterQueue(boto3.client("sqs"), config.dlq_url) if config.dlq_url else None
    return TelemetryProcessor(repository, dlq, ttl_days=config.ttl_days)
