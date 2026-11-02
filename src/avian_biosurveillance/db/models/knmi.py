"""
KNMI Data Models

Provides data models for KNMI weather and seismic data:
- ``KNMIWeatherObservation``: 10-minute in-situ observations.
- ``SeismicEvent``: Earthquake catalog from the KNMI FDSN Event API.

Note: ``KNMIDailySummary`` was removed in v6. Daily summaries are now derived
automatically from knmi_tbl via the knmi_daily_cagg_tbl
TimescaleDB continuous aggregate. There is no longer a separate daily-summary
table or ETL path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class KNMIWeatherObservation:
    """One 10-min KNMI station observation.

    Maps 1-to-1 with ``knmi_tbl``.
    """

    # Temporal
    observed_at: datetime
    observation_date: date

    # Station Identity & Location
    station_id: str
    station_name: str | None = None
    station_lat: float | None = None
    station_lon: float | None = None
    station_elevation_m: float | None = None

    # Measurements
    temperature_c: float | None = None
    relative_humidity_pct: float | None = None
    wind_speed_ms: float | None = None
    wind_direction_deg: float | None = None
    pressure_hpa: float | None = None
    precipitation_mm: float | None = None
    solar_radiation_wm2: float | None = None
    cloud_cover_okta: float | None = None

    # -- Factory ------------------------------------------------

    @classmethod
    def from_edr_coverage(
        cls,
        coverage: dict[str, Any],
        location_id: str,
        location_name: str | None = None,
        location_elevation_m: float | None = None,
    ) -> list[KNMIWeatherObservation]:
        """Build list from a CoverageJSON coverage.

        Args:
            coverage: Single coverage from EDR response.
            location_id: KNMI WMO location id.
            location_name: Human-readable station name.
            location_elevation_m: Station elevation above mean sea level.

        Returns:
            One ``KNMIWeatherObservation`` per timestep.
        """
        domain = coverage.get("domain", {})
        axes = domain.get("axes", {})
        timestamps = axes.get("t", {}).get("values", [])
        x_vals = axes.get("x", {}).get("values")
        y_vals = axes.get("y", {}).get("values")
        lon_val = x_vals[0] if x_vals else None
        lat_val = y_vals[0] if y_vals else None
        ranges = coverage.get("ranges", {})

        def _vals(key: str) -> list[Any]:
            arr = ranges.get(key, {}).get("values")
            return arr if arr else [None] * len(timestamps)

        dd = _vals("dd")
        ff = _vals("ff")
        pp = _vals("pp")
        rh = _vals("rh")
        ta = _vals("ta")
        r1h = _vals("R1H")
        qg = _vals("qg")
        n = _vals("n")

        results: list[KNMIWeatherObservation] = []
        for i, ts_str in enumerate(timestamps):
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            results.append(cls(
                observed_at=ts,
                observation_date=ts.date(),
                station_id=location_id,
                station_name=location_name,
                station_lat=lat_val,
                station_lon=lon_val,
                station_elevation_m=location_elevation_m,
                temperature_c=(ta[i] if i < len(ta) else None),
                relative_humidity_pct=(rh[i] if i < len(rh) else None),
                wind_speed_ms=(ff[i] if i < len(ff) else None),
                wind_direction_deg=(dd[i] if i < len(dd) else None),
                pressure_hpa=(pp[i] if i < len(pp) else None),
                precipitation_mm=(r1h[i] if i < len(r1h) else None),
                solar_radiation_wm2=(qg[i] if i < len(qg) else None),
                cloud_cover_okta=(n[i] if i < len(n) else None),
            ))
        return results

    # -- Helpers -------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Flat dict for DataFrame or DB insert."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @staticmethod
    def column_names() -> list[str]:
        """Ordered column names matching knmi_tbl."""
        return [f.name for f in fields(KNMIWeatherObservation)]

    @staticmethod
    def pandas_dtypes() -> dict[str, str]:
        """Dtype map for ``pandas.DataFrame``."""
        return {
            "observed_at": "datetime64[ns, UTC]",
            "observation_date": "object",
            "station_id": "string",
            "station_name": "string",
            "station_lat": "Float64",
            "station_lon": "Float64",
            "station_elevation_m": "Float64",
            "temperature_c": "Float64",
            "relative_humidity_pct": "Float64",
            "wind_speed_ms": "Float64",
            "wind_direction_deg": "Float64",
            "pressure_hpa": "Float64",
            "precipitation_mm": "Float64",
            "solar_radiation_wm2": "Float64",
            "cloud_cover_okta": "Float64",
        }


# =====================================================================
# Seismic Event
# =====================================================================


@dataclass(frozen=True, slots=True)
class SeismicEvent:
    """One earthquake event from KNMI FDSN catalog."""

    event_id: str
    event_time: datetime

    latitude: float
    longitude: float
    depth_km: float | None = None
    magnitude: float | None = None
    magnitude_type: str | None = None
    description: str | None = None
    region: str | None = None

    attributes: str = "{}"  # JSONB serialised

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> SeismicEvent:
        """Build from an earthquake dict (as returned by KNMI parser)."""
        known = {
            "event_id", "time", "latitude", "longitude",
            "depth", "magnitude", "magnitude_type",
            "description", "region",
        }
        extra = {k: v for k, v in raw.items() if k not in known and v}

        ts = raw.get("time")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        if not isinstance(ts, datetime):
            raise ValueError("Missing or invalid seismic event timestamp")

        return cls(
            event_id=str(raw["event_id"]),
            event_time=ts,
            latitude=float(raw["latitude"]),
            longitude=float(raw["longitude"]),
            depth_km=raw.get("depth"),
            magnitude=raw.get("magnitude"),
            magnitude_type=raw.get("magnitude_type"),
            description=raw.get("description"),
            region=raw.get("region"),
            attributes=json.dumps(extra) if extra else "{}",
        )

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}
