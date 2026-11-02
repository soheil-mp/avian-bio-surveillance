"use client";

import { useCallback, useState } from "react";
import { HeroMap } from "./HeroMap";
import { LiveDetectionsTable, type LiveDetection } from "./LiveDetectionsTable";
import { SpeciesTracker } from "./SpeciesTracker";
import { STATION_TYPE_IDS } from "@/config/stations";

const FLOATING_PANEL_CLASS = "h-[82vh] min-h-[560px] max-h-[960px] w-[340px]";
const PANEL_SHELL_STYLE = {
  borderColor: "rgba(255,255,255,0.08)",
  background: "rgba(18,18,18,0.72)",
  backdropFilter: "blur(20px)",
  boxShadow: "0 18px 64px rgba(0,0,0,0.58), inset 0 1px 0 rgba(255,255,255,0.03)",
};

export interface MapFilters {
  types: string[];
  species: string[];
  minQuality: number;
  showStations: boolean;
}

export function HomeContent({ initialDetections = [] }: { initialDetections?: LiveDetection[] }) {
  const [filters, setFilters] = useState<MapFilters>({ types: STATION_TYPE_IDS, species: [], minQuality: 0, showStations: true });
  const [focusedStationId, setFocusedStationId] = useState<string | null>(null);
  const [mapStationId, setMapStationId] = useState<string | null>(null);
  const [mapGridId, setMapGridId] = useState<string | null>(null);
  const [detailFocus, setDetailFocus] = useState<{ lat: number; lon: number } | null>(null);

  const handleFilterChange = useCallback((next: MapFilters) => {
    setFilters(next);
  }, []);

  const handleStationSelect = useCallback((stationId: string | null) => {
    setMapStationId(stationId);
    if (stationId) setMapGridId(null);
  }, []);

  const handleGridSelect = useCallback((gridCellId: string | null) => {
    setMapGridId(gridCellId);
    if (gridCellId) setMapStationId(null);
  }, []);

  return (
    <div className="relative h-full w-full bg-surface-base overflow-hidden">
      {/* Map fills the entire background */}
      <div className="absolute inset-0">
        <HeroMap onFilterChange={handleFilterChange} focusedStationId={focusedStationId} onStationSelect={handleStationSelect} onGridSelect={handleGridSelect} flyFocus={detailFocus} />
      </div>

      {/* Floating live detections table on the left, vertically centered */}
      <div className={`absolute top-1/2 -translate-y-1/2 left-5 z-20 pointer-events-none ${FLOATING_PANEL_CLASS}`}>
        <div
          className="h-full w-full pointer-events-auto rounded-2xl overflow-hidden border"
          style={PANEL_SHELL_STYLE}
        >
          <LiveDetectionsTable initialData={initialDetections} filters={filters} spotlightStationId={mapStationId} spotlightGridCellId={mapGridId} onDetailFocusChange={setDetailFocus} />
        </div>
      </div>

      {/* Floating species tracker on the right, vertically centered */}
      <div className={`absolute top-1/2 -translate-y-1/2 right-5 z-20 pointer-events-none ${FLOATING_PANEL_CLASS}`}>
        <div
          className="h-full w-full pointer-events-auto rounded-2xl overflow-hidden border"
          style={PANEL_SHELL_STYLE}
        >
          <SpeciesTracker onFocusStation={setFocusedStationId} />
        </div>
      </div>

    </div>
  );
}
