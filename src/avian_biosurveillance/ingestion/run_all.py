"""Run all ingestion modules in a single command.

Default behaviour fetches data for all configured target species
(``config/target_species.json``) in the Netherlands.  Individual modules
can be disabled with ``--no-<module>`` flags.

Examples::

    # Last 24 h, all modules, Netherlands, target species
    poetry run python -m avian_biosurveillance.ingestion.run_all

    # Specific date range with GPU background classification
    poetry run python -m avian_biosurveillance.ingestion.run_all \\
        --start-date 2025-01-01 --end-date 2025-01-31 \\
        --classify-background --bg-device cuda

    # Only BirdWeather + KNMI
    poetry run python -m avian_biosurveillance.ingestion.run_all \\
        --no-ebird --no-movebank --no-sovon
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from avian_biosurveillance.config.species import registry as _sp_registry
from avian_biosurveillance.utils.log_utils import fmt


logger = logging.getLogger(__name__)

# eBird region codes for countries used in the project.
_COUNTRY_TO_EBIRD_REGION: dict[str, str] = {
    "netherlands": "NL",
    "germany": "DE",
    "belgium": "BE",
    "france": "FR",
    "united kingdom": "GB",
}


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _load_target_species() -> list[dict[str, Any]]:
    """Return target-species records from the shared species registry."""
    return [
        {
            "english": species.english,
            "dutch": species.dutch,
            "scientific": species.scientific,
            "sovon_id": species.sovon_id,
            "birdweather_id": species.birdweather_id,
            "ebird_code": species.ebird_code,
            "movebank_taxon": species.movebank_taxon_name,
        }
        for species in _sp_registry
    ]


def _ebird_region(country: str) -> str:
    """Map a country name to an eBird region code."""
    return _COUNTRY_TO_EBIRD_REGION.get(country.lower(), country)


@dataclass
class ModuleResult:
    """Outcome of a single module run."""

    name: str
    status: str = "skipped"
    elapsed_s: float = 0.0
    detail: str = ""


@dataclass
class RunAllStats:
    """Aggregated results across all modules."""

    modules: list[ModuleResult] = field(default_factory=list)

    def summary(self) -> str:
        lines = [fmt.phase("RUN-ALL SUMMARY")]
        for m in self.modules:
            if m.status == "success":
                icon = "\u2713"
            elif m.status == "error":
                icon = "\u2717"
            else:
                icon = "\u2013"
            lines.append(
                f"  {icon} {m.name:<20s}  {m.status:<8s}"
                f"  {m.elapsed_s:6.1f}s  {m.detail}",
            )
        return "\n".join(lines)


# ------------------------------------------------------------------
# Per-module runners
# ------------------------------------------------------------------


def _run_birdweather(
    species: list[dict[str, Any]],
    *,
    start_date: date | None,
    end_date: date | None,
    hours: int | None,
    country: str,
    workers: int,
    min_confidence: float,
    target: str,
    classify_background: bool,
    bg_device: str,
    bg_batch_size: int,
    enrich_weather: bool,
) -> ModuleResult:
    from avian_biosurveillance.ingestion.birdweather import AudioCollector

    bw_ids = [str(s["birdweather_id"]) for s in species if s.get("birdweather_id")]
    collector = AudioCollector()

    kwargs: dict[str, Any] = {
        "min_confidence": min_confidence,
        "country": country,
        "species_ids": bw_ids or None,
        "download_audio": True,
        "workers": workers,
        "target": target,
        "enrich_weather": enrich_weather,
    }
    if start_date and end_date:
        kwargs["start_time"] = datetime(
            start_date.year,
            start_date.month,
            start_date.day,
            tzinfo=UTC,
        )
        kwargs["end_time"] = datetime(
            end_date.year,
            end_date.month,
            end_date.day,
            23,
            59,
            59,
            tzinfo=UTC,
        )
    else:
        kwargs["hours"] = hours or 24

    stats = collector.collect(**kwargs)
    detail = str(stats)

    # Optional background classification on freshly downloaded audio.
    if classify_background:
        from avian_biosurveillance.ingestion import (
            background_sound_classification as bsc,
        )

        bw_root = collector.output_dir
        date_dirs = sorted(
            p
            for p in bw_root.iterdir()
            if p.is_dir() and (p / "detections.jsonl").exists()
        )
        for dd in date_dirs:
            logger.info(
                "Classifying background sounds in %s",
                dd.name,
            )
            bsc.enrich_detections(
                dd,
                device=bg_device,
                batch_size=bg_batch_size,
            )

    return ModuleResult(name="birdweather", status="success", detail=detail)


def _run_ebird(
    _species: list[dict[str, Any]],
    *,
    start_date: date | None,
    end_date: date | None,
    hours: int | None,
    country: str,
    target: str,
) -> ModuleResult:
    from avian_biosurveillance.ingestion.ebird import EBirdCollector

    region = _ebird_region(country)
    collector = EBirdCollector()

    if start_date and end_date:
        stats = collector.collect_historic(
            region=region,
            start_date=start_date,
            end_date=end_date,
            target_species_only=True,
            target=target,
        )
    else:
        back = min((hours or 24) // 24 or 1, 30)
        stats = collector.collect_recent(
            region=region,
            back=back,
            target_species_only=True,
            target=target,
        )

    return ModuleResult(name="ebird", status="success", detail=str(stats))


def _run_knmi(
    *,
    start_date: date | None,
    end_date: date | None,
    hours: int | None,
    target: str,
) -> ModuleResult:
    from avian_biosurveillance.ingestion.knmi import KNMIIngestionManager

    manager = KNMIIngestionManager()

    if start_date and end_date:
        result = manager.ingest_date_range(
            start_date=start_date,
            end_date=end_date,
            target=target,
        )
    else:
        result = manager.ingest_meteorological_observations(
            hours_back=hours or 24,
            target=target,
        )

    return ModuleResult(name="knmi", status="success", detail=str(result))


def _run_movebank(
    *,
    start_date: date | None,
    end_date: date | None,
    target: str,
) -> ModuleResult:
    from avian_biosurveillance.ingestion.movebank import MovebankCollector

    collector = MovebankCollector()
    stats = collector.collect_target_species_events(
        start_date=start_date,
        end_date=end_date,
        try_all_sensors=True,
        target=target,
    )

    return ModuleResult(name="movebank", status="success", detail=str(stats))


def _run_sovon(
    *,
    target: str,
) -> ModuleResult:
    from avian_biosurveillance.ingestion.sovon import (
        download_charts,
        extract_data,
    )

    download_charts()
    extract_data(target=target)

    return ModuleResult(name="sovon", status="success")


def _run_earth_engine(
    *,
    start_date: date | None,
    end_date: date | None,
    target: str,
) -> ModuleResult:
    from avian_biosurveillance.ingestion.earth_engine import EarthEngineGridCollector

    grid_collector = EarthEngineGridCollector(grid_size_km=5.0)

    if not (start_date and end_date):
        # Default to today
        today = date.today()
        start_date = start_date or today
        end_date = end_date or today

    grid_results = grid_collector.collect_date_range(
        start_date=start_date,
        end_date=end_date,
        target=target,
    )

    grid_total = sum(s.locations_profiled for s in grid_results)
    grid_failed = sum(s.locations_failed for s in grid_results)
    detail = f"grid: {len(grid_results)} days, {grid_total} profiles, {grid_failed} failures at 5.0 km"
    return ModuleResult(name="earth_engine", status="success", detail=detail)


# ------------------------------------------------------------------
# Orchestrator
# ------------------------------------------------------------------


def run_all(
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    hours: int | None = None,
    country: str = "Netherlands",
    workers: int = 10,
    min_confidence: float = 0.1,
    target: str = "local",
    classify_background: bool = False,
    bg_device: str = "cpu",
    bg_batch_size: int = 8,
    enrich_weather: bool = True,
    enable_birdweather: bool = True,
    enable_ebird: bool = True,
    enable_knmi: bool = True,
    enable_movebank: bool = True,
    enable_sovon: bool = True,
    enable_earth_engine: bool = True,
) -> RunAllStats:
    """Run enabled ingestion modules sequentially.

    Args:
        start_date: Start of collection window (inclusive).
        end_date: End of collection window (inclusive).
        hours: Rolling look-back window (ignored when dates given).
        country: Country filter for BirdWeather / eBird.
        workers: Parallel download workers (BirdWeather).
        min_confidence: Minimum detection confidence (BirdWeather).
        target: Storage target — ``"local"`` saves to disk only (default),
            ``"database"`` saves to disk **and** persists to TimescaleDB /
            MinIO.
        classify_background: Run AST background classification (BirdWeather).
        bg_device: Device for AST inference (``cpu``, ``cuda``, ``mps``).
        bg_batch_size: Batch size for AST inference.
        enrich_weather: Fetch station weather data (BirdWeather).
        enable_birdweather: Toggle BirdWeather module.
        enable_ebird: Toggle eBird module.
        enable_knmi: Toggle KNMI module.
        enable_movebank: Toggle Movebank module.
        enable_sovon: Toggle Sovon module.
        enable_earth_engine: Toggle Earth Engine module.

    Returns:
        Aggregated :class:`RunAllStats` with per-module outcomes.
    """
    species = _load_target_species()
    all_stats = RunAllStats()

    runners: list[tuple[str, bool, Any]] = [
        (
            "birdweather",
            enable_birdweather,
            lambda: _run_birdweather(
                species,
                start_date=start_date,
                end_date=end_date,
                hours=hours,
                country=country,
                workers=workers,
                min_confidence=min_confidence,
                target=target,
                classify_background=classify_background,
                bg_device=bg_device,
                bg_batch_size=bg_batch_size,
                enrich_weather=enrich_weather,
            ),
        ),
        (
            "ebird",
            enable_ebird,
            lambda: _run_ebird(
                species,
                start_date=start_date,
                end_date=end_date,
                hours=hours,
                country=country,
                target=target,
            ),
        ),
        (
            "knmi",
            enable_knmi,
            lambda: _run_knmi(
                start_date=start_date,
                end_date=end_date,
                hours=hours,
                target=target,
            ),
        ),
        (
            "movebank",
            enable_movebank,
            lambda: _run_movebank(
                start_date=start_date,
                end_date=end_date,
                target=target,
            ),
        ),
        (
            "sovon",
            enable_sovon,
            lambda: _run_sovon(
                target=target,
            ),
        ),
        (
            "earth_engine",
            enable_earth_engine,
            lambda: _run_earth_engine(
                start_date=start_date,
                end_date=end_date,
                target=target,
            ),
        ),
    ]

    for name, enabled, runner in runners:
        if not enabled:
            all_stats.modules.append(ModuleResult(name=name, status="skipped"))
            continue

        logger.info(fmt.start(name))

        t0 = time.perf_counter()
        try:
            result = runner()
            result.elapsed_s = time.perf_counter() - t0
            all_stats.modules.append(result)
            logger.info(fmt.done(name, result.detail, result.elapsed_s))
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            logger.exception("%s failed", name)
            all_stats.modules.append(
                ModuleResult(
                    name=name,
                    status="error",
                    elapsed_s=elapsed,
                    detail=str(exc),
                )
            )
            logger.info(fmt.fail(name, str(exc)))

    logger.info(all_stats.summary())
    return all_stats


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------


def _parse_date(value: str) -> date:
    """Parse ``YYYY-MM-DD`` string into a :class:`date`."""
    return datetime.strptime(value, "%Y-%m-%d").date()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run all ingestion modules at once.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Time window
    time_grp = parser.add_argument_group("time window")
    time_grp.add_argument(
        "--start-date",
        type=_parse_date,
        help="Start date (YYYY-MM-DD)",
    )
    time_grp.add_argument(
        "--end-date",
        type=_parse_date,
        help="End date (YYYY-MM-DD)",
    )
    time_grp.add_argument(
        "--hours",
        type=int,
        default=None,
        help="Look-back hours (default: 24)",
    )

    # Filters
    filt_grp = parser.add_argument_group("filters")
    filt_grp.add_argument(
        "--country",
        default="Netherlands",
        help="Country filter (default: Netherlands)",
    )
    filt_grp.add_argument(
        "--min-confidence",
        type=float,
        default=0.1,
        help="Min confidence (default: 0.1)",
    )
    filt_grp.add_argument(
        "--workers",
        type=int,
        default=10,
        help="Parallel workers (default: 10)",
    )

    # Storage
    store_grp = parser.add_argument_group("storage")
    store_grp.add_argument(
        "--target",
        choices=["local", "database"],
        default="local",
        help="Storage target: local (disk only, default) or database "
        "(disk + TimescaleDB + MinIO)",
    )

    # Background classification
    bg_grp = parser.add_argument_group(
        "background classification (BirdWeather)",
    )
    bg_grp.add_argument(
        "--classify-background",
        action="store_true",
        help="Run AST background classification",
    )
    bg_grp.add_argument(
        "--bg-device",
        default="cpu",
        help="Device (cpu, cuda, mps)",
    )
    bg_grp.add_argument(
        "--bg-batch-size",
        type=int,
        default=8,
        help="Batch size (default: 8)",
    )
    bg_grp.add_argument(
        "--no-enrich-weather",
        action="store_true",
        dest="no_enrich_weather",
        help="Skip station weather enrichment (enabled by default)",
    )

    # Module toggles
    tog_grp = parser.add_argument_group("module toggles")
    tog_grp.add_argument(
        "--no-birdweather",
        action="store_true",
        help="Skip BirdWeather",
    )
    tog_grp.add_argument(
        "--no-ebird",
        action="store_true",
        help="Skip eBird",
    )
    tog_grp.add_argument(
        "--no-knmi",
        action="store_true",
        help="Skip KNMI",
    )
    tog_grp.add_argument(
        "--no-movebank",
        action="store_true",
        help="Skip Movebank",
    )
    tog_grp.add_argument(
        "--no-sovon",
        action="store_true",
        help="Skip Sovon",
    )
    tog_grp.add_argument(
        "--no-earth-engine",
        action="store_true",
        help="Skip Earth Engine",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    run_all(
        start_date=args.start_date,
        end_date=args.end_date,
        hours=args.hours,
        country=args.country,
        workers=args.workers,
        min_confidence=args.min_confidence,
        target=args.target,
        classify_background=args.classify_background,
        bg_device=args.bg_device,
        bg_batch_size=args.bg_batch_size,
        enrich_weather=not args.no_enrich_weather,
        enable_birdweather=not args.no_birdweather,
        enable_ebird=not args.no_ebird,
        enable_knmi=not args.no_knmi,
        enable_movebank=not args.no_movebank,
        enable_sovon=not args.no_sovon,
        enable_earth_engine=not args.no_earth_engine,
    )


if __name__ == "__main__":
    main()
