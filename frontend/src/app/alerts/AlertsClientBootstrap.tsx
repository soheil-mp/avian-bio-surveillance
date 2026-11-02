"use client";

import dynamic from "next/dynamic";
import { useEffect, useState, type ComponentProps } from "react";
import AlertsLoadingState from "./AlertsLoadingState";

const alertsShellCache = new Map<string, AlertsClientData>();

const AlertsClient = dynamic(() => import("./AlertsClient"), {
  ssr: false,
  loading: () => (
    <AlertsLoadingState
      label="Loading alerts page"
      className="min-h-[58vh] w-full"
    />
  ),
});

type AlertsClientData = ComponentProps<typeof AlertsClient>["data"];

function loadAlertsShell(queryString: string): Promise<AlertsClientData> {
  const cacheKey = queryString;
  const cached = alertsShellCache.get(cacheKey);
  if (cached) {
    return Promise.resolve(cached);
  }

  const route = `/api/alerts/shell${queryString ? `?${queryString}` : ""}`;
  return fetch(route, { cache: "no-store" })
    .then(async (response) => {
      if (!response.ok) {
        throw new Error(`Alerts shell request failed with ${response.status}`);
      }

      return response.json() as Promise<AlertsClientData>;
    })
    .then((payload) => {
      alertsShellCache.set(cacheKey, payload);
      return payload;
    });
}

export default function AlertsClientBootstrap({
  initialQueryString,
}: {
  initialQueryString: string;
}) {
  const [data, setData] = useState<AlertsClientData | null>(null);
  const [status, setStatus] = useState<"loading" | "error" | "ready">("loading");

  useEffect(() => {
    let cancelled = false;

    setStatus("loading");
    setData(null);

    loadAlertsShell(initialQueryString)
      .then((payload) => {
        if (cancelled) {
          return;
        }

        setData(payload);
        setStatus("ready");
      })
      .catch((error) => {
        if (cancelled) {
          return;
        }

        console.error("[alerts] failed to bootstrap shell", error);
        setStatus("error");
      });

    return () => {
      cancelled = true;
    };
  }, [initialQueryString]);

  if (data) {
    return <AlertsClient data={data} />;
  }

  return (
    <AlertsLoadingState
      label={status === "error" ? "Alerts data unavailable" : "Loading alerts page"}
      tone={status === "error" ? "error" : "loading"}
      className="min-h-[58vh] w-full"
    />
  );
}