import { describe, expect, it } from "vitest";

import {
  buildDetectedAtBounds,
  buildExploreMediaPlan,
  buildOnDemandVisualUrl,
  buildVisualUrls,
  extractBackgroundTags,
  readDetectionAnomalyScore,
  readConfidenceParam,
  readDateParam,
  readPositiveIntParam,
  readVisualMode,
  type ExploreDetectionRow,
} from "./server";

function buildRow(
  overrides: Partial<ExploreDetectionRow> = {},
): ExploreDetectionRow {
  return {
    detection_id: "det-42",
    detected_at: "2025-01-15T02:00:00Z",
    observation_date: "2025-01-15",
    common_name: "Song Thrush",
    scientific_name: "Turdus philomelos",
    confidence: 0.77,
    station_name: "PUC-HML",
    station_country: "Netherlands",
    station_state: "Zuid-Holland",
    station_id: "station-9",
    species_id: 61,
    audio_filename: "2025-01-15/audio/clip.flac",
    audio_url: " https://media.birdweather.com/audio/clip.flac ",
    audio_start_time_s: 4,
    audio_end_time_s: 9,
    species_image_url: null,
    species_thumbnail_url: null,
    species_color: null,
    birdnet_alert_score: null,
    perch_v2_alert_score: null,
    background_sound_clf: null,
    feature_artifacts: null,
    has_processed_clip: true,
    ...overrides,
  };
}

describe("Explore server helpers", () => {
  it("normalizes positive integer search params", () => {
    expect(readPositiveIntParam("7", 1)).toBe(7);
    expect(readPositiveIntParam([" 12 "], 1)).toBe(12);
    expect(readPositiveIntParam("0", 5)).toBe(5);
    expect(readPositiveIntParam("-3", 5)).toBe(5);
    expect(readPositiveIntParam(undefined, 5)).toBe(5);
  });

  it("normalizes confidence params and visual mode", () => {
    expect(readConfidenceParam("80")).toBe(0.8);
    expect(readConfidenceParam("1.4")).toBeCloseTo(0.014);
    expect(readConfidenceParam("-2")).toBe(0);
    expect(readConfidenceParam(undefined)).toBeNull();

    expect(readDateParam("2026-05-15")).toBe("2026-05-15");
    expect(readDateParam("2026-15-05")).toBeNull();
    expect(readDateParam("15-05-2026")).toBeNull();

    expect(buildDetectedAtBounds("2026-05-15", "2026-05-17")).toEqual({
      detectedAtFrom: "2026-05-15T00:00:00.000Z",
      detectedAtToExclusive: "2026-05-18T00:00:00.000Z",
    });
    expect(buildDetectedAtBounds(null, null)).toEqual({
      detectedAtFrom: null,
      detectedAtToExclusive: null,
    });

    expect(readVisualMode("smooth-scope")).toBe("smooth-scope");
    expect(readVisualMode("spectrogram")).toBe("spectrogram");
    expect(readVisualMode("heatmap")).toBe("bar-scope");
  });

  it("builds waveform and spectrogram paths from artifact paths", () => {
    const row = buildRow({
      feature_artifacts: {
        processed_clip: "processed-audio/birdweather/2025-01-15/station-9/det-42.flac",
        waveform: "features/waveforms/birdweather/2025-01-15/station-9/det-42.png",
        smooth_waveform:
          "features/waveforms/birdweather/2025-01-15/station-9/det-42_smooth.png",
        spectrogram:
          "features/spectrograms/birdweather/2025-01-15/station-9/det-42.png",
      },
    });

    const mediaPlan = buildExploreMediaPlan(row);

    expect(mediaPlan.processedAudioPath).toBe(
      "birdweather/2025-01-15/station-9/det-42.flac",
    );
    expect(mediaPlan.rawAudioPath).toBe("2025-01-15/audio/clip.flac");
    expect(mediaPlan.waveformPath).toBe(
      "waveforms/birdweather/2025-01-15/station-9/det-42_smooth.png",
    );
    expect(mediaPlan.spectrogramPath).toBe(
      "spectrograms/birdweather/2025-01-15/station-9/det-42.png",
    );
    expect(mediaPlan.directAudioUrl).toBe(
      "https://media.birdweather.com/audio/clip.flac",
    );
  });

  it("falls back to convention paths when artifacts are missing", () => {
    const row = buildRow({ feature_artifacts: null });

    const mediaPlan = buildExploreMediaPlan(row);

    expect(mediaPlan.processedAudioPath).toBe(
      "birdweather/2025-01-15/station-9/det-42.flac",
    );
    expect(mediaPlan.rawAudioPath).toBe("2025-01-15/audio/clip.flac");
    expect(mediaPlan.waveformPath).toBe(
      "waveforms/birdweather/2025-01-15/station-9/det-42_smooth.png",
    );
    expect(mediaPlan.spectrogramPath).toBe(
      "spectrograms/birdweather/2025-01-15/station-9/det-42.png",
    );
  });

  it("skips legacy feature fallback paths beyond current feature coverage", () => {
    const row = buildRow({
      detected_at: "2025-08-26T23:52:00Z",
      observation_date: "2025-08-26",
      feature_artifacts: null,
    });

    const mediaPlan = buildExploreMediaPlan(row);

    expect(mediaPlan.processedAudioPath).toBe(
      "birdweather/2025-08-26/station-9/det-42.flac",
    );
    expect(mediaPlan.rawAudioPath).toBe("2025-08-26/audio/clip.flac");
    expect(mediaPlan.waveformPath).toBeNull();
    expect(mediaPlan.spectrogramPath).toBeNull();
  });

  it("shapes client visual urls for inline waveform and spectrogram rendering", () => {
    expect(
      buildVisualUrls({
        waveformUrl: "wave-url",
        spectrogramUrl: "spec-url",
        detectionId: "det-42",
        hasProcessedClip: true,
      }),
    ).toEqual({
      waveform: "wave-url",
      spectrogram: "spec-url",
    });

    expect(buildOnDemandVisualUrl("det-42", "waveform", true)).toBe(
      "/api/explore/artifact?detectionId=det-42&kind=waveform",
    );
    expect(
      buildVisualUrls({
        waveformUrl: null,
        spectrogramUrl: null,
        detectionId: "det-42",
        hasProcessedClip: true,
      }),
    ).toEqual({
      waveform: "/api/explore/artifact?detectionId=det-42&kind=waveform",
      spectrogram: "/api/explore/artifact?detectionId=det-42&kind=spectrogram",
    });
    expect(
      buildVisualUrls({
        waveformUrl: null,
        spectrogramUrl: null,
        detectionId: "det-42",
        hasProcessedClip: false,
      }),
    ).toEqual({
      waveform: null,
      spectrogram: null,
    });
  });

  it("filters and sorts background tags", () => {
    expect(
      extractBackgroundTags({
        tags: [
          { label: "Hammer", score: 0.95 },
          { label: "Cowbell", score: 0.91 },
          { label: "Ignore", score: 0.1 },
          { label: 123, score: 0.99 },
          { label: "Sawing", score: 0.98 },
          { label: "Sanding", score: 0.9 },
          { label: "Jackhammer", score: 0.89 },
        ],
      }),
    ).toEqual([
      { label: "Sawing", score: 0.98 },
      { label: "Hammer", score: 0.95 },
      { label: "Cowbell", score: 0.91 },
      { label: "Sanding", score: 0.9 },
      { label: "Jackhammer", score: 0.89 },
    ]);
  });

  it("prefers the strongest finite detection anomaly score", () => {
    expect(
      readDetectionAnomalyScore(
        buildRow({ perch_v2_alert_score: 0.41, birdnet_alert_score: 0.27 }),
      ),
    ).toBe(0.41);
    expect(
      readDetectionAnomalyScore(
        buildRow({ perch_v2_alert_score: null, birdnet_alert_score: 1.4 }),
      ),
    ).toBe(1);
    expect(readDetectionAnomalyScore(buildRow())).toBeNull();
  });
});