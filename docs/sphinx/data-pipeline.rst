Data pipeline
=============

The pipeline moves data from raw records to dashboard-ready results: ingestion,
quality checks, audio processing, and feature engineering.

Data sources
------------

BirdWeather is the primary source. Weather and environmental sources add
context, while SOVON mortality records are used only as an evaluation
reference — never as training labels — because of their limited spatial
resolution and delayed availability. All sources are public.

.. list-table::
   :header-rows: 1
   :widths: 24 76

   * - Source
     - Description
   * - **BirdWeather**
     - Real-time bird detections and audio references (primary source).
   * - **KNMI**
     - Weather measurements such as temperature, precipitation, and wind.
   * - **Google Earth Engine**
     - Environmental summaries such as vegetation and land-surface context.
   * - **CDS drought**
     - Drought indicators such as SPI and SPEI.
   * - **SOVON**
     - Monthly national dead-bird records (evaluation only).

Ingestion
---------

Each source is ingested on its own schedule. The pipeline distinguishes three
execution modes: regular scheduled jobs (daily, bi-weekly, or monthly depending
on data availability), forward-walking historical catchup jobs that backfill
past date ranges, and manual triggers for ad-hoc repair. BirdWeather runs daily
at 00:00, KNMI at 00:30, Google Earth Engine bi-weekly at 03:00, and CDS drought
and SOVON monthly.

Data quality
------------

Because the platform relies partly on citizen-science infrastructure, a
quality-assurance layer runs after ingestion and before downstream processing.
It checks three dimensions:

* **Completeness** — required fields (station ID, timestamp, species, confidence, coordinates, audio reference) must be present.
* **Temporal continuity** — stations are checked for unexpected gaps or unusually long silent periods.
* **Audio validity** — files must be decodable, non-empty, and contain enough acoustic content for feature extraction.

Outcomes are stored in a dedicated data-quality table and linked to the original
records, so downstream jobs can filter or separate records with missing
metadata, invalid audio, station outages, or privacy-sensitive content. This
keeps model outputs auditable: an alert can be traced to biology rather than a
data fault.

Audio processing and feature engineering
----------------------------------------

For each detection the pipeline:

#. Crops the original soundscape to the BirdNET detection window and exports a **mono FLAC** clip (lossless, compact, preserving the frequencies needed for call analysis).
#. Applies **background-sound classification** (a pre-trained Hugging Face model) to tag speech, traffic, wind, or rain so privacy-sensitive clips can be withheld.
#. Renders **mel-spectrograms and waveforms**, stored in MinIO and shown in the dashboard.
#. Extracts learned **BirdNET and Perch v2 embeddings**, which place similar calls close together in feature space so unusual calls stand out — even without labeled examples of infected birds.
