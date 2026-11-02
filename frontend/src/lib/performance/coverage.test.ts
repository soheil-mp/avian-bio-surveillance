import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

async function walkFiles(root: string): Promise<string[]> {
  const entries = await readdir(root, { withFileTypes: true });
  const nested = await Promise.all(
    entries.map(async (entry) => {
      const fullPath = join(root, entry.name);
      if (entry.isDirectory()) {
        return walkFiles(fullPath);
      }
      return [fullPath];
    }),
  );

  return nested.flat();
}

describe("performance instrumentation coverage", () => {
  it("wraps internal API routes with measured route handling", async () => {
    const appRoot = join(process.cwd(), "src", "app");
    const routeFiles = (await walkFiles(appRoot))
      .filter((filePath) => filePath.endsWith("route.ts"))
      .filter((filePath) => !filePath.includes(join("api", "performance", "client")))
      .filter((filePath) => !filePath.includes(join("api", "performance", "flush")));

    const fileContents = await Promise.all(
      routeFiles.map(async (filePath) => ({
        filePath,
        content: await readFile(filePath, "utf8"),
      })),
    );

    const missing = fileContents
      .filter(({ content }) => !content.includes("withMeasuredApiRoute("))
      .map(({ filePath }) => filePath);

    expect(missing).toEqual([]);
  });

  it("wraps DB and file-backed pages with measured page rendering", async () => {
    const appRoot = join(process.cwd(), "src", "app");
    const pageFiles = (await walkFiles(appRoot)).filter((filePath) =>
      filePath.endsWith("page.tsx"),
    );

    const fileContents = await Promise.all(
      pageFiles.map(async (filePath) => ({
        filePath,
        content: await readFile(filePath, "utf8"),
      })),
    );

    const relevant = fileContents.filter(
      ({ content }) =>
        content.includes("@/lib/db") || content.includes("@/lib/server-fs"),
    );
    const missing = relevant
      .filter(({ content }) => !content.includes("measurePageRender("))
      .map(({ filePath }) => filePath);

    expect(missing).toEqual([]);
  });

  it("wraps server actions with measured server-action handling", async () => {
    const appRoot = join(process.cwd(), "src", "app");
    const actionFiles = (await walkFiles(appRoot)).filter((filePath) =>
      filePath.endsWith("actions.ts"),
    );

    const fileContents = await Promise.all(
      actionFiles.map(async (filePath) => ({
        filePath,
        content: await readFile(filePath, "utf8"),
      })),
    );

    const missing = fileContents
      .filter(({ content }) => content.includes("use server"))
      .filter(({ content }) => !content.includes("measureServerAction("))
      .map(({ filePath }) => filePath);

    expect(missing).toEqual([]);
  });
});