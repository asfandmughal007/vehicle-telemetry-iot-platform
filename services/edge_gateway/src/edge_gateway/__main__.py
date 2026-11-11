"""Entry point: ``python -m edge_gateway``."""

from __future__ import annotations

import logging
import signal
import sys
from types import FrameType

import can
from pydantic import ValidationError

from edge_gateway.backoff import ExponentialBackoff
from edge_gateway.buffer import SqliteBuffer
from edge_gateway.can_decoder import CanDecoder
from edge_gateway.config import GatewaySettings
from edge_gateway.forwarder import ForwardWorker, StoreAndForwarder
from edge_gateway.gateway import EdgeGateway, GatewayPipeline
from edge_gateway.message_builder import MessageBuilder
from edge_gateway.signal_cache import SignalCache
from edge_gateway.transport import PahoTransport
from telemetry_common.schema import ConnectionStatus
from telemetry_common.structured_logging import configure_logging
from telemetry_common.topics import Channel, build_topic

logger = logging.getLogger("edge_gateway")

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_CONFIG_ERROR = 2


def main() -> int:
    try:
        settings = GatewaySettings()
    except ValidationError as exc:
        print(f"Invalid gateway configuration:\n{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    configure_logging("edge-gateway", settings.log_level, json_output=settings.log_json)

    builder = MessageBuilder(
        vehicle_id=settings.vehicle_id,
        gateway_id=settings.effective_gateway_id,
        firmware_version=settings.firmware_version,
    )
    try:
        bus = can.Bus(interface=settings.can_interface, channel=settings.can_channel)
    except (can.CanError, OSError, ValueError, NotImplementedError) as exc:
        logger.error("can_bus_unavailable", extra={"error": str(exc)})
        return EXIT_RUNTIME_ERROR

    buffer = SqliteBuffer(settings.buffer_path, settings.buffer_max_messages)
    transport = PahoTransport(
        settings,
        status_topic=build_topic(settings.vehicle_id, Channel.STATUS),
        online_payload=builder.build_status(ConnectionStatus.ONLINE).model_dump_json(),
        offline_payload=builder.build_status(ConnectionStatus.OFFLINE).model_dump_json(),
    )
    forwarder = StoreAndForwarder(
        buffer,
        transport,
        ExponentialBackoff(settings.mqtt_reconnect_min_s, settings.mqtt_reconnect_max_s),
        batch_size=settings.forward_batch_size,
        ack_timeout_s=settings.mqtt_ack_timeout_s,
    )
    worker = ForwardWorker(forwarder)
    pipeline = GatewayPipeline(
        CanDecoder(),
        SignalCache(settings.signal_max_age_s),
        builder,
        buffer,
        vehicle_id=settings.vehicle_id,
    )
    gateway = EdgeGateway(bus, pipeline, publish_hz=settings.publish_hz, on_enqueue=worker.wake)

    def handle_signal(signum: int, _frame: FrameType | None) -> None:
        logger.info("shutdown_requested", extra={"signal": signal.Signals(signum).name})
        gateway.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)  # sent by `docker stop`

    transport.start()
    worker.start()
    try:
        gateway.run()
    finally:
        worker.stop()
        worker.join(timeout=10)
        transport.stop()
        logger.info("unsent_messages_kept", extra={"count": len(buffer)})
        buffer.close()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
