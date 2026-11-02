import { createHash } from "node:crypto";

import { Pool, type PoolConfig, type QueryResult, type QueryResultRow } from "pg";

import {
  captureOperationSample,
  computeThroughput,
  finalizeOperationSample,
  getOperationDurationMs,
  performanceErrorDetails,
  recordPerformanceEvent,
} from "@/lib/performance/server";

type GlobalPoolState = typeof globalThis & {
  __avianFrontendPool?: Pool;
};

function parsePort(value: string | undefined, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function resolvePoolConfig(): PoolConfig {
  const connectionString = process.env.DATABASE_URL?.trim();
  const baseConfig = {
    max: 10,
    idleTimeoutMillis: 30_000,
    connectionTimeoutMillis: 10_000,
    allowExitOnIdle: true,
    // Hard ceiling so no single query can wedge a pooled connection (and
    // thereby starve every other route). Dashboard queries are sub-second;
    // 30s is generous headroom while still failing fast on pathological plans.
    statement_timeout: 30_000,
    query_timeout: 30_000,
  } satisfies PoolConfig;

  if (connectionString) {
    return {
      connectionString,
      ...baseConfig,
    };
  }

  return {
    host: process.env.PGHOST || process.env.POSTGRES_HOST,
    port: parsePort(process.env.PGPORT || process.env.TSDB_PORT, 5432),
    user: process.env.PGUSER || process.env.POSTGRES_USER || "root",
    password: process.env.PGPASSWORD ?? process.env.POSTGRES_PASSWORD ?? "",
    database:
      process.env.PGDATABASE || process.env.POSTGRES_DB || "avian_biosurveillance",
    ...baseConfig,
  };
}

const globalPoolState = globalThis as GlobalPoolState;

function normalizeSql(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

function sqlFingerprint(text: string): { fingerprint: string; sample: string } {
  const sample = normalizeSql(text).slice(0, 240);
  return {
    fingerprint: createHash("sha1").update(sample).digest("hex").slice(0, 16),
    sample,
  };
}

function paramsByteLength(params: readonly unknown[] | undefined): number {
  if (!params || params.length === 0) {
    return 0;
  }

  try {
    return Buffer.byteLength(JSON.stringify(params), "utf8");
  } catch {
    return 0;
  }
}

export const pool =
  globalPoolState.__avianFrontendPool ?? new Pool(resolvePoolConfig());

if (process.env.NODE_ENV !== "production") {
  globalPoolState.__avianFrontendPool = pool;
}

export function query<Row extends QueryResultRow = QueryResultRow>(
  text: string,
  params?: readonly unknown[],
): Promise<QueryResult<Row>> {
  const startedAt = process.hrtime.bigint();
  const sample = captureOperationSample();
  const sql = sqlFingerprint(text);
  const inputBytes = Buffer.byteLength(text, "utf8") + paramsByteLength(params);

  return pool
    .query<Row>(text, params as unknown[] | undefined)
    .then((result) => {
      const durationMs = getOperationDurationMs(startedAt);

      recordPerformanceEvent({
        eventKind: "db_query",
        surface: "db",
        operation: "postgres_query",
        target: `sha1:${sql.fingerprint}`,
        durationMs,
        throughputPerSecond: computeThroughput(result.rowCount ?? null, durationMs),
        rowCount: result.rowCount ?? result.rows.length,
        bytesWritten: inputBytes,
        metadata: {
          queryFingerprint: sql.fingerprint,
          querySample: sql.sample,
          parameterCount: params?.length ?? 0,
        },
        success: true,
        ...finalizeOperationSample(sample),
      });

      return result;
    })
    .catch((error: unknown) => {
      const durationMs = getOperationDurationMs(startedAt);
      const { errorName, errorMessage } = performanceErrorDetails(error);

      recordPerformanceEvent({
        eventKind: "db_query",
        surface: "db",
        operation: "postgres_query",
        target: `sha1:${sql.fingerprint}`,
        durationMs,
        bytesWritten: inputBytes,
        success: false,
        errorName,
        errorMessage,
        metadata: {
          queryFingerprint: sql.fingerprint,
          querySample: sql.sample,
          parameterCount: params?.length ?? 0,
        },
        ...finalizeOperationSample(sample),
      });

      throw error;
    });
}