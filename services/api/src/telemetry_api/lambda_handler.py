"""AWS Lambda entry point (API Gateway HTTP API -> Lambda).

Handler: ``telemetry_api.lambda_handler.handler``

The same FastAPI app runs here and in the container image; Mangum translates API Gateway
events to ASGI. The app is built once per execution environment and reused while warm.
"""

from __future__ import annotations

from mangum import Mangum

from telemetry_api.main import create_app

# lifespan="off": the app has no startup/shutdown hooks (and Lambda has no such lifecycle).
handler = Mangum(create_app(), lifespan="off")
