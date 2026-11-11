"""The same behaviour against moto-backed DynamoDB, plus the Lambda adapter."""

from __future__ import annotations

import importlib
import json
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api_helpers import (
    NOW,
    TELEMETRY_TABLE,
    VEHICLES_TABLE,
    at,
    ms,
)
from telemetry_api.app import build_app
from telemetry_api.config import ApiSettings
from telemetry_api.errors import RepositoryUnavailableError
from telemetry_api.repository import DynamoVehicleRepository


@pytest.fixture
def repo(dynamodb: Any) -> DynamoVehicleRepository:
    return DynamoVehicleRepository(dynamodb, TELEMETRY_TABLE, VEHICLES_TABLE)


# --- repository --------------------------------------------------------------


def query(repo: DynamoVehicleRepository, **overrides: Any) -> list[int]:
    kwargs: dict[str, Any] = {
        "start_ms": None,
        "end_ms": None,
        "limit": 100,
        "newest_first": False,
    }
    kwargs.update(overrides)
    return [round(float(i["speed"])) for i in repo.query_telemetry("VH-0001", **kwargs)]


def test_query_returns_items_in_requested_order(repo: DynamoVehicleRepository) -> None:
    assert query(repo) == list(range(25))
    assert query(repo, newest_first=True) == list(range(24, -1, -1))


def test_query_applies_each_kind_of_bound(repo: DynamoVehicleRepository) -> None:
    assert query(repo, start_ms=ms(at(20))) == [20, 21, 22, 23, 24]
    assert query(repo, end_ms=ms(at(2))) == [0, 1, 2]
    assert query(repo, start_ms=ms(at(3)), end_ms=ms(at(5))) == [3, 4, 5]
    assert query(repo, start_ms=ms(at(500))) == []


def test_query_honours_limit(repo: DynamoVehicleRepository) -> None:
    assert query(repo, limit=4, newest_first=True) == [24, 23, 22, 21]


def test_query_never_mixes_vehicles(repo: DynamoVehicleRepository) -> None:
    items = repo.query_telemetry(
        "VH-0002", start_ms=None, end_ms=None, limit=100, newest_first=False
    )

    assert [i["vehicle_id"] for i in items] == ["VH-0002"]


def test_vehicle_lookup_and_listing(repo: DynamoVehicleRepository) -> None:
    assert repo.get_vehicle("VH-0001")["active_dtc"] == "P0217"  # type: ignore[index]
    assert repo.get_vehicle("VH-9999") is None
    assert {v["vehicle_id"] for v in repo.list_vehicles()} == {"VH-0001", "VH-0002"}


def test_aws_errors_become_repository_unavailable(dynamodb: Any) -> None:
    missing = DynamoVehicleRepository(dynamodb, "no-such-table", "no-such-table")

    for call in (
        lambda: missing.list_vehicles(),
        lambda: missing.get_vehicle("VH-0001"),
        lambda: missing.query_telemetry(
            "VH-0001", start_ms=None, end_ms=None, limit=1, newest_first=True
        ),
    ):
        with pytest.raises(RepositoryUnavailableError):
            call()


# --- full stack: HTTP -> service -> DynamoDB ---------------------------------


@pytest.fixture
def client(repo: DynamoVehicleRepository) -> TestClient:
    return TestClient(
        build_app(repo, ApiSettings(), clock=lambda: NOW), raise_server_exceptions=False
    )


def test_http_over_dynamodb_returns_decimals_as_numbers(client: TestClient) -> None:
    body = client.get("/vehicles/VH-0001/latest").json()

    assert body["speed"] == 24.0
    assert body["battery_voltage"] == pytest.approx(14.1)
    assert body["diagnostic_code"] == "P0217"


def test_http_pagination_over_dynamodb(client: TestClient) -> None:
    collected: list[int] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 7, **({"cursor": cursor} if cursor else {})}
        body = client.get("/vehicles/VH-0001/telemetry", params=params).json()
        collected += [round(i["speed"]) for i in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert collected == list(range(24, -1, -1))


def test_stale_cursor_does_not_make_dynamodb_reject_an_inverted_range(client: TestClient) -> None:
    import base64

    cursor = base64.urlsafe_b64encode(json.dumps({"ts": ms(at(10))}).encode()).decode()

    response = client.get(
        "/vehicles/VH-0001/telemetry", params={"start": at(10).isoformat(), "cursor": cursor}
    )

    assert response.status_code == 200
    assert response.json()["items"] == []


def test_vehicle_list_over_dynamodb_marks_old_vehicle_offline(client: TestClient) -> None:
    online = {v["vehicle_id"]: v["online"] for v in client.get("/vehicles").json()}

    assert online == {"VH-0001": True, "VH-0002": True}  # NOW is 60 s after T0, threshold 120 s


def test_outage_is_a_503_over_http(dynamodb: Any) -> None:
    missing = DynamoVehicleRepository(dynamodb, "no-such-table", "no-such-table")
    client = TestClient(build_app(missing, ApiSettings()), raise_server_exceptions=False)

    response = client.get("/vehicles")

    assert response.status_code == 503
    assert "no-such-table" not in response.text


# --- Lambda adapter ----------------------------------------------------------


def http_api_event(path: str, query: str = "") -> dict[str, Any]:
    """Minimal API Gateway HTTP API (payload v2) event."""
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": query,
        "headers": {"host": "api.example.com"},
        "requestContext": {
            "http": {
                "method": "GET",
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "203.0.113.9",
                "userAgent": "pytest",
            },
            "stage": "$default",
        },
        "isBase64Encoded": False,
    }


@pytest.fixture
def lambda_handler(monkeypatch: pytest.MonkeyPatch, dynamodb: Any) -> Any:
    monkeypatch.setenv("API_TELEMETRY_TABLE", TELEMETRY_TABLE)
    monkeypatch.setenv("API_VEHICLES_TABLE", VEHICLES_TABLE)
    module = importlib.import_module("telemetry_api.lambda_handler")
    return importlib.reload(module).handler  # rebuild the app with this test's environment


def test_lambda_serves_health(lambda_handler: Any) -> None:
    response = lambda_handler(http_api_event("/health"), None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["status"] == "ok"


def test_lambda_serves_data_from_dynamodb(lambda_handler: Any) -> None:
    response = lambda_handler(http_api_event("/vehicles/VH-0001/telemetry", "limit=2"), None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert [round(i["speed"]) for i in body["items"]] == [24, 23]
    assert body["next_cursor"]


def test_lambda_maps_errors_to_status_codes(lambda_handler: Any) -> None:
    assert lambda_handler(http_api_event("/vehicles/VH-9999"), None)["statusCode"] == 404
    assert (
        lambda_handler(http_api_event("/vehicles/VH-0001/telemetry", "limit=0"), None)["statusCode"]
        == 422
    )


def test_time_window_helper_sanity() -> None:
    assert at(1) - at(0) == timedelta(seconds=1)
