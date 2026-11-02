"""Orchestration layer — Dagster flows for the Avian Biosurveillance pipeline.

This package exposes the Dagster-backed flows that coordinate data ingestion,
post-processing, and ISO 25012 data-quality auditing.

Submodules
----------
birdweather_flow
    Acoustic-detection ingestion and gap-fill backfill from BirdWeather.
cds_drought_flow
    Copernicus CDS ERA5-derived monthly drought grid ingestion.
data_quality_flow
    ISO 25012 quality audit across all source tables.
earth_engine_flow
    Satellite-derived environmental profiling via Google Earth Engine.
ebird_flow
    Recent bird-observation ingestion from the eBird API.
knmi_flow
    KNMI 10-minute weather observations, date-range backfill, and
    seismic-event ingestion.
movebank_flow
    GPS tracking-event ingestion from Movebank.
sovon_flow
    Sovon mortality chart download and vision-model extraction.
station_summary_flow
    Materialised-view refresh for ``birdweather_stations_tbl``.
serve
    Deployment entry-point registering cron schedules with Dagster.
generate_drawio
    draw.io diagram generator for the orchestration architecture.
"""

from __future__ import annotations


__all__ = [
    "birdweather_flow",
    "cds_drought_flow",
    "data_quality_flow",
    "earth_engine_flow",
    "ebird_flow",
    "generate_drawio",
    "knmi_flow",
    "movebank_flow",
    "serve",
    "sovon_flow",
    "station_summary_flow",
]
