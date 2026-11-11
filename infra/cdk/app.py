#!/usr/bin/env python3
"""CDK entry point.

    python scripts/build_lambdas.py          # build the Lambda zips (once, and after code changes)
    cdk deploy -c environment=dev -c vehicle_ids=VH-0001,VH-0002 -c alarm_email=you@example.com

Account and region come from the AWS CLI profile / environment (CDK_DEFAULT_*), never from code.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_lambda as lambda_

from vehicle_telemetry_cdk import StackConfig, TelemetryStack

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
DASHBOARD = ROOT.parent.parent / "dashboard"


def _lambda_code(name: str) -> lambda_.Code:
    path = BUILD / name
    if not path.is_dir():
        sys.exit(f"Missing {path}. Run `python scripts/build_lambdas.py` first.")
    return lambda_.Code.from_asset(str(path))


def main() -> None:
    app = cdk.App()
    config = StackConfig.from_context({key: app.node.try_get_context(key) for key in _CONTEXT_KEYS})
    TelemetryStack(
        app,
        f"VehicleTelemetry-{config.environment}",
        config=config,
        processor_code=_lambda_code("processor"),
        api_code=_lambda_code("api"),
        dashboard_assets_dir=str(DASHBOARD),
        env=cdk.Environment(
            account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
            region=os.environ.get("CDK_DEFAULT_REGION"),
        ),
    )
    app.synth()


_CONTEXT_KEYS = (
    "environment",
    "vehicle_ids",
    "alarm_email",
    "telemetry_ttl_days",
    "alarm_on_missing_telemetry",
    "api_cors_origins",
)

if __name__ == "__main__":
    main()
