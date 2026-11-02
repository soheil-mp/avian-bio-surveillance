"""Centralised loader for deployment schedules.

Every orchestration consumer that needs cron schedules or deployment
parameters MUST import from this module instead of hardcoding values.

The canonical data lives in ``config/schedules.json``.  If the file
is missing or malformed the loader raises immediately — no silent
fallback is provided so configuration errors surface right away.

Usage::

    from avian_biosurveillance.config.schedules import schedule_registry

    for dep in schedule_registry:
        print(dep.name, dep.cron)

    schedule_registry.get("historical-catchup").parameters
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeploymentSchedule:
    """Configuration for a single Dagster deployment."""

    name: str
    flow: str
    cron: str
    tags: list[str]
    description: str
    concurrency_limit: int = 1
    enabled: bool = True
    run_on_startup: bool = False
    execution_modes: list[str] = field(default_factory=lambda: ["cron"])
    depends_on: list[str] = field(default_factory=list)
    parameters: dict[str, object] = field(default_factory=dict)

    def has_execution_mode(self, mode: str) -> bool:
        """Return ``True`` when *mode* is declared in ``execution_modes``."""
        return mode in self.execution_modes


@dataclass
class ScheduleRegistry:
    """Typed, read-only facade over the deployment-schedule list.

    Constructed once at module load and exposed as
    :pydata:`schedule_registry`.
    """

    _deployments: list[DeploymentSchedule] = field(
        default_factory=list,
        repr=False,
    )

    # -- Sequence-like accessors ------------------------------------------

    def __len__(self) -> int:
        return len(self._deployments)

    def __iter__(self) -> Iterator[DeploymentSchedule]:
        return iter(self._deployments)

    def __getitem__(self, idx: int) -> DeploymentSchedule:
        return self._deployments[idx]

    # -- Look-ups ---------------------------------------------------------

    def get(self, name: str) -> DeploymentSchedule | None:
        """Return the deployment with *name*, or ``None``."""
        for dep in self._deployments:
            if dep.name == name:
                return dep
        return None

    @property
    def enabled(self) -> list[DeploymentSchedule]:
        """Return only deployments where ``enabled`` is *True*."""
        return [d for d in self._deployments if d.enabled]

    @property
    def startup_deployments(self) -> list[DeploymentSchedule]:
        """Return deployments that should run immediately on startup."""
        return [d for d in self._deployments if d.run_on_startup]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

_CONFIG_RELATIVE = Path("config/schedules.json")


def _string_list(value: object, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(
            f"Expected list for {field_name!r}, got {type(value).__name__}."
        )
    return [str(item) for item in value]


def _parameter_map(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(
            f"Expected object for 'parameters', got {type(value).__name__}."
        )
    return {str(key): item for key, item in value.items()}


def _parse_deployment(raw: dict[str, object]) -> DeploymentSchedule:
    """Parse a single deployment entry from the JSON config."""
    concurrency_limit = int(str(raw.get("concurrency_limit", 1)))
    execution_modes = _string_list(
        raw.get("execution_modes", ["cron"]),
        field_name="execution_modes",
    )
    if bool(raw.get("run_on_startup", False)) and "startup" not in execution_modes:
        execution_modes.append("startup")
    if concurrency_limit != 1:
        msg = (
            "Unsupported concurrency_limit="
            f"{concurrency_limit!r} for deployment {raw.get('name', '<unknown>')!r}. "
            "The current Dagster instance enforces one active run per "
            "deployment tag, so schedules.json must keep concurrency_limit=1."
        )
        raise ValueError(msg)

    return DeploymentSchedule(
        name=str(raw["name"]),
        flow=str(raw["flow"]),
        cron=str(raw["cron"]),
        tags=_string_list(raw.get("tags", []), field_name="tags"),
        description=str(raw.get("description", "")),
        concurrency_limit=concurrency_limit,
        enabled=bool(raw.get("enabled", True)),
        run_on_startup=bool(raw.get("run_on_startup", False)),
        execution_modes=execution_modes,
        depends_on=_string_list(raw.get("depends_on", []), field_name="depends_on"),
        parameters=_parameter_map(raw.get("parameters", {})),
    )


def _validate_dependencies(registry: ScheduleRegistry) -> None:
    known_deployments = {dep.name for dep in registry}

    for dep in registry:
        unknown = sorted(
            {name for name in dep.depends_on if name not in known_deployments}
        )
        if unknown:
            msg = (
                f"Deployment {dep.name!r} depends_on unknown deployment(s): "
                f"{', '.join(unknown)}."
            )
            raise ValueError(msg)


def load_schedule_registry(
    config_path: Path | None = None,
) -> ScheduleRegistry:
    """Load the schedule registry from a JSON config.

    Resolution order:
        1. Explicit *config_path* argument.
        2. ``config/schedules.json`` relative to cwd.

    Raises
    ------
    FileNotFoundError
        If no valid config file is found in any of the searched paths.
    ValueError
        If the file exists but cannot be parsed or contains no deployments.
    """
    paths_to_try: list[Path] = []
    if config_path is not None:
        paths_to_try.append(config_path)
    paths_to_try.append(Path.cwd() / _CONFIG_RELATIVE)

    for p in paths_to_try:
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                msg = f"Invalid JSON in schedule config {p}: {exc}"
                raise ValueError(msg) from exc

            items = data.get("deployments", [])
            if not items:
                msg = f"Schedule config {p} has no 'deployments' entries."
                raise ValueError(msg)

            reg = ScheduleRegistry([_parse_deployment(d) for d in items])
            _validate_dependencies(reg)
            logger.info(
                "Loaded %d deployment schedule(s) from %s",
                len(reg),
                p,
            )
            return reg

    searched = ", ".join(str(p) for p in paths_to_try)
    msg = (
        f"Schedule config not found. Searched: {searched}. "
        f"Create config/schedules.json or pass an explicit path."
    )
    raise FileNotFoundError(msg)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

schedule_registry: ScheduleRegistry = load_schedule_registry()
"""Module-level singleton -- ready to use at import time."""
