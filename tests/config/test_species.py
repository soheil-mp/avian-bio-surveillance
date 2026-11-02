"""Tests for avian_biosurveillance.config.species."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from avian_biosurveillance.config.species import (
    Species,
    SpeciesRegistry,
    load_registry,
    registry,
)


# ---------------------------------------------------------------------------
# Species dataclass
# ---------------------------------------------------------------------------


class TestSpecies:
    def _make(self, **overrides) -> Species:
        defaults = {
            "english": "Eurasian Blackbird",
            "dutch": "Merel",
            "scientific": "Turdus merula",
            "sovon_id": 11870,
            "birdweather_id": 35,
            "ebird_code": "eurbla",
            "movebank_taxon": "Turdus merula",
            "recommended_threshold": 0.363,
        }
        defaults.update(overrides)
        return Species(**defaults)

    def test_fields(self):
        sp = self._make()
        assert sp.english == "Eurasian Blackbird"
        assert sp.dutch == "Merel"
        assert sp.scientific == "Turdus merula"
        assert sp.sovon_id == 11870
        assert sp.birdweather_id == 35
        assert sp.ebird_code == "eurbla"
        assert sp.movebank_taxon == "Turdus merula"
        assert sp.recommended_threshold == pytest.approx(0.363)

    def test_birdweather_id_str(self):
        sp = self._make(birdweather_id=35)
        assert sp.birdweather_id_str == "35"

    def test_movebank_taxon_name_falls_back_to_scientific(self):
        sp = self._make(movebank_taxon=None)
        assert sp.movebank_taxon_name == "Turdus merula"

    def test_frozen(self):
        sp = self._make()
        with pytest.raises((AttributeError, TypeError)):
            sp.english = "X"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SpeciesRegistry
# ---------------------------------------------------------------------------


def _make_registry(*species_kwargs) -> SpeciesRegistry:
    species = [
        Species(
            english=kw["english"],
            dutch=kw.get("dutch", "NL"),
            scientific=kw.get("scientific", "Genus species"),
            sovon_id=kw.get("sovon_id", 1),
            birdweather_id=kw.get("birdweather_id", 1),
            ebird_code=kw.get("ebird_code"),
            movebank_taxon=kw.get("movebank_taxon"),
            recommended_threshold=kw.get("recommended_threshold"),
        )
        for kw in species_kwargs
    ]
    return SpeciesRegistry(species)


class TestSpeciesRegistry:
    def test_len(self):
        reg = _make_registry({"english": "Robin"}, {"english": "Wren"})
        assert len(reg) == 2

    def test_iter(self):
        reg = _make_registry({"english": "Robin"}, {"english": "Wren"})
        names = [sp.english for sp in reg]
        assert names == ["Robin", "Wren"]

    def test_getitem(self):
        reg = _make_registry({"english": "Robin"}, {"english": "Wren"})
        assert reg[0].english == "Robin"
        assert reg[1].english == "Wren"

    def test_english_names(self):
        reg = _make_registry({"english": "Robin"}, {"english": "Wren"})
        assert reg.english_names == ["Robin", "Wren"]

    def test_dutch_names(self):
        reg = _make_registry(
            {"english": "A", "dutch": "DA"}, {"english": "B", "dutch": "DB"}
        )
        assert reg.dutch_names == ["DA", "DB"]

    def test_birdweather_ids(self):
        reg = _make_registry(
            {"english": "A", "birdweather_id": 10},
            {"english": "B", "birdweather_id": 20},
        )
        assert reg.birdweather_ids == ["10", "20"]

    def test_ebird_codes(self):
        reg = _make_registry(
            {"english": "A", "ebird_code": "alpha"},
            {"english": "B", "ebird_code": "bravo"},
            {"english": "C", "ebird_code": None},
        )
        assert reg.ebird_codes == ["alpha", "bravo"]

    def test_movebank_taxa(self):
        reg = _make_registry(
            {
                "english": "A",
                "scientific": "Genus alpha",
                "movebank_taxon": "Movebank alpha",
            },
            {"english": "B", "scientific": "Genus beta"},
        )
        assert reg.movebank_taxa == ["Movebank alpha", "Genus beta"]

    def test_sovon_ids(self):
        reg = _make_registry(
            {"english": "A", "sovon_id": 100},
            {"english": "B", "sovon_id": 200},
        )
        assert reg.sovon_ids == [100, 200]

    def test_dutch_by_english(self):
        reg = _make_registry({"english": "Robin", "dutch": "Roodborst"})
        assert reg.dutch_by_english["Robin"] == "Roodborst"

    def test_english_by_dutch(self):
        reg = _make_registry({"english": "Robin", "dutch": "Roodborst"})
        assert reg.english_by_dutch["Roodborst"] == "Robin"

    def test_sovon_id_by_english(self):
        reg = _make_registry({"english": "Robin", "sovon_id": 999})
        assert reg.sovon_id_by_english["Robin"] == 999

    def test_english_by_sovon_id(self):
        reg = _make_registry({"english": "Robin", "sovon_id": 999})
        assert reg.english_by_sovon_id[999] == "Robin"

    def test_birdweather_id_by_english(self):
        reg = _make_registry({"english": "Robin", "birdweather_id": 6})
        assert reg.birdweather_id_by_english["Robin"] == "6"

    def test_ebird_code_by_english(self):
        reg = _make_registry({"english": "Robin", "ebird_code": "eurrob1"})
        assert reg.ebird_code_by_english["Robin"] == "eurrob1"

    def test_movebank_taxon_by_english(self):
        reg = _make_registry(
            {
                "english": "Robin",
                "scientific": "Erithacus rubecula",
                "movebank_taxon": "Movebank robin",
            }
        )
        assert reg.movebank_taxon_by_english["Robin"] == "Movebank robin"

    def test_recommended_threshold_by_birdweather_id(self):
        reg = _make_registry(
            {
                "english": "Robin",
                "birdweather_id": 6,
                "recommended_threshold": 0.42,
            }
        )
        assert reg.recommended_threshold_by_birdweather_id["6"] == pytest.approx(0.42)

    def test_english_by_birdweather_id(self):
        reg = _make_registry({"english": "Robin", "birdweather_id": 6})
        assert reg.english_by_birdweather_id["6"] == "Robin"

    def test_sovon_id_by_dutch(self):
        reg = _make_registry(
            {"english": "Robin", "dutch": "Roodborst", "sovon_id": 999}
        )
        assert reg.sovon_id_by_dutch["Roodborst"] == 999


# ---------------------------------------------------------------------------
# load_registry
# ---------------------------------------------------------------------------


def _write_species_json(path: Path, species: list[dict]) -> None:
    path.write_text(json.dumps({"species": species}), encoding="utf-8")


class TestLoadRegistry:
    def test_loads_from_explicit_path(self, tmp_path):
        f = tmp_path / "species.json"
        _write_species_json(
            f,
            [
                {
                    "english": "Robin",
                    "dutch": "Roodborst",
                    "scientific": "E. rubecula",
                    "sovon_id": 10990,
                    "birdweather_id": 6,
                    "ebird_code": "eurrob1",
                    "movebank_taxon": "Erithacus rubecula",
                    "recommended threshold": 0.42,
                },
            ],
        )
        reg = load_registry(f)
        assert len(reg) == 1
        assert reg[0].english == "Robin"
        assert reg[0].ebird_code == "eurrob1"
        assert reg[0].movebank_taxon == "Erithacus rubecula"
        assert reg[0].recommended_threshold == pytest.approx(0.42)

    def test_loads_from_source_checkout_when_cwd_has_no_config(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)  # no config/target_species.json here
        reg = load_registry()
        assert len(reg) > 0

    def test_raises_when_explicit_file_missing_and_no_other_config_exists(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(FileNotFoundError):
            load_registry(tmp_path / "no_such_file.json")

    def test_raises_on_bad_json(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "bad.json"
        f.write_text("not valid json", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid JSON"):
            load_registry(f)

    def test_raises_on_empty_species(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "empty.json"
        _write_species_json(f, [])
        with pytest.raises(ValueError, match="has no 'species' entries"):
            load_registry(f)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


class TestRegistrySingleton:
    def test_singleton_loaded(self):
        assert len(registry) > 0

    def test_singleton_has_english_names(self):
        assert len(registry.english_names) == len(registry)
