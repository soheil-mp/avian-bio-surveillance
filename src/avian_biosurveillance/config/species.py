"""Centralised loader for the target-species registry.

Every consumer that needs species identifiers (quality audit, Sovon scraper,
BirdWeather flow, species thresholds) MUST import from this module instead of
maintaining its own hard-coded list.

The canonical data lives in ``config/target_species.json``. The loader reads
that file directly and raises immediately if it is missing or malformed so the
runtime never silently drifts from the checked-in config.

Usage::

    from avian_biosurveillance.config.species import registry

    registry.english_names  # ['Eurasian Blackbird', ...]
    registry.birdweather_ids  # ['35', '61', ...]
    registry.dutch_by_english  # {'Eurasian Blackbird': 'Merel', ...}
    registry.sovon_id_by_english  # {'Eurasian Blackbird': 11870, ...}
    registry.recommended_threshold_by_birdweather_id["35"]  # 0.363
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Species:
    """A single target species with all cross-system identifiers."""

    english: str
    dutch: str
    scientific: str
    sovon_id: int
    birdweather_id: int
    ebird_code: str | None = None
    movebank_taxon: str | None = None
    recommended_threshold: float | None = None

    @property
    def birdweather_id_str(self) -> str:
        return str(self.birdweather_id)

    @property
    def movebank_taxon_name(self) -> str:
        """Movebank taxon name, falling back to the scientific name."""
        return self.movebank_taxon or self.scientific


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class SpeciesRegistry:
    """Typed, read-only facade over the target-species list.

    Constructed once at module load and exposed as :pydata:`registry`.
    """

    _species: list[Species] = field(
        default_factory=list,
        repr=False,
    )

    # -- Sequence-like accessors ------------------------------------------

    def __len__(self) -> int:
        return len(self._species)

    def __iter__(self) -> Iterator[Species]:
        return iter(self._species)

    def __getitem__(self, idx: int) -> Species:
        return self._species[idx]

    # -- Derived look-ups (cached via property) ---------------------------

    @property
    def english_names(self) -> list[str]:
        """Canonical English names (BirdWeather ``commonName``)."""
        return [s.english for s in self._species]

    @property
    def dutch_names(self) -> list[str]:
        return [s.dutch for s in self._species]

    @property
    def scientific_names(self) -> list[str]:
        return [s.scientific for s in self._species]

    @property
    def birdweather_ids(self) -> list[str]:
        """BirdWeather API species IDs as strings."""
        return [s.birdweather_id_str for s in self._species]

    @property
    def ebird_codes(self) -> list[str]:
        """eBird species codes from the target-species config."""
        return [s.ebird_code for s in self._species if s.ebird_code]

    @property
    def movebank_taxa(self) -> list[str]:
        """Movebank canonical taxon names for target species."""
        return [s.movebank_taxon_name for s in self._species]

    @property
    def sovon_ids(self) -> list[int]:
        return [s.sovon_id for s in self._species]

    # -- Dict look-ups ----------------------------------------------------

    @property
    def dutch_by_english(self) -> dict[str, str]:
        return {s.english: s.dutch for s in self._species}

    @property
    def english_by_dutch(self) -> dict[str, str]:
        return {s.dutch: s.english for s in self._species}

    @property
    def sovon_id_by_english(self) -> dict[str, int]:
        return {s.english: s.sovon_id for s in self._species}

    @property
    def english_by_sovon_id(self) -> dict[int, str]:
        return {s.sovon_id: s.english for s in self._species}

    @property
    def birdweather_id_by_english(self) -> dict[str, str]:
        return {s.english: s.birdweather_id_str for s in self._species}

    @property
    def ebird_code_by_english(self) -> dict[str, str]:
        return {
            s.english: s.ebird_code
            for s in self._species
            if s.ebird_code is not None
        }

    @property
    def movebank_taxon_by_english(self) -> dict[str, str]:
        return {s.english: s.movebank_taxon_name for s in self._species}

    @property
    def recommended_threshold_by_birdweather_id(self) -> dict[str, float]:
        return {
            s.birdweather_id_str: float(s.recommended_threshold)
            for s in self._species
            if s.recommended_threshold is not None
        }

    @property
    def minimum_recommended_threshold(self) -> float | None:
        thresholds = [
            float(s.recommended_threshold)
            for s in self._species
            if s.recommended_threshold is not None
        ]
        return min(thresholds) if thresholds else None

    @property
    def english_by_birdweather_id(self) -> dict[str, str]:
        return {s.birdweather_id_str: s.english for s in self._species}

    @property
    def sovon_id_by_dutch(self) -> dict[str, int]:
        """Dutch name -> Sovon ID (Sovon URL builder)."""
        return {s.dutch: s.sovon_id for s in self._species}


_CONFIG_RELATIVE = Path("config/target_species.json")


def _repo_root_from_source() -> Path:
    """Return the repository root when running from an editable checkout."""
    return Path(__file__).resolve().parents[3]


def _config_candidates(config_path: Path | None) -> list[Path]:
    if config_path is not None:
        return [config_path.expanduser()]
    candidates: list[Path] = []
    candidates.extend(
        [
            Path.cwd() / _CONFIG_RELATIVE,
            _repo_root_from_source() / _CONFIG_RELATIVE,
        ]
    )
    deduplicated: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.expanduser()
        if resolved not in seen:
            deduplicated.append(resolved)
            seen.add(resolved)
    return deduplicated


def _parse_species(
    raw: Sequence[dict[str, object]],
) -> list[Species]:
    return [
        Species(
            english=str(d["english"]),
            dutch=str(d["dutch"]),
            scientific=str(d["scientific"]),
            sovon_id=int(str(d["sovon_id"])),
            birdweather_id=int(str(d["birdweather_id"])),
            ebird_code=(
                str(ebird_code).strip()
                if (ebird_code := d.get("ebird_code")) is not None
                else None
            ),
            movebank_taxon=(
                str(movebank_taxon).strip()
                if (movebank_taxon := d.get("movebank_taxon")) is not None
                else None
            ),
            recommended_threshold=(
                float(str(threshold))
                if (
                    threshold := d.get(
                        "recommended threshold",
                        d.get("recommended_threshold"),
                    )
                )
                is not None
                else None
            ),
        )
        for d in raw
    ]


def load_registry(
    config_path: Path | None = None,
) -> SpeciesRegistry:
    """Load the species registry from a JSON config.

    Resolution order:
        1. Explicit *config_path* argument.
        2. ``config/target_species.json`` relative to cwd.
        3. ``config/target_species.json`` relative to the source checkout.

    Raises
    ------
    FileNotFoundError
        If no valid config file is found in any of the searched paths.
    ValueError
        If the file exists but cannot be parsed or contains no species.
    """
    paths_to_try = _config_candidates(config_path)
    for p in paths_to_try:
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                msg = f"Invalid JSON in species config {p}: {exc}"
                raise ValueError(msg) from exc

            items = data.get("species", [])
            if not items:
                msg = f"Species config {p} has no 'species' entries."
                raise ValueError(msg)

            try:
                reg = SpeciesRegistry(_parse_species(items))
            except (KeyError, TypeError, ValueError) as exc:
                msg = f"Failed to parse species config {p}: {exc}"
                raise ValueError(msg) from exc
            logger.info(
                "Loaded %d target species from %s",
                len(reg),
                p,
            )
            return reg

    searched = ", ".join(str(p) for p in paths_to_try)
    msg = (
        "Species config not found. Searched: "
        f"{searched}. Create config/target_species.json or pass an explicit path."
    )
    raise FileNotFoundError(msg)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

registry: SpeciesRegistry = load_registry()
"""Module-level singleton -- ready to use at import time."""
