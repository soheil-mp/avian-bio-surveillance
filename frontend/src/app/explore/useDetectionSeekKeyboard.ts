"use client";

import { useEffect } from "react";

type AvianSeekDetail = {
  id?: string;
  delta?: number;
};

export function useDetectionSeekKeyboard({
  detectionId,
  enabled,
  onSeek,
}: {
  detectionId: string;
  enabled: boolean;
  onSeek: (delta: number) => void;
}) {
  useEffect(() => {
    if (!enabled) return;

    const handler = (event: Event) => {
      const detail = (event as CustomEvent<AvianSeekDetail>).detail;
      if (detail?.id !== detectionId || typeof detail.delta !== "number") {
        return;
      }

      onSeek(detail.delta);
    };

    window.addEventListener("avian-seek", handler);
    return () => window.removeEventListener("avian-seek", handler);
  }, [detectionId, enabled, onSeek]);
}