import { Client } from "minio";

type EndpointConfig = {
  endPoint: string;
  port: number;
  useSSL: boolean;
};

function parseBoolean(value: string | undefined): boolean | undefined {
  if (!value) return undefined;
  const normalized = value.trim().toLowerCase();
  if (["1", "true", "yes", "on"].includes(normalized)) return true;
  if (["0", "false", "no", "off"].includes(normalized)) return false;
  return undefined;
}

function parseEndpoint(
  endpoint: string | undefined,
  fallback: EndpointConfig,
): EndpointConfig {
  const raw = endpoint?.trim();
  if (!raw) {
    return fallback;
  }

  const normalized = raw.includes("://") ? raw : `http://${raw}`;

  try {
    const url = new URL(normalized);
    return {
      endPoint: url.hostname,
      port: url.port ? Number(url.port) : url.protocol === "https:" ? 443 : 80,
      useSSL: url.protocol === "https:",
    };
  } catch {
    return {
      endPoint: raw,
      port: fallback.port,
      useSSL: fallback.useSSL,
    };
  }
}

const defaultBrowserEndpoint: EndpointConfig = {
  endPoint: "127.0.0.1",
  port: 19000,
  useSSL: false,
};

const internalEndpointConfig = parseEndpoint(
  process.env.MINIO_ENDPOINT,
  defaultBrowserEndpoint,
);

const browserEndpointConfig = parseEndpoint(
  process.env.MINIO_PUBLIC_ENDPOINT || process.env.NEXT_PUBLIC_MINIO_ENDPOINT,
  defaultBrowserEndpoint,
);

const minioRegion =
  process.env.MINIO_REGION || process.env.AWS_REGION || "us-east-1";

function createMinioClient(endpointConfig: EndpointConfig) {
  return new Client({
    endPoint: endpointConfig.endPoint,
    port: endpointConfig.port,
    useSSL: parseBoolean(process.env.MINIO_USE_SSL) ?? endpointConfig.useSSL,
    region: minioRegion,
    accessKey:
      process.env.MINIO_ACCESS_KEY || process.env.MINIO_ROOT_USER || "minioadmin",
    secretKey:
      process.env.MINIO_SECRET_KEY ||
      process.env.MINIO_ROOT_PASSWORD ||
      "minioadmin",
  });
}

export const minioClient = createMinioClient(internalEndpointConfig);

const browserMinioClient = createMinioClient(browserEndpointConfig);

export async function presignedGetObjectUrl(
  bucket: string,
  objectPath: string,
  expiresSeconds = 60 * 60,
): Promise<string> {
  return browserMinioClient.presignedGetObject(
    bucket,
    objectPath,
    expiresSeconds,
  );
}

export async function objectExists(
  bucket: string,
  objectPath: string,
): Promise<boolean> {
  try {
    await minioClient.statObject(bucket, objectPath);
    return true;
  } catch (error) {
    const code =
      typeof error === 'object' && error && 'code' in error
        ? String((error as { code?: unknown }).code ?? '')
        : '';
    if (
      code === 'NotFound' ||
      code === 'NoSuchKey' ||
      code === 'NoSuchObject'
    ) {
      return false;
    }
    throw error;
  }
}

const DATE_FOLDER_RE = /(\d{4}-\d{2}-\d{2})$/;

/**
 * Find the most recent date partition that actually has processed clips, by
 * listing the date "folders" under `<parentPrefix>` in the processed-audio
 * bucket. This is a cheap object-storage LIST (delimiter-based, common
 * prefixes only) and is used to anchor the live feed on the newest *playable*
 * data — the ingest/processing backfill runs ahead of the station-summary
 * table, and a MAX(detected_at) scan on the hypertable is too slow under
 * backfill IO load to use at request time.
 *
 * Returns a `YYYY-MM-DD` string, or null if nothing is found / storage is
 * unavailable.
 */
export async function latestProcessedAudioDate(
  bucket = "processed-audio",
  parentPrefix = "birdweather/",
  timeoutMs = 2500,
): Promise<string | null> {
  return new Promise((resolve) => {
    let latest: string | null = null;
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      resolve(latest);
    };

    // Never let a stalled object-storage LIST hold up the caller (e.g. the live
    // feed route). Fail fast to whatever we have (usually null → DB fallback).
    const timer = setTimeout(finish, timeoutMs);
    if (typeof timer.unref === "function") timer.unref();

    const stream = minioClient.listObjectsV2(bucket, parentPrefix, false);

    const consider = (key: string | undefined) => {
      if (!key) return;
      const match = DATE_FOLDER_RE.exec(key.replace(/\/+$/, ""));
      if (match) {
        const date = match[1];
        // ISO YYYY-MM-DD sorts lexicographically.
        if (!latest || date > latest) latest = date;
      }
    };

    stream.on("data", (obj) => consider(obj.prefix ?? obj.name));
    stream.on("error", () => {
      clearTimeout(timer);
      finish();
    });
    stream.on("end", () => {
      clearTimeout(timer);
      finish();
    });
    stream.on("close", () => {
      clearTimeout(timer);
      finish();
    });
  });
}

/**
 * Find the most recent date partition that has fully materialized visual
 * artifacts (spectrogram + waveform), by listing the date "folders" under
 * `spectrograms/birdweather/` in the features bucket. Spectrograms and
 * waveforms are materialized together, so the spectrogram prefix is a faithful
 * proxy for "this day has all visuals". Like {@link latestProcessedAudioDate}
 * this is a cheap, delimiter-based LIST (common prefixes only) used to anchor
 * the Explore inventory on the newest data that already has audio + visuals,
 * instead of scanning the (unindexed-for-this-predicate) hypertable.
 *
 * Returns a `YYYY-MM-DD` string, or null if nothing is found / storage is
 * unavailable.
 */
export async function latestVisualArtifactDate(
  bucket = "features",
  parentPrefix = "spectrograms/birdweather/",
  timeoutMs = 2500,
): Promise<string | null> {
  return latestProcessedAudioDate(bucket, parentPrefix, timeoutMs);
}
