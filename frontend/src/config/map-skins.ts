import type { StyleSpecification } from "maplibre-gl";

import {
  ENVIRONMENTAL_PROFILING_STYLE,
  NETHERLANDS_BOUNDS,
  SATELLITE_STYLE,
} from "@/config/map";

export type MapSkinId = "satellite" | "black_white" | "lcz" | "standard";

export interface MapSkinMeta {
  id: MapSkinId;
  label: string;
  shortLabel: string;
  description: string;
  attribution?: string;
}

export const MAP_SKIN_ORDER: MapSkinId[] = [
  "standard",
  "satellite",
  "black_white",
  "lcz",
];

export const MAP_SKIN_META: Record<MapSkinId, MapSkinMeta> = {
  standard: {
    id: "standard",
    label: "Map",
    shortLabel: "Map",
    description: "Road and place map using the existing Carto vector tiles.",
    attribution: "CARTO, OpenStreetMap contributors",
  },
  satellite: {
    id: "satellite",
    label: "Satellite (Colored)",
    shortLabel: "Sat",
    description: "Full-color satellite and aerial imagery.",
    attribution: "Esri, Maxar, Earthstar Geographics, PDOK Luchtfoto",
  },
  black_white: {
    id: "black_white",
    label: "Satellite (Black and White)",
    shortLabel: "B/W",
    description: "Monochrome satellite imagery for higher contrast overlays.",
    attribution: "Esri, Maxar, Earthstar Geographics, PDOK Luchtfoto",
  },
  lcz: {
    id: "lcz",
    label: "Local Climate Zone (LCZ)",
    shortLabel: "LCZ",
    description: "Local Climate Zones classification map by Demuzere et al. (2022).",
    attribution: "LCZ map © Matthias Demuzere et al. 2022 · DOI: 10.5194/essd-14-3835-2022",
  },
};

function buildStandardStyle(): StyleSpecification {
  return {
    version: 8,
    name: "Avian Biosurveillance Standard",
    glyphs: "https://fonts.openmaptiles.org/{fontstack}/{range}.pbf",
    sources: {
      carto: {
        type: "vector",
        url: "https://tiles.basemaps.cartocdn.com/vector/carto.streets/v1/tiles.json",
        attribution: MAP_SKIN_META.standard.attribution,
      },
    },
    layers: [
      {
        id: "background",
        type: "background",
        paint: {
          "background-color": "#eef2ef",
        },
      },
      {
        id: "landcover-wood",
        type: "fill",
        source: "carto",
        "source-layer": "landcover",
        filter: ["in", ["get", "class"], ["literal", ["wood", "grass", "scrub", "crop"]]],
        paint: {
          "fill-color": [
            "match",
            ["get", "class"],
            "wood",
            "#d9e8d3",
            "grass",
            "#e7f0da",
            "scrub",
            "#e1ead1",
            "crop",
            "#efe8cc",
            "#e6edd9",
          ],
          "fill-opacity": 0.96,
        },
      },
      {
        id: "park",
        type: "fill",
        source: "carto",
        "source-layer": "park",
        paint: {
          "fill-color": "#dcebd5",
          "fill-opacity": 0.92,
        },
      },
      {
        id: "landuse",
        type: "fill",
        source: "carto",
        "source-layer": "landuse",
        filter: ["in", ["get", "class"], ["literal", ["residential", "industrial", "commercial", "school", "hospital"]]],
        paint: {
          "fill-color": [
            "match",
            ["get", "class"],
            "industrial",
            "#e9e2dc",
            "commercial",
            "#ece5e0",
            "school",
            "#efe8dd",
            "hospital",
            "#f1e6e6",
            "#f4efe7",
          ],
          "fill-opacity": 0.9,
        },
      },
      {
        id: "water",
        type: "fill",
        source: "carto",
        "source-layer": "water",
        paint: {
          "fill-color": "#b6d7ea",
          "fill-opacity": 0.98,
        },
      },
      {
        id: "waterway",
        type: "line",
        source: "carto",
        "source-layer": "waterway",
        paint: {
          "line-color": "#8bc0dd",
          "line-width": [
            "interpolate",
            ["linear"],
            ["zoom"],
            6,
            0.6,
            10,
            1.2,
            14,
            2.2,
          ],
          "line-opacity": 0.9,
        },
      },
      {
        id: "building",
        type: "fill",
        source: "carto",
        "source-layer": "building",
        minzoom: 11,
        paint: {
          "fill-color": "#ddd6cf",
          "fill-opacity": [
            "interpolate",
            ["linear"],
            ["zoom"],
            11,
            0.35,
            14,
            0.65,
          ],
        },
      },
      {
        id: "boundary",
        type: "line",
        source: "carto",
        "source-layer": "boundary",
        paint: {
          "line-color": "rgba(152, 160, 168, 0.72)",
          "line-width": [
            "interpolate",
            ["linear"],
            ["zoom"],
            5,
            0.4,
            8,
            0.8,
            12,
            1.1,
          ],
          "line-dasharray": [2, 2],
        },
      },
      {
        id: "road-minor",
        type: "line",
        source: "carto",
        "source-layer": "transportation",
        filter: ["in", ["get", "class"], ["literal", ["minor", "service", "track", "path"]]],
        paint: {
          "line-color": "#ffffff",
          "line-width": [
            "interpolate",
            ["linear"],
            ["zoom"],
            10,
            0.4,
            12,
            0.8,
            14,
            1.4,
          ],
          "line-opacity": [
            "interpolate",
            ["linear"],
            ["zoom"],
            10,
            0,
            11,
            0.72,
            14,
            0.92,
          ],
        },
      },
      {
        id: "road-main",
        type: "line",
        source: "carto",
        "source-layer": "transportation",
        filter: ["in", ["get", "class"], ["literal", ["motorway", "trunk", "primary", "secondary", "tertiary"]]],
        paint: {
          "line-color": [
            "match",
            ["get", "class"],
            "motorway",
            "#e7b67c",
            "trunk",
            "#ebc58d",
            "primary",
            "#efd9a8",
            "secondary",
            "#f4e7bc",
            "#f6eed0",
          ],
          "line-width": [
            "interpolate",
            ["linear"],
            ["zoom"],
            6,
            0.8,
            10,
            1.6,
            12,
            2.6,
            14,
            4.6,
          ],
          "line-opacity": 0.96,
        },
      },
      {
        id: "road-rail",
        type: "line",
        source: "carto",
        "source-layer": "transportation",
        filter: ["in", ["get", "class"], ["literal", ["rail", "transit"]]],
        paint: {
          "line-color": "#a6adb5",
          "line-width": [
            "interpolate",
            ["linear"],
            ["zoom"],
            6,
            0.5,
            12,
            1.1,
            14,
            1.8,
          ],
          "line-dasharray": [1, 1.4],
          "line-opacity": 0.72,
        },
      },
      {
        id: "road-name",
        type: "symbol",
        source: "carto",
        "source-layer": "transportation_name",
        minzoom: 11.5,
        layout: {
          "symbol-placement": "line",
          "text-field": ["get", "name"],
          "text-font": ["Open Sans Regular"],
          "text-size": ["interpolate", ["linear"], ["zoom"], 11.5, 9, 14, 11],
          "text-letter-spacing": 0.02,
        },
        paint: {
          "text-color": "rgba(98, 106, 112, 0.78)",
          "text-halo-color": "rgba(246, 245, 241, 0.92)",
          "text-halo-width": 1,
        },
      },
      {
        id: "label-city-major",
        type: "symbol",
        source: "carto",
        "source-layer": "place",
        minzoom: 6.8,
        filter: ["all", ["==", "class", "city"], ["<=", "rank", 5]],
        layout: {
          "text-field": ["get", "name"],
          "text-font": ["Open Sans Regular"],
          "text-size": ["interpolate", ["linear"], ["zoom"], 6.8, 11, 12, 15],
          "text-transform": "uppercase",
          "text-letter-spacing": 0.12,
        },
        paint: {
          "text-color": "rgba(51, 57, 62, 0.92)",
          "text-halo-color": "rgba(246, 245, 241, 0.95)",
          "text-halo-width": 1.5,
        },
      },
      {
        id: "label-city-minor",
        type: "symbol",
        source: "carto",
        "source-layer": "place",
        minzoom: 7.4,
        filter: ["all", ["==", "class", "city"], [">", "rank", 5]],
        layout: {
          "text-field": ["get", "name"],
          "text-font": ["Open Sans Regular"],
          "text-size": ["interpolate", ["linear"], ["zoom"], 7.4, 10, 14, 13],
          "text-letter-spacing": 0.04,
        },
        paint: {
          "text-color": "rgba(75, 81, 86, 0.86)",
          "text-halo-color": "rgba(246, 245, 241, 0.94)",
          "text-halo-width": 1.4,
        },
      },
      {
        id: "label-town",
        type: "symbol",
        source: "carto",
        "source-layer": "place",
        minzoom: 8.5,
        filter: ["all", ["==", "class", "town"]],
        layout: {
          "text-field": ["get", "name"],
          "text-font": ["Open Sans Regular"],
          "text-size": ["interpolate", ["linear"], ["zoom"], 8.5, 9, 15, 11],
        },
        paint: {
          "text-color": "rgba(92, 97, 102, 0.78)",
          "text-halo-color": "rgba(246, 245, 241, 0.92)",
          "text-halo-width": 1.2,
        },
      },
      {
        id: "label-village",
        type: "symbol",
        source: "carto",
        "source-layer": "place",
        minzoom: 11.8,
        filter: ["all", ["in", "class", "village", "suburb"]],
        layout: {
          "text-field": ["get", "name"],
          "text-font": ["Open Sans Regular"],
          "text-size": 9,
        },
        paint: {
          "text-color": "rgba(108, 114, 118, 0.7)",
          "text-halo-color": "rgba(246, 245, 241, 0.88)",
          "text-halo-width": 1,
        },
      },
    ],
  };
}

function buildLczOverlayLayers(): StyleSpecification["layers"] {
  return ENVIRONMENTAL_PROFILING_STYLE.layers.flatMap((layer) => {
    if (layer.id === "pdok-satellite") {
      return [
        {
          ...layer,
          paint: {
            ...(layer.paint ?? {}),
            "raster-opacity": [
              "interpolate",
              ["linear"],
              ["zoom"],
              8,
              0,
              10,
              0.9,
            ],
            "raster-brightness-min": 0.08,
            "raster-brightness-max": 0.76,
          },
        } as StyleSpecification["layers"][number],
        {
          id: "lcz-raster",
          type: "raster",
          source: "lcz",
          paint: {
            "raster-opacity": [
              "interpolate",
              ["linear"],
              ["zoom"],
              6,
              0.46,
              8.5,
              0.58,
              11,
              0.68,
              14,
              0.76,
            ],
            "raster-contrast": 0.02,
            "raster-brightness-min": 0.1,
            "raster-brightness-max": 0.92,
            "raster-resampling": "nearest",
          },
        } as StyleSpecification["layers"][number],
      ];
    }

    if (layer.id === "label-city-major") {
      return [
        {
          ...layer,
          paint: {
            ...(layer.paint ?? {}),
            "text-color": "rgba(235, 245, 239, 0.72)",
            "text-halo-color": "rgba(5, 8, 6, 0.78)",
            "text-halo-width": 1.4,
          },
        } as StyleSpecification["layers"][number],
      ];
    }

    if (layer.id === "label-city-minor") {
      return [
        {
          ...layer,
          paint: {
            ...(layer.paint ?? {}),
            "text-color": "rgba(214, 232, 220, 0.56)",
            "text-halo-color": "rgba(5, 8, 6, 0.72)",
            "text-halo-width": 1.25,
          },
        } as StyleSpecification["layers"][number],
      ];
    }

    if (layer.id === "label-town") {
      return [
        {
          ...layer,
          paint: {
            ...(layer.paint ?? {}),
            "text-color": "rgba(194, 216, 203, 0.42)",
            "text-halo-color": "rgba(5, 8, 6, 0.64)",
            "text-halo-width": 1,
          },
        } as StyleSpecification["layers"][number],
      ];
    }

    return [{ ...layer } as StyleSpecification["layers"][number]];
  });
}

function buildLczStyle(): StyleSpecification {
  return {
    ...ENVIRONMENTAL_PROFILING_STYLE,
    name: "Avian Biosurveillance LCZ",
    sources: {
      ...ENVIRONMENTAL_PROFILING_STYLE.sources,
      lcz: {
        type: "raster",
        tiles: [
          "https://lcz-generator.rub.de/tms/global-map-tiles/v3/{z}/{x}/{y}.png",
        ],
        scheme: "xyz",
        tileSize: 256,
        attribution: MAP_SKIN_META.lcz.attribution,
        minzoom: 2,
        maxzoom: 13,
        bounds: [...NETHERLANDS_BOUNDS],
      },
    },
    layers: buildLczOverlayLayers(),
  };
}

const MAP_SKIN_STYLE_MAP: Record<MapSkinId, StyleSpecification> = {
  standard: buildStandardStyle(),
  satellite: SATELLITE_STYLE,
  black_white: ENVIRONMENTAL_PROFILING_STYLE,
  lcz: buildLczStyle(),
};

export function getMapSkinStyle(skin: MapSkinId): StyleSpecification {
  return MAP_SKIN_STYLE_MAP[skin];
}
