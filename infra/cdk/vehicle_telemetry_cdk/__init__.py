"""AWS CDK infrastructure for the Vehicle Telemetry IoT Platform."""

from vehicle_telemetry_cdk.config import StackConfig
from vehicle_telemetry_cdk.telemetry_stack import TelemetryStack

__all__ = ["StackConfig", "TelemetryStack"]
