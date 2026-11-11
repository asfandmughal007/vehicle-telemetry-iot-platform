"""CloudWatch custom metrics via the Embedded Metric Format (EMF).

A single JSON line on stdout is turned into metrics by CloudWatch Logs. This needs no
``PutMetricData`` call (no extra latency, no extra IAM permission, no cost per call).
"""

from __future__ import annotations

import json
import sys
import time
from typing import TextIO

NAMESPACE = "VehicleTelemetry"
SERVICE = "telemetry-processor"


def emit_metric(
    name: str, value: float = 1, unit: str = "Count", *, stream: TextIO | None = None
) -> None:
    record = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": [["Service"]],
                    "Metrics": [{"Name": name, "Unit": unit}],
                }
            ],
        },
        "Service": SERVICE,
        name: value,
    }
    out = stream if stream is not None else sys.stdout
    out.write(json.dumps(record) + "\n")
    out.flush()
