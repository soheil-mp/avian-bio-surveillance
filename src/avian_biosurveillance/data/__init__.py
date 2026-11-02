"""Data layer — canonical schemas, helpers, and station profiling."""

# Guard imports that require Python 3.10+ features (e.g. dataclass slots=True).
__all__: list[str] = []

try:
    from avian_biosurveillance.db.models import (  # noqa: F401
        BirdWeatherDetection,
        KNMIWeatherObservation,
    )
    __all__ += [
        "BirdWeatherDetection",
        "KNMIWeatherObservation",
    ]
except TypeError:
    pass  # models.py uses slots=True (Python 3.10+)

try:
    from avian_biosurveillance.data.quality import (  # noqa: F401
        MetricResult,
        QualityReport,
        StationMetrics,
        load_target_species,
        run_audit,
        store_results,
    )
    __all__ += ["MetricResult", "QualityReport", "StationMetrics",
                "load_target_species", "run_audit", "store_results"]
except Exception:
    pass  # quality module may not be importable without DB deps

try:
    from avian_biosurveillance.data.stations import (  # noqa: F401
        StationProfile,
        StationStats,
        extract_station_profiles,
        generate_stations_file,
    )
    __all__ += [
        "StationProfile",
        "StationStats",
        "extract_station_profiles",
        "generate_stations_file",
    ]
except Exception:
    pass
