"""Domain errors; the HTTP layer maps each one to a status code."""

from __future__ import annotations


class VehicleNotFoundError(LookupError):
    """No data has ever been received for this vehicle (HTTP 404)."""

    def __init__(self, vehicle_id: str) -> None:
        self.vehicle_id = vehicle_id
        super().__init__(f"vehicle '{vehicle_id}' not found")


class InvalidQueryError(ValueError):
    """The request is well-formed but its parameters make no sense (HTTP 400)."""


class RepositoryUnavailableError(RuntimeError):
    """The data store failed or is unreachable (HTTP 503). Callers may retry."""
