export const REQUEST_ID_HEADER = "x-avian-request-id";
export const REQUEST_PATH_HEADER = "x-avian-request-path";
export const REQUEST_METHOD_HEADER = "x-avian-request-method";

export const PERFORMANCE_CSV_COLUMNS = [
  "timestamp",
  "request_id",
  "span_id",
  "parent_span_id",
  "event_kind",
  "surface",
  "route_path",
  "method",
  "operation",
  "target",
  "duration_ms",
  "throughput_per_second",
  "status_code",
  "success",
  "error_name",
  "error_message",
  "cpu_user_micros",
  "cpu_system_micros",
  "rss_bytes",
  "heap_used_bytes",
  "external_bytes",
  "row_count",
  "bytes_read",
  "bytes_written",
  "response_bytes",
  "cache_state",
  "metadata_json",
] as const;

export type PerformanceEventKind =
  | "request"
  | "server_action"
  | "db_query"
  | "file_read"
  | "file_write"
  | "client_fetch"
  | "client_navigation"
  | "client_longtask";

export type PerformanceSurface =
  | "api"
  | "page"
  | "action"
  | "db"
  | "file"
  | "browser";

export interface PerformanceEventInput {
  timestamp?: string;
  requestId?: string | null;
  spanId?: string;
  parentSpanId?: string | null;
  eventKind: PerformanceEventKind;
  surface: PerformanceSurface;
  routePath?: string | null;
  method?: string | null;
  operation: string;
  target?: string | null;
  durationMs?: number | null;
  throughputPerSecond?: number | null;
  statusCode?: number | null;
  success?: boolean;
  errorName?: string | null;
  errorMessage?: string | null;
  cpuUserMicros?: number | null;
  cpuSystemMicros?: number | null;
  rssBytes?: number | null;
  heapUsedBytes?: number | null;
  externalBytes?: number | null;
  rowCount?: number | null;
  bytesRead?: number | null;
  bytesWritten?: number | null;
  responseBytes?: number | null;
  cacheState?: string | null;
  metadata?: Record<string, unknown> | null;
}

export interface PerformanceEventRow {
  timestamp: string;
  requestId: string | null;
  spanId: string;
  parentSpanId: string | null;
  eventKind: PerformanceEventKind;
  surface: PerformanceSurface;
  routePath: string | null;
  method: string | null;
  operation: string;
  target: string | null;
  durationMs: number | null;
  throughputPerSecond: number | null;
  statusCode: number | null;
  success: boolean;
  errorName: string | null;
  errorMessage: string | null;
  cpuUserMicros: number | null;
  cpuSystemMicros: number | null;
  rssBytes: number | null;
  heapUsedBytes: number | null;
  externalBytes: number | null;
  rowCount: number | null;
  bytesRead: number | null;
  bytesWritten: number | null;
  responseBytes: number | null;
  cacheState: string | null;
  metadataJson: string | null;
}

export interface ClientPerformancePayload {
  events: PerformanceEventInput[];
}