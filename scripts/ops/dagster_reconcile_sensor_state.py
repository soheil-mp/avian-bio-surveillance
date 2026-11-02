#!/usr/bin/env python3
"""Reconcile persisted Dagster sensor state with the live workspace.

Dagster 1.9.13 can mis-handle obsolete standard sensor rows that remain in the
instance database after repository refactors. When that happens, AssetDaemon
tries to recreate the implicit ``default_automation_condition_sensor`` and
collides with the existing ``jobs.job_origin_id`` row.

This operational helper discovers the sensors currently declared in the live
workspace, then removes persisted sensor rows whose origin or selector IDs no
longer exist in code.
"""

from __future__ import annotations

import os
import time

import psycopg2
from dagster import DagsterError, DagsterInstance
from dagster._check import CheckError
from dagster._core.workspace.load import load_workspace_process_context_from_yaml_paths
from psycopg2 import errorcodes


DEFAULT_WORKSPACE_PATH = "/opt/dagster_home/workspace.yaml"
RETRY_ATTEMPTS = 10
RETRY_DELAY_SECONDS = 2
LOG_PREFIX = "[dagster-daemon-reconcile]"


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required.")
    return value


def _load_current_sensor_ids(workspace_path: str) -> tuple[set[str], set[str]]:
    instance = DagsterInstance.get()
    with load_workspace_process_context_from_yaml_paths(
        instance=instance,
        yaml_paths=[workspace_path],
    ) as workspace_context:
        request_context = workspace_context.create_request_context()

        origin_ids: set[str] = set()
        selector_ids: set[str] = set()

        for location_name in request_context.code_location_names:
            code_location = request_context.get_code_location(location_name)
            for repository_name in code_location.get_repository_names():
                repository = code_location.get_repository(repository_name)
                for sensor in repository.get_sensors():
                    origin_ids.add(sensor.get_remote_origin_id())
                    selector_ids.add(sensor.selector_id)

    return origin_ids, selector_ids


def _load_current_sensor_ids_with_retry(
    workspace_path: str,
) -> tuple[set[str], set[str]]:
    last_error: OSError | RuntimeError | DagsterError | CheckError | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return _load_current_sensor_ids(workspace_path)
        except (OSError, RuntimeError, DagsterError, CheckError) as error:
            last_error = error
            print(
                f"{LOG_PREFIX} Workspace load attempt "
                f"{attempt}/{RETRY_ATTEMPTS} failed: {error}"
            )
            if attempt == RETRY_ATTEMPTS:
                break
            time.sleep(RETRY_DELAY_SECONDS)

    raise RuntimeError(
        "Unable to load the Dagster workspace before daemon startup."
    ) from last_error


def _load_stale_ids(
    cursor: psycopg2.extensions.cursor,
    *,
    table_name: str,
    id_column: str,
    type_column: str,
    keep_ids: set[str],
) -> list[str]:
    if keep_ids:
        cursor.execute(
            f"""
            SELECT {id_column}
            FROM {table_name}
            WHERE {type_column} = %s
              AND NOT ({id_column} = ANY(%s))
            ORDER BY {id_column}
            """,
            ("SENSOR", sorted(keep_ids)),
        )
    else:
        cursor.execute(
            f"""
            SELECT {id_column}
            FROM {table_name}
            WHERE {type_column} = %s
            ORDER BY {id_column}
            """,
            ("SENSOR",),
        )

    return [row[0] for row in cursor.fetchall()]


def _prune_stale_sensor_state(
    *,
    keep_origin_ids: set[str],
    keep_selector_ids: set[str],
) -> tuple[list[str], list[str]]:
    connection = psycopg2.connect(
        host=_required_env("DAGSTER_PG_HOST"),
        port=int(_required_env("DAGSTER_PG_PORT")),
        user=_required_env("DAGSTER_PG_USER"),
        password=_required_env("DAGSTER_PG_PASSWORD"),
        dbname=_required_env("DAGSTER_PG_DB"),
    )

    try:
        with connection, connection.cursor() as cursor:
            try:
                stale_selector_ids = _load_stale_ids(
                    cursor,
                    table_name="instigators",
                    id_column="selector_id",
                    type_column="instigator_type",
                    keep_ids=keep_selector_ids,
                )
                stale_origin_ids = _load_stale_ids(
                    cursor,
                    table_name="jobs",
                    id_column="job_origin_id",
                    type_column="job_type",
                    keep_ids=keep_origin_ids,
                )
            except psycopg2.Error as error:
                if error.pgcode != errorcodes.UNDEFINED_TABLE:
                    raise
                print(
                    f"{LOG_PREFIX} Dagster instance tables are not "
                    "initialized yet; skipping sensor cleanup."
                )
                return [], []

            if stale_selector_ids:
                cursor.execute(
                    """
                        DELETE FROM instigators
                        WHERE instigator_type = %s
                          AND selector_id = ANY(%s)
                        """,
                    ("SENSOR", stale_selector_ids),
                )

            if stale_origin_ids:
                cursor.execute(
                    """
                        DELETE FROM jobs
                        WHERE job_type = %s
                          AND job_origin_id = ANY(%s)
                        """,
                    ("SENSOR", stale_origin_ids),
                )

        return stale_origin_ids, stale_selector_ids
    finally:
        connection.close()


def main() -> int:
    workspace_path = os.environ.get("DAGSTER_WORKSPACE_PATH", DEFAULT_WORKSPACE_PATH)
    current_origin_ids, current_selector_ids = _load_current_sensor_ids_with_retry(
        workspace_path
    )

    stale_origin_ids, stale_selector_ids = _prune_stale_sensor_state(
        keep_origin_ids=current_origin_ids,
        keep_selector_ids=current_selector_ids,
    )

    print(
        f"{LOG_PREFIX} Current sensors: "
        f"{len(current_origin_ids)} origin IDs, {len(current_selector_ids)} selector IDs."
    )
    print(
        f"{LOG_PREFIX} Removed stale sensor rows: "
        f"{len(stale_origin_ids)} jobs, {len(stale_selector_ids)} instigators."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
