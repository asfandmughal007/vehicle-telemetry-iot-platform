"""HTTP-level tests against the in-memory repository (no AWS involved)."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api_helpers import NOW, at, ms, telemetry_item, vehicle_item
from telemetry_api.app import build_app
from telemetry_api.config import ApiSettings
from telemetry_api.errors import RepositoryUnavailableError
from telemetry_api.repository import InMemoryVehicleRepository


def make_repository() -> InMemoryVehicleRepository:
    return InMemoryVehicleRepository(
        vehicles=[
            vehicle_item("VH-0002", -3600),  # last seen an hour ago -> offline
            vehicle_item("VH-0001", 24, dtc="P0217"),
            vehicle_item("VH-0003", 0),  # known, but no telemetry history
        ],
        telemetry=[telemetry_item("VH-0001", s) for s in range(25)]
        + [telemetry_item("VH-0002", s) for s in range(3)],
    )


def make_client(
    repository: Any = None, settings: ApiSettings | None = None, **client_kwargs: Any
) -> TestClient:
    app = build_app(repository or make_repository(), settings or ApiSettings(), clock=lambda: NOW)
    return TestClient(app, raise_server_exceptions=False, **client_kwargs)


@pytest.fixture
def client() -> TestClient:
    return make_client()


def seconds_of(items: list[dict[str, Any]]) -> list[int]:
    return [round(float(i["speed"])) for i in items]  # speed == sample second in the fixtures


# --- health / vehicles -------------------------------------------------------


def test_health(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["version"]


def test_vehicle_list_is_sorted_and_reports_online_state(client: TestClient) -> None:
    response = client.get("/vehicles")

    assert response.status_code == 200
    body = response.json()
    assert [v["vehicle_id"] for v in body] == ["VH-0001", "VH-0002", "VH-0003"]
    assert [v["online"] for v in body] == [True, False, True]
    assert body[0]["active_dtc"] == "P0217"
    assert body[1]["active_dtc"] is None


def test_vehicle_detail_contains_latest_snapshot(client: TestClient) -> None:
    body = client.get("/vehicles/VH-0001").json()

    assert body["gateway_id"] == "gw-VH-0001"
    assert body["online"] is True
    assert body["latest"]["speed"] == 24.0
    assert body["latest"]["diagnostic_code"] == "P0217"
    assert body["latest"]["timestamp"] == at(24).isoformat().replace("+00:00", "Z")


def test_latest_endpoint_returns_the_snapshot(client: TestClient) -> None:
    body = client.get("/vehicles/VH-0001/latest").json()

    assert body["vehicle_id"] == "VH-0001"
    assert body["engine_status"] == "RUNNING"
    assert body["diagnostic_code"] == "P0217"
    assert body["latitude"] == pytest.approx(49.0134)


@pytest.mark.parametrize("path", ["/vehicles/VH-9999", "/vehicles/VH-9999/latest"])
def test_unknown_vehicle_is_404(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 404
    assert response.json() == {"detail": "vehicle 'VH-9999' not found"}


@pytest.mark.parametrize("vehicle_id", ["ab", "a%20b", "x" * 65, "VH%2F1"])
def test_invalid_vehicle_ids_are_rejected_before_any_lookup(
    client: TestClient, vehicle_id: str
) -> None:
    assert client.get(f"/vehicles/{vehicle_id}").status_code in {404, 422}
    assert client.get(f"/vehicles/{vehicle_id}/latest").status_code in {404, 422}


# --- telemetry history -------------------------------------------------------


def test_telemetry_defaults_to_newest_first(client: TestClient) -> None:
    body = client.get("/vehicles/VH-0001/telemetry").json()

    assert body["vehicle_id"] == "VH-0001"
    assert seconds_of(body["items"]) == list(range(24, -1, -1))
    assert body["next_cursor"] is None


def test_order_asc(client: TestClient) -> None:
    body = client.get("/vehicles/VH-0001/telemetry", params={"order": "asc", "limit": 3}).json()

    assert seconds_of(body["items"]) == [0, 1, 2]


@pytest.mark.parametrize("order", ["desc", "asc"])
def test_pagination_walks_every_item_exactly_once(client: TestClient, order: str) -> None:
    collected: list[int] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 10, "order": order}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/vehicles/VH-0001/telemetry", params=params).json()
        collected += seconds_of(body["items"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break

    expected = list(range(25)) if order == "asc" else list(range(24, -1, -1))
    assert collected == expected
    assert pages == 3


def test_no_trailing_empty_page_when_item_count_is_a_multiple_of_the_limit(
    client: TestClient,
) -> None:
    assert (
        client.get("/vehicles/VH-0001/telemetry", params={"limit": 25}).json()["next_cursor"]
        is None
    )

    last_of_five: dict[str, Any] = {}
    cursor = None
    for _ in range(5):
        params = {"limit": 5, **({"cursor": cursor} if cursor else {})}
        last_of_five = client.get("/vehicles/VH-0001/telemetry", params=params).json()
        cursor = last_of_five["next_cursor"]

    assert len(last_of_five["items"]) == 5
    assert cursor is None


def test_time_range_is_inclusive_on_both_ends(client: TestClient) -> None:
    body = client.get(
        "/vehicles/VH-0001/telemetry",
        params={"start": at(5).isoformat(), "end": at(9).isoformat(), "order": "asc"},
    ).json()

    assert seconds_of(body["items"]) == [5, 6, 7, 8, 9]


def test_time_range_with_pagination_stays_inside_the_range(client: TestClient) -> None:
    params = {"start": at(5).isoformat(), "end": at(20).isoformat(), "limit": 6}
    first = client.get("/vehicles/VH-0001/telemetry", params=params).json()
    second = client.get(
        "/vehicles/VH-0001/telemetry", params={**params, "cursor": first["next_cursor"]}
    ).json()

    assert seconds_of(first["items"]) == [20, 19, 18, 17, 16, 15]
    assert seconds_of(second["items"]) == [14, 13, 12, 11, 10, 9]


def test_empty_window_of_a_known_vehicle_is_200_with_no_items(client: TestClient) -> None:
    response = client.get("/vehicles/VH-0001/telemetry", params={"start": at(1000).isoformat()})

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["next_cursor"] is None


def test_known_vehicle_without_history_is_200_but_unknown_vehicle_is_404(
    client: TestClient,
) -> None:
    assert client.get("/vehicles/VH-0003/telemetry").json()["items"] == []
    assert client.get("/vehicles/VH-9999/telemetry").status_code == 404


def test_a_cursor_cannot_be_used_to_read_another_vehicle(client: TestClient) -> None:
    cursor = client.get("/vehicles/VH-0001/telemetry", params={"limit": 2}).json()["next_cursor"]

    body = client.get("/vehicles/VH-0002/telemetry", params={"cursor": cursor}).json()

    assert {item["vehicle_id"] for item in body["items"]} <= {"VH-0002"}


def test_stale_cursor_that_leaves_an_empty_window_returns_an_empty_page(
    client: TestClient,
) -> None:
    cursor = base64.urlsafe_b64encode(json.dumps({"ts": ms(at(10))}).encode()).decode()

    response = client.get(
        "/vehicles/VH-0001/telemetry", params={"start": at(10).isoformat(), "cursor": cursor}
    )

    assert response.status_code == 200
    assert response.json()["items"] == []


# --- validation errors -------------------------------------------------------


def test_start_after_end_is_400(client: TestClient) -> None:
    response = client.get(
        "/vehicles/VH-0001/telemetry",
        params={"start": at(10).isoformat(), "end": at(5).isoformat()},
    )

    assert response.status_code == 400
    assert "start" in response.json()["detail"]


@pytest.mark.parametrize(
    "cursor",
    [
        "garbage",
        base64.urlsafe_b64encode(b"not json").decode(),
        base64.urlsafe_b64encode(json.dumps({"ts": "x"}).encode()).decode(),
        base64.urlsafe_b64encode(json.dumps({"ts": -5}).encode()).decode(),
        base64.urlsafe_b64encode(json.dumps({"other": 1}).encode()).decode(),
        base64.urlsafe_b64encode(json.dumps({"ts": True}).encode()).decode(),
    ],
)
def test_invalid_cursors_are_400(client: TestClient, cursor: str) -> None:
    response = client.get("/vehicles/VH-0001/telemetry", params={"cursor": cursor})

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid cursor"}


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 1001},
        {"limit": "many"},
        {"order": "sideways"},
        {"start": "2026-11-10T09:00:00"},  # no timezone
        {"start": "yesterday"},
    ],
)
def test_invalid_query_parameters_are_422(client: TestClient, params: dict[str, Any]) -> None:
    assert client.get("/vehicles/VH-0001/telemetry", params=params).status_code == 422


def test_page_size_limits_are_configurable() -> None:
    client = make_client(settings=ApiSettings(max_page_size=5, default_page_size=2))

    assert len(client.get("/vehicles/VH-0001/telemetry").json()["items"]) == 2
    assert client.get("/vehicles/VH-0001/telemetry", params={"limit": 6}).status_code == 422


# --- cross-cutting behaviour -------------------------------------------------


def test_request_id_is_generated_or_echoed_when_well_formed(client: TestClient) -> None:
    generated = client.get("/health").headers["X-Request-ID"]
    echoed = client.get("/health", headers={"X-Request-ID": "trace-123"}).headers["X-Request-ID"]
    replaced = client.get("/health", headers={"X-Request-ID": "bad id\twith;chars"}).headers[
        "X-Request-ID"
    ]

    assert len(generated) == 36
    assert echoed == "trace-123"
    assert replaced != "bad id\twith;chars"
    assert len(replaced) == 36


def test_repository_outage_is_503_without_internals() -> None:
    class Down(InMemoryVehicleRepository):
        def list_vehicles(self) -> list[dict[str, Any]]:
            raise RepositoryUnavailableError("table arn:aws:dynamodb:secret is down")

    response = make_client(Down()).get("/vehicles")

    assert response.status_code == 503
    assert "arn" not in response.text
    assert response.headers["X-Request-ID"]


def test_unexpected_error_is_500_without_internals() -> None:
    class Broken(InMemoryVehicleRepository):
        def list_vehicles(self) -> list[dict[str, Any]]:
            raise RuntimeError("secret internal detail")

    response = make_client(Broken()).get("/vehicles")

    assert response.status_code == 500
    assert response.json() == {"detail": "internal server error"}


def test_cors_allows_only_configured_origins() -> None:
    client = make_client(
        settings=ApiSettings(cors_origins="http://localhost:3000, https://dash.example")
    )

    allowed = client.get("/health", headers={"Origin": "http://localhost:3000"})
    other = client.get("/health", headers={"Origin": "https://evil.example"})

    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-origin" not in other.headers


def test_no_cors_headers_by_default(client: TestClient) -> None:
    response = client.get("/health", headers={"Origin": "http://localhost:3000"})

    assert "access-control-allow-origin" not in response.headers


def test_openapi_documents_all_endpoints(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert set(paths) == {
        "/health",
        "/vehicles",
        "/vehicles/{vehicle_id}",
        "/vehicles/{vehicle_id}/telemetry",
        "/vehicles/{vehicle_id}/latest",
    }


# --- configuration -----------------------------------------------------------


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_TELEMETRY_TABLE", "t1")
    monkeypatch.setenv("API_CORS_ORIGINS", "http://a, http://b ,")
    monkeypatch.setenv("API_DYNAMODB_ENDPOINT_URL", "")  # empty counts as unset

    settings = ApiSettings()

    assert settings.telemetry_table == "t1"
    assert settings.cors_origin_list == ["http://a", "http://b"]
    assert settings.dynamodb_endpoint_url is None


@pytest.mark.parametrize(
    ("name", "value"),
    [("API_MAX_PAGE_SIZE", "0"), ("API_DEFAULT_PAGE_SIZE", "5000"), ("API_LOG_LEVEL", "LOUD")],
)
def test_invalid_settings_fail_fast(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    from pydantic import ValidationError

    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        ApiSettings()
