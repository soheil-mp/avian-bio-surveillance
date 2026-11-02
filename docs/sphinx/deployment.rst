Deployment
==========

Running the stack
-----------------

The platform is containerized, so the whole system runs from a single command in
local development:

.. code-block:: bash

   bash infrastructure/docker/compose.sh up -d

This brings up the database, object store, ingestion, model, Dagster, and
frontend services. See ``infrastructure/docker/README.md`` for prerequisites and
environment files. On on-premise servers where Docker is not available, the
platform runs under **Singularity / Apptainer**; see
``infrastructure/singularity/README.md``.

Orchestration
-------------

**Dagster** connects all parts of the system — ingestion, processing, quality
checks, feature engineering, and anomaly scoring. Rather than starting each step
by hand, it runs them in the right order, tracks success and failure, and makes
it possible to re-run only the parts that need fixing. This turns the project
from a set of separate scripts into an automated system that moves new or
historical data from raw records to dashboard-ready results.

Development practices
---------------------

Development follows standard software-engineering practices to keep the work
reproducible and maintainable:

* **Version control** — Git and GitHub, with a pre-commit hook that runs checks before each commit.
* **Linting and types** — Ruff (lint and format) and mypy for Python; SQLFluff for SQL.
* **Tests** — a pytest suite mirroring the main code areas (ingestion, processing, features, modeling, and database).
* **Documentation** — kept alongside the code, including this Sphinx build and per-area README files.

Combined with the containerized deployment and Dagster's ability to re-run
individual steps, these practices keep the pipeline reproducible and its results
auditable.

Technical stack
---------------

* **Backend** — Python and Poetry; pandas/NumPy, scikit-learn/statsmodels, BirdNET (birdnetlib), Perch v2, PyTorch/TensorFlow/Hugging Face Transformers, librosa/soundfile/FFmpeg, and xarray/netCDF4.
* **Storage and orchestration** — TimescaleDB/PostgreSQL, MinIO, and Dagster.
* **Frontend** — Next.js, React, and TypeScript; Tailwind CSS, MapLibre GL / react-map-gl, and Recharts.
* **Runtime** — Docker / Docker Compose for local development and cloud deployment; Singularity / Apptainer on-premise.
