
# Geographic visualization or Satellite map visualization

## Selected: MapLibre GL JS via react-map-gl

**Why MapLibre GL JS:**
- WebGL/GPU-accelerated rendering — handles thousands of BirdWeather station markers efficiently
- Free and open-source (no API key for the library itself)
- Deck.gl can be layered on top later for heatmaps, arcs, and advanced data visualization
- Mature React wrapper (`react-map-gl` by Vis.gl/Uber)
- Supports raster satellite tiles, 3D terrain, and custom layers

**Satellite tile sources (free, no API key):**
- PDOK Luchtfoto (Dutch aerial photography, ~8 cm/px over NL): `service.pdok.nl`
- Esri World Imagery (global coverage, fallback): `server.arcgisonline.com`

## Other candidates considered

- https://www.rayshader.com/
- CesiumJS — 3D globe, overkill for 2D map use case, heavy bundle
- Aerialod
- D3.js — too low-level for interactive maps
- Blender GIS
- Three.js — too low-level for map visualization
  - https://www.procedural.eu/
  - https://github.com/w3reality/three-geo
- https://developmentseed.org/lonboard/latest/examples/
- https://3dtilesnederland.nl/
- https://deck.gl/ — can be added on top of MapLibre later
- https://github.com/rnd-team-dev/plotoptix

