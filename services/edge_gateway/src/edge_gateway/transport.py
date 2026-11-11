"""MQTT transport.

The rest of the gateway only sees the small :class:`Transport` protocol, so the
store-and-forward logic is tested without a broker.

Reconnect: paho's network thread (``loop_start``) reconnects automatically, also when
the *first* connection attempt fails, using the exponential delay configured with
``reconnect_delay_set``.

Delivery semantics: QoS 1 is *at least once*. If the connection drops after the
broker received a message but before the PUBACK arrived, the forwarder retries and the
cloud sees a duplicate. This is expected; the Lambda writes idempotently by
``message_id``.
"""

from __future__ import annotations

import logging
import ssl
import threading
from typing import Protocol

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from edge_gateway.config import GatewaySettings

logger = logging.getLogger(__name__)

_QOS = 1
_SHUTDOWN_TIMEOUT_S = 2.0


class Transport(Protocol):
    @property
    def connected(self) -> bool: ...

    def publish(self, topic: str, payload: str, *, timeout_s: float) -> bool:
        """Publish at QoS 1; ``True`` only once the broker acknowledged the message."""
        ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


class PahoTransport:
    """paho-mqtt (v2 API) client with TLS mutual auth, last will and status messages."""

    def __init__(
        self,
        settings: GatewaySettings,
        *,
        status_topic: str,
        online_payload: str,
        offline_payload: str,
    ) -> None:
        self._settings = settings
        self._status_topic = status_topic
        self._online_payload = online_payload
        self._offline_payload = offline_payload
        self._connected = threading.Event()

        client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id=settings.effective_gateway_id,
            protocol=mqtt.MQTTv311,
        )
        client.reconnect_delay_set(
            min_delay=int(settings.mqtt_reconnect_min_s),
            max_delay=int(settings.mqtt_reconnect_max_s),
        )
        # Published by the *broker* if the gateway vanishes without a clean disconnect.
        client.will_set(status_topic, offline_payload, qos=_QOS, retain=True)
        if settings.mqtt_tls:
            client.tls_set(
                ca_certs=str(settings.mqtt_ca_file),
                certfile=str(settings.mqtt_cert_file),
                keyfile=str(settings.mqtt_key_file),
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        self._client = client

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        s = self._settings
        logger.info(
            "mqtt_connecting", extra={"host": s.mqtt_host, "port": s.mqtt_port, "tls": s.mqtt_tls}
        )
        self._client.connect_async(s.mqtt_host, s.mqtt_port, keepalive=s.mqtt_keepalive_s)
        self._client.loop_start()

    def publish(self, topic: str, payload: str, *, timeout_s: float) -> bool:
        if not self.connected:
            return False
        info = self._client.publish(topic, payload, qos=_QOS)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            return False
        try:
            info.wait_for_publish(timeout=timeout_s)
        except (RuntimeError, ValueError):
            return False
        return info.is_published()

    def stop(self) -> None:
        """Announce ``offline`` explicitly (a clean disconnect does not trigger the will)."""
        if self.connected:
            info = self._client.publish(
                self._status_topic, self._offline_payload, qos=_QOS, retain=True
            )
            try:
                info.wait_for_publish(timeout=_SHUTDOWN_TIMEOUT_S)
            except (RuntimeError, ValueError):
                logger.warning("offline_status_not_sent")
        self._client.disconnect()
        self._client.loop_stop()

    # ---------------------------------------------------------------- callbacks

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: object,
        flags: mqtt.ConnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        if reason_code.is_failure:
            logger.error("mqtt_connect_refused", extra={"reason": str(reason_code)})
            return
        self._connected.set()
        logger.info("mqtt_connected")
        client.publish(self._status_topic, self._online_payload, qos=_QOS, retain=True)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: object,
        flags: mqtt.DisconnectFlags,
        reason_code: ReasonCode,
        properties: Properties | None,
    ) -> None:
        self._connected.clear()
        logger.warning("mqtt_disconnected", extra={"reason": str(reason_code)})
