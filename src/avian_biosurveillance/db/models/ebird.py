"""eBird Observation Data Model.

Maps to the ``ebird_tbl`` TimescaleDB hypertable, which stores
citizen-science bird observations fetched from the eBird API 2.0
(with merged hotspot reference data).
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class EBirdObservation:
    """One bird observation from eBird.

    Maps 1-to-1 with ``ebird_tbl``.
    Observation, species and location data are flattened from the API response.
    """

    # Identity & Timestamp
    sub_id: str
    species_code: str
    obs_dt: datetime
    observation_date: date

    # Species
    common_name: str = ""
    scientific_name: str | None = None

    # Count
    how_many: int | None = None
    presence_noted: bool = False

    # Location
    loc_id: str = ""
    loc_name: str | None = None
    lat: float = 0.0
    lng: float = 0.0
    location_private: bool = False

    # Review status
    obs_valid: bool = True
    obs_reviewed: bool = False

    # Region hierarchy (populated when detail=full)
    subnational2_code: str | None = None
    subnational2_name: str | None = None
    subnational1_code: str | None = None
    subnational1_name: str | None = None
    country_code: str | None = None
    country_name: str | None = None

    # Observer (populated when detail=full)
    user_display_name: str | None = None

    # Checklist metadata
    checklist_id: str | None = None
    has_comments: bool | None = None
    has_rich_media: bool | None = None

    # Hotspot reference (merged from ebird_hotspot)
    num_species_all_time: int | None = None
    is_hotspot: bool | None = None

    # -- Factory ------------------------------------------------

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> EBirdObservation:
        """Build from an eBird API 2.0 observation response dict.

        Args:
            raw: One observation dict from any ``/data/obs`` endpoint.

        Returns:
            Populated ``EBirdObservation``.
        """
        # Parse observation datetime – eBird format: "2026-01-21 16:35"
        obs_dt_str = raw.get("obsDt", "")
        try:
            obs_dt = datetime.strptime(obs_dt_str, "%Y-%m-%d %H:%M")
        except ValueError:
            try:
                obs_dt = datetime.strptime(obs_dt_str, "%Y-%m-%d")
            except ValueError:
                obs_dt = datetime.now()

        return cls(
            sub_id=raw.get("subId", ""),
            species_code=raw.get("speciesCode", ""),
            obs_dt=obs_dt,
            observation_date=obs_dt.date(),
            common_name=raw.get("comName", ""),
            scientific_name=raw.get("sciName"),
            how_many=raw.get("howMany"),
            presence_noted=raw.get("presenceNoted", False),
            loc_id=raw.get("locId", ""),
            loc_name=raw.get("locName"),
            lat=raw.get("lat", 0.0),
            lng=raw.get("lng", 0.0),
            location_private=raw.get("locationPrivate", False),
            obs_valid=raw.get("obsValid", True),
            obs_reviewed=raw.get("obsReviewed", False),
            subnational2_code=raw.get("subnational2Code"),
            subnational2_name=raw.get("subnational2Name"),
            subnational1_code=raw.get("subnational1Code"),
            subnational1_name=raw.get("subnational1Name"),
            country_code=raw.get("countryCode"),
            country_name=raw.get("countryName"),
            user_display_name=raw.get("userDisplayName"),
            checklist_id=raw.get("checklistId"),
            has_comments=raw.get("hasComments"),
            has_rich_media=raw.get("hasRichMedia"),
            num_species_all_time=raw.get("numSpeciesAllTime"),
            is_hotspot=raw.get("isHotspot"),
        )

    # -- Helpers -------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Flat dict for DataFrame or DB insert."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @staticmethod
    def column_names() -> list[str]:
        """Ordered column names matching the SQL schema."""
        return [f.name for f in fields(EBirdObservation)]

    @staticmethod
    def pandas_dtypes() -> dict[str, str]:
        """Dtype map for ``pandas.DataFrame``."""
        return {
            "sub_id": "string",
            "species_code": "string",
            "obs_dt": "datetime64[ns]",
            "observation_date": "object",
            "common_name": "string",
            "scientific_name": "string",
            "how_many": "Int64",
            "presence_noted": "boolean",
            "loc_id": "string",
            "loc_name": "string",
            "lat": "Float64",
            "lng": "Float64",
            "location_private": "boolean",
            "obs_valid": "boolean",
            "obs_reviewed": "boolean",
            "subnational2_code": "string",
            "subnational2_name": "string",
            "subnational1_code": "string",
            "subnational1_name": "string",
            "country_code": "string",
            "country_name": "string",
            "user_display_name": "string",
            "checklist_id": "string",
            "has_comments": "boolean",
            "has_rich_media": "boolean",
            "num_species_all_time": "Int64",
            "is_hotspot": "boolean",
        }
