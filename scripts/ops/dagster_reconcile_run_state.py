#!/usr/bin/env python3
"""Reconcile orphaned Dagster run state before the daemon resumes work.

This Docker Compose deployment uses Dagster's ``DefaultRunLauncher``. Active
run workers launched by that runner do not survive a host or container restart,
but Dagster 1.9 can still leave those runs in ``STARTING`` / ``STARTED`` /
``CANCELING`` state in persistent storage.

That is especially visible during the historical backfill sensor, which treats
``STARTING`` and ``STARTED`` as active and will wait forever on the interrupted
logical day. This startup helper converts those orphaned runs into terminal
states before ``dagster-daemon run`` starts scheduling again.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence

from dagster import DagsterInstance, DagsterRunStatus, RunsFilter
from dagster._core.storage.dagster_run import DagsterRun, RunRecord


LOG_PREFIX = "[dagster-daemon-run-reconcile]"
_CODE_LOCATION_ENV = "AVIAN_DAGSTER_CODE_LOCATION"
_CODE_LOCATION_ALL = "all"
_RUN_GRPC_INFO_TAG = ".dagster/grpc_info"
_RUN_REPOSITORY_TAG = ".dagster/repository"
_EXPECTED_REPOSITORY_SUFFIX = {
    "ingestion": "@pipelines",
    "model": "@models",
}
_FAIL_STATUSES = (DagsterRunStatus.STARTING, DagsterRunStatus.STARTED)
_CANCEL_STATUSES = (DagsterRunStatus.CANCELING,)


def _run_label(run: DagsterRun) -> str:
    tags = run.tags or {}
    deployment = tags.get("avian/deployment", "unknown-deployment")
    logical_date = tags.get("avian/logical_date")
    logical_suffix = f" logical_date={logical_date}" if logical_date else ""
    return (
        f"run_id={run.run_id} job={run.job_name} status={run.status.value} "
        f"deployment={deployment}{logical_suffix}"
    )


def _requested_code_location() -> str:
    configured = os.getenv(_CODE_LOCATION_ENV, _CODE_LOCATION_ALL).strip().lower()
    return configured or _CODE_LOCATION_ALL


def _grpc_host(tags: Mapping[str, str]) -> str | None:
    payload = tags.get(_RUN_GRPC_INFO_TAG)
    if not payload:
        return None

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None

    host = parsed.get("host")
    if isinstance(host, str):
        normalized = host.strip().lower()
        return normalized or None
    return None


def should_reconcile_run(run: DagsterRun) -> bool:
    location = _requested_code_location()
    if location == _CODE_LOCATION_ALL:
        return True

    tags = run.tags or {}
    grpc_host = _grpc_host(tags)
    if grpc_host is not None:
        return grpc_host == location

    repository = tags.get(_RUN_REPOSITORY_TAG, "")
    expected_suffix = _EXPECTED_REPOSITORY_SUFFIX.get(location)
    if expected_suffix:
        return repository.endswith(expected_suffix)

    return False


def _run_records(
    instance: DagsterInstance,
    statuses: Sequence[DagsterRunStatus],
) -> Sequence[RunRecord]:
    return instance.get_run_records(filters=RunsFilter(statuses=list(statuses)))


def _fail_orphaned_runs(instance: DagsterInstance) -> int:
    message = (
        "Marked failed during dagster-daemon startup because the previous local "
        "run worker disappeared before the control plane restarted. "
        "This Docker Compose deployment uses DefaultRunLauncher, so active "
        "runs cannot survive a host or container restart."
    )
    failed = 0

    for record in _run_records(instance, _FAIL_STATUSES):
        run = record.dagster_run
        if not should_reconcile_run(run):
            continue
        instance.report_run_failed(run, message=message)
        failed += 1
        print(f"{LOG_PREFIX} Marked failed: {_run_label(run)}")

    return failed


def _cancel_orphaned_runs(instance: DagsterInstance) -> int:
    message = (
        "Marked canceled during dagster-daemon startup because the previous "
        "local run worker disappeared before cancellation completed."
    )
    canceled = 0

    for record in _run_records(instance, _CANCEL_STATUSES):
        run = record.dagster_run
        if not should_reconcile_run(run):
            continue
        instance.report_run_canceled(run, message=message)
        canceled += 1
        print(f"{LOG_PREFIX} Marked canceled: {_run_label(run)}")

    return canceled


def main() -> int:
    instance = DagsterInstance.get()
    failed = _fail_orphaned_runs(instance)
    canceled = _cancel_orphaned_runs(instance)

    print(
        f"{LOG_PREFIX} Reconciled orphaned runs: {failed} failed, {canceled} canceled."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
