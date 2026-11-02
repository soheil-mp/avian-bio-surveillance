from __future__ import annotations

import json
import shutil
import zipfile
from datetime import date
from pathlib import Path

import numpy as np
import xarray as xr

from avian_biosurveillance.ingestion.cds_drought import (
    CDSDroughtCollector,
    _RequestSpec,
)


def _write_dataset(
    path: Path,
    *,
    var_name: str,
    values: np.ndarray,
    latitudes: list[float],
    longitudes: list[float],
    time_values: list[np.datetime64],
) -> None:
    ds = xr.Dataset(
        {
            var_name: (("time", "lat", "lon"), values),
        },
        coords={
            "time": np.asarray(time_values, dtype="datetime64[ns]"),
            "lat": np.asarray(latitudes, dtype=float),
            "lon": np.asarray(longitudes, dtype=float),
        },
    )
    ds.to_netcdf(path)
    ds.close()


def _build_archive(
    archive_path: Path, members: dict[str, tuple[str, np.ndarray]]
) -> Path:
    latitudes = [52.25]
    longitudes = [4.75]
    time_values = [np.datetime64("2025-01-01")]
    with zipfile.ZipFile(archive_path, "w") as archive:
        for filename, (var_name, values) in members.items():
            source_path = archive_path.parent / filename
            current_time_values = time_values
            if values.shape[0] > 1:
                current_time_values = [np.datetime64("2025-01-01")] * values.shape[0]
            _write_dataset(
                source_path,
                var_name=var_name,
                values=values,
                latitudes=latitudes,
                longitudes=longitudes,
                time_values=current_time_values,
            )
            archive.write(source_path, arcname=filename)
            source_path.unlink()
    return archive_path


def test_build_request_specs_uses_quality_variables_only_when_supported() -> None:
    collector = CDSDroughtCollector(include_ensemble_members=True)

    specs = collector.build_request_specs(date(2025, 1, 1))
    spec_map = {
        (spec.dataset_type, spec.product_type): spec.variables for spec in specs
    }

    assert spec_map[("consolidated_dataset", "reanalysis")] == (
        "standardised_precipitation_index",
        "standardised_precipitation_evapotranspiration_index",
        "probability_of_zero_precipitation_spi",
        "test_for_normality_spi",
        "test_for_normality_spei",
    )
    assert spec_map[("intermediate_dataset", "reanalysis")] == (
        "standardised_precipitation_index",
        "standardised_precipitation_evapotranspiration_index",
    )
    assert spec_map[("consolidated_dataset", "ensemble_members")] == (
        "standardised_precipitation_index",
        "standardised_precipitation_evapotranspiration_index",
    )


def test_parse_archive_merges_main_and_quality_metrics(tmp_path: Path) -> None:
    archive_path = _build_archive(
        tmp_path / "consolidated.zip",
        {
            "SPI1_gamma_global_era5_moda_ref1991to2020_202501.nc": (
                "SPI1",
                np.array([[[-1.25]]], dtype=float),
            ),
            "SPEI1_genlogistic_global_era5_moda_ref1991to2020_202501.nc": (
                "SPEI1",
                np.array([[[-1.5]]], dtype=float),
            ),
            "SPI1_spipzero_gamma_global_era5_moda_ref1991to2020_202501.nc": (
                "pzero",
                np.array([[[0.12]]], dtype=float),
            ),
            "SPI1_spisignificance_gamma_global_era5_moda_ref1991to2020_202501.nc": (
                "significance",
                np.array([[[1.0]]], dtype=float),
            ),
            "SPEI1_speisignificance_genlogistic_global_era5_moda_ref1991to2020_202501.nc": (
                "significance",
                np.array([[[0.0]]], dtype=float),
            ),
        },
    )
    collector = CDSDroughtCollector(output_dir=tmp_path)
    spec = _RequestSpec(
        observation_month=date(2025, 1, 1),
        dataset_type="consolidated_dataset",
        product_type="reanalysis",
        version="1_0",
        area=(53.7, 3.2, 50.7, 7.3),
        variables=(
            "standardised_precipitation_index",
            "standardised_precipitation_evapotranspiration_index",
            "probability_of_zero_precipitation_spi",
            "test_for_normality_spi",
            "test_for_normality_spei",
        ),
    )

    records = collector.parse_archive(
        spec, archive_path, scratch_dir=tmp_path / "scratch"
    )

    assert len(records) == 1
    record = records[0]
    assert record.spi_1m == -1.25
    assert record.spei_1m == -1.5
    assert record.spi_p0_1m == 0.12
    assert record.spi_normality_1m is True
    assert record.spei_normality_1m is False
    assert record.dataset_type == "consolidated_dataset"
    assert record.product_type == "reanalysis"


def test_parse_archive_expands_ensemble_members_from_time_axis(tmp_path: Path) -> None:
    archive_path = _build_archive(
        tmp_path / "ensemble.zip",
        {
            "SPI1_gamma_global_era5t_edmo_ref1991to2020_202501.nc": (
                "SPI1",
                np.array([[[-0.2]], [[-0.4]]], dtype=float),
            ),
        },
    )
    collector = CDSDroughtCollector(output_dir=tmp_path)
    spec = _RequestSpec(
        observation_month=date(2025, 1, 1),
        dataset_type="intermediate_dataset",
        product_type="ensemble_members",
        version="1_0",
        area=(53.7, 3.2, 50.7, 7.3),
        variables=("standardised_precipitation_index",),
    )

    records = collector.parse_archive(
        spec, archive_path, scratch_dir=tmp_path / "scratch"
    )

    assert len(records) == 2
    members = sorted((record.ensemble_member, record.spi_1m) for record in records)
    assert members == [(0, -0.2), (1, -0.4)]


def test_collect_month_writes_local_outputs(tmp_path: Path) -> None:
    archive_path = _build_archive(
        tmp_path / "local.zip",
        {
            "SPI1_gamma_global_era5_moda_ref1991to2020_202501.nc": (
                "SPI1",
                np.array([[[-1.0]]], dtype=float),
            ),
            "SPEI1_genlogistic_global_era5_moda_ref1991to2020_202501.nc": (
                "SPEI1",
                np.array([[[-1.1]]], dtype=float),
            ),
        },
    )
    collector = CDSDroughtCollector(
        output_dir=tmp_path / "out",
        include_ensemble_members=False,
        dataset_types=("intermediate_dataset",),
    )

    original_download = collector._download_archive

    def _fake_download(spec: _RequestSpec, target_path: Path) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archive_path, target_path)
        return target_path

    collector._download_archive = _fake_download  # type: ignore[method-assign]
    try:
        stats = collector.collect_month(date(2025, 1, 1), target="local")
    finally:
        collector._download_archive = original_download  # type: ignore[method-assign]

    month_dir = collector.output_dir / "2025-01"
    metadata = json.loads((month_dir / "metadata.json").read_text(encoding="utf-8"))
    records = json.loads((month_dir / "records.json").read_text(encoding="utf-8"))

    assert stats.requests_downloaded == 1
    assert stats.rows_parsed == 1
    assert metadata["run"]["completed"] is True
    assert metadata["results"]["rows_parsed"] == 1
    assert records[0]["spi_1m"] == -1.0
    assert records[0]["spei_1m"] == -1.1
