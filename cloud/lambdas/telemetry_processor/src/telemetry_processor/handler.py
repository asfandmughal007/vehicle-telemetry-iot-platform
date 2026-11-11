"""AWS Lambda entry point.

Handler: ``telemetry_processor.handler.lambda_handler``

Triggered by an AWS IoT rule::

    SELECT *, topic(2) AS topic_vehicle_id FROM 'vehicles/+/telemetry'
"""

from __future__ import annotations

import logging
import os
from typing import Any

from telemetry_common.structured_logging import configure_logging
from telemetry_processor.config import ProcessorConfig
from telemetry_processor.factory import build_processor
from telemetry_processor.processor import TelemetryProcessor

configure_logging("telemetry-processor", os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

# Created on first invocation and reused while the execution environment is warm.
_processor: TelemetryProcessor | None = None


def _get_processor() -> TelemetryProcessor:
    global _processor
    if _processor is None:
        _processor = build_processor(ProcessorConfig.from_env())
    return _processor


def lambda_handler(event: Any, context: object) -> dict[str, str]:
    outcome = _get_processor().process(event)
    return {"outcome": outcome.value}
