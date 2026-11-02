import { describe, expect, it } from "vitest";

import {
  assessAlertsArtifactCoverage,
  alertsWeekEndDateInput,
  alertsWeekStartDateInput,
  buildDefaultAlertsDateWindow,
  normalizeAlertsDateRange,
  preferAvailableWeeksWithData,
  readConfidenceParam,
  selectLatestAlertsCoverageDate,
} from "./server";

describe("Alerts server helpers", () => {
  it("anchors the default window to the latest available week when it exists", () => {
    expect(
      buildDefaultAlertsDateWindow({
        availableWeeks: [
          { periodStart: "2026-05-04", periodEnd: "2026-05-10" },
          { periodStart: "2026-05-11", periodEnd: "2026-05-17" },
          { periodStart: "2026-05-18", periodEnd: "2026-05-24" },
          { periodStart: "2026-05-18", periodEnd: "2026-05-24" },
        ],
        latestDetectedAt: "2026-05-21T16:42:18Z",
      }),
    ).toEqual({
      latestAvailableDate: "2026-05-24",
      dateFrom: "2026-05-18",
      dateTo: "2026-05-24",
      label: "Latest available 1 week · 2026-05-18 to 2026-05-24",
    });
  });

  it("ignores stale disjoint weeks when building the default window", () => {
    expect(
      buildDefaultAlertsDateWindow({
        availableWeeks: [
          { periodStart: "2024-01-01", periodEnd: "2024-01-07" },
          { periodStart: "2026-05-11", periodEnd: "2026-05-17" },
        ],
        latestDetectedAt: "2026-05-21T16:42:18Z",
      }),
    ).toEqual({
      latestAvailableDate: "2026-05-17",
      dateFrom: "2026-05-11",
      dateTo: "2026-05-17",
      label: "Latest available 1 week · 2026-05-11 to 2026-05-17",
    });
  });

  it("prefers artifact weeks that also have real embedding-backed detections", () => {
    expect(
      preferAvailableWeeksWithData({
        availableWeeks: [
          { periodStart: "2025-01-27", periodEnd: "2025-02-02" },
          { periodStart: "2025-06-30", periodEnd: "2025-07-06" },
        ],
        weeksWithData: [
          { periodStart: "2025-01-27", periodEnd: "2025-02-02" },
          { periodStart: "2024-06-03", periodEnd: "2024-06-09" },
        ],
      }),
    ).toEqual([
      { periodStart: "2025-01-27", periodEnd: "2025-02-02" },
    ]);
  });

  it("falls back to the modeled artifact weeks when no overlap has data", () => {
    expect(
      preferAvailableWeeksWithData({
        availableWeeks: [
          { periodStart: "2025-06-30", periodEnd: "2025-07-06" },
        ],
        weeksWithData: [
          { periodStart: "2025-01-27", periodEnd: "2025-02-02" },
        ],
      }),
    ).toEqual([
      { periodStart: "2025-06-30", periodEnd: "2025-07-06" },
    ]);
  });

  it("falls back to the reference date when no detection timestamp is available", () => {
    expect(
      buildDefaultAlertsDateWindow({
        latestDetectedAt: null,
        referenceNow: new Date("2026-05-21T02:10:00Z"),
      }),
    ).toEqual({
      latestAvailableDate: "2026-05-21",
      dateFrom: "2026-05-08",
      dateTo: "2026-05-21",
      label: "Latest available 14 days · 2026-05-08 to 2026-05-21",
    });
  });

  it("ignores artifact weeks that extend beyond local BirdWeather coverage", () => {
    expect(
      assessAlertsArtifactCoverage({
        availableWeeks: [
          { periodStart: "2024-01-01", periodEnd: "2024-01-07" },
          { periodStart: "2026-05-11", periodEnd: "2026-05-17" },
        ],
        latestDetectedAt: "2024-06-09T12:15:00Z",
        latestEmbeddedDetectedAt: null,
      }),
    ).toEqual({
      trusted: false,
      latestArtifactDate: "2026-05-17",
      latestDetectedDate: "2024-06-09",
      latestEmbeddedDate: null,
      reason:
        "Ignored persisted weekly modeling run because the latest modeled window ends 2026-05-17, ahead of local BirdWeather coverage ending 2024-06-09.",
    });
  });

  it("falls back to raw BirdWeather coverage when no embedded clips exist locally", () => {
    expect(
      buildDefaultAlertsDateWindow({
        latestDetectedAt: selectLatestAlertsCoverageDate({
          latestDetectedAt: "2024-06-09T12:15:00Z",
          latestEmbeddedDetectedAt: null,
        }),
      }),
    ).toEqual({
      latestAvailableDate: "2024-06-09",
      dateFrom: "2024-05-27",
      dateTo: "2024-06-09",
      label: "Latest available 14 days · 2024-05-27 to 2024-06-09",
    });
  });

  it("snaps explicit date picks to modeled Monday-to-Sunday weeks", () => {
    expect(alertsWeekStartDateInput("2024-01-03")).toBe("2024-01-01");
    expect(alertsWeekEndDateInput("2024-01-03")).toBe("2024-01-07");
  });

  it("snaps explicit start and end dates to full modeled weeks", () => {
    const defaults = buildDefaultAlertsDateWindow({
      availableWeeks: [
        { periodStart: "2024-01-01", periodEnd: "2024-01-07" },
        { periodStart: "2024-01-08", periodEnd: "2024-01-14" },
      ],
    });

    expect(
      normalizeAlertsDateRange(
        { dateFrom: "2024-01-03", dateTo: "2024-01-10" },
        defaults,
      ),
    ).toEqual({
      dateFrom: "2024-01-01",
      dateTo: "2024-01-14",
      dateFromIso: "2024-01-01T00:00:00.000Z",
      dateToIso: "2024-01-14T23:59:59.999Z",
      rangeLabel: "Weekly range · 2024-01-01 to 2024-01-14",
    });
  });

  it("expands one-sided explicit date picks to a full modeled week", () => {
    const defaults = buildDefaultAlertsDateWindow({
      availableWeeks: [
        { periodStart: "2024-01-01", periodEnd: "2024-01-07" },
        { periodStart: "2024-01-08", periodEnd: "2024-01-14" },
      ],
    });

    expect(
      normalizeAlertsDateRange({ dateTo: "2024-01-10" }, defaults),
    ).toEqual({
      dateFrom: "2024-01-08",
      dateTo: "2024-01-14",
      dateFromIso: "2024-01-08T00:00:00.000Z",
      dateToIso: "2024-01-14T23:59:59.999Z",
      rangeLabel: "Weekly range · 2024-01-08 to 2024-01-14",
    });
  });

  it("falls back to the default window when the URL has no valid explicit dates", () => {
    const defaults = buildDefaultAlertsDateWindow({
      availableWeeks: [
        { periodStart: "2024-01-01", periodEnd: "2024-01-07" },
        { periodStart: "2024-01-08", periodEnd: "2024-01-14" },
      ],
    });

    expect(
      normalizeAlertsDateRange(
        { dateFrom: "not-a-date", dateTo: "" },
        defaults,
      ),
    ).toEqual({
      dateFrom: "2024-01-08",
      dateTo: "2024-01-14",
      dateFromIso: "2024-01-08T00:00:00.000Z",
      dateToIso: "2024-01-14T23:59:59.999Z",
      rangeLabel: "Latest available 1 week · 2024-01-08 to 2024-01-14",
    });
  });

  it("falls back to the default modeled week when the URL targets a stale unavailable week", () => {
    const defaults = buildDefaultAlertsDateWindow({
      availableWeeks: [
        { periodStart: "2024-01-01", periodEnd: "2024-01-07" },
        { periodStart: "2024-01-08", periodEnd: "2024-01-14" },
      ],
    });

    expect(
      normalizeAlertsDateRange(
        { dateFrom: "2025-06-23", dateTo: "2025-06-29" },
        defaults,
        [
          { periodStart: "2024-01-01", periodEnd: "2024-01-07" },
          { periodStart: "2024-01-08", periodEnd: "2024-01-14" },
        ],
      ),
    ).toEqual({
      dateFrom: "2024-01-08",
      dateTo: "2024-01-14",
      dateFromIso: "2024-01-08T00:00:00.000Z",
      dateToIso: "2024-01-14T23:59:59.999Z",
      rangeLabel: "Latest available 1 week · 2024-01-08 to 2024-01-14",
    });
  });

  it("allows explicit scored weeks outside the projection-limited default window", () => {
    const defaults = buildDefaultAlertsDateWindow({
      availableWeeks: [
        { periodStart: "2024-09-09", periodEnd: "2024-09-15" },
        { periodStart: "2024-09-16", periodEnd: "2024-09-22" },
      ],
    });

    expect(
      normalizeAlertsDateRange(
        { dateFrom: "2024-11-25", dateTo: "2024-12-01" },
        defaults,
        [
          { periodStart: "2024-09-09", periodEnd: "2024-09-15" },
          { periodStart: "2024-09-16", periodEnd: "2024-09-22" },
          { periodStart: "2024-11-25", periodEnd: "2024-12-01" },
        ],
      ),
    ).toEqual({
      dateFrom: "2024-11-25",
      dateTo: "2024-12-01",
      dateFromIso: "2024-11-25T00:00:00.000Z",
      dateToIso: "2024-12-01T23:59:59.999Z",
      rangeLabel: "Weekly range · 2024-11-25 to 2024-12-01",
    });
  });

  it("keeps alerts confidence normalization behavior unchanged", () => {
    expect(readConfidenceParam("80")).toBe("0.8");
    expect(readConfidenceParam("1.4")).toBe("0.01");
    expect(readConfidenceParam("-2")).toBe("");
    expect(readConfidenceParam(undefined)).toBe("");
  });
});
