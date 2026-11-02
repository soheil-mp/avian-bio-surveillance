"""Tests for avian_biosurveillance.db.models.ebird."""

from __future__ import annotations

from dataclasses import fields
from datetime import date, datetime

import pytest

from avian_biosurveillance.db.models.ebird import EBirdObservation


def _minimal_raw(**overrides) -> dict:
    base = {
        "subId": "S123",
        "speciesCode": "eurbla",
        "obsDt": "2026-01-15 14:30",
        "comName": "Eurasian Blackbird",
        "locId": "L123",
        "lat": 52.1,
        "lng": 5.1,
    }
    base.update(overrides)
    return base


class TestFromApi:
    def test_minimal_raw(self):
        obs = EBirdObservation.from_api(_minimal_raw())
        assert obs.sub_id == "S123"
        assert obs.species_code == "eurbla"
        assert obs.common_name == "Eurasian Blackbird"
        assert obs.loc_id == "L123"
        assert obs.lat == 52.1
        assert obs.lng == 5.1

    def test_datetime_parsed_with_time(self):
        obs = EBirdObservation.from_api(_minimal_raw(obsDt="2026-03-20 09:15"))
        assert obs.obs_dt == datetime(2026, 3, 20, 9, 15)
        assert obs.observation_date == date(2026, 3, 20)

    def test_datetime_parsed_date_only(self):
        obs = EBirdObservation.from_api(_minimal_raw(obsDt="2026-03-20"))
        assert obs.obs_dt.date() == date(2026, 3, 20)

    def test_invalid_datetime_defaults_to_now(self):
        # Should not raise; falls back to datetime.now()
        obs = EBirdObservation.from_api(_minimal_raw(obsDt="bad date"))
        assert isinstance(obs.obs_dt, datetime)

    def test_optional_fields_none_by_default(self):
        obs = EBirdObservation.from_api(_minimal_raw())
        assert obs.how_many is None
        assert obs.scientific_name is None
        assert obs.is_hotspot is None

    def test_how_many(self):
        obs = EBirdObservation.from_api(_minimal_raw(howMany=3))
        assert obs.how_many == 3

    def test_presence_noted_default_false(self):
        obs = EBirdObservation.from_api(_minimal_raw())
        assert obs.presence_noted is False

    def test_obs_valid_default_true(self):
        obs = EBirdObservation.from_api(_minimal_raw())
        assert obs.obs_valid is True

    def test_full_location_fields(self):
        obs = EBirdObservation.from_api(
            _minimal_raw(
                locName="Vondelpark",
                locationPrivate=False,
                subnational2Code="NL-NH-AMS",
                subnational2Name="Amsterdam",
                subnational1Code="NL-NH",
                subnational1Name="Noord-Holland",
                countryCode="NL",
                countryName="Netherlands",
            )
        )
        assert obs.loc_name == "Vondelpark"
        assert obs.country_code == "NL"
        assert obs.subnational1_code == "NL-NH"

    def test_hotspot_fields(self):
        obs = EBirdObservation.from_api(
            _minimal_raw(
                isHotspot=True,
                numSpeciesAllTime=150,
            )
        )
        assert obs.is_hotspot is True
        assert obs.num_species_all_time == 150


class TestHelpers:
    def test_to_dict_has_all_fields(self):
        obs = EBirdObservation.from_api(_minimal_raw())
        d = obs.to_dict()
        assert set(d.keys()) == {f.name for f in fields(obs)}

    def test_column_names_matches_to_dict(self):
        obs = EBirdObservation.from_api(_minimal_raw())
        assert set(EBirdObservation.column_names()) == set(obs.to_dict().keys())

    def test_pandas_dtypes_keys_match_column_names(self):
        assert set(EBirdObservation.pandas_dtypes().keys()) == set(
            EBirdObservation.column_names()
        )

    def test_frozen(self):
        obs = EBirdObservation.from_api(_minimal_raw())
        with pytest.raises((AttributeError, TypeError)):
            obs.lat = 0.0  # type: ignore[misc]
