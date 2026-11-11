"""Local stand-in for "AWS IoT Core rule -> Lambda".

Subscribes to ``vehicles/+/telemetry`` on a local MQTT broker and feeds every message to
the very same :class:`TelemetryProcessor` that runs in the Lambda. The adapter mimics the
IoT rule: it adds ``topic_vehicle_id`` taken from the topic.

Local simplification: processing errors are logged and the message is skipped, whereas in
AWS the Lambda retry / DLQ would apply.

Run: ``python -m telemetry_processor.local_ingestor`` (needs the ``local`` extra).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from types import FrameType
from typing import Any

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from telemetry_common.structured_logging import configure_logging
from telemetry_common.topics import Channel, InvalidTopicError, parse_topic, subscription_filter
from telemetry_processor.config import ConfigError, ProcessorConfig
from telemetry_processor.factory import build_processor, dynamodb_resource
from telemetry_processor.processor import Outcome, TelemetryProcessor
from telemetry_processor.tables import create_tables
from telemetry_processor.validation import TOPIC_VEHICLE_ID_KEY

logger = logging.getLogger("local_ingestor")

_TABLE_RETRIES = 30


def handle_message(processor: TelemetryProcessor, topic: str, payload: bytes) -> Outcome:
    """Convert one MQTT message into a Lambda-style event and process it."""
    try:
        vehicle_id, channel = parse_topic(topic)
    except InvalidTopicError as exc:
        return processor.reject("invalid_topic", [str(exc)], {"topic": topic})
    if channel is not Channel.TELEMETRY:
        return processor.reject("unexpected_channel", [topic], {"topic": topic})

    try:
        event: Any = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        # In AWS the IoT rule engine cannot parse this and never invokes the Lambda.
        return processor.reject("invalid_json", [], {"topic": topic, "bytes": len(payload)})

    if isinstance(event, dict):
        event[TOPIC_VEHICLE_ID_KEY] = vehicle_id  # what `topic(2) AS topic_vehicle_id` does
    return processor.process(event)


def _wait_for_tables(config: ProcessorConfig) -> None:
    dynamodb = dynamodb_resource(config)
    for attempt in range(1, _TABLE_RETRIES + 1):
        try:
            create_tables(dynamodb, config.telemetry_table, config.vehicles_table)
        except Exception as exc:
            logger.warning("dynamodb_not_ready", extra={"attempt": attempt, "error": str(exc)})
            time.sleep(1.0)
        else:
            return
    raise RuntimeError("DynamoDB did not become available")


def main() -> int:
    configure_logging("local-ingestor", os.environ.get("LOG_LEVEL", "INFO"))
    try:
        config = ProcessorConfig.from_env()
    except ConfigError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return 2

    _wait_for_tables(config)
    processor = build_processor(config)

    client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id="local-ingestor")
    topic_filter = subscription_filter(Channel.TELEMETRY)

    def on_connect(
        c: mqtt.Client,
        userdata: object,
        flags: mqtt.ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        if reason_code.is_failure:
            logger.error("mqtt_connect_refused", extra={"reason": str(reason_code)})
            return
        c.subscribe(topic_filter, qos=1)  # (re-)subscribe after every reconnect
        logger.info("subscribed", extra={"topic": topic_filter})

    def on_message(c: mqtt.Client, userdata: object, msg: mqtt.MQTTMessage) -> None:
        try:
            handle_message(processor, msg.topic, bytes(msg.payload))
        except Exception:
            logger.exception("processing_failed", extra={"topic": msg.topic})

    def on_signal(signum: int, _frame: FrameType | None) -> None:
        logger.info("shutdown_requested", extra={"signal": signal.Signals(signum).name})
        client.disconnect()

    client.on_connect = on_connect
    client.on_message = on_message
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(
        os.environ.get("MQTT_HOST", "localhost"), int(os.environ.get("MQTT_PORT", "1883"))
    )
    client.loop_forever(retry_first_connection=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
