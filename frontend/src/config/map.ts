import type { StyleSpecification } from "maplibre-gl";

/** Geographic center of the Netherlands (WGS 84). */
export const NETHERLANDS_CENTER = { lng: 5.2913, lat: 52.1326 } as const;

/** Shows the full country shape including the coastline. */
export const DEFAULT_ZOOM = 7.0;

/** Maximum tilt angle. 85 allows viewing buildings almost horizontally. */
export const MAX_PITCH = 85;

/** Default pitch. */
export const DEFAULT_PITCH = 30;

/** Slight rotation for a dramatic aerial angle. */
export const DEFAULT_BEARING = -10;

/** Bounding box enclosing the Netherlands (SW, NE corners). */
export const NETHERLANDS_BOUNDS: [number, number, number, number] = [
  3.31, 50.75, 7.21, 53.55,
];

/**
 * PDOK WFS endpoint for Dutch province boundaries.
 */
export const PROVINCES_WFS_URL =
  "https://service.pdok.nl/kadaster/bestuurlijkegebieden/wfs/v1_0?" +
  "service=WFS&version=2.0.0&request=GetFeature&" +
  "typeName=bestuurlijkegebieden:Provinciegebied&" +
  "outputFormat=json&srsName=EPSG:4326";

/**
 * PDOK WFS endpoint for the Dutch national boundary.
 * Used to create an inverted polygon mask that hides other countries.
 */
export const COUNTRY_WFS_URL =
  "https://service.pdok.nl/kadaster/bestuurlijkegebieden/wfs/v1_0?" +
  "service=WFS&version=2.0.0&request=GetFeature&" +
  "typeName=bestuurlijkegebieden:Landgebied&" +
  "outputFormat=json&srsName=EPSG:4326";

/**
 * PDOK WFS endpoint for Dutch municipality boundaries.
 */
export const MUNICIPALITIES_WFS_URL =
  "https://service.pdok.nl/kadaster/bestuurlijkegebieden/wfs/v1_0?" +
  "service=WFS&version=2.0.0&request=GetFeature&" +
  "typeName=bestuurlijkegebieden:Gemeentegebied&" +
  "outputFormat=json&srsName=EPSG:4326";

/**
 * MapLibre style: satellite imagery with surveillance-grade color grading.
 *
 * Layers:
 *  - Esri World Imagery (global, desaturated + boosted contrast)
 *  - PDOK Luchtfoto (Dutch aerial, fades in at z8+)
 *
 * Glyphs from OpenMapTiles CDN for province labels.
 */
export const SATELLITE_STYLE: StyleSpecification = {
  version: 8,
  name: "Avian Biosurveillance Satellite",
  glyphs: "https://fonts.openmaptiles.org/{fontstack}/{range}.pbf",
  light: {
    anchor: "viewport",
    color: "#f8fafc",
    intensity: 0.35,
    position: [1.15, 210, 30] // cast dramatic shadows
  },
  sources: {
    "esri-world-imagery": {
      type: "raster",
      tiles: [
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      ],
      tileSize: 256,
      attribution:
        "Esri, Maxar, Earthstar Geographics, and the GIS User Community",
      maxzoom: 19,
    },
    "pdok-luchtfoto": {
      type: "raster",
      tiles: [
        "https://service.pdok.nl/hwh/luchtfotorgb/wmts/v1_0/Actueel_orthoHR/EPSG:3857/{z}/{x}/{y}.jpeg",
      ],
      tileSize: 256,
      attribution: "PDOK Luchtfoto &copy; Kadaster / Beeldmateriaal.nl",
      minzoom: 8,
      maxzoom: 20,
    },
    "carto": {
      type: "vector",
      url: "https://tiles.basemaps.cartocdn.com/vector/carto.streets/v1/tiles.json",
    },
    "terrain": {
      type: "raster-dem",
      tiles: [
        "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png",
      ],
      encoding: "terrarium",
      tileSize: 256,
    },
  },
  terrain: {
    source: "terrain",
    exaggeration: 1.5,
  },
  layers: [
    {
      id: "background",
      type: "background",
      paint: {
        "background-color": "#111111",
      },
    },
    {
      id: "esri-satellite",
      type: "raster",
      source: "esri-world-imagery",
      paint: {
        "raster-saturation": -0.15,
        "raster-contrast": 0.1,
        "raster-brightness-min": 0.04,
      },
    },
    {
      id: "pdok-satellite",
      type: "raster",
      source: "pdok-luchtfoto",
      minzoom: 8,
      paint: {
        "raster-opacity": [
          "interpolate",
          ["linear"],
          ["zoom"],
          8, 0,
          10, 1,
        ],
        "raster-saturation": -0.1,
        "raster-contrast": 0.05,
      },
    },
    {
      id: "label-city-major",
      type: "symbol",
      source: "carto",
      "source-layer": "place",
      minzoom: 7.5,
      filter: ["all", ["==", "class", "city"], ["<=", "rank", 5]],
      layout: {
        "text-field": ["get", "name"],
        "text-font": ["Open Sans Regular"],
        "text-size": ["interpolate", ["linear"], ["zoom"], 7.5, 12, 12, 16],
        "text-transform": "uppercase",
        "text-letter-spacing": 0.2,
      },
      paint: {
        "text-color": "rgba(167, 243, 208, 0.95)", // brand-200
        "text-halo-color": "rgba(17, 17, 17, 0.9)",
        "text-halo-width": 2,
      },
    },
    {
      id: "label-city-minor",
      type: "symbol",
      source: "carto",
      "source-layer": "place",
      minzoom: 8.0,
      filter: ["all", ["==", "class", "city"], [">", "rank", 5]],
      layout: {
        "text-field": ["get", "name"],
        "text-font": ["Open Sans Regular"],
        "text-size": ["interpolate", ["linear"], ["zoom"], 8.0, 10, 14, 14],
        "text-transform": "uppercase",
        "text-letter-spacing": 0.1,
      },
      paint: {
        "text-color": "rgba(110, 231, 183, 0.85)", // brand-300
        "text-halo-color": "rgba(17, 17, 17, 0.9)",
        "text-halo-width": 2,
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
        "text-size": ["interpolate", ["linear"], ["zoom"], 8.5, 9, 15, 12],
      },
      paint: {
        "text-color": "rgba(52, 211, 153, 0.75)", // brand-400
        "text-halo-color": "rgba(17, 17, 17, 0.9)",
        "text-halo-width": 1.5,
      },
    },
    {
      id: "label-village",
      type: "symbol",
      source: "carto",
      "source-layer": "place",
      minzoom: 12,
      filter: ["all", ["in", "class", "village", "suburb"]],
      layout: {
        "text-field": ["get", "name"],
        "text-font": ["Open Sans Regular"],
        "text-size": 9,
      },
      paint: {
        "text-color": "rgba(16, 185, 129, 0.6)", // brand-500
        "text-halo-color": "rgba(17, 17, 17, 0.8)",
        "text-halo-width": 1,
      },
    },
  ],
};

export const ENVIRONMENTAL_PROFILING_STYLE: StyleSpecification = {
  ...SATELLITE_STYLE,
  name: "Avian Biosurveillance Environmental",
  light: {
    ...SATELLITE_STYLE.light,
    color: "#dcfce7",
    intensity: 0.2,
  },
  layers: SATELLITE_STYLE.layers.map((layer) => {
      if (layer.id === "background") {
        return {
          ...layer,
          paint: {
            ...(layer.paint ?? {}),
            "background-color": "#050806",
          },
        } as StyleSpecification["layers"][number];
      }

      if (layer.id === "esri-satellite") {
        return {
          ...layer,
          paint: {
            ...(layer.paint ?? {}),
            "raster-saturation": -1,
            "raster-contrast": 0.04,
            "raster-brightness-min": 0.02,
            "raster-brightness-max": 0.7,
          },
        } as StyleSpecification["layers"][number];
      }

      if (layer.id === "pdok-satellite") {
        return {
          ...layer,
          paint: {
            ...(layer.paint ?? {}),
            "raster-opacity": [
              "interpolate",
              ["linear"],
              ["zoom"],
              8, 0,
              10, 0.82,
            ],
            "raster-saturation": -1,
            "raster-contrast": 0.02,
            "raster-brightness-min": 0.06,
            "raster-brightness-max": 0.72,
          },
        } as StyleSpecification["layers"][number];
      }

      if (layer.id.startsWith("label-")) {
        return {
          ...layer,
          paint: {
            ...(layer.paint ?? {}),
            "text-color": layer.id === "label-city-major"
              ? "rgba(220, 252, 231, 0.9)"
              : layer.id === "label-city-minor"
                ? "rgba(187, 247, 208, 0.78)"
                : "rgba(134, 239, 172, 0.66)",
            "text-halo-color": "rgba(5, 8, 6, 0.92)",
          },
        } as StyleSpecification["layers"][number];
      }

      return { ...layer } as StyleSpecification["layers"][number];
    }),
};
