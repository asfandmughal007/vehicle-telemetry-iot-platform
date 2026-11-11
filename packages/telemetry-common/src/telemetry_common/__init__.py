"""Shared contract between all components of the Vehicle Telemetry IoT Platform.

Everything that more than one component must agree on lives here:

* :mod:`telemetry_common.schema` -- the telemetry message (JSON contract, v1.0)
* :mod:`telemetry_common.topics` -- MQTT topic hierarchy
* :mod:`telemetry_common.dtc` -- diagnostic trouble code packing for CAN
* :mod:`telemetry_common.can_db` -- the vehicle DBC (requires the ``can`` extra)
* :mod:`telemetry_common.structured_logging` -- JSON log format
"""

__version__ = "0.1.0"
