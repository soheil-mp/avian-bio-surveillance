# Singularity / Apptainer — Rootless on-prem ETL deployment

This directory contains the **server-tested** Singularity / Apptainer workflow
for running the Avian Biosurveillance **ETL stack** on an on-prem server
with **rootless access**.

The workflow has been validated on an on-prem Linux host with Singularity CE 3.11
using an isolated sandbox under the project directory.

## What runs in the minimal ETL stack

For on-prem ingestion you only need these four services:

| Service | Why it exists |
|---|---|
| `timescaledb` | TimescaleDB / PostgreSQL for application data and Dagster state |
| `minio` | MinIO object storage for audio artifacts |
| `dagster-webserver` | Dagster UI + GraphQL endpoint |
| `dagster-daemon` | Dagster scheduler, sensors, and run-queue worker |
| `orchestration` | Hosts the Dagster gRPC code-server (registers all jobs/schedules/sensors) |

`frontend` remains optional and is **not** part of the recommended
rootless ETL workflow.

## Host safety at a glance

- **Rootless only:** no Docker daemon or root privileges are required on the server.
- **Loopback-only listeners:** `start.sh etl` binds ETL services to `127.0.0.1`,
    not `0.0.0.0`.
- **Project-local homes and caches:** `start.sh` overrides writable service
    homes, Python/Node caches, and temp directories so runtime writes do not
    fall back to the host user's `~/.cache`, `~/.config`, or `/tmp`.
- **No blanket shutdowns:** `start.sh` does not stop anything. `stop.sh` only
    targets the instance names managed by this project.
- **Instance names are explicit:** all managed instances are prefixed with `ab-`
    (`ab-timescaledb`, `ab-minio`, `ab-dagster-webserver`, `ab-dagster-daemon`, `ab-orchestration`, etc.).
- **Logs are plain files:** service output goes to
    `infrastructure/singularity/volumes/logs/ab-*.log` for easy inspection.

> **Can `stop.sh` stop unrelated Singularity instances?**
>
> No. The scripts do **not** enumerate or stop arbitrary Singularity instances.
> They only start / stop the `ab-*` names defined in `start.sh` and `stop.sh`.

## Why the wrapper scripts look unusual

Singularity CE `instance start` only launches `sinit`; it does **not** execute the
Docker `CMD` / `ENTRYPOINT`. Because of that, `start.sh`:

1. creates the instance,
2. then explicitly runs the real service process with `exec instance://...`,
3. and redirects logs to `infrastructure/singularity/volumes/logs/ab-*.log`.

This is why a healthy ETL start involves both:

- a running instance in `singularity instance list`, and
- a live process writing to the corresponding log file.

## Required environment-file behavior

The runtime environment file stays on the **host**. It is **not baked into the images**.

By default the scripts read `REPO_ROOT/.env`, but both `start.sh` and `build.sh`
also support:

- `--env-file /path/to/server.env`
- `AVIAN_ENV_FILE=/path/to/server.env`

That means you can keep one stable secret file **outside** the repo / sandbox and
point every sandbox at the same path.

### Auto-decrypt from `.env.enc`

If `.env` is missing but `.env.enc` exists in the repo root, both `start.sh` and
`build.sh` will automatically attempt to decrypt it using
`scripts/encryption/env_crypto.py`. The scripts try `poetry` → `python3` → `python`
in that order. If no Python runtime is available, they fall back to the original
error message and suggest manual decryption.

```bash
# Explicit decrypt (if auto-decrypt is not desired)
python3 scripts/encryption/env_crypto.py decrypt
```

`build.sh --deploy` also transfers the encryption scripts to the server so
auto-decrypt works there too.

Key values used by the Singularity workflow include:

| Variable | Purpose |
|---|---|
| `DOCKERHUB_NAMESPACE` | Namespace for custom OCI images |
| `SINGULARITY_IMAGE_TAG` | Immutable tag used by `build.sh --from-registry` |
| `ONPREM_ENV_FILE` | Optional stable server-side destination path for `build.sh --sync-env` |
| `TSDB_PORT`, `MINIO_API_PORT`, `MINIO_CONSOLE_PORT`, `DAGSTER_PORT` | Host ports for ETL services |
| `ONPREM_SSH_USER`, `ONPREM_SSH_PASSWORD`, `ONPREM_WORKDIR` | Used by `--sync-env` / `--deploy` helpers |
| DB / MinIO / API secrets | Passed into the running services at startup |

Use **immutable tags** such as `sha-<gitsha>` or a dated release tag. Avoid
`latest` for reproducible on-prem deployments.

## Image sources

| SIF | Source |
|---|---|
| `timescaledb.sif` | `timescale/timescaledb:2.17.2-pg16` |
| `minio.sif` | `minio/minio:RELEASE.2025-09-07T16-13-09Z` |
| `dagster.sif` | `<DOCKERHUB_NAMESPACE>/avian-biosurveillance-dagster:<SINGULARITY_IMAGE_TAG>` |
| `orchestration.sif` | `<DOCKERHUB_NAMESPACE>/avian-biosurveillance-ingestion:<SINGULARITY_IMAGE_TAG>` |

If the custom images are private, authenticate once on the server before pulling:

```bash
apptainer registry login docker://docker.io
```

## Port safety on the server

Common ports such as `5432` may already be in use. The ETL stack defaults to a
safe `1xxxx` range:

| Service | Default server port | `.env` variable |
|---|---:|---|
| TimescaleDB | `15432` | `TSDB_PORT` |
| MinIO API | `19000` | `MINIO_API_PORT` |
| MinIO Console | `19001` | `MINIO_CONSOLE_PORT` |
| Dagster | `13030` | `DAGSTER_PORT` |

Check the ETL ports before starting:

```bash
bash infrastructure/singularity/start.sh --check-ports etl
```

If a conflict appears, update the relevant value in `.env` and rerun the check.

## Recommended deployment workflow

Use OCI images as the source of truth and build the SIF files on the server.

```
Laptop / CI
    └─ publish custom images (Dagster + orchestration)
                ↓
Server sandbox
    ├─ reference stable host env file
    ├─ build.sh --from-registry   (or reuse existing .sif files)
    └─ start.sh etl
```

Fallback path: if the server cannot reach Docker Hub, you can still build SIFs
locally with `build.sh --from-local` and copy them with `build.sh --deploy`.

## Step-by-step (recommended and server-tested)

### 1. Publish the custom images

On your laptop or CI runner, publish the two custom ETL images:

```bash
./scripts/tooling/publish_docker_images.sh -n soheilmp -s dagster,ingestion -t sha-abc123
```

The tag should match `SINGULARITY_IMAGE_TAG` in the server env file.

### 2. Sync the environment file to the server

The runtime env file is gitignored, so sync it explicitly. Recommended: copy it
to a stable path outside any sandbox (for example `/path/to/server.env`):

```bash
bash infrastructure/singularity/build.sh --sync-env --remote-env-file /path/to/server.env
```

This reads `ONPREM_SSH_USER`, `ONPREM_SSH_PASSWORD`, and `ONPREM_WORKDIR`
from your local env file and copies the selected file to the server. If
`ONPREM_ENV_FILE` is set, `--sync-env` uses that as the default server path.

### 3. Create an isolated server sandbox

Avoid deploying directly from a working checkout. Instead, create a sandbox
under the project directory and unpack a clean tree there:

```bash
cd /path/to/avian-biosurveillance
git fetch origin

SANDBOX=".server-sandbox/master-etl-$(date +%Y%m%d)"
mkdir -p "$SANDBOX"

git archive origin/master \
    pyproject.toml \
    README.md \
    config \
    data/reference \
    db \
    infrastructure/singularity \
    src \
    | tar -x -C "$SANDBOX"
```

When the env file lives outside the sandbox, point startup at it explicitly:

```bash
cd "$SANDBOX"
bash infrastructure/singularity/start.sh --env-file /path/to/server.env etl
```

If you prefer shell-level defaults, export `AVIAN_ENV_FILE=/path/to/server.env`
once before running `build.sh` / `start.sh`.

> **Note:** a nested sandbox is **not** its own Git repository. Running
> `git status` inside it can walk up to the parent checkout and report changes
> from the outer repo instead of the sandbox contents.

### 4. Build the SIF images on the server

From inside the sandbox:

```bash
cd "$SANDBOX"
bash infrastructure/singularity/build.sh --from-registry
```

By default this builds the ETL stack only:

- `timescaledb`
- `minio`
- `dagster`
- `orchestration`

You can narrow it further if needed:

```bash
bash infrastructure/singularity/build.sh --from-registry --only dagster orchestration
```

### 5. Optional: reuse already-built SIFs

If you already have a validated sandbox, you can copy the SIFs instead of rebuilding:

```bash
mkdir -p infrastructure/singularity/sif
cp -p ../feat8-etl/infrastructure/singularity/sif/*.sif infrastructure/singularity/sif/
```

This was successfully used during server validation.

### 6. Start the ETL stack

```bash
bash infrastructure/singularity/start.sh --env-file /path/to/server.env --check-ports etl
bash infrastructure/singularity/start.sh --env-file /path/to/server.env etl
```

Startup order:

1. `timescaledb`
2. `minio`
3. readiness checks
4. database roles + schema init + MinIO bucket init
5. `orchestration` (gRPC code-server)
6. `dagster-webserver`
7. `dagster-daemon`
6. `orchestration`

On the validated server run, approximate readiness times were:

- TimescaleDB SQL readiness: ~34s
- Dagster webserver health readiness: ~10s after the gRPC code-server is reachable

## Logs, health checks, and how to confirm orchestration is running

### Quick status

```bash
bash infrastructure/singularity/start.sh status
singularity instance list
```

These confirm whether the instances exist and show the expected log file paths.

### Data orchestration log

The orchestration process writes to:

```bash
infrastructure/singularity/volumes/logs/ab-orchestration.log
```

Useful commands:

```bash
tail -f infrastructure/singularity/volumes/logs/ab-orchestration.log
tail -n 200 infrastructure/singularity/volumes/logs/ab-orchestration.log
grep -E "\[serve\]|ERROR|Traceback|Exception" infrastructure/singularity/volumes/logs/ab-orchestration.log
```

What healthy startup usually looks like:

- `Dagster code-server listening on 0.0.0.0:4000`
- `Loaded code location 'avian_biosurveillance' with N jobs / N schedules`
- optional schedule-fire messages from the daemon log

Important: after startup, the orchestration log may become **quiet**. That is
normal — the gRPC code-server sits idle while the Dagster daemon polls it for
schedule ticks.

To confirm it is still alive:

```bash
singularity instance list | grep ab-orchestration
```

If `ab-orchestration` is still listed and
`infrastructure/singularity/volumes/logs/ab-orchestration.log` contains no
`Traceback`, `Exception`, or fatal error, the orchestration server is usually healthy.

### BirdWeather temporary files

Yes — BirdWeather may temporarily stage audio files before uploading them to MinIO.
In `target="database"` mode it uses Python's `tempfile.TemporaryDirectory`, which
defaults to the process temp directory if `TMPDIR` is not set.

For the Singularity ETL wrapper, `start.sh` now redirects that temp area to a
project-local bind mount instead of the host system temp directory:

```text
infrastructure/singularity/volumes/orchestration-tmp/  ->  /app/data/tmp
```

This avoids exhausting the server's system temp filesystem during large
BirdWeather runs. If you see errors like below, the orchestration instance is
still using the old wrapper and should be restarted after copying the updated
`start.sh`:

```text
OSError: [Errno 28] No space left on device: '<system-temp>/birdweather_...'
```

The wrapper also redirects the orchestration service's effective home and
cache/config roots to a project-local runtime home:

```text
infrastructure/singularity/volumes/orchestration-home/  ->  /home/appuser
```

This keeps libraries such as Hugging Face Transformers, Torch, Matplotlib,
Google auth helpers, and other XDG-aware tools from writing to the host user's
home directory outside the project tree.

### Other service logs

```bash
tail -f infrastructure/singularity/volumes/logs/ab-timescaledb.log
tail -f infrastructure/singularity/volumes/logs/ab-minio.log
tail -f infrastructure/singularity/volumes/logs/ab-dagster-webserver.log
tail -f infrastructure/singularity/volumes/logs/ab-dagster-daemon.log
tail -f infrastructure/singularity/volumes/logs/ab-frontend.log
```

For Dagster specifically, you can also verify the webserver health endpoint:

```bash
curl -sf http://127.0.0.1:${DAGSTER_PORT:-13030}/server_info
```

## Stopping the stack

Stop only the ETL services:

```bash
bash infrastructure/singularity/stop.sh etl
```

Stop the full stack:

```bash
bash infrastructure/singularity/stop.sh all
```

This stops services in reverse dependency order. The ETL stop order is:

1. `ab-frontend`
2. `ab-dagster-daemon`
3. `ab-dagster-webserver`
4. `ab-orchestration`
5. `ab-minio`
6. `ab-timescaledb`

## Troubleshooting notes

- **Passwords or JSON values with commas / punctuation:** on Singularity CE 3.11,
    `--env` can be parsed incorrectly. The wrapper uses temporary `--env-file`
    files instead.
- **Port open is not the same as DB ready:** `start.sh` waits for PostgreSQL SQL
    readiness with `pg_isready`, not just a listening socket.
- **Dagster needs writable runtime paths:** the script binds writable directories
    for `DAGSTER_HOME` (storage of compute logs, run history index, schedule
    cursor state) shared between the webserver and daemon containers.
- **Orchestration writes into `data/`:** because the `.sif` filesystem is read-only,
    `start.sh` must bind writable host paths for `/app/data/raw`,
    `/app/data/processed`, and `/app/data/quality`. If you see
    `OSError: [Errno 30] Read-only file system: 'data/raw/...'` in
    `infrastructure/singularity/volumes/logs/ab-orchestration.log`, the orchestration container is missing those
    writable binds.
- **BirdWeather database mode uses temporary staging:** if `TMPDIR` is unset,
    Python temp files land under the host system temp directory, which can fill up on large runs.
    The Singularity wrapper now binds `/app/data/tmp` to
    `infrastructure/singularity/volumes/orchestration-tmp/` and sets
    `TMPDIR`, `TEMP`, and `TMP` accordingly.
- **Host home leakage under Apptainer:** by default, Apptainer can expose the
    invoking user's home directory inside the container. `start.sh` now assigns
    project-local service homes (for example `volumes/orchestration-home/` →
    `/home/appuser`, `volumes/dagster-home/` → `/opt/dagster_home`) so runtime
    caches and config files stay inside the project folder.
- **Optional app service readiness file:** `frontend/docker-entrypoint.sh` now
    honours `APP_READY_FILE`; the Singularity wrapper points it at a
    project-local path instead of a system temp ready-file location.
- **Station summary refresh needs owner-level DB credentials:**
    `refresh_continuous_aggregate('birdweather_daily_cagg_tbl', ...)`
    requires ownership of the continuous aggregate view. `start.sh` now injects
    `STATION_SUMMARY_DB_*` using the same `root` credentials so
    `station-summary-refresh` does not fail with
    `psycopg2.errors.InsufficientPrivilege: must be owner of view birdweather_daily_cagg_tbl`.
- **Large builds can fill the system temp directory:** `build.sh` defaults Singularity / Apptainer
    cache and temp directories to local project paths:
    `infrastructure/singularity/.cache/` and `infrastructure/singularity/.tmp/`.
- **WSL / Apptainer local runs may need Timescale tune disabled:** if
    `timescaledb-tune` crashes during first boot with cgroup / memory-detection
    errors, rerun with `DISABLE_TIMESCALEDB_TUNE=1` to bind the local
    `disable_timescaledb_tune.sh` compatibility helper as a no-op override for
    `/docker-entrypoint-initdb.d/001_timescaledb_tune.sh`. This is intended as a
    local compatibility workaround; server-tested deployments should not need it.
- **A fresh archive still behaving like older scripts?** Verify these markers:

    ```bash
    grep -nE "wait_for_postgres|dagster-home|--env-file|HAS_ONLY|SINGULARITY_TMPDIR|SINGULARITY_CACHEDIR" \\
        infrastructure/singularity/build.sh infrastructure/singularity/start.sh
    ```

    If they are missing, compare or replace `start.sh`, `build.sh`, and `.gitignore`
    from a known-good validated sandbox before retrying.

## Files

```text
infrastructure/singularity/
├── build.sh        # Registry/local image → SIF conversion, plus optional deploy helpers
├── start.sh        # Start ETL or full stack with readiness checks
├── stop.sh         # Stop ETL or full stack in reverse dependency order
├── .gitignore      # Excludes generated SIFs, volumes, cache, and temp files
├── README.md       # This file
├── sif/            # Generated .sif images (not committed)
├── volumes/        # Runtime persistent data (not committed)
├── .cache/         # Local Singularity / Apptainer cache (not committed)
└── .tmp/           # Local Singularity / Apptainer build scratch (not committed)
```
