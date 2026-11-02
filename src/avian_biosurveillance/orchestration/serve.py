"""Avian Biosurveillance — Dagster code-server launcher.

Replaces the legacy Prefect ``serve.py`` entry-point.  Boots a long-running
Dagster gRPC code-server that exposes
:data:`avian_biosurveillance.orchestration.dagster_defs.defs` to the
``dagster-webserver`` and ``dagster-daemon`` containers.

Configuration
-------------
``config/runtime.env``
    Source of truth for the Dagster gRPC bind host and port.
``DAGSTER_HOME``
    Required by the underlying Dagster runtime; mounted as
    ``/opt/dagster_home`` inside the container.
"""

from __future__ import annotations

import logging
import sys

from avian_biosurveillance.config.runtime import load_runtime_config
from avian_biosurveillance.orchestration.dagster_defs.code_locations import (
    active_code_location,
)


logger = logging.getLogger(__name__)


def main() -> None:
    """Hand off to ``dagster api grpc`` with the project's code-location."""
    runtime_config = load_runtime_config()
    host = runtime_config.dagster_code_server_host
    port = str(runtime_config.dagster_code_server_port)
    code_location = active_code_location()

    # Late import so a missing Dagster install fails clearly at startup
    # rather than at module-load time elsewhere.
    from dagster._cli.api import grpc_command

    args = [
        "--host",
        host,
        "--port",
        port,
        "--module-name",
        "avian_biosurveillance.orchestration.dagster_defs",
        "--attribute",
        "defs",
    ]
    logger.info(
        "Starting Dagster %s code-server on %s:%s",
        code_location,
        host,
        port,
    )
    # ``grpc_command`` is a Click command; invoke it directly.
    grpc_command.main(args=args, standalone_mode=False)  # type: ignore[arg-type]


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # Alembic plugin registration is a one-time bootstrap detail, not an
    # operator-facing startup event.
    logging.getLogger("alembic.runtime.plugins").setLevel(logging.WARNING)
    # Earth Engine's HTTP transport (googleapiclient) logs every automatic
    # 429 retry at WARNING level; collapse to ERROR to avoid log noise.
    logging.getLogger("googleapiclient.http").setLevel(logging.ERROR)
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
    main()
