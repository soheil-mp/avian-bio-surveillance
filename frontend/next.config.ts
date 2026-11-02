import type { NextConfig } from "next";
import { existsSync } from "fs";
import { resolve } from "path";

type RemotePattern = NonNullable<
  NonNullable<NextConfig["images"]>["remotePatterns"]
>[number];

function loadWorkspaceEnv(relativePath: string): void {
  const envPath = resolve(import.meta.dirname, relativePath);
  if (existsSync(envPath)) {
    process.loadEnvFile(envPath);
  }
}

loadWorkspaceEnv("../config/runtime.env");
loadWorkspaceEnv("../.env");

function parseRemotePattern(endpoint: string | undefined): RemotePattern | null {
  if (!endpoint) return null;

  const normalized = endpoint.includes("://") ? endpoint : `http://${endpoint}`;

  try {
    const url = new URL(normalized);
    return {
      protocol: url.protocol === "https:" ? "https" : "http",
      hostname: url.hostname,
      port: url.port || undefined,
      pathname: "/**",
    };
  } catch {
    return null;
  }
}

function dedupeRemotePatterns(
  patterns: Array<RemotePattern | null>,
): RemotePattern[] {
  const seen = new Set<string>();

  return patterns.filter((pattern): pattern is RemotePattern => {
    if (!pattern) return false;

    const key = JSON.stringify(pattern);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

const imageRemotePatterns = dedupeRemotePatterns([
  {
    protocol: "https",
    hostname: "media.birdweather.com",
    pathname: "/**",
  },
  {
    protocol: "http",
    hostname: "127.0.0.1",
    port: "19000",
    pathname: "/**",
  },
  {
    protocol: "http",
    hostname: "localhost",
    port: "19000",
    pathname: "/**",
  },
  parseRemotePattern(process.env.MINIO_PUBLIC_ENDPOINT),
  parseRemotePattern(process.env.NEXT_PUBLIC_MINIO_ENDPOINT),
]);

const isProductionBuild = process.env.NODE_ENV === "production";
const workspaceRoot = resolve(import.meta.dirname, "..");

const nextConfig: NextConfig = {
  serverExternalPackages: ["pg", "minio"],
  outputFileTracingRoot: workspaceRoot,
  experimental: {
    optimizePackageImports: ["lucide-react"],
  },
  images: {
    remotePatterns: imageRemotePatterns,
  },
};

if (isProductionBuild) {
  nextConfig.output = "standalone";
}

export default nextConfig;
