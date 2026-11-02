"use client";

import { useEffect, useRef } from "react";
import { usePathname } from "next/navigation";

import type {
  ClientPerformancePayload,
  PerformanceEventInput,
} from "@/lib/performance/shared";

const METRICS_ENDPOINT = "/api/performance/client";
const MAX_BATCH_SIZE = 20;
const FLUSH_DELAY_MS = 1_000;
const PERFORMANCE_CLIENT_ENABLED =
  process.env.NODE_ENV === "production" ||
  process.env.NEXT_PUBLIC_ENABLE_DEV_PERFORMANCE_AUDIT === "true";

type BrowserMemory = {
  usedJSHeapSize: number;
  totalJSHeapSize: number;
  jsHeapSizeLimit: number;
};

type PerformanceWithMemory = Performance & {
  memory?: BrowserMemory;
};

function readMemorySnapshot() {
  const memory = (performance as PerformanceWithMemory).memory;
  if (!memory) {
    return null;
  }

  return {
    heapUsedBytes: memory.usedJSHeapSize,
    totalHeapBytes: memory.totalJSHeapSize,
    heapLimitBytes: memory.jsHeapSizeLimit,
  };
}

function errorDetails(error: unknown) {
  if (error instanceof Error) {
    return { errorName: error.name, errorMessage: error.message };
  }

  if (typeof error === "string") {
    return { errorName: "Error", errorMessage: error };
  }

  return { errorName: null, errorMessage: null };
}

function responseBytes(response: Response | undefined): number | null {
  if (!response) {
    return null;
  }

  const raw = response.headers.get("content-length");
  const parsed = raw ? Number(raw) : Number.NaN;
  return Number.isFinite(parsed) ? parsed : null;
}

function sendPayload(
  originalFetch: typeof window.fetch,
  payload: ClientPerformancePayload,
) {
  const body = JSON.stringify(payload);

  if (navigator.sendBeacon) {
    const sent = navigator.sendBeacon(
      METRICS_ENDPOINT,
      new Blob([body], { type: "application/json" }),
    );
    if (sent) {
      return;
    }
  }

  void originalFetch(METRICS_ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
    keepalive: true,
  }).catch(() => {
    // Swallow audit delivery failures on the client.
  });
}

export function PerformanceClientBootstrap() {
  const pathname = usePathname();
  const pathnameRef = useRef(pathname);
  const queueRef = useRef<PerformanceEventInput[]>([]);
  const timeoutRef = useRef<number | null>(null);

  useEffect(() => {
    if (!PERFORMANCE_CLIENT_ENABLED) {
      return undefined;
    }

    pathnameRef.current = pathname;
  }, [pathname]);

  useEffect(() => {
    if (!PERFORMANCE_CLIENT_ENABLED) {
      return undefined;
    }

    const originalFetch = window.fetch.bind(window);

    const flushQueue = () => {
      if (timeoutRef.current != null) {
        window.clearTimeout(timeoutRef.current);
        timeoutRef.current = null;
      }

      if (queueRef.current.length === 0) {
        return;
      }

      const payload: ClientPerformancePayload = {
        events: queueRef.current.splice(0, queueRef.current.length),
      };
      sendPayload(originalFetch, payload);
    };

    const scheduleFlush = () => {
      if (timeoutRef.current != null) {
        return;
      }

      timeoutRef.current = window.setTimeout(() => {
        flushQueue();
      }, FLUSH_DELAY_MS);
    };

    const enqueue = (event: PerformanceEventInput) => {
      queueRef.current.push({
        ...event,
        timestamp: event.timestamp ?? new Date().toISOString(),
        spanId: event.spanId ?? crypto.randomUUID(),
        surface: "browser",
        routePath: event.routePath ?? pathnameRef.current,
      });

      if (queueRef.current.length >= MAX_BATCH_SIZE) {
        flushQueue();
        return;
      }

      scheduleFlush();
    };

    const instrumentedFetch: typeof window.fetch = async (
      input,
      init,
    ) => {
      const request = input instanceof Request ? input : new Request(input, init);
      const url = new URL(request.url, window.location.origin);
      const isInternalApi =
        url.origin === window.location.origin &&
        url.pathname.startsWith("/api/") &&
        url.pathname !== METRICS_ENDPOINT;

      if (!isInternalApi) {
        return originalFetch(input, init);
      }

      const startedAt = performance.now();
      let response: Response | undefined;
      let caughtError: unknown;

      try {
        response = await originalFetch(input, init);
        return response;
      } catch (error) {
        caughtError = error;
        throw error;
      } finally {
        const durationMs = performance.now() - startedAt;
        const memory = readMemorySnapshot();
        const { errorName, errorMessage } = errorDetails(caughtError);

        enqueue({
          eventKind: "client_fetch",
          surface: "browser",
          operation: "browser_fetch",
          method: request.method,
          target: url.pathname,
          durationMs,
          responseBytes: responseBytes(response),
          statusCode: response?.status ?? (caughtError ? 0 : null),
          success: !caughtError && (response?.status ?? 200) < 500,
          errorName,
          errorMessage,
          heapUsedBytes: memory?.heapUsedBytes ?? null,
          metadata: {
            search: url.search || null,
            pagePath: pathnameRef.current,
            totalHeapBytes: memory?.totalHeapBytes ?? null,
            heapLimitBytes: memory?.heapLimitBytes ?? null,
          },
        });
      }
    };

    window.fetch = instrumentedFetch;

    const navigationEntry = performance.getEntriesByType("navigation")[0] as
      | PerformanceNavigationTiming
      | undefined;
    if (navigationEntry) {
      const memory = readMemorySnapshot();
      enqueue({
        eventKind: "client_navigation",
        surface: "browser",
        operation: "navigation",
        target: pathnameRef.current,
        durationMs:
          navigationEntry.loadEventEnd > 0
            ? navigationEntry.loadEventEnd
            : navigationEntry.duration,
        responseBytes:
          navigationEntry.transferSize > 0 ? navigationEntry.transferSize : null,
        bytesRead:
          navigationEntry.decodedBodySize > 0
            ? navigationEntry.decodedBodySize
            : null,
        success: true,
        heapUsedBytes: memory?.heapUsedBytes ?? null,
        metadata: {
          domInteractive: navigationEntry.domInteractive,
          domComplete: navigationEntry.domComplete,
          type: navigationEntry.type,
          totalHeapBytes: memory?.totalHeapBytes ?? null,
          heapLimitBytes: memory?.heapLimitBytes ?? null,
        },
      });
    }

    let observer: PerformanceObserver | null = null;
    if (typeof PerformanceObserver !== "undefined") {
      try {
        observer = new PerformanceObserver((list) => {
          for (const entry of list.getEntries()) {
            enqueue({
              eventKind: "client_longtask",
              surface: "browser",
              operation: "longtask",
              target: pathnameRef.current,
              durationMs: entry.duration,
              success: true,
              metadata: {
                name: entry.name,
                entryType: entry.entryType,
                startTime: entry.startTime,
              },
            });
          }
        });
        observer.observe({ type: "longtask", buffered: true });
      } catch {
        observer = null;
      }
    }

    const handleVisibilityChange = () => {
      if (document.visibilityState === "hidden") {
        flushQueue();
      }
    };

    const handlePageHide = () => {
      flushQueue();
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    window.addEventListener("pagehide", handlePageHide);

    return () => {
      window.fetch = originalFetch;
      observer?.disconnect();
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      window.removeEventListener("pagehide", handlePageHide);
      flushQueue();
    };
  }, []);

  return null;
}