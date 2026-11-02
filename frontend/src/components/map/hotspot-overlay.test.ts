import { describe, expect, it } from "vitest";

import { buildHotspotOverlayFeatureCollection } from "./hotspot-overlay";
import type { HotspotSummaryUnit } from "../../app/alerts/hotspot-summary";

function unit(
  overrides: Partial<HotspotSummaryUnit>,
): HotspotSummaryUnit {
  return {
    location_type: "grid",
    location_id: "nl-5km-r1-c1",
    location_name: "nl-5km-r1-c1",
    admin_level: null,
    grid_cell_id: "nl-5km-r1-c1",
    period_start: "2024-11-25",
    score: 0.7,
    bioacoustic_alert_rows: 4,
    renderable: true,
    ...overrides,
  };
}

describe("hotspot overlay feature joins", () => {
  it("joins grid hotspot rows by grid_cell_id", () => {
    const overlay = buildHotspotOverlayFeatureCollection({
      grain: "grid",
      source: "bioacoustic",
      hotspots: [
        unit({ grid_cell_id: "nl-5km-r2-c3", location_id: "nl-5km-r2-c3" }),
      ],
      regions: {
        type: "FeatureCollection",
        features: [
          {
            type: "Feature",
            geometry: {
              type: "Polygon",
              coordinates: [[[4, 52], [5, 52], [5, 53], [4, 53], [4, 52]]],
            },
            properties: { grid_cell_id: "nl-5km-r2-c3" },
          },
        ],
      },
    });

    expect(overlay?.features).toHaveLength(1);
    expect(overlay?.features[0].properties.hotspot_location_id).toBe(
      "nl-5km-r2-c3",
    );
    expect(overlay?.features[0].properties.hotspot_score).toBe(0.7);
  });

  it("joins admin hotspot rows by municipality slug", () => {
    const overlay = buildHotspotOverlayFeatureCollection({
      grain: "admin_area",
      source: "bioacoustic",
      hotspots: [
        unit({
          location_type: "admin_area",
          location_id: "municipality:s-hertogenbosch",
          location_name: "'s-Hertogenbosch",
          admin_level: "municipality",
          grid_cell_id: null,
          bioacoustic_alert_rows: 2,
        }),
      ],
      regions: {
        type: "FeatureCollection",
        features: [
          {
            type: "Feature",
            geometry: {
              type: "Polygon",
              coordinates: [[[5, 51], [6, 51], [6, 52], [5, 52], [5, 51]]],
            },
            properties: { naam: "'s-Hertogenbosch" },
          },
        ],
      },
    });

    expect(overlay?.features).toHaveLength(1);
    expect(overlay?.features[0].properties.hotspot_source).toBe("bioacoustic");
    expect(overlay?.features[0].properties.hotspot_bioacoustic_alert_rows).toBe(2);
  });

  it("excludes non-renderable city fallback admin rows", () => {
    const overlay = buildHotspotOverlayFeatureCollection({
      grain: "admin_area",
      source: "bioacoustic",
      hotspots: [
        unit({
          location_type: "admin_area",
          location_id: "city:rotterdam",
          location_name: "Rotterdam",
          admin_level: "city",
          grid_cell_id: null,
          renderable: false,
        }),
      ],
      regions: {
        type: "FeatureCollection",
        features: [
          {
            type: "Feature",
            geometry: {
              type: "Polygon",
              coordinates: [[[4, 51], [5, 51], [5, 52], [4, 52], [4, 51]]],
            },
            properties: { naam: "Rotterdam" },
          },
        ],
      },
    });

    expect(overlay).toBeNull();
  });
});
