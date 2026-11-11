"""Decode CAN frames into named telemetry fields using the vehicle DBC."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import can
from cantools.database import DecodeError

from telemetry_common.can_db import RAW_TO_ENGINE_STATUS, FrameName, load_vehicle_dbc
from telemetry_common.dtc import InvalidDtcError, decode_dtc

if TYPE_CHECKING:
    from cantools.database.can import Database

#: DBC signal name -> field name of the telemetry message (plain physical values).
_SIGNAL_TO_FIELD = {
    "EngineSpeed": "rpm",
    "CoolantTemperature": "coolant_temperature",
    "VehicleSpeed": "speed",
    "FuelLevel": "fuel_level",
    "BatteryVoltage": "battery_voltage",
    "Latitude": "latitude",
    "Longitude": "longitude",
}


class FrameDecodeError(ValueError):
    """A frame with a known ID could not be decoded (wrong length, invalid content)."""


class CanDecoder:
    """Stateless frame decoder.

    Unknown arbitration IDs are *not* an error: on a real bus the gateway sees many
    frames it has no interest in. ``decode`` returns ``None`` for them.
    """

    def __init__(self, database: Database | None = None) -> None:
        self._db = database if database is not None else load_vehicle_dbc()
        self._known_ids = {self._db.get_message_by_name(f.value).frame_id for f in FrameName}

    def decode(self, frame: can.Message) -> dict[str, Any] | None:
        """Return ``{telemetry field: value}`` for a known frame, ``None`` for an unknown one.

        Raises:
            FrameDecodeError: the frame is known but malformed.
        """
        if frame.arbitration_id not in self._known_ids:
            return None
        if frame.is_error_frame or frame.is_remote_frame:
            raise FrameDecodeError(f"unusable frame type for id {frame.arbitration_id:#x}")

        message = self._db.get_message_by_frame_id(frame.arbitration_id)
        try:
            raw = cast(
                "dict[str, Any]",
                message.decode(bytes(frame.data), decode_choices=False, scaling=True),
            )
        except (DecodeError, ValueError) as exc:
            raise FrameDecodeError(f"{message.name}: {exc}") from exc

        try:
            return self._to_fields(message.name, raw)
        except (KeyError, InvalidDtcError) as exc:
            raise FrameDecodeError(f"{message.name}: invalid content ({exc!r})") from exc

    @staticmethod
    def _to_fields(frame_name: str, raw: dict[str, Any]) -> dict[str, Any]:
        fields: dict[str, Any] = {
            _SIGNAL_TO_FIELD[name]: float(value)
            for name, value in raw.items()
            if name in _SIGNAL_TO_FIELD
        }
        if frame_name == FrameName.ENGINE_DATA:
            fields["engine_status"] = RAW_TO_ENGINE_STATUS[int(raw["EngineStatus"])]
        elif frame_name == FrameName.DIAGNOSTIC_TROUBLE_CODE:
            active = int(raw["DtcActive"]) == 1
            fields["diagnostic_code"] = (
                decode_dtc(int(raw["DtcCategory"]), int(raw["DtcCode"])) if active else None
            )
        return fields
