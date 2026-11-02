#!/usr/bin/env python3
"""Check that a local Dagster code-server can answer real gRPC requests."""

from __future__ import annotations

import os

from dagster._api.list_repositories import sync_list_repositories_grpc
from dagster._grpc.client import DagsterGrpcClient


DEFAULT_HOST = "127.0.0.1"
DEFAULT_TIMEOUT_SECONDS = 5


def _required_int_env(name: str) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} is required.")
    try:
        parsed = int(value)
    except ValueError as error:
        raise RuntimeError(
            f"Environment variable {name} must be an integer."
        ) from error
    if parsed <= 0:
        raise RuntimeError(f"Environment variable {name} must be greater than zero.")
    return parsed


def _timeout_seconds() -> int:
    value = os.environ.get("DAGSTER_HEALTHCHECK_TIMEOUT_SECONDS")
    if value is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        parsed = int(value)
    except ValueError as error:
        raise RuntimeError(
            "Environment variable DAGSTER_HEALTHCHECK_TIMEOUT_SECONDS must be an integer."
        ) from error
    if parsed <= 0:
        raise RuntimeError(
            "Environment variable DAGSTER_HEALTHCHECK_TIMEOUT_SECONDS must be greater than zero."
        )
    return parsed


def main() -> int:
    host = (
        os.environ.get("DAGSTER_CODE_SERVER_HEALTHCHECK_HOST", DEFAULT_HOST).strip()
        or DEFAULT_HOST
    )
    port = _required_int_env("DAGSTER_CODE_SERVER_PORT")
    timeout = int(_timeout_seconds())

    client = DagsterGrpcClient(host=host, port=port)
    client.get_server_id(timeout=timeout)
    response = sync_list_repositories_grpc(client)
    if not response.repository_symbols:
        raise RuntimeError(
            "Dagster gRPC server is reachable but reported no loadable repositories."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
