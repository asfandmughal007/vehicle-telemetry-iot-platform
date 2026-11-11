from __future__ import annotations

import io
import json
import logging

import pytest

from telemetry_common.can_db import (
    ENGINE_STATUS_TO_RAW,
    RAW_TO_ENGINE_STATUS,
    FrameName,
    load_vehicle_dbc,
)
from telemetry_common.schema import EngineStatus
from telemetry_common.structured_logging import JsonFormatter, configure_logging

# --- DBC ---------------------------------------------------------------------

EXPECTED_FRAMES = {
    FrameName.ENGINE_DATA: (0x100, 100),
    FrameName.VEHICLE_DYNAMICS: (0x200, 100),
    FrameName.BATTERY_STATUS: (0x300, 1000),
    FrameName.GNSS_POSITION: (0x400, 1000),
    FrameName.DIAGNOSTIC_TROUBLE_CODE: (0x500, 1000),
}


@pytest.mark.parametrize("frame", list(FrameName))
def test_dbc_defines_every_frame_with_id_and_cycle_time(frame: FrameName) -> None:
    message = load_vehicle_dbc().get_message_by_name(frame.value)
    expected_id, expected_cycle_ms = EXPECTED_FRAMES[frame]

    assert message.frame_id == expected_id
    assert message.cycle_time == expected_cycle_ms
    assert message.length == 8
    assert not message.is_extended_frame


def test_dbc_is_loaded_once() -> None:
    assert load_vehicle_dbc() is load_vehicle_dbc()


def test_engine_status_mapping_matches_dbc_value_table() -> None:
    signal = (
        load_vehicle_dbc()
        .get_message_by_name(FrameName.ENGINE_DATA.value)
        .get_signal_by_name("EngineStatus")
    )
    dbc_choices = {raw: str(name) for raw, name in (signal.choices or {}).items()}

    assert dbc_choices == {raw: status.value for status, raw in ENGINE_STATUS_TO_RAW.items()}
    assert set(RAW_TO_ENGINE_STATUS.values()) == set(EngineStatus)


def test_gnss_resolution_is_better_than_two_centimetres() -> None:
    message = load_vehicle_dbc().get_message_by_name(FrameName.GNSS_POSITION.value)
    position = {"Latitude": 49.0134123, "Longitude": -122.4194155}

    decoded = message.decode(message.encode(position))

    assert decoded["Latitude"] == pytest.approx(position["Latitude"], abs=1e-7)
    assert decoded["Longitude"] == pytest.approx(position["Longitude"], abs=1e-7)


# --- logging -----------------------------------------------------------------


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord("test.logger", logging.INFO, __file__, 1, "dtc_%s", ("set",), None)
    record.__dict__.update(extra)
    return record


def test_json_formatter_emits_standard_fields_and_extras() -> None:
    line = JsonFormatter("unit-test").format(_record(vehicle_id="VH-0001", dtc="P0217"))

    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["service"] == "unit-test"
    assert payload["logger"] == "test.logger"
    assert payload["message"] == "dtc_set"
    assert payload["vehicle_id"] == "VH-0001"
    assert payload["dtc"] == "P0217"
    assert payload["timestamp"].endswith("+00:00")
    assert "\n" not in line


def test_json_formatter_survives_unserialisable_context() -> None:
    payload = json.loads(JsonFormatter("unit-test").format(_record(obj=object())))

    assert payload["obj"].startswith("<object object")


def test_json_formatter_includes_exception() -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())

    payload = json.loads(JsonFormatter("unit-test").format(record))

    assert "RuntimeError: boom" in payload["exception"]


def test_configure_logging_writes_json_to_stream() -> None:
    stream = io.StringIO()
    root = logging.getLogger()
    previous_handlers, previous_level = root.handlers[:], root.level
    try:
        configure_logging("unit-test", "warning", stream=stream)
        logging.getLogger("x").info("hidden")
        logging.getLogger("x").warning("shown", extra={"count": 3})
    finally:
        root.handlers[:] = previous_handlers
        root.setLevel(previous_level)

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["count"] == 3
