"""Copernicus CDS ERA5-derived drought ingestion.

Retrieves monthly gridded drought indices from the CDS dataset
``derived-drought-historical-monthly`` and stores them either locally under
``data/raw/cds_drought/`` or directly into ``cds_drought_tbl``.

The CDS returns a ZIP archive containing one NetCDF file per variable and
accumulation window. This collector flattens those files into a wide monthly row
per grid cell / dataset type / product type / ensemble member.
"""

# mypy: disable-error-code=import-untyped

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import tempfile
import time
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import cdsapi
import numpy as np
import xarray as xr
from requests import HTTPError

from avian_biosurveillance.config.runtime import load_runtime_env
from avian_biosurveillance.db.loaders.cds_drought import upsert_cds_drought_observations
from avian_biosurveillance.db.models.cds_drought import (
    DEFAULT_CDS_RESOLUTION_DEGREES,
    CDSDroughtObservation,
    build_cds_bbox,
    build_cds_grid_cell_id,
)
from avian_biosurveillance.utils.runtime_paths import preferred_temp_dir


load_runtime_env()

logger = logging.getLogger(__name__)

_CDS_ROOT = Path(__file__).resolve().parents[3] / "data" / "raw" / "cds_drought"
_CDS_DATASET = "derived-drought-historical-monthly"
_CDS_API_URL = "https://cds.climate.copernicus.eu/api"
_NL_AREA = (53.7, 3.2, 50.7, 7.3)
_ACCUMULATION_PERIODS = (1, 3, 6, 12, 24, 36, 48)
_MAIN_VARIABLES = (
    "standardised_precipitation_index",
    "standardised_precipitation_evapotranspiration_index",
)
_QUALITY_VARIABLES = (
    "probability_of_zero_precipitation_spi",
    "test_for_normality_spi",
    "test_for_normality_spei",
)
_FILENAME_PERIOD_RE = re.compile(r"^(SPI|SPEI)(?P<period>\d+)", re.IGNORECASE)


class CDSRequestUnavailableError(RuntimeError):
    """Raised when a CDS request combination is syntactically valid but absent."""


@dataclass(frozen=True)
class _RequestSpec:
    """One concrete CDS retrieval request."""

    observation_month: date
    dataset_type: str
    product_type: str
    version: str
    area: tuple[float, float, float, float]
    variables: tuple[str, ...]

    @property
    def slug(self) -> str:
        """Stable filename fragment for the request."""
        month_token = self.observation_month.strftime("%Y-%m")
        return f"{month_token}_{self.dataset_type}_{self.product_type}_v{self.version}"

    def to_request(self) -> dict[str, Any]:
        """Return the request payload expected by CDS."""
        return {
            "version": self.version,
            "variable": list(self.variables),
            "accumulation_period": [str(period) for period in _ACCUMULATION_PERIODS],
            "product_type": [self.product_type],
            "dataset_type": self.dataset_type,
            "year": [f"{self.observation_month.year:04d}"],
            "month": [f"{self.observation_month.month:02d}"],
            "area": list(self.area),
        }


@dataclass
class CollectStats:
    """Summary for one collected month."""

    month: str
    requests_attempted: int = 0
    requests_downloaded: int = 0
    requests_unavailable: int = 0
    requests_failed: int = 0
    rows_parsed: int = 0
    rows_upserted: int = 0
    elapsed_s: float = 0.0

    def __str__(self) -> str:
        return (
            f"{self.month}: downloaded={self.requests_downloaded}, "
            f"unavailable={self.requests_unavailable}, failed={self.requests_failed}, "
            f"rows={self.rows_parsed}, upserted={self.rows_upserted}"
        )


def _month_start(value: date) -> date:
    """Return the first day of *value*'s month."""
    return date(value.year, value.month, 1)


def _shift_months(value: date, offset: int) -> date:
    """Shift a month-start date by *offset* months."""
    month_index = value.year * 12 + (value.month - 1) + offset
    return date(month_index // 12, (month_index % 12) + 1, 1)


def parse_month(value: str) -> date:
    """Parse ``YYYY-MM`` into a first-of-month ``date``."""
    try:
        return datetime.strptime(value, "%Y-%m").date().replace(day=1)
    except ValueError as exc:
        msg = f"Invalid month '{value}'. Use YYYY-MM."
        raise ValueError(msg) from exc


def _normalise_target(target: str) -> str:
    """Validate the storage target."""
    if target not in {"local", "database"}:
        msg = f"target must be 'local' or 'database', got '{target}'"
        raise ValueError(msg)
    return target


class CDSDroughtCollector:
    """Retrieve and flatten monthly CDS drought files."""

    def __init__(
        self,
        output_dir: Path | None = None,
        *,
        api_url: str | None = None,
        api_key: str | None = None,
        area: Sequence[float] | None = None,
        version: str = "1_0",
        include_ensemble_members: bool = True,
        dataset_types: Sequence[str] | None = None,
    ) -> None:
        self.output_dir = output_dir or _CDS_ROOT
        self.api_url = api_url or os.environ.get("CDS_API_URL", _CDS_API_URL)
        self.api_key = api_key or os.environ.get("CDS_API_KEY")
        self.area = tuple(float(part) for part in (area or _NL_AREA))
        self.version = version
        self.include_ensemble_members = include_ensemble_members
        self.dataset_types = tuple(
            dataset_types or ("consolidated_dataset", "intermediate_dataset")
        )
        self._client: cdsapi.Client | None = None

    @property
    def client(self) -> cdsapi.Client:
        """Lazily create the CDS client once the key is needed."""
        if self._client is None:
            if not self.api_key:
                msg = (
                    "CDS_API_KEY is required for CDS drought ingestion. "
                    "Add it to .env or pass api_key explicitly."
                )
                raise ValueError(msg)
            self._client = cdsapi.Client(
                url=self.api_url,
                key=self.api_key,
                quiet=True,
                progress=False,
            )
        return self._client

    @staticmethod
    def _variables_for_request(
        dataset_type: str,
        product_type: str,
    ) -> tuple[str, ...]:
        """Return the valid variable set for a given CDS request variant."""
        if product_type == "ensemble_members":
            return _MAIN_VARIABLES
        if dataset_type == "consolidated_dataset":
            return _MAIN_VARIABLES + _QUALITY_VARIABLES
        return _MAIN_VARIABLES

    def build_request_specs(self, observation_month: date) -> list[_RequestSpec]:
        """Build all request variants for a month."""
        month = _month_start(observation_month)
        product_types = ["reanalysis"]
        if self.include_ensemble_members:
            product_types.append("ensemble_members")

        specs: list[_RequestSpec] = []
        for dataset_type in self.dataset_types:
            for product_type in product_types:
                specs.append(
                    _RequestSpec(
                        observation_month=month,
                        dataset_type=dataset_type,
                        product_type=product_type,
                        version=self.version,
                        area=self.area,
                        variables=self._variables_for_request(
                            dataset_type,
                            product_type,
                        ),
                    )
                )
        return specs

    def _archive_path_for(
        self,
        month_dir: Path,
        spec: _RequestSpec,
    ) -> Path:
        """Return the on-disk archive path for a request."""
        return month_dir / spec.dataset_type / spec.product_type / f"{spec.slug}.zip"

    def _download_archive(self, spec: _RequestSpec, target_path: Path) -> Path:
        """Download one CDS archive to *target_path*."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.client.retrieve(_CDS_DATASET, spec.to_request(), str(target_path))
        except HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 400:
                raise CDSRequestUnavailableError(str(exc)) from exc
            raise
        return target_path

    @staticmethod
    def _iter_netcdf_files(archive_path: Path, scratch_dir: Path) -> list[Path]:
        """Return the extracted NetCDF files for a CDS archive."""
        if zipfile.is_zipfile(archive_path):
            with zipfile.ZipFile(archive_path) as archive:
                members = [
                    member
                    for member in archive.namelist()
                    if member.lower().endswith((".nc", ".nc4"))
                ]
                if not members:
                    msg = f"No NetCDF members found in {archive_path.name}"
                    raise ValueError(msg)
                extracted: list[Path] = []
                for member in members:
                    extracted.append(Path(archive.extract(member, scratch_dir)))
                return extracted

        if archive_path.suffix.lower() in {".nc", ".nc4"}:
            return [archive_path]

        msg = f"Unsupported CDS archive format: {archive_path}"
        raise ValueError(msg)

    @staticmethod
    def _metric_column_from_filename(filename: str) -> str:
        """Map a CDS NetCDF filename to a wide-table metric column name."""
        match = _FILENAME_PERIOD_RE.match(filename)
        if match is None:
            msg = f"Could not infer accumulation period from {filename}"
            raise ValueError(msg)
        period = int(match.group("period"))
        lower_name = filename.lower()
        if "_spipzero_" in lower_name:
            return f"spi_p0_{period}m"
        if "_spisignificance_" in lower_name:
            return f"spi_normality_{period}m"
        if "_speisignificance_" in lower_name:
            return f"spei_normality_{period}m"
        if lower_name.startswith("spei"):
            return f"spei_{period}m"
        if lower_name.startswith("spi"):
            return f"spi_{period}m"
        msg = f"Unsupported CDS drought filename: {filename}"
        raise ValueError(msg)

    @staticmethod
    def _coerce_metric_value(column: str, raw_value: Any) -> float | bool | None:
        """Normalise raw NetCDF values into the table's Python types."""
        if raw_value is None:
            return None
        value = float(raw_value)
        if not np.isfinite(value):
            return None
        if "normality_" in column:
            return bool(round(value))
        return value

    @staticmethod
    def _base_row(
        *,
        lat: float,
        lon: float,
        observation_month: date,
        dataset_type: str,
        product_type: str,
        ensemble_member: int,
        data_version: str,
    ) -> dict[str, Any]:
        """Build the shared row payload for a single grid point."""
        bbox_min_lat, bbox_min_lon, bbox_max_lat, bbox_max_lon = build_cds_bbox(
            lat,
            lon,
            resolution_degrees=DEFAULT_CDS_RESOLUTION_DEGREES,
        )
        return {
            "grid_cell_id": build_cds_grid_cell_id(lat, lon),
            "observation_month": observation_month,
            "dataset_type": dataset_type,
            "product_type": product_type,
            "ensemble_member": ensemble_member,
            "data_version": data_version,
            "lat": lat,
            "lon": lon,
            "bbox_min_lat": bbox_min_lat,
            "bbox_min_lon": bbox_min_lon,
            "bbox_max_lat": bbox_max_lat,
            "bbox_max_lon": bbox_max_lon,
        }

    def parse_archive(
        self,
        spec: _RequestSpec,
        archive_path: Path,
        *,
        scratch_dir: Path,
    ) -> list[CDSDroughtObservation]:
        """Flatten one CDS archive into wide drought rows."""
        row_buffer: dict[tuple[str, date, str, str, int], dict[str, Any]] = {}

        for netcdf_path in self._iter_netcdf_files(archive_path, scratch_dir):
            metric_column = self._metric_column_from_filename(netcdf_path.name)
            dataset = xr.open_dataset(netcdf_path)
            try:
                data_var_name = next(iter(dataset.data_vars))
                data_array = dataset[data_var_name]
                if "time" not in data_array.dims:
                    data_array = data_array.expand_dims(
                        time=[np.datetime64(spec.observation_month)]
                    )
                # Newer CDS responses for `ensemble_members` carry a separate
                # `realization` (sometimes `number`/`member`) dimension. Detect
                # it so the transpose covers every dim — otherwise xarray
                # raises ``ValueError: must be a permuted list``.
                ensemble_dim = next(
                    (
                        d
                        for d in ("realization", "number", "member")
                        if d in data_array.dims
                    ),
                    None,
                )
                if ensemble_dim is not None:
                    data_array = data_array.transpose(
                        "time", ensemble_dim, "lat", "lon"
                    )
                else:
                    data_array = data_array.transpose("time", "lat", "lon")

                lat_values = [float(value) for value in data_array.coords["lat"].values]
                lon_values = [float(value) for value in data_array.coords["lon"].values]
                values = np.asarray(data_array.values)

                if ensemble_dim is not None:
                    iter_indices = (
                        (t, e)
                        for t in range(values.shape[0])
                        for e in range(values.shape[1])
                    )
                else:
                    iter_indices = (
                        (t, t if spec.product_type == "ensemble_members" else 0)
                        for t in range(values.shape[0])
                    )

                for time_idx, ensemble_member in iter_indices:
                    cell_values = (
                        values[time_idx, ensemble_member]
                        if ensemble_dim is not None
                        else values[time_idx]
                    )

                    for lat_idx, lat in enumerate(lat_values):
                        for lon_idx, lon in enumerate(lon_values):
                            metric_value = self._coerce_metric_value(
                                metric_column,
                                cell_values[lat_idx, lon_idx],
                            )
                            if metric_value is None:
                                continue

                            grid_cell_id = build_cds_grid_cell_id(lat, lon)
                            key = (
                                grid_cell_id,
                                spec.observation_month,
                                spec.dataset_type,
                                spec.product_type,
                                ensemble_member,
                            )
                            row = row_buffer.setdefault(
                                key,
                                self._base_row(
                                    lat=lat,
                                    lon=lon,
                                    observation_month=spec.observation_month,
                                    dataset_type=spec.dataset_type,
                                    product_type=spec.product_type,
                                    ensemble_member=ensemble_member,
                                    data_version=spec.version,
                                ),
                            )
                            row[metric_column] = metric_value
            finally:
                dataset.close()

        return [CDSDroughtObservation.from_mapping(row) for row in row_buffer.values()]

    @staticmethod
    def _register_cds_grid_cells(
        records: Sequence[CDSDroughtObservation],
    ) -> int:
        """Upsert canonical CDS cells into ``grid_cells_tbl``.

        Required before inserting into ``cds_drought_tbl`` because the table
        carries a trigger that rejects unknown cells (FKs are not permitted
        on compressed hypertables).
        """
        if not records:
            return 0

        import math

        from avian_biosurveillance.db.loaders.grid import upsert_grid_cells
        from avian_biosurveillance.db.models.grid import GridCell

        unique: dict[tuple[str, str], GridCell] = {}
        for row in records:
            key = (row.grid_cell_id, row.grid_system)
            if key in unique:
                continue
            size_km = 0.25 * 111.0 * math.cos(math.radians(row.lat))
            polygon = {
                "type": "Polygon",
                "coordinates": [
                    [
                        [row.bbox_min_lon, row.bbox_min_lat],
                        [row.bbox_max_lon, row.bbox_min_lat],
                        [row.bbox_max_lon, row.bbox_max_lat],
                        [row.bbox_min_lon, row.bbox_max_lat],
                        [row.bbox_min_lon, row.bbox_min_lat],
                    ]
                ],
            }
            unique[key] = GridCell.from_spec(
                grid_cell_id=row.grid_cell_id,
                grid_system=row.grid_system,
                source="copernicus_cds_era5_drought",
                lat=row.lat,
                lon=row.lon,
                bbox_min_lat=row.bbox_min_lat,
                bbox_min_lon=row.bbox_min_lon,
                bbox_max_lat=row.bbox_max_lat,
                bbox_max_lon=row.bbox_max_lon,
                grid_size_km=size_km,
                cell_geojson=polygon,
            )
        try:
            return upsert_grid_cells(list(unique.values()))
        except Exception:
            logger.exception("DB upsert for CDS canonical grid cells failed")
            return 0

    @staticmethod
    def _write_local_outputs(
        month_dir: Path,
        *,
        stats: CollectStats,
        records: Sequence[CDSDroughtObservation],
        downloaded_archives: Sequence[dict[str, str]],
        include_ensemble_members: bool,
    ) -> None:
        """Persist parsed records and run metadata for local inspection."""
        month_dir.mkdir(parents=True, exist_ok=True)
        records_path = month_dir / "records.json"
        with records_path.open("w", encoding="utf-8") as fh:
            json.dump(
                [record.to_dict() for record in records],
                fh,
                indent=2,
                default=str,
            )

        metadata = {
            "source": "Copernicus Climate Data Store",
            "dataset": _CDS_DATASET,
            "month": stats.month,
            "results": {
                "requests_attempted": stats.requests_attempted,
                "requests_downloaded": stats.requests_downloaded,
                "requests_unavailable": stats.requests_unavailable,
                "requests_failed": stats.requests_failed,
                "rows_parsed": stats.rows_parsed,
                "rows_upserted": stats.rows_upserted,
                "archives": list(downloaded_archives),
            },
            "run": {
                "completed": True,
                "elapsed_s": stats.elapsed_s,
                "ingested_at": datetime.now(UTC).isoformat(),
                "include_ensemble_members": include_ensemble_members,
            },
        }
        with (month_dir / "metadata.json").open("w", encoding="utf-8") as fh:
            json.dump(metadata, fh, indent=2, default=str)

    def collect_month(
        self,
        observation_month: date,
        *,
        target: str = "database",
        force: bool = False,
    ) -> CollectStats:
        """Collect one month of drought files and optionally upsert them."""
        target = _normalise_target(target)
        month = _month_start(observation_month)
        month_token = month.strftime("%Y-%m")
        month_dir = self.output_dir / month_token
        metadata_path = month_dir / "metadata.json"

        if target == "local" and not force and metadata_path.exists():
            try:
                previous = json.loads(metadata_path.read_text(encoding="utf-8"))
                if previous.get("run", {}).get("completed"):
                    logger.info(
                        "Skipping CDS drought month %s — already complete.",
                        month_token,
                    )
                    return CollectStats(month=month_token)
            except (json.JSONDecodeError, KeyError):
                pass

        started = time.perf_counter()
        specs = self.build_request_specs(month)
        stats = CollectStats(month=month_token, requests_attempted=len(specs))
        records: list[CDSDroughtObservation] = []
        downloaded_archives: list[dict[str, str]] = []
        scratch_dir = Path(
            tempfile.mkdtemp(prefix="cds-drought-", dir=preferred_temp_dir())
        )

        try:
            for spec in specs:
                archive_target = self._archive_path_for(month_dir, spec)
                if target != "local":
                    archive_target = scratch_dir / archive_target.name

                try:
                    logger.info(
                        "CDS drought: downloading %s %s for %s.",
                        spec.dataset_type,
                        spec.product_type,
                        month_token,
                    )
                    archive_path = self._download_archive(spec, archive_target)
                    parsed_records = self.parse_archive(
                        spec,
                        archive_path,
                        scratch_dir=scratch_dir,
                    )
                except CDSRequestUnavailableError:
                    stats.requests_unavailable += 1
                    logger.warning(
                        "CDS drought request unavailable for %s (%s/%s).",
                        month_token,
                        spec.dataset_type,
                        spec.product_type,
                    )
                    continue
                except Exception:
                    stats.requests_failed += 1
                    logger.exception(
                        "CDS drought request failed for %s (%s/%s).",
                        month_token,
                        spec.dataset_type,
                        spec.product_type,
                    )
                    continue

                stats.requests_downloaded += 1
                records.extend(parsed_records)
                downloaded_archives.append(
                    {
                        "dataset_type": spec.dataset_type,
                        "product_type": spec.product_type,
                        "path": str(archive_path),
                    }
                )

            stats.rows_parsed = len(records)
            if target == "database" and records:
                self._register_cds_grid_cells(records)
                stats.rows_upserted = upsert_cds_drought_observations(records)
            stats.elapsed_s = round(time.perf_counter() - started, 2)
            if target == "local":
                self._write_local_outputs(
                    month_dir,
                    stats=stats,
                    records=records,
                    downloaded_archives=downloaded_archives,
                    include_ensemble_members=self.include_ensemble_members,
                )
            return stats
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)

    def collect_month_range(
        self,
        start_month: date,
        end_month: date,
        *,
        target: str = "database",
        force: bool = False,
    ) -> list[CollectStats]:
        """Collect all months in the inclusive ``[start_month, end_month]`` range."""
        start = _month_start(start_month)
        end = _month_start(end_month)
        if end < start:
            msg = f"end_month ({end}) must be >= start_month ({start})"
            raise ValueError(msg)

        stats: list[CollectStats] = []
        current = start
        while current <= end:
            stats.append(self.collect_month(current, target=target, force=force))
            current = _shift_months(current, 1)
        logger.info(
            "CDS drought complete: %d month(s), %d parsed rows, %d upserted rows.",
            len(stats),
            sum(item.rows_parsed for item in stats),
            sum(item.rows_upserted for item in stats),
        )
        return stats

    def collect_recent_months(
        self,
        lookback_months: int,
        *,
        reference_date: date | None = None,
        target: str = "database",
        force: bool = False,
    ) -> list[CollectStats]:
        """Collect the last *lookback_months* complete calendar months."""
        if lookback_months < 1:
            msg = "lookback_months must be >= 1"
            raise ValueError(msg)

        ref = reference_date or date.today()
        last_complete_month = _shift_months(_month_start(ref), -1)
        first_month = _shift_months(last_complete_month, -(lookback_months - 1))
        return self.collect_month_range(
            first_month,
            last_complete_month,
            target=target,
            force=force,
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect Copernicus CDS ERA5-derived monthly drought data.",
    )
    parser.add_argument("--start-month", required=True, help="First month (YYYY-MM).")
    parser.add_argument("--end-month", help="Last month (YYYY-MM), inclusive.")
    parser.add_argument(
        "--target",
        choices=("local", "database"),
        default="local",
        help="Persist raw outputs locally or upsert into the database.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run months that already have a completed local metadata file.",
    )
    parser.add_argument(
        "--no-ensemble",
        action="store_true",
        help="Skip ensemble-member requests and ingest only deterministic reanalysis.",
    )
    return parser.parse_args()


def main() -> None:
    """CLI entry-point for ad-hoc CDS drought collection."""
    logging.basicConfig(level=logging.INFO)
    args = _parse_args()
    start_month = parse_month(args.start_month)
    end_month = parse_month(args.end_month) if args.end_month else start_month

    collector = CDSDroughtCollector(
        include_ensemble_members=not args.no_ensemble,
    )
    collector.collect_month_range(
        start_month,
        end_month,
        target=args.target,
        force=args.force,
    )


if __name__ == "__main__":
    main()
