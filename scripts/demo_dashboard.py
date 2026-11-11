#!/usr/bin/env python3
"""See the API and dashboard working with no Docker, no AWS and no MQTT broker.

Generates ~7 minutes of history for two simulated vehicles (one of them overheats and sets
DTC P0217, the other went offline two hours ago), serves the real FastAPI application on
http://127.0.0.1:8080 and the real dashboard on http://127.0.0.1:8081.

    python scripts/demo_dashboard.py            # opens the dashboard in your browser
    python scripts/demo_dashboard.py --no-browser

Stop with Ctrl+C. The data is held in memory and is static (it does not keep updating).
"""

from __future__ import annotations

import argparse
import functools
import http.server
import random
import threading
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn

from telemetry_api.app import build_app
from telemetry_api.config import ApiSettings
from telemetry_api.repository import InMemoryVehicleRepository
from vehicle_simulator.faults import FaultType
from vehicle_simulator.model import VehicleModel

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"
SIGNALS = (
    "speed",
    "rpm",
    "coolant_temperature",
    "battery_voltage",
    "fuel_level",
    "latitude",
    "longitude",
)
HISTORY_SECONDS = 420


def simulate(vehicle_id: str, seed: int, *, overheat: bool, now: datetime) -> tuple[list, dict]:
    """Run the vehicle model and return (telemetry items, latest-state item)."""
    clock = {"t": now - timedelta(seconds=HISTORY_SECONDS)}
    model = VehicleModel(
        vehicle_id,
        start_latitude=49.0134,
        start_longitude=12.1016,
        rng=random.Random(seed),
        clock=lambda: clock["t"],
    )
    items: list[dict[str, Any]] = []
    snapshot: dict[str, Any] = {}
    for i in range(HISTORY_SECONDS):
        clock["t"] = now - timedelta(seconds=HISTORY_SECONDS - i)
        if overheat and i == 260:
            model.faults.inject(FaultType.OVERHEAT, 400)
        snapshot = model.step(1.0).to_dict()
        item: dict[str, Any] = {k: snapshot[k] for k in SIGNALS}
        item.update(
            vehicle_id=vehicle_id,
            engine_status=snapshot["engine_status"],
            timestamp=snapshot["timestamp"],
            ts=int(clock["t"].timestamp() * 1000),
            message_id=f"{seed:08d}-0000-4000-8000-{i:012d}",
        )
        if snapshot["diagnostic_code"]:
            item["diagnostic_code"] = snapshot["diagnostic_code"]
        items.append(item)
    last = items[-1]
    latest: dict[str, Any] = {
        "vehicle_id": vehicle_id,
        "last_ts": last["ts"],
        "last_seen": last["timestamp"],
        "message_id": last["message_id"],
        "gateway_id": f"gw-{vehicle_id}",
        "engine_status": last["engine_status"],
        "latest": {k: last[k] for k in (*SIGNALS, "engine_status")},
    }
    if snapshot["diagnostic_code"]:
        latest["active_dtc"] = snapshot["diagnostic_code"]
    return items, latest


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {  # noqa: RUF012 - some Windows setups map .js to text/plain
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
    }

    def log_message(self, format: str, *args: Any) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    parser.add_argument("--api-port", type=int, default=8080)
    parser.add_argument("--web-port", type=int, default=8081)
    args = parser.parse_args()
    if args.api_port != 8080:
        print("Note: edit dashboard/config.js (apiBaseUrl) if you change the API port.")

    now = datetime.now(UTC)
    telemetry: list[dict[str, Any]] = []
    vehicles: list[dict[str, Any]] = []
    for vehicle_id, seed, overheat in (("VH-0001", 11, True), ("VH-0002", 12, False)):
        items, latest = simulate(vehicle_id, seed, overheat=overheat, now=now)
        telemetry += items
        vehicles.append(latest)
    vehicles[1]["last_seen"] = (now - timedelta(hours=2)).isoformat()  # pretend: offline

    origins = f"http://127.0.0.1:{args.web_port},http://localhost:{args.web_port}"
    app = build_app(
        InMemoryVehicleRepository(vehicles, telemetry), ApiSettings(cors_origins=origins)
    )

    handler = functools.partial(QuietHandler, directory=str(DASHBOARD_DIR))
    web = http.server.ThreadingHTTPServer(("127.0.0.1", args.web_port), handler)
    threading.Thread(target=web.serve_forever, daemon=True).start()

    url = f"http://127.0.0.1:{args.web_port}/index.html"
    print(f"\nDashboard: {url}\nAPI docs:  http://127.0.0.1:{args.api_port}/docs\nCtrl+C to stop\n")
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=[url]).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.api_port, log_level="warning")
    finally:
        web.shutdown()


if __name__ == "__main__":
    main()
