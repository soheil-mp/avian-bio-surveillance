import "server-only";

import { readFile, writeFile } from "node:fs/promises";

import {
  captureOperationSample,
  computeThroughput,
  finalizeOperationSample,
  getOperationDurationMs,
  getRelativeAuditTarget,
  performanceErrorDetails,
  recordPerformanceEvent,
} from "@/lib/performance/server";

function readByteLength(content: string | Buffer): number {
  return typeof content === "string"
    ? Buffer.byteLength(content, "utf8")
    : content.byteLength;
}

function writeByteLength(content: string | Uint8Array): number {
  return typeof content === "string"
    ? Buffer.byteLength(content, "utf8")
    : content.byteLength;
}

export async function readFileMeasured(
  filePath: string,
  encoding: BufferEncoding,
): Promise<string> {
  const startedAt = process.hrtime.bigint();
  const sample = captureOperationSample();
  const target = getRelativeAuditTarget(filePath);

  try {
    const content = await readFile(filePath, encoding);
    const durationMs = getOperationDurationMs(startedAt);
    const bytesRead = readByteLength(content);

    recordPerformanceEvent({
      eventKind: "file_read",
      surface: "file",
      operation: "read_file",
      target,
      durationMs,
      bytesRead,
      throughputPerSecond: computeThroughput(bytesRead, durationMs),
      success: true,
      metadata: { encoding },
      ...finalizeOperationSample(sample),
    });

    return content;
  } catch (error) {
    const durationMs = getOperationDurationMs(startedAt);
    const { errorName, errorMessage } = performanceErrorDetails(error);

    recordPerformanceEvent({
      eventKind: "file_read",
      surface: "file",
      operation: "read_file",
      target,
      durationMs,
      success: false,
      errorName,
      errorMessage,
      metadata: { encoding },
      ...finalizeOperationSample(sample),
    });

    throw error;
  }
}

export async function writeFileMeasured(
  filePath: string,
  content: string | Uint8Array,
  encoding: BufferEncoding = "utf8",
): Promise<void> {
  const startedAt = process.hrtime.bigint();
  const sample = captureOperationSample();
  const target = getRelativeAuditTarget(filePath);
  const bytesWritten = writeByteLength(content);

  try {
    await writeFile(filePath, content, typeof content === "string" ? encoding : undefined);
    const durationMs = getOperationDurationMs(startedAt);

    recordPerformanceEvent({
      eventKind: "file_write",
      surface: "file",
      operation: "write_file",
      target,
      durationMs,
      bytesWritten,
      throughputPerSecond: computeThroughput(bytesWritten, durationMs),
      success: true,
      metadata: typeof content === "string" ? { encoding } : null,
      ...finalizeOperationSample(sample),
    });
  } catch (error) {
    const durationMs = getOperationDurationMs(startedAt);
    const { errorName, errorMessage } = performanceErrorDetails(error);

    recordPerformanceEvent({
      eventKind: "file_write",
      surface: "file",
      operation: "write_file",
      target,
      durationMs,
      bytesWritten,
      success: false,
      errorName,
      errorMessage,
      metadata: typeof content === "string" ? { encoding } : null,
      ...finalizeOperationSample(sample),
    });

    throw error;
  }
}