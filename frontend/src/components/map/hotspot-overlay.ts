import {
  municipalityLocationIdForName,
  type HotspotGrain,
  type HotspotSource,
  type HotspotSummaryUnit,
} from "../../app/alerts/hotspot-summary";

type HotspotOverlayProperties = Record<string, unknown> & {
  hotspot_source: HotspotSource;
  hotspot_covered: boolean;
  hotspot_score: number;
  hotspot_period_start: string;
  hotspot_location_id: string;
  hotspot_location_name: string;
  hotspot_bioacoustic_alert_rows: number;
  hotspot_fill_color: string;
  hotspot_line_color: string;
};

export type HotspotOverlayFeature = GeoJSON.Feature<
  GeoJSON.Geometry,
  HotspotOverlayProperties
>;

export type HotspotOverlayFeatureCollection = GeoJSON.FeatureCollection<
  GeoJSON.Geometry,
  HotspotOverlayProperties
>;

// Any selected hotspot renders covered cells in red; cells in the tessellation
// with no signal for that source render in a darker gray so coverage gaps read
// at a glance. The fill layer keys opacity off `hotspot_covered`: covered cells
// scale opacity with score, uncovered cells use a fixed, clearly-visible level.
// Colors are solid (rgb) — the layer is the single source of opacity.
const COVERED_COLORS = {
  fill: "rgb(248,113,113)",
  line: "rgb(252,165,165)",
};
const UNCOVERED_COLORS = {
  fill: "rgb(51,65,85)",
  line: "rgb(100,116,139)",
};

function readProperties(
  feature: GeoJSON.Feature | null | undefined,
): Record<string, unknown> {
  return feature?.properties && typeof feature.properties === "object"
    ? (feature.properties as Record<string, unknown>)
    : {};
}

function geometryKeyForFeature(
  feature: GeoJSON.Feature,
  grain: HotspotGrain,
): string {
  const properties = readProperties(feature);
  if (grain === "grid") {
    return String(
      properties.grid_cell_id ?? properties.station_id ?? "",
    ).trim();
  }
  return municipalityLocationIdForName(
    properties.naam ?? properties.name ?? properties.municipality,
  );
}

function hotspotKey(unit: HotspotSummaryUnit, grain: HotspotGrain): string {
  if (grain === "grid") {
    return String(unit.grid_cell_id ?? unit.location_id).trim();
  }
  return unit.location_id;
}

function bestRenderableHotspotByKey(
  hotspots: readonly HotspotSummaryUnit[],
  grain: HotspotGrain,
): Map<string, HotspotSummaryUnit> {
  const bestByKey = new Map<string, HotspotSummaryUnit>();
  for (const hotspot of hotspots) {
    if (!hotspot.renderable || hotspot.location_type !== grain) continue;

    const key = hotspotKey(hotspot, grain);
    if (!key) continue;

    const current = bestByKey.get(key);
    if (
      !current ||
      hotspot.score > current.score ||
      (hotspot.score === current.score &&
        hotspot.period_start.localeCompare(current.period_start) > 0)
    ) {
      bestByKey.set(key, hotspot);
    }
  }
  return bestByKey;
}

export function buildHotspotOverlayFeatureCollection({
  regions,
  hotspots,
  grain,
  source,
}: {
  regions: GeoJSON.FeatureCollection | null | undefined;
  hotspots: readonly HotspotSummaryUnit[];
  grain: HotspotGrain;
  source: HotspotSource;
}): HotspotOverlayFeatureCollection | null {
  if (!regions?.features?.length || hotspots.length === 0) return null;

  const bestByKey = bestRenderableHotspotByKey(hotspots, grain);
  if (bestByKey.size === 0) return null;

  const features: HotspotOverlayFeature[] = [];
  for (const feature of regions.features) {
    if (!feature.geometry) continue;

    const key = geometryKeyForFeature(feature, grain);
    const hotspot = key ? bestByKey.get(key) : undefined;
    const props = readProperties(feature);

    if (hotspot) {
      features.push({
        type: "Feature",
        geometry: feature.geometry,
        properties: {
          ...props,
          hotspot_source: source,
          hotspot_covered: true,
          hotspot_score: hotspot.score,
          hotspot_period_start: hotspot.period_start,
          hotspot_location_id: hotspot.location_id,
          hotspot_location_name: hotspot.location_name,
          hotspot_bioacoustic_alert_rows: hotspot.bioacoustic_alert_rows,
          hotspot_fill_color: COVERED_COLORS.fill,
          hotspot_line_color: COVERED_COLORS.line,
        },
      });
    } else {
      // No signal for this source here → faint gray "no coverage" cell.
      features.push({
        type: "Feature",
        geometry: feature.geometry,
        properties: {
          ...props,
          hotspot_source: source,
          hotspot_covered: false,
          hotspot_score: 0,
          hotspot_period_start: "",
          hotspot_location_id: String(
            props.grid_cell_id ?? props.station_id ?? props.naam ?? props.name ?? "",
          ),
          hotspot_location_name: String(
            props.naam ?? props.name ?? props.municipality ?? props.grid_cell_id ?? "",
          ),
          hotspot_bioacoustic_alert_rows: 0,
          hotspot_fill_color: UNCOVERED_COLORS.fill,
          hotspot_line_color: UNCOVERED_COLORS.line,
        },
      });
    }
  }

  return features.length > 0
    ? {
        type: "FeatureCollection",
        features,
      }
    : null;
}
