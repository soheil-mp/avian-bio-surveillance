import { measurePageRender } from "@/lib/performance/server";

import AlertsClientBootstrap from "./AlertsClientBootstrap";
import { loadAlertsData, loadAlertsShellData } from "./data";
import type { SearchParamValue } from "./server";

export const dynamic = "force-dynamic";
const shouldPrewarmAlertsData = process.env.NODE_ENV === "production";

export default async function AlertsPage({
  searchParams,
}: {
  searchParams?: Promise<Record<string, SearchParamValue>>;
}) {
  return measurePageRender(async () => {
    const params = searchParams ? await searchParams : undefined;
    const initialQuery = new URLSearchParams();

    if (params) {
      for (const [key, value] of Object.entries(params)) {
        if (Array.isArray(value)) {
          for (const item of value) {
            initialQuery.append(key, item);
          }
          continue;
        }

        if (typeof value === "string") {
          initialQuery.set(key, value);
        }
      }
    }

    if (shouldPrewarmAlertsData) {
      void loadAlertsShellData(params).catch(() => {});
      void loadAlertsData(params).catch(() => {});
    }
    return <AlertsClientBootstrap initialQueryString={initialQuery.toString()} />;
  }, { routePath: "/alerts" });
}
