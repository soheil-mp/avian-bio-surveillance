#!/usr/bin/env python3
"""Wait until the Dagster user-code gRPC servers answer Dagster requests."""

from __future__ import annotations

import os
import time

from dagster._api.list_repositories import sync_list_repositories_grpc
from dagster._grpc.client import DagsterGrpcClient


LOG_PREFIX = "[dagster-code-server-wait]"
DEFAULT_ATTEMPTS = 30
DEFAULT_DELAY_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 5.0


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required.")
    return value


def _wait_setting(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as error:
        raise RuntimeError(f"Environment variable {name} must be numeric.") from error
    if parsed <= 0:
        raise RuntimeError(f"Environment variable {name} must be greater than zero.")
    return parsed


def _endpoint_specs() -> list[tuple[str, int, str]]:
    return [
        (
            _required_env("DAGSTER_CODE_SERVER_DOCKER_HOST"),
            int(_required_env("DAGSTER_CODE_SERVER_PORT")),
            "ingestion code-server",
        ),
        (
            _required_env("DAGSTER_MODEL_CODE_SERVER_DOCKER_HOST"),
            int(_required_env("DAGSTER_MODEL_CODE_SERVER_PORT")),
            "model code-server",
        ),
    ]


def _loaded_repository_labels(host: str, port: int) -> list[str]:
    client = DagsterGrpcClient(host=host, port=port)
    client.get_server_id(timeout=REQUEST_TIMEOUT_SECONDS)
    response = sync_list_repositories_grpc(client)
    repositories = [
        f"{symbol.repository_name}@{symbol.attribute}"
        for symbol in response.repository_symbols
    ]
    if not repositories:
        raise RuntimeError(
            "gRPC server is reachable but reported no loadable repositories."
        )
    return repositories


def _wait_for_endpoint(
    host: str, port: int, label: str, attempts: int, delay_seconds: float
) -> None:
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            repositories = _loaded_repository_labels(host, port)
            print(
                f"{LOG_PREFIX} {label} ready at {host}:{port} "
                f"with repositories: {', '.join(repositories)}."
            )
            return
        except Exception as error:
            last_error = error
            print(
                f"{LOG_PREFIX} Waiting for {label} at {host}:{port} "
                f"({attempt}/{attempts}): {error}"
            )
            if attempt == attempts:
                break
            time.sleep(delay_seconds)

    raise RuntimeError(
        f"Timed out waiting for {label} at {host}:{port}."
    ) from last_error


def main() -> int:
    attempts = int(_wait_setting("DAGSTER_CODE_SERVER_WAIT_ATTEMPTS", DEFAULT_ATTEMPTS))
    delay_seconds = _wait_setting(
        "DAGSTER_CODE_SERVER_WAIT_DELAY_SECONDS", DEFAULT_DELAY_SECONDS
    )

    for host, port, label in _endpoint_specs():
        _wait_for_endpoint(host, port, label, attempts, delay_seconds)

    print(f"{LOG_PREFIX} All Dagster code servers are reachable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
