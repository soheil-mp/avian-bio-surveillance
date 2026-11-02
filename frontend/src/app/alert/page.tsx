import { redirect } from "next/navigation";

import type { SearchParamValue } from "../alerts/server";

function serializeSearchParams(params?: Record<string, SearchParamValue>): string {
  const query = new URLSearchParams();

  for (const [key, value] of Object.entries(params ?? {})) {
    if (Array.isArray(value)) {
      for (const entry of value) {
        query.append(key, entry);
      }
    } else if (typeof value === "string") {
      query.set(key, value);
    }
  }

  const serialized = query.toString();
  return serialized ? `?${serialized}` : "";
}

export default async function AlertRedirectPage({
  searchParams,
}: {
  searchParams?: Promise<Record<string, SearchParamValue>>;
}) {
  const params = searchParams ? await searchParams : undefined;
  redirect(`/alerts${serializeSearchParams(params)}`);
}
