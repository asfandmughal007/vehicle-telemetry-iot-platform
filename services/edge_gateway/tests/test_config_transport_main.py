from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from edge_gateway.__main__ import EXIT_CONFIG_ERROR, EXIT_RUNTIME_ERROR, main
from edge_gateway.config import GatewaySettings
from edge_gateway.transport import PahoTransport


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(os.environ):
        if key.startswith("GW_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)  # no stray .env file


# --- configuration -----------------------------------------------------------


def test_settings_defaults() -> None:
    settings = GatewaySettings()

    assert settings.effective_gateway_id == "gw-VH-0001"
    assert settings.mqtt_port == 1883
    assert not settings.mqtt_tls
    assert settings.publish_hz == 1.0


def test_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GW_VEHICLE_ID", "VH-0007")
    monkeypatch.setenv("GW_MQTT_HOST", "broker.local")
    monkeypatch.setenv("GW_GATEWAY_ID", "custom-gw")

    settings = GatewaySettings()

    assert settings.vehicle_id == "VH-0007"
    assert settings.mqtt_host == "broker.local"
    assert settings.effective_gateway_id == "custom-gw"


def test_empty_environment_variable_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GW_GATEWAY_ID", "")

    assert GatewaySettings().effective_gateway_id == "gw-VH-0001"


def test_tls_requires_all_certificate_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GW_MQTT_TLS", "true")
    with pytest.raises(ValidationError, match="GW_MQTT_CA_FILE"):
        GatewaySettings()

    monkeypatch.setenv("GW_MQTT_CA_FILE", "ca.pem")
    monkeypatch.setenv("GW_MQTT_CERT_FILE", "cert.pem")
    monkeypatch.setenv("GW_MQTT_KEY_FILE", "key.pem")
    assert GatewaySettings().mqtt_tls


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GW_VEHICLE_ID", "a/b"),
        ("GW_PUBLISH_HZ", "0"),
        ("GW_MQTT_PORT", "70000"),
        ("GW_BUFFER_MAX_MESSAGES", "0"),
        ("GW_LOG_LEVEL", "LOUD"),
    ],
)
def test_invalid_settings_fail_fast(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        GatewaySettings()


def test_reconnect_bounds_must_be_ordered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GW_MQTT_RECONNECT_MIN_S", "30")
    monkeypatch.setenv("GW_MQTT_RECONNECT_MAX_S", "5")

    with pytest.raises(ValidationError, match="mqtt_reconnect_max_s"):
        GatewaySettings()


# --- MQTT transport (without a broker) --------------------------------------


class FakeReason:
    def __init__(self, failure: bool) -> None:
        self.is_failure = failure

    def __str__(self) -> str:
        return "failure" if self.is_failure else "success"


class FakeClient:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
        self.published.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})


def make_transport() -> PahoTransport:
    return PahoTransport(
        GatewaySettings(),
        status_topic="vehicles/VH-0001/status",
        online_payload="ONLINE",
        offline_payload="OFFLINE",
    )


def test_publish_is_refused_while_disconnected() -> None:
    transport = make_transport()

    assert not transport.connected
    assert not transport.publish("vehicles/VH-0001/telemetry", "{}", timeout_s=0.1)


def test_successful_connect_marks_connected_and_announces_online_retained() -> None:
    transport, client = make_transport(), FakeClient()

    transport._on_connect(client, None, None, FakeReason(False), None)  # type: ignore[arg-type]

    assert transport.connected
    assert client.published == [
        {"topic": "vehicles/VH-0001/status", "payload": "ONLINE", "qos": 1, "retain": True}
    ]


def test_refused_connect_stays_disconnected() -> None:
    transport, client = make_transport(), FakeClient()

    transport._on_connect(client, None, None, FakeReason(True), None)  # type: ignore[arg-type]

    assert not transport.connected
    assert client.published == []


def test_disconnect_callback_clears_connected_flag() -> None:
    transport, client = make_transport(), FakeClient()
    transport._on_connect(client, None, None, FakeReason(False), None)  # type: ignore[arg-type]

    transport._on_disconnect(client, None, None, FakeReason(True), None)  # type: ignore[arg-type]

    assert not transport.connected


def test_start_and_stop_without_a_broker_do_not_hang() -> None:
    transport = make_transport()  # nothing listens on localhost:1883 in the test environment

    transport.start()
    transport.stop()

    assert not transport.connected


# --- entry point -------------------------------------------------------------


def test_main_reports_invalid_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GW_PUBLISH_HZ", "-1")

    assert main() == EXIT_CONFIG_ERROR
    assert "Invalid gateway configuration" in capsys.readouterr().err


def test_main_reports_unavailable_can_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GW_CAN_INTERFACE", "no_such_interface")

    assert main() == EXIT_RUNTIME_ERROR
