const DEFAULT_DAGSTER_UI_URL = "http://localhost:13030";

const FLOW_TO_ASSET: Record<string, string> = {
  birdweather_ingest: "birdweather_raw_data",
  knmi_ingest: "knmi_raw_data",
  seismic_ingest: "seismic_raw_data",
  ebird_ingest: "ebird_raw_data",
  movebank_ingest: "movebank_raw_data",
  earth_engine_ingest: "earth_engine_raw_data",
  earth_engine_grid_ingest: "earth_engine_grid_raw_data",
  cds_drought_ingest: "cds_drought_raw_data",
  sovon_ingest: "sovon_raw_data",
};

const RANGE_FLOW_KEYS = new Set([
  "birdweather_catchup",
  "birdweather_repair",
  "knmi_repair",
  "ebird_repair",
  "movebank_repair",
  "earth_engine_grid_repair",
  "cds_drought_catchup",
  "historical_catchup",
]);

const POSTPROCESSING_JOB_NAMES: Record<string, string> = {
  station_summary_refresh: "station_summary_refresh",
  derived_aggregates_refresh: "derived_aggregates_refresh",
  quality_audit: "quality_audit",
  birdweather_raw_audio_ingestion: "birdweather_raw_audio_ingestion_job",
  birdweather_audio_processing: "birdweather_audio_processing_job",
  birdweather_feature_engineering: "birdweather_feature_engineering_job",
  model_anomaly_scoring: "model_anomaly_scoring_job",
  birdweather_audio_feature_pipeline: "birdweather_audio_feature_pipeline",
};

const MODEL_LOCATION_FLOW_KEYS = new Set([
  "birdweather_feature_engineering",
  "model_anomaly_scoring",
]);

export const DAGSTER_REPOSITORY_NAME = "__repository__";

export const DAGSTER_LOCATION_NAMES = Array.from(
  new Set([
    process.env.DAGSTER_LOCATION_NAME || "pipelines",
    process.env.DAGSTER_MODEL_LOCATION_NAME || "models",
  ]),
);

function dagsterBaseUrl(): string {
  return (
    process.env.DAGSTER_GRAPHQL_URL ||
    process.env.NEXT_PUBLIC_DAGSTER_GRAPHQL_URL ||
    DEFAULT_DAGSTER_UI_URL
  );
}

function dagsterGraphQLEndpoint(): string {
  const baseUrl = dagsterBaseUrl().replace(/\/$/, "");
  return baseUrl.endsWith("/graphql") ? baseUrl : `${baseUrl}/graphql`;
}

function filteredConfig(
  parameters: Record<string, unknown>,
  allowedKeys: Set<string>,
): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(parameters).filter(
      ([key, value]) => allowedKeys.has(key) && value !== null && value !== undefined,
    ),
  );
}

function singleAssetRunConfig(
  assetName: string,
  parameters: Record<string, unknown>,
  allowedKeys: Set<string>,
): Record<string, unknown> {
  const config = filteredConfig(parameters, allowedKeys);
  return Object.keys(config).length === 0
    ? {}
    : {
        ops: {
          [assetName]: {
            config,
          },
        },
      };
}

export function deploymentToScheduleName(deploymentName: string): string {
  return deploymentName.replace(/-/g, "_");
}

export function flowKeyToLocationName(flowKey: string): string {
  return MODEL_LOCATION_FLOW_KEYS.has(flowKey)
    ? process.env.DAGSTER_MODEL_LOCATION_NAME || "models"
    : process.env.DAGSTER_LOCATION_NAME || "pipelines";
}

export function flowKeyToJobName(flowKey: string): string {
  if (flowKey in POSTPROCESSING_JOB_NAMES) {
    return POSTPROCESSING_JOB_NAMES[flowKey];
  }
  return `${flowKey}_job`;
}

export function buildDagsterRunConfig(
  flowKey: string,
  parameters: Record<string, unknown>,
): Record<string, unknown> {
  if (flowKey === "birdweather_raw_audio_ingestion") {
    return {
      ops: {
        birdweather_audio_download_queue: {
          config: filteredConfig(
            parameters,
            new Set(["lookback_days", "limit", "force"]),
          ),
        },
        birdweather_raw_audio_objects: {
          config: filteredConfig(
            parameters,
            new Set(["lookback_days", "limit", "force", "workers"]),
          ),
        },
      },
    };
  }

  if (flowKey === "birdweather_audio_processing") {
    return singleAssetRunConfig(
      "birdweather_audio_processing",
      parameters,
      new Set(["lookback_days", "limit", "force"]),
    );
  }

  if (flowKey === "birdweather_feature_engineering") {
    return singleAssetRunConfig(
      "birdweather_feature_engineering",
      parameters,
      new Set([
        "lookback_days",
        "limit",
        "force",
        "background_device",
        "background_top_k",
        "background_min_confidence",
      ]),
    );
  }

  if (flowKey === "model_anomaly_scoring") {
    return singleAssetRunConfig(
      "model_anomaly_scores",
      parameters,
      new Set([
        "history_weeks",
        "score_weeks",
        "seasonal_window_weeks",
        "min_location_history",
        "min_seasonal_history",
        "target_species_only",
        "include_sovon_context",
        "include_embedding_drift",
        "embedding_min_confidence",
        "embedding_min_baseline_clips",
        "embedding_min_week_clips",
        "embedding_k_neighbors",
        "embedding_max_rows",
        "review_queue_top_k",
      ]),
    );
  }

  if (flowKey === "birdweather_audio_feature_pipeline") {
    return {
      ops: {
        birdweather_audio_download_queue: {
          config: filteredConfig(
            parameters,
            new Set(["lookback_days", "limit", "force"]),
          ),
        },
        birdweather_raw_audio_objects: {
          config: filteredConfig(
            parameters,
            new Set(["lookback_days", "limit", "force", "workers"]),
          ),
        },
        birdweather_audio_processing: {
          config: filteredConfig(
            parameters,
            new Set(["lookback_days", "limit", "force"]),
          ),
        },
        birdweather_feature_engineering: {
          config: filteredConfig(
            parameters,
            new Set([
              "lookback_days",
              "limit",
              "force",
              "background_device",
              "background_top_k",
              "background_min_confidence",
            ]),
          ),
        },
      },
    };
  }

  if (flowKey === "derived_aggregates_refresh") {
    const maxPoints = Number(parameters.max_points);
    return {
      ops: {
        movebank_tracks: {
          config: {
            max_points: Number.isFinite(maxPoints) ? maxPoints : 500,
          },
        },
      },
    };
  }

  if (flowKey in POSTPROCESSING_JOB_NAMES) {
    return {};
  }

  if (RANGE_FLOW_KEYS.has(flowKey)) {
    return {
      ops: {
        [flowKey]: {
          config: Object.fromEntries(
            Object.entries(parameters).filter(([, value]) => value !== null && value !== undefined),
          ),
        },
      },
    };
  }

  if (flowKey in FLOW_TO_ASSET) {
    const assetName = FLOW_TO_ASSET[flowKey];
    const config = Object.fromEntries(
      Object.entries(parameters).filter(([, value]) => value !== null && value !== undefined),
    );

    return Object.keys(config).length === 0
      ? {}
      : {
          ops: {
            [assetName]: {
              config,
            },
          },
        };
  }

  return {};
}

export async function dagsterGraphQL<Response>(
  source: string,
  variables: Record<string, unknown>,
  timeoutMs = 10_000,
): Promise<Response> {
  const response = await fetch(dagsterGraphQLEndpoint(), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ query: source, variables }),
    cache: "no-store",
    signal: AbortSignal.timeout(timeoutMs),
  });

  if (!response.ok) {
    throw new Error(`Dagster GraphQL request failed (HTTP ${response.status}).`);
  }

  const body = (await response.json()) as {
    data?: Response;
    errors?: Array<{ message?: string }>;
  };

  if (body.errors && body.errors.length > 0) {
    throw new Error(
      body.errors
        .map((error) => error.message || "Unknown Dagster GraphQL error")
        .join("; "),
    );
  }

  if (!body.data) {
    throw new Error("Dagster GraphQL returned no data.");
  }

  return body.data;
}
