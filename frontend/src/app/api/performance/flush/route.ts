import { readFile } from "node:fs/promises";

import { NextResponse } from "next/server";

import {
  flushPerformanceWrites,
  getPerformanceMetricsExportPath,
} from "@/lib/performance/server";

async function countExportRows(filePath: string): Promise<number> {
  try {
    const content = await readFile(filePath, "utf8");
    const lines = content
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line.length > 0);

    return Math.max(lines.length - 1, 0);
  } catch {
    return 0;
  }
}

export async function GET() {
  await flushPerformanceWrites();

  const exportPath = getPerformanceMetricsExportPath();
  return NextResponse.json({
    ok: true,
    exportPath,
    rowCount: await countExportRows(exportPath),
  });
}