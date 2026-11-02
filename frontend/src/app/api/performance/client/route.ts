import { NextRequest, NextResponse } from "next/server";

import {
  recordPerformanceEvent,
} from "@/lib/performance/server";
import type {
  ClientPerformancePayload,
  PerformanceEventInput,
} from "@/lib/performance/shared";

const ALLOWED_EVENT_KINDS = new Set<string>([
  "client_fetch",
  "client_navigation",
  "client_longtask",
] as const);

function trimText(value: unknown, maxLength: number): string | null {
  if (typeof value !== "string") {
    return null;
  }

  const trimmed = value.trim();
  return trimmed ? trimmed.slice(0, maxLength) : null;
}

function sanitizeNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function sanitizeMetadata(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }

  return value as Record<string, unknown>;
}

function sanitizeEvent(event: PerformanceEventInput): PerformanceEventInput | null {
  if (!ALLOWED_EVENT_KINDS.has(event.eventKind)) {
    return null;
  }

  return {
    timestamp: trimText(event.timestamp, 64) ?? undefined,
    requestId: trimText(event.requestId ?? null, 64),
    spanId: trimText(event.spanId ?? null, 64) ?? undefined,
    parentSpanId: trimText(event.parentSpanId ?? null, 64),
    eventKind: event.eventKind,
    surface: "browser",
    routePath: trimText(event.routePath ?? null, 256),
    method: trimText(event.method ?? null, 16),
    operation: trimText(event.operation, 64) ?? "browser_event",
    target: trimText(event.target ?? null, 256),
    durationMs: sanitizeNumber(event.durationMs),
    throughputPerSecond: sanitizeNumber(event.throughputPerSecond),
    statusCode: sanitizeNumber(event.statusCode),
    success: event.success ?? true,
    errorName: trimText(event.errorName ?? null, 64),
    errorMessage: trimText(event.errorMessage ?? null, 512),
    cpuUserMicros: null,
    cpuSystemMicros: null,
    rssBytes: null,
    heapUsedBytes: sanitizeNumber(event.heapUsedBytes),
    externalBytes: sanitizeNumber(event.externalBytes),
    rowCount: sanitizeNumber(event.rowCount),
    bytesRead: sanitizeNumber(event.bytesRead),
    bytesWritten: sanitizeNumber(event.bytesWritten),
    responseBytes: sanitizeNumber(event.responseBytes),
    cacheState: trimText(event.cacheState ?? null, 64),
    metadata: sanitizeMetadata(event.metadata),
  };
}

export async function POST(request: NextRequest) {
  try {
    const rawPayload = await request.text();
    if (!rawPayload.trim()) {
      return NextResponse.json({ ok: true, accepted: 0 });
    }

    const payload = JSON.parse(rawPayload) as ClientPerformancePayload;
    if (!payload || !Array.isArray(payload.events)) {
      return NextResponse.json({ error: "Invalid payload" }, { status: 400 });
    }

    const accepted = payload.events
      .slice(0, 100)
      .map((event) => sanitizeEvent(event))
      .filter((event): event is PerformanceEventInput => event !== null);

    for (const event of accepted) {
      recordPerformanceEvent(event);
    }

    return NextResponse.json({ ok: true, accepted: accepted.length });
  } catch (error) {
    if (error instanceof SyntaxError) {
      return NextResponse.json({ ok: true, accepted: 0 });
    }

    console.error("[performance] failed to ingest client events", error);
    return NextResponse.json(
      { error: "Failed to ingest performance events" },
      { status: 400 },
    );
  }
}