"""Application factory for production use (configuration from the environment).

Container:  ``uvicorn telemetry_api.main:create_app --factory --host 0.0.0.0 --port 8080``
Lambda:     see :mod:`telemetry_api.lambda_handler`
"""

from __future__ import annotations

import boto3
from fastapi import FastAPI

from telemetry_api.app import build_app
from telemetry_api.config import ApiSettings
from telemetry_api.repository import DynamoVehicleRepository
from telemetry_common.structured_logging import configure_logging


def create_app() -> FastAPI:
    settings = ApiSettings()
    configure_logging("telemetry-api", settings.log_level, json_output=settings.log_json)
    dynamodb = boto3.resource("dynamodb", endpoint_url=settings.dynamodb_endpoint_url)
    repository = DynamoVehicleRepository(
        dynamodb, settings.telemetry_table, settings.vehicles_table
    )
    return build_app(repository, settings)
