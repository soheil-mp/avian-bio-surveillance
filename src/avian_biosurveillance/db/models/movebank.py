"""Movebank Data Models.

Maps to the ``movebank_tbl`` TimescaleDB hypertable (GPS tracking
measurements with selected individual, tag, and deployment reference data).
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any


def _parse_float(val: Any) -> float | None:
    """Safely parse a float, returning None for empty/invalid."""
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _parse_int(val: Any) -> int | None:
    """Safely parse an int, returning None for empty/invalid."""
    if val is None or val == "":
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def _parse_bool(val: Any) -> bool | None:
    """Parse Movebank boolean (true/false string or empty)."""
    if val is None or val == "":
        return None
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() == "true"


def _parse_timestamp(val: Any) -> datetime | None:
    """Parse Movebank timestamp format: '2008-05-31 13:30:02.001'."""
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip().strip('"')
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _clamp_lat(val: float | None) -> float | None:
    """Null out latitude values outside WGS-84 range (-90..90)."""
    if val is None:
        return None
    return val if -90.0 <= val <= 90.0 else None


def _clamp_lon(val: float | None) -> float | None:
    """Null out longitude values outside WGS-84 range (-180..180)."""
    if val is None:
        return None
    return val if -180.0 <= val <= 180.0 else None


@dataclass(frozen=True, slots=True)
class MovebankEvent:
    """One tracking event (e.g. GPS fix) from Movebank.

    Maps 1-to-1 with ``movebank_tbl``.
    """

    # Identity & Time
    event_id: int
    study_id: int
    timestamp: datetime

    # Individual
    individual_id: int | None = None
    individual_local_identifier: str | None = None
    individual_taxon_canonical_name: str | None = None

    # Tag / Deployment
    tag_id: int | None = None
    tag_local_identifier: str | None = None
    deployment_id: int | None = None

    # Sensor
    sensor_type_id: int | None = None

    # Location (WGS 84)
    location_lat: float | None = None
    location_long: float | None = None

    # Kinematics
    ground_speed: float | None = None
    heading: float | None = None
    height_above_ellipsoid: float | None = None

    # Quality
    visible: bool | None = None

    # Individual reference (merged from movebank_individual)
    individual_sex: str | None = None
    individual_life_stage: str | None = None

    # Tag reference (merged from movebank_tag)
    tag_manufacturer_name: str | None = None
    tag_model: str | None = None
    tag_serial_number: str | None = None

    # Deployment reference (merged from movebank_deployment)
    deployment_local_identifier: str | None = None
    deploy_on_timestamp: datetime | None = None
    deploy_off_timestamp: datetime | None = None
    deploy_on_latitude: float | None = None
    deploy_on_longitude: float | None = None
    deployment_comments: str | None = None
    location_accuracy_comments: str | None = None
    manipulation_type: str | None = None
    study_site: str | None = None
    tag_readout_method: str | None = None
    alt_project_id: str | None = None
    attachment_type: str | None = None
    animal_mass_g: float | None = None

    # Event-level update / quality flags
    update_ts: datetime | None = None
    import_marked_outlier: bool | None = None
    event_comments: str | None = None

    # Individual (animal) reference
    individual_comments: str | None = None
    individual_ring_id: str | None = None
    individual_number_of_events: int | None = None
    individual_number_of_deployments: int | None = None
    individual_taxon_detail: str | None = None

    # Tag reference (extended)
    tag_comments: str | None = None

    # Extended altitude & barometry (sensor-agnostic)
    height_above_msl_m: float | None = None
    barometric_pressure_pa: float | None = None

    # Environmental sensors on the tag
    external_temperature_c: float | None = None

    # -- Factory ------------------------------------------------

    @classmethod
    def from_csv_row(
        cls, row: dict[str, str], *, study_id: int | None = None,
    ) -> MovebankEvent:
        """Build from a Movebank CSV/direct-read row dict.

        Args:
            row: One row dict from CSV parsing.
            study_id: Override study_id (if not in the row).

        Returns:
            Populated ``MovebankEvent``.
        """
        ts = _parse_timestamp(row.get("timestamp"))
        if ts is None:
            raise ValueError(
                f"Missing or invalid timestamp: {row.get('timestamp')}")

        event_id_val = _parse_int(row.get("event_id"))
        if event_id_val is None:
            # Generate a deterministic ID from key fields
            ident = row.get("individual_id", "")
            ts_str = row.get("timestamp", "")
            event_id_val = abs(hash(f"{ident}_{ts_str}")) % (2**63)

        sid = (
            study_id
            or _parse_int(row.get("study_id"))
            or _parse_int(row.get("_study_id"))
            or 0
        )

        def _s(key: str) -> str | None:
            """Read a stripped, quote-free string or None."""
            v = row.get(key)
            if v is None:
                return None
            v = str(v).strip().strip('"')
            return v or None

        return cls(
            event_id=event_id_val,
            study_id=sid,
            timestamp=ts,
            individual_id=_parse_int(row.get("individual_id")),
            individual_local_identifier=_s("individual_local_identifier"),
            individual_taxon_canonical_name=_s(
                "individual_taxon_canonical_name"),
            tag_id=_parse_int(row.get("tag_id")),
            tag_local_identifier=_s("tag_local_identifier"),
            deployment_id=_parse_int(row.get("deployment_id")),
            sensor_type_id=_parse_int(row.get("sensor_type_id")),
            location_lat=_clamp_lat(_parse_float(row.get("location_lat"))),
            location_long=_clamp_lon(_parse_float(row.get("location_long"))),
            ground_speed=_parse_float(row.get("ground_speed")),
            heading=_parse_float(row.get("heading")),
            height_above_ellipsoid=_parse_float(
                row.get("height_above_ellipsoid")
            ),
            visible=_parse_bool(row.get("visible")),
            individual_sex=_s("individual_sex"),
            individual_life_stage=_s("individual_life_stage"),
            tag_manufacturer_name=_s("tag_manufacturer_name"),
            tag_model=_s("tag_model"),
            tag_serial_number=_s("tag_serial_number"),
            deployment_local_identifier=_s("deployment_local_identifier"),
            deploy_on_timestamp=_parse_timestamp(
                row.get("deploy_on_timestamp")),
            deploy_off_timestamp=_parse_timestamp(
                row.get("deploy_off_timestamp")),
            deploy_on_latitude=_clamp_lat(
                _parse_float(row.get("deploy_on_latitude"))),
            deploy_on_longitude=_clamp_lon(
                _parse_float(row.get("deploy_on_longitude"))),
            deployment_comments=_s("deployment_comments"),
            location_accuracy_comments=_s("location_accuracy_comments"),
            manipulation_type=_s("manipulation_type"),
            study_site=_s("study_site"),
            tag_readout_method=_s("tag_readout_method"),
            alt_project_id=_s("alt_project_id"),
            attachment_type=_s("attachment_type"),
            animal_mass_g=_parse_float(row.get("animal_mass_g")
                                       or row.get("animal_mass")),
            update_ts=_parse_timestamp(row.get("update_ts")),
            import_marked_outlier=_parse_bool(
                row.get("import_marked_outlier")),
            event_comments=_s("comments") or _s("event_comments"),
            individual_comments=_s("individual_comments"),
            individual_ring_id=_s("individual_ring_id") or _s("ring_id"),
            individual_number_of_events=_parse_int(
                row.get("individual_number_of_events")
                or row.get("number_of_events")),
            individual_number_of_deployments=_parse_int(
                row.get("individual_number_of_deployments")
                or row.get("number_of_deployments")),
            individual_taxon_detail=_s("individual_taxon_detail")
            or _s("taxon_detail"),
            tag_comments=_s("tag_comments"),
            height_above_msl_m=_parse_float(
                row.get("height_above_mean_sea_level")
                or row.get("height_above_msl")
                or row.get("height_above_msl_m")),
            barometric_pressure_pa=_parse_float(
                row.get("barometric_pressure")
                or row.get("barometric_pressure_pa")),
            external_temperature_c=_parse_float(
                row.get("external_temperature")
                or row.get("external_temperature_c")),
        )

    # -- Helpers -------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Flat dict for DataFrame or DB insert."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @staticmethod
    def column_names() -> list[str]:
        """Ordered column names matching the SQL schema."""
        return [f.name for f in fields(MovebankEvent)]

    @staticmethod
    def pandas_dtypes() -> dict[str, str]:
        """Dtype map for ``pandas.DataFrame``."""
        return {
            "event_id": "Int64",
            "study_id": "Int64",
            "timestamp": "datetime64[ns]",
            "individual_id": "Int64",
            "individual_local_identifier": "string",
            "individual_taxon_canonical_name": "string",
            "tag_id": "Int64",
            "tag_local_identifier": "string",
            "deployment_id": "Int64",
            "sensor_type_id": "Int64",
            "location_lat": "Float64",
            "location_long": "Float64",
            "ground_speed": "Float64",
            "heading": "Float64",
            "height_above_ellipsoid": "Float64",
            "visible": "boolean",
            "individual_sex": "string",
            "individual_life_stage": "string",
            "tag_manufacturer_name": "string",
            "tag_model": "string",
            "tag_serial_number": "string",
            "deployment_local_identifier": "string",
            "deploy_on_timestamp": "datetime64[ns]",
            "deploy_off_timestamp": "datetime64[ns]",
            "deploy_on_latitude": "Float64",
            "deploy_on_longitude": "Float64",
            "deployment_comments": "string",
            "location_accuracy_comments": "string",
            "manipulation_type": "string",
            "study_site": "string",
            "tag_readout_method": "string",
            "alt_project_id": "string",
            "attachment_type": "string",
            "animal_mass_g": "Float64",
            "update_ts": "datetime64[ns]",
            "import_marked_outlier": "boolean",
            "event_comments": "string",
            "individual_comments": "string",
            "individual_ring_id": "string",
            "individual_number_of_events": "Int64",
            "individual_number_of_deployments": "Int64",
            "individual_taxon_detail": "string",
            "tag_comments": "string",
            "height_above_msl_m": "Float64",
            "barometric_pressure_pa": "Float64",
            "external_temperature_c": "Float64",
        }
