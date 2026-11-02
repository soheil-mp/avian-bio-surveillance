import "server-only";

import { AsyncLocalStorage } from "node:async_hooks";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, relative, resolve } from "node:path";

import { headers } from "next/headers";

import {
  PERFORMANCE_CSV_COLUMNS,
  REQUEST_ID_HEADER,
  REQUEST_METHOD_HEADER,
  REQUEST_PATH_HEADER,
  type PerformanceEventInput,
  type PerformanceEventRow,
  type PerformanceSurface,
} from "@/lib/performance/shared";

type RequestScopedSurface = Extract<PerformanceSurface, "api" | "page" | "action">;

type RequestContext = {
  requestId: string;
  spanId: string;
  path: string | null;
  method: string | null;
  surface: RequestScopedSurface;
};

type ServerSample = {
  cpuUsage: NodeJS.CpuUsage;
  memoryUsage: NodeJS.MemoryUsage;
};

type GlobalPerformanceState = typeof globalThis & {
  __avianPerformanceWriteQueue?: Promise<void>;
  __avianPerformancePendingRows?: string[][];
};

const requestContextStore = new AsyncLocalStorage<RequestContext>();
const globalPerformanceState = globalThis as GlobalPerformanceState;

const frontendRoot = process.cwd();
const parentOfFrontendRoot = resolve(frontendRoot, "..");
const repoRoot = existsSync(resolve(parentOfFrontendRoot, "pyproject.toml"))
  ? parentOfFrontendRoot
  : frontendRoot;
const runtimeRoot = process.env.APP_READY_FILE?.trim()
  ? dirname(process.env.APP_READY_FILE.trim())
  : resolve(frontendRoot, ".runtime");
const performanceMetricsExportPath =
  process.env.PERFORMANCE_METRICS_EXPORT_PATH?.trim() ||
  process.env.PERFORMANCE_AUDIT_CSV_PATH?.trim() ||
  resolve(
    repoRoot === parentOfFrontendRoot ? repoRoot : runtimeRoot,
    "performance_metrics_export.csv",
  );
const csvHeaderLine = `${PERFORMANCE_CSV_COLUMNS.join(",")}\n`;
const durationColumnIndex = PERFORMANCE_CSV_COLUMNS.indexOf("duration_ms");

function createRequestContext(
  surface: RequestScopedSurface,
  values: Pick<RequestContext, "path" | "method"> & { requestId?: string | null },
): RequestContext {
  return {
    requestId: values.requestId?.trim() || randomUUID(),
    spanId: randomUUID(),
    path: values.path ?? null,
    method: values.method ?? null,
    surface,
  };
}

function resolveRequestPath(request: Request): string {
  if ("nextUrl" in request) {
    const nextUrl = (request as Request & { nextUrl?: URL }).nextUrl;
    if (nextUrl instanceof URL) {
      return nextUrl.pathname;
    }
  }

  try {
    return new URL(request.url).pathname;
  } catch {
    return request.url;
  }
}

function finiteNumberOrNull(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function deriveThroughput(
  quantity: number | null | undefined,
  durationMs: number | null | undefined,
): number | null {
  if (!Number.isFinite(quantity) || !Number.isFinite(durationMs) || !durationMs || durationMs <= 0) {
    return null;
  }

  return Number(((quantity ?? 0) / (durationMs / 1000)).toFixed(3));
}

function captureServerSample(): ServerSample {
  return {
    cpuUsage: process.cpuUsage(),
    memoryUsage: process.memoryUsage(),
  };
}

function diffServerSample(before: ServerSample) {
  const cpuUsage = process.cpuUsage(before.cpuUsage);
  const memoryUsage = process.memoryUsage();

  return {
    cpuUserMicros: cpuUsage.user,
    cpuSystemMicros: cpuUsage.system,
    rssBytes: memoryUsage.rss,
    heapUsedBytes: memoryUsage.heapUsed,
    externalBytes: memoryUsage.external,
  };
}

function elapsedMilliseconds(startedAt: bigint): number {
  return Number(process.hrtime.bigint() - startedAt) / 1_000_000;
}

function normalizeMessage(message: string | null | undefined): string | null {
  if (!message) return null;

  return message.replace(/\s+/g, " ").trim().slice(0, 600) || null;
}

function normalizeMetadata(metadata: Record<string, unknown> | null | undefined): string | null {
  if (!metadata || Object.keys(metadata).length === 0) {
    return null;
  }

  try {
    return JSON.stringify(metadata);
  } catch {
    return JSON.stringify({ serialization_error: true });
  }
}

function normalizeCsvValue(value: string | number | boolean | null | undefined): string {
  if (value === null || value === undefined) {
    return "";
  }

  const text = String(value);
  if (!/[",\n]/.test(text)) {
    return text;
  }

  return `"${text.replace(/"/g, '""')}"`;
}

function normalizeEventRow(input: PerformanceEventInput): PerformanceEventRow {
  const requestContext = requestContextStore.getStore();
  const success = input.success ?? !input.errorName;

  return {
    timestamp: input.timestamp ?? new Date().toISOString(),
    requestId: input.requestId ?? requestContext?.requestId ?? null,
    spanId: input.spanId ?? randomUUID(),
    parentSpanId: input.parentSpanId ?? requestContext?.spanId ?? null,
    eventKind: input.eventKind,
    surface: input.surface,
    routePath: input.routePath ?? requestContext?.path ?? null,
    method: input.method ?? requestContext?.method ?? null,
    operation: input.operation,
    target: input.target ?? null,
    durationMs: finiteNumberOrNull(input.durationMs),
    throughputPerSecond:
      finiteNumberOrNull(input.throughputPerSecond) ??
      deriveThroughput(
        finiteNumberOrNull(input.responseBytes) ?? finiteNumberOrNull(input.rowCount),
        finiteNumberOrNull(input.durationMs),
      ),
    statusCode: finiteNumberOrNull(input.statusCode),
    success,
    errorName: input.errorName ?? null,
    errorMessage: normalizeMessage(input.errorMessage),
    cpuUserMicros: finiteNumberOrNull(input.cpuUserMicros),
    cpuSystemMicros: finiteNumberOrNull(input.cpuSystemMicros),
    rssBytes: finiteNumberOrNull(input.rssBytes),
    heapUsedBytes: finiteNumberOrNull(input.heapUsedBytes),
    externalBytes: finiteNumberOrNull(input.externalBytes),
    rowCount: finiteNumberOrNull(input.rowCount),
    bytesRead: finiteNumberOrNull(input.bytesRead),
    bytesWritten: finiteNumberOrNull(input.bytesWritten),
    responseBytes: finiteNumberOrNull(input.responseBytes),
    cacheState: input.cacheState ?? null,
    metadataJson: normalizeMetadata(input.metadata),
  };
}

function eventRowToCsvValues(row: PerformanceEventRow): string[] {
  const orderedValues = [
    row.timestamp,
    row.requestId,
    row.spanId,
    row.parentSpanId,
    row.eventKind,
    row.surface,
    row.routePath,
    row.method,
    row.operation,
    row.target,
    row.durationMs,
    row.throughputPerSecond,
    row.statusCode,
    row.success,
    row.errorName,
    row.errorMessage,
    row.cpuUserMicros,
    row.cpuSystemMicros,
    row.rssBytes,
    row.heapUsedBytes,
    row.externalBytes,
    row.rowCount,
    row.bytesRead,
    row.bytesWritten,
    row.responseBytes,
    row.cacheState,
    row.metadataJson,
  ] satisfies Array<string | number | boolean | null | undefined>;

  return orderedValues.map((value) => (value === null || value === undefined ? "" : String(value)));
}

function serializeCsvValues(values: string[]): string {
  return values.map((value) => normalizeCsvValue(value)).join(",");
}

function parseCsvLine(line: string): string[] {
  const values: string[] = [];
  let current = "";
  let inQuotes = false;

  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];

    if (inQuotes) {
      if (char === '"') {
        if (line[index + 1] === '"') {
          current += '"';
          index += 1;
        } else {
          inQuotes = false;
        }
      } else {
        current += char;
      }
      continue;
    }

    if (char === ',') {
      values.push(current);
      current = "";
      continue;
    }

    if (char === '"') {
      inQuotes = true;
      continue;
    }

    current += char;
  }

  values.push(current);
  return values;
}

async function loadExistingCsvRows(): Promise<string[][]> {
  try {
    const content = await readFile(performanceMetricsExportPath, "utf8");
    const lines = content
      .split(/\r?\n/)
      .map((line) => line.trimEnd())
      .filter((line) => line.length > 0);

    if (lines.length <= 1) {
      return [];
    }

    return lines.slice(1).map((line) => parseCsvLine(line));
  } catch {
    return [];
  }
}

function durationValue(values: string[]): number {
  const parsed = Number(values[durationColumnIndex] ?? "");
  return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
}

function sortRowsByDuration(rows: string[][]): string[][] {
  return [...rows].sort((left, right) => durationValue(right) - durationValue(left));
}

async function writeSortedCsvRows(rows: string[][]): Promise<void> {
  const content = rows.length
    ? `${csvHeaderLine}${rows.map((row) => serializeCsvValues(row)).join("\n")}\n`
    : csvHeaderLine;
  await mkdir(dirname(performanceMetricsExportPath), { recursive: true });
  await writeFile(performanceMetricsExportPath, content, "utf8");
}

function enqueueCsvWork(work: () => Promise<void>): Promise<void> {
  const pendingWork = globalPerformanceState.__avianPerformanceWriteQueue?.catch(() => undefined)
    ?? Promise.resolve();
  const nextWork = pendingWork.then(async () => {
    await work();
  });
  globalPerformanceState.__avianPerformanceWriteQueue = nextWork;
  return nextWork;
}

function queueCsvWrite(row: PerformanceEventRow): void {
  const rowValues = eventRowToCsvValues(row);

  void enqueueCsvWork(async () => {
    const pendingRows = globalPerformanceState.__avianPerformancePendingRows ?? [];
    pendingRows.push(rowValues);
    globalPerformanceState.__avianPerformancePendingRows = pendingRows;
  })
    .catch((error: unknown) => {
      console.error("[performance] failed to append audit row", error);
    });
}

function errorDetails(error: unknown): { errorName: string | null; errorMessage: string | null } {
  if (error instanceof Error) {
    return {
      errorName: error.name,
      errorMessage: error.message,
    };
  }

  if (typeof error === "string") {
    return {
      errorName: "Error",
      errorMessage: error,
    };
  }

  return {
    errorName: null,
    errorMessage: null,
  };
}

function requestContextFromHeaderSource(
  surface: RequestScopedSurface,
  headerSource: Pick<Headers, "get">,
  fallback: { path: string | null; method: string | null },
): RequestContext {
  return createRequestContext(surface, {
    requestId: headerSource.get(REQUEST_ID_HEADER),
    path: headerSource.get(REQUEST_PATH_HEADER) ?? fallback.path,
    method: headerSource.get(REQUEST_METHOD_HEADER) ?? fallback.method,
  });
}

function contentLength(response: Response | null | undefined): number | null {
  if (!response) {
    return null;
  }

  const rawValue = response.headers.get("content-length");
  const parsedValue = rawValue ? Number(rawValue) : Number.NaN;

  return Number.isFinite(parsedValue) ? parsedValue : null;
}

export function getPerformanceMetricsExportPath(): string {
  return performanceMetricsExportPath;
}

export function getPerformanceAuditCsvPath(): string {
  return getPerformanceMetricsExportPath();
}

export function getRequestContext(): RequestContext | undefined {
  return requestContextStore.getStore();
}

export function withRequestContext<T>(
  context: RequestContext,
  work: () => Promise<T> | T,
): Promise<T> {
  return requestContextStore.run(context, async () => await work());
}

export function recordPerformanceEvent(input: PerformanceEventInput): void {
  queueCsvWrite(normalizeEventRow(input));
}

export async function flushPerformanceWrites(): Promise<void> {
  await enqueueCsvWork(async () => {
    const pendingRows = globalPerformanceState.__avianPerformancePendingRows ?? [];
    if (pendingRows.length === 0) {
      return;
    }

    const existingRows = await loadExistingCsvRows();
    await writeSortedCsvRows(sortRowsByDuration([...existingRows, ...pendingRows]));
    globalPerformanceState.__avianPerformancePendingRows = [];
  });
}

export function getRelativeAuditTarget(rawTarget: string): string {
  const relativeTarget = relative(repoRoot, rawTarget);
  return relativeTarget && !relativeTarget.startsWith("..") ? relativeTarget : rawTarget;
}

export function captureOperationSample(): ServerSample {
  return captureServerSample();
}

export function finalizeOperationSample(before: ServerSample) {
  return diffServerSample(before);
}

export function getOperationDurationMs(startedAt: bigint): number {
  return elapsedMilliseconds(startedAt);
}

export function computeThroughput(
  quantity: number | null | undefined,
  durationMs: number | null | undefined,
): number | null {
  return deriveThroughput(quantity, durationMs);
}

export function performanceErrorDetails(error: unknown) {
  return errorDetails(error);
}

export function createApiRequestContext(request: Request): RequestContext {
  return requestContextFromHeaderSource(
    "api",
    request.headers,
    {
      path: resolveRequestPath(request),
      method: request.method,
    },
  );
}

export async function createPageRequestContext(
  fallbackPath: string | null = null,
): Promise<RequestContext> {
  const headerStore = await headers();
  return requestContextFromHeaderSource(
    "page",
    headerStore,
    {
      path: fallbackPath,
      method: "GET",
    },
  );
}

export async function createActionRequestContext(): Promise<RequestContext> {
  const headerStore = await headers();
  return requestContextFromHeaderSource(
    "action",
    headerStore,
    {
      path: null,
      method: headerStore.get(REQUEST_METHOD_HEADER) ?? "POST",
    },
  );
}

type RouteHandler<TArgs extends [Request, ...unknown[]]> = (
  ...args: TArgs
) => Promise<Response> | Response;

export function withMeasuredApiRoute<TArgs extends [Request, ...unknown[]]>(
  handler: RouteHandler<TArgs>,
): RouteHandler<TArgs> {
  return (async (...args: TArgs) => {
    const request = args[0];
    const requestContext = createApiRequestContext(request);
    const startedAt = process.hrtime.bigint();
    const sample = captureServerSample();
    let response: Response | undefined;
    let caughtError: unknown;

    return withRequestContext(requestContext, async () => {
      try {
        response = await handler(...args);
        return response;
      } catch (error) {
        caughtError = error;
        throw error;
      } finally {
        const durationMs = elapsedMilliseconds(startedAt);
        const serverSample = diffServerSample(sample);
        const { errorName, errorMessage } = errorDetails(caughtError);

        recordPerformanceEvent({
          eventKind: "request",
          surface: "api",
          operation: "api_request",
          target: requestContext.path,
          durationMs,
          throughputPerSecond: deriveThroughput(contentLength(response), durationMs),
          statusCode: response?.status ?? (caughtError ? 500 : null),
          success: !caughtError && (response?.status ?? 200) < 500,
          errorName,
          errorMessage,
          responseBytes: contentLength(response),
          cacheState:
            response?.headers.get("x-nextjs-cache") ??
            response?.headers.get("cache-control"),
          ...serverSample,
        });
      }
    });
  }) as RouteHandler<TArgs>;
}

export async function measurePageRender<T>(
  render: () => Promise<T> | T,
  options?: { routePath?: string | null },
): Promise<T> {
  const existingContext = requestContextStore.getStore();
  const explicitRoutePath = options?.routePath ?? null;
  const requestContext = existingContext ?? (await createPageRequestContext(explicitRoutePath));
  const resolvedRoutePath = requestContext.path ?? explicitRoutePath;
  const startedAt = process.hrtime.bigint();
  const sample = captureServerSample();
  let caughtError: unknown;

  const runRender = async () => {
    try {
      return await render();
    } catch (error) {
      caughtError = error;
      throw error;
    } finally {
      const durationMs = elapsedMilliseconds(startedAt);
      const serverSample = diffServerSample(sample);
      const { errorName, errorMessage } = errorDetails(caughtError);

      recordPerformanceEvent({
        eventKind: "request",
        surface: "page",
        routePath: resolvedRoutePath,
        operation: "page_render",
        target: resolvedRoutePath,
        durationMs,
        success: !caughtError,
        errorName,
        errorMessage,
        ...serverSample,
      });
    }
  };

  if (existingContext) {
    return runRender();
  }

  return withRequestContext(requestContext, runRender);
}

export async function measureServerAction<T>(
  actionName: string,
  work: () => Promise<T> | T,
): Promise<T> {
  const existingContext = requestContextStore.getStore();
  const requestContext = existingContext ?? (await createActionRequestContext());
  const startedAt = process.hrtime.bigint();
  const sample = captureServerSample();
  let caughtError: unknown;

  const runAction = async () => {
    try {
      return await work();
    } catch (error) {
      caughtError = error;
      throw error;
    } finally {
      const durationMs = elapsedMilliseconds(startedAt);
      const serverSample = diffServerSample(sample);
      const { errorName, errorMessage } = errorDetails(caughtError);

      recordPerformanceEvent({
        eventKind: "server_action",
        surface: "action",
        operation: actionName,
        target: actionName,
        durationMs,
        success: !caughtError,
        errorName,
        errorMessage,
        ...serverSample,
      });
    }
  };

  if (existingContext) {
    return runAction();
  }

  return withRequestContext(requestContext, runAction);
}