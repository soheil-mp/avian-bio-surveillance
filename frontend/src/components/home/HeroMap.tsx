"use client";

import type {
    MapViewState,
    SatelliteMapRef,
    Station,
} from "@/components/map/SatelliteMap";
import type {
  HotspotGrain,
  HotspotSource,
} from "@/app/alerts/hotspot-summary";
import {
    EARTH_ENGINE_OVERLAYS,
    EARTH_ENGINE_VISUALIZATIONS,
    EARTH_ENGINE_VISUALIZATION_META,
    type EarthEngineOverlayId,
    type EarthEngineVisualizationMode,
} from "@/config/earth-engine-overlays";
import type { LiveWeatherOverlay } from "@/components/map/WeatherOverlay";
  import {
    MAP_SKIN_META,
    MAP_SKIN_ORDER,
    type MapSkinId,
  } from "@/config/map-skins";
import {
    STATION_QUALITY_LEVELS,
    STATION_TYPE_IDS,
    STATION_TYPE_OPTIONS,
    areAllStationTypesSelected,
    formatStationQualitySummary,
} from "@/config/stations";
import { TARGET_SPECIES } from "@/config/target-species";
import dynamic from "next/dynamic";
import {
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

const SatelliteMap = dynamic(
  () => import("@/components/map/SatelliteMap").then((mod) => mod.SatelliteMap),
  {
    ssr: false,
    loading: () => (
      <div className="w-full h-full flex items-center justify-center">
        <div className="flex flex-col items-center gap-3">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-brand-500 border-t-transparent" />
          <span className="text-xs text-text-muted tracking-widest uppercase">
            Initialising scope&hellip;
          </span>
        </div>
      </div>
    ),
  },
);

const CARDINALS = [
  { label: "N", deg: 0 },
  { label: "E", deg: 90 },
  { label: "S", deg: 180 },
  { label: "W", deg: 270 },
] as const;

const TICKS = Array.from({ length: 72 }, (_, i) => i * 5).filter(
  (d) => d % 90 !== 0,
);

/** Degree labels at 30° intervals (excluding cardinals) */
const DEGREE_LABELS = [30, 60, 120, 150, 210, 240, 300, 330];

function tickHeight(deg: number) {
  if (deg % 30 === 0) return 10;
  if (deg % 10 === 0) return 6;
  return 3;
}

function tickOpacity(deg: number) {
  if (deg % 30 === 0) return 0.25;
  if (deg % 10 === 0) return 0.12;
  return 0.06;
}

/* ── Dropdown trigger + popover (shared styles) ── */
const DROP_BG = "rgba(20,20,20,0.94)";
const DROP_BORDER = "1px solid rgba(255,255,255,0.08)";
const DROP_DIVIDER = "1px solid rgba(255,255,255,0.06)";
const DROP_SHADOW =
  "0 28px 70px rgba(0,0,0,0.58), 0 12px 28px rgba(0,0,0,0.28), inset 0 1px 0 rgba(255,255,255,0.04)";
const TEAL = "rgb(52,211,153)";
const TEAL_DIM = "rgba(52,211,153,0.45)";
const TEAL_BG = "rgba(52,211,153,0.12)";
const TEAL_ROW = "rgba(52,211,153,0.14)";
const WHITE_DIM = "rgba(255,255,255,0.56)";
const DROP_TEXT = "rgba(247,249,250,0.94)";
const DROP_TEXT_DIM = "rgba(255,255,255,0.72)";
const DROP_TEXT_SOFT = "rgba(255,255,255,0.42)";
const DROP_SURFACE = "rgba(255,255,255,0.03)";
const DROP_SURFACE_STRONG = "rgba(255,255,255,0.05)";
const DROP_ITEM_FONT_SIZE = 10;
const DROP_SECTION_FONT_SIZE = 10;
const FILTER_BAR_DROPDOWN_WIDTH = "w-80 max-w-[calc(100vw-2rem)]";
const HOTSPOT_SOURCE_OPTIONS: Array<{
  value: HotspotSource | null;
  label: string;
  detail: string;
}> = [
  { value: null, label: "Off", detail: "No hotspot fill overlay" },
  { value: "bioacoustic", label: "Bioacoustic", detail: "Model anomaly alerts" },
];

const LATEST_WEEK_VALUE = "latest";

function DropdownPanel({
  className,
  children,
}: {
  className: string;
  children: ReactNode;
}) {
  return (
    <div
      className={`absolute bottom-full mb-2 overflow-hidden rounded-[20px] backdrop-blur-2xl z-50 ${className}`}
      style={{
        background: DROP_BG,
        border: DROP_BORDER,
        boxShadow: DROP_SHADOW,
      }}
    >
      <div className="relative font-mono text-[10px]">{children}</div>
    </div>
  );
}

function getDropdownItemStyle(active: boolean, fontSize = DROP_ITEM_FONT_SIZE) {
  return {
    fontSize,
    color: active ? DROP_TEXT : DROP_TEXT_DIM,
    background: active ? TEAL_ROW : undefined,
  };
}

function SkinChip({
  activeSkin,
  onSelectSkin,
}: {
  activeSkin: MapSkinId;
  onSelectSkin: (skin: MapSkinId) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useClickOutside(ref, () => setOpen(false), open);

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex items-center gap-1 px-1.5 py-0.5 rounded-sm transition-colors cursor-pointer"
        style={{ background: "transparent", color: TEAL_DIM }}
      >
        <span className="truncate max-w-[88px]">Map View</span>
        <svg
          width="6"
          height="6"
          viewBox="0 0 6 6"
          fill="currentColor"
          style={{ opacity: 0.5 }}
        >
          <path d="M0 1.5 L3 4.5 L6 1.5" />
        </svg>
      </button>

      {open && (
        <DropdownPanel className={`right-0 ${FILTER_BAR_DROPDOWN_WIDTH}`}>
          <div className="overflow-y-auto p-1.5" style={{ maxHeight: 220 }}>
            {MAP_SKIN_ORDER.map((skinId) => {
              const skin = MAP_SKIN_META[skinId];
              const active = activeSkin === skinId;

              return (
                <button
                  key={skin.id}
                  type="button"
                  onClick={() => {
                    onSelectSkin(skin.id);
                    setOpen(false);
                  }}
                  className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left font-mono transition-colors hover:bg-white/5"
                  style={getDropdownItemStyle(active)}
                >
                  <span
                    className="w-4 h-4 rounded-full border flex items-center justify-center flex-shrink-0"
                    style={{
                      borderColor: active
                        ? "rgba(52,211,153,0.5)"
                        : "rgba(255,255,255,0.15)",
                      background: active
                        ? "rgba(52,211,153,0.16)"
                        : "rgba(255,255,255,0.02)",
                    }}
                  >
                    {active ? (
                      <span
                        className="w-1.5 h-1.5 rounded-full"
                        style={{ background: TEAL }}
                      />
                    ) : null}
                  </span>
                  <span className="truncate leading-tight">
                    {skin.label}
                  </span>
                </button>
              );
            })}
          </div>
        </DropdownPanel>
      )}
    </div>
  );
}

function useClickOutside(
  ref: React.RefObject<HTMLDivElement | null>,
  onClose: () => void,
  active: boolean,
) {
  useEffect(() => {
    if (!active) return;
    const handleClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", handleClick);
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("mousedown", handleClick);
      document.removeEventListener("keydown", handleKey);
    };
  }, [active, ref, onClose]);
}

/* ── Spatial partitioning dropdown ── */
function SpatialPartitionChip({
  activeMode,
  onSelectMode,
}: {
  activeMode: EarthEngineVisualizationMode;
  onSelectMode: (mode: EarthEngineVisualizationMode) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useClickOutside(ref, () => setOpen(false), open);

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1 px-1.5 py-0.5 rounded-sm transition-colors cursor-pointer"
        style={{ background: "transparent", color: TEAL_DIM }}
      >
        <span className="truncate max-w-[132px]">Tessellation</span>
        <svg width="6" height="6" viewBox="0 0 6 6" fill="currentColor" style={{ opacity: 0.5 }}>
          <path d="M0 1.5 L3 4.5 L6 1.5" />
        </svg>
      </button>

      {open && (
        <DropdownPanel className={`left-1/2 ${FILTER_BAR_DROPDOWN_WIDTH} -translate-x-1/2`}>
          <div className="overflow-y-auto p-1.5" style={{ maxHeight: 180 }}>
            {EARTH_ENGINE_VISUALIZATIONS.map((mode) => {
              const active = activeMode === mode.id;
              return (
                <button
                  key={mode.id}
                  type="button"
                  onClick={() => {
                    onSelectMode(mode.id);
                    setOpen(false);
                  }}
                  className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left font-mono transition-colors hover:bg-white/5"
                  style={getDropdownItemStyle(active)}
                >
                  <span
                    className="w-4 h-4 rounded-full border flex items-center justify-center flex-shrink-0"
                    style={{
                      borderColor: active ? "rgba(52,211,153,0.5)" : "rgba(255,255,255,0.15)",
                      background: active ? "rgba(52,211,153,0.2)" : "transparent",
                    }}
                  >
                    {active && <span className="w-1.5 h-1.5 rounded-full" style={{ background: TEAL }} />}
                  </span>
                  <span className="truncate tracking-[0.06em] uppercase">{mode.label}</span>
                </button>
              );
            })}
          </div>
        </DropdownPanel>
      )}
    </div>
  );
}

/* ── Combined Stations dropdown (Type + Quality) ── */
function StationsChip({
  activeTypes,
  onToggleType,
  onSelectAllTypes,
  quality,
  onQualityChange,
  showStations,
  onShowStationsChange,
  onReset,
}: {
  activeTypes: string[];
  onToggleType: (type: string) => void;
  onSelectAllTypes: () => void;
  quality: number;
  onQualityChange: (v: number) => void;
  showStations: boolean;
  onShowStationsChange: (visible: boolean) => void;
  onReset: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [typeMenuOpen, setTypeMenuOpen] = useState(false);
  const [qualityMenuOpen, setQualityMenuOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useClickOutside(ref, () => setOpen(false), open);

  useEffect(() => {
    if (!open) {
      setTypeMenuOpen(false);
      setQualityMenuOpen(false);
    }
  }, [open]);

  const allTypesActive = areAllStationTypesSelected(activeTypes);
  const hasFilter = !allTypesActive || quality > 0;
  const activeTypeLabels = STATION_TYPE_OPTIONS.filter((option) =>
    activeTypes.includes(option.id),
  ).map((option) => option.shortLabel);
  const activeTypeCount = STATION_TYPE_OPTIONS.filter((option) =>
    activeTypes.includes(option.id),
  ).length;
  const stationTypeSummary = allTypesActive
    ? "All station types"
    : activeTypeCount === 1
      ? (STATION_TYPE_OPTIONS.find((option) => activeTypes.includes(option.id))
          ?.label ?? activeTypeLabels[0] ?? "Station type")
      : `${activeTypeCount} station types`;
  const selectedQualityLevel =
    STATION_QUALITY_LEVELS.find((level) => level.value === quality) ??
    STATION_QUALITY_LEVELS[0];

  const label = (() => {
    if (!showStations) return "Stations hidden";
    const parts: string[] = [];
    if (!allTypesActive) {
      parts.push(
        activeTypeLabels.length <= 2
          ? activeTypeLabels.join("/")
          : `${activeTypeLabels.slice(0, 2).join("/")} +${activeTypeLabels.length - 2}`,
      );
    }
    if (quality > 0) parts.push(formatStationQualitySummary(quality));
    return parts.length > 0 ? parts.join(" · ") : "Stations";
  })();

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1 px-1.5 py-0.5 rounded-sm transition-colors cursor-pointer"
        style={{
          background: hasFilter ? TEAL_BG : "transparent",
          color: hasFilter ? TEAL : TEAL_DIM,
        }}
      >
        <span className="truncate max-w-[130px]">{label}</span>
        <svg
          width="6"
          height="6"
          viewBox="0 0 6 6"
          fill="currentColor"
          style={{ opacity: 0.5 }}
        >
          <path d="M0 1.5 L3 4.5 L6 1.5" />
        </svg>
      </button>

      {open && (
        <DropdownPanel className={`left-1/2 ${FILTER_BAR_DROPDOWN_WIDTH} -translate-x-1/2`}>
          <div className="px-3 py-2.5" style={{ borderBottom: DROP_DIVIDER }}>
            <div className="mb-2 flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={onReset}
                className="shrink-0 rounded-full border px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.14em] transition-colors hover:bg-white/5"
                style={{
                  color: hasFilter || !showStations ? TEAL : DROP_TEXT_DIM,
                  background: DROP_SURFACE,
                  borderColor: "rgba(255,255,255,0.08)",
                }}
              >
                Reset
              </button>
            </div>
            <div
              className="flex items-center rounded-full p-0.5 flex-1 min-w-0"
              style={{ background: DROP_SURFACE_STRONG }}
            >
              <button
                type="button"
                onClick={() => onShowStationsChange(true)}
                className="rounded-full px-2.5 py-1 text-[10px] font-mono transition-colors flex-1"
                style={{
                  color: showStations
                    ? "rgba(4,15,10,0.95)"
                    : "rgba(255,255,255,0.55)",
                  background: showStations
                    ? "rgba(52,211,153,0.95)"
                    : "transparent",
                }}
              >
                Show
              </button>
              <button
                type="button"
                onClick={() => onShowStationsChange(false)}
                className="rounded-full px-2.5 py-1 text-[10px] font-mono transition-colors flex-1"
                style={{
                  color: !showStations
                    ? "rgba(4,15,10,0.95)"
                    : "rgba(255,255,255,0.55)",
                  background: !showStations
                    ? "rgba(52,211,153,0.95)"
                    : "transparent",
                }}
              >
                Hide
              </button>
            </div>
          </div>

          <div
            className="px-3 py-2.5"
            style={{ borderBottom: DROP_DIVIDER }}
          >
            <div
              className="font-mono uppercase tracking-[0.16em]"
              style={{ color: TEAL_DIM, fontSize: DROP_SECTION_FONT_SIZE }}
            >
              Station Type
            </div>
            <div className="relative mt-2">
              <button
                type="button"
                onClick={() => {
                  setTypeMenuOpen((value) => !value);
                  setQualityMenuOpen(false);
                }}
                className="flex w-full items-center justify-between rounded-xl border px-3 py-2 text-left font-mono transition-colors hover:bg-white/5"
                style={{
                  fontSize: DROP_ITEM_FONT_SIZE,
                  borderColor: typeMenuOpen
                    ? "rgba(52,211,153,0.24)"
                    : "rgba(255,255,255,0.08)",
                  background: typeMenuOpen ? DROP_SURFACE_STRONG : DROP_SURFACE,
                  color: allTypesActive ? DROP_TEXT_DIM : DROP_TEXT,
                }}
              >
                <span className="min-w-0 truncate uppercase tracking-[0.12em]">
                  {stationTypeSummary}
                </span>
                <svg
                  width="10"
                  height="10"
                  viewBox="0 0 6 6"
                  fill="currentColor"
                  className="shrink-0"
                  style={{
                    color: typeMenuOpen ? TEAL : DROP_TEXT_SOFT,
                    transform: typeMenuOpen ? "rotate(180deg)" : "none",
                    transition: "transform 160ms ease, color 160ms ease",
                  }}
                >
                  <path d="M0 1.5 L3 4.5 L6 1.5" />
                </svg>
              </button>

              {typeMenuOpen && (
                <div
                  className="mt-2 rounded-2xl border p-1.5"
                  style={{
                    borderColor: "rgba(255,255,255,0.08)",
                    background: DROP_BG,
                  }}
                >
                  <button
                    type="button"
                    onClick={onSelectAllTypes}
                    className="flex w-full items-center justify-between gap-3 rounded-xl px-3 py-2 text-left font-mono transition-colors hover:bg-white/5"
                    style={{
                      ...getDropdownItemStyle(allTypesActive),
                    }}
                  >
                    <span className="truncate">All station types</span>
                    <span
                      className="rounded-full border px-2 py-0.5 text-[8px] uppercase tracking-[0.18em]"
                      style={{
                        color: allTypesActive ? DROP_TEXT : DROP_TEXT_SOFT,
                        background: allTypesActive ? TEAL_ROW : DROP_SURFACE,
                        borderColor: allTypesActive
                          ? "rgba(52,211,153,0.24)"
                          : "rgba(255,255,255,0.08)",
                      }}
                    >
                      All
                    </span>
                  </button>

                  {STATION_TYPE_OPTIONS.map((option) => {
                    const active = !allTypesActive && activeTypes.includes(option.id);
                    return (
                      <button
                        key={option.id}
                        type="button"
                        onClick={() => onToggleType(option.id)}
                        className="flex w-full items-center justify-between gap-3 rounded-xl px-3 py-2 text-left font-mono transition-colors hover:bg-white/5"
                        style={{
                          ...getDropdownItemStyle(active),
                        }}
                      >
                        <span className="min-w-0 truncate">{option.label}</span>
                        <span
                          className="rounded-full border px-2 py-0.5 text-[8px] uppercase tracking-[0.18em]"
                          style={{
                            color: active ? DROP_TEXT : DROP_TEXT_SOFT,
                            background: active ? TEAL_ROW : DROP_SURFACE,
                            borderColor: active
                              ? "rgba(52,211,153,0.24)"
                              : "rgba(255,255,255,0.08)",
                          }}
                        >
                          {option.shortLabel}
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          </div>

          <div className="px-3 py-2.5">
            <div
              className="font-mono uppercase tracking-[0.16em]"
              style={{ color: TEAL_DIM, fontSize: DROP_SECTION_FONT_SIZE }}
            >
              Lifetime Availability
            </div>
            <div className="relative mt-2">
              <button
                type="button"
                aria-label="Station lifetime availability filter"
                onClick={() => {
                  setQualityMenuOpen((value) => !value);
                  setTypeMenuOpen(false);
                }}
                className="flex w-full items-center justify-between rounded-xl border px-3 py-2 text-left font-mono transition-colors hover:bg-white/5"
                style={{
                  fontSize: DROP_ITEM_FONT_SIZE,
                  color: quality > 0 ? DROP_TEXT : DROP_TEXT_DIM,
                  borderColor: qualityMenuOpen
                    ? "rgba(52,211,153,0.24)"
                    : "rgba(255,255,255,0.08)",
                  background: qualityMenuOpen ? DROP_SURFACE_STRONG : DROP_SURFACE,
                }}
              >
                <span className="min-w-0 truncate uppercase tracking-[0.12em]">
                  {selectedQualityLevel.label}
                </span>
                <svg
                  width="10"
                  height="10"
                  viewBox="0 0 6 6"
                  fill="currentColor"
                  className="shrink-0"
                  style={{
                    color: qualityMenuOpen ? TEAL : DROP_TEXT_SOFT,
                    transform: qualityMenuOpen ? "rotate(180deg)" : "none",
                    transition: "transform 160ms ease, color 160ms ease",
                  }}
                >
                  <path d="M0 1.5 L3 4.5 L6 1.5" />
                </svg>
              </button>

              {qualityMenuOpen && (
                <div
                  className="mt-2 rounded-2xl border p-1.5"
                  style={{
                    borderColor: "rgba(255,255,255,0.08)",
                    background: DROP_BG,
                  }}
                >
                  {STATION_QUALITY_LEVELS.map((level) => {
                    const active = level.value === quality;

                    return (
                      <button
                        key={level.value}
                        type="button"
                        onClick={() => {
                          onQualityChange(level.value);
                          setQualityMenuOpen(false);
                        }}
                        className="flex w-full items-center justify-between gap-3 rounded-xl px-3 py-2 text-left font-mono transition-colors hover:bg-white/5"
                        style={{
                          ...getDropdownItemStyle(active),
                        }}
                      >
                        <span className="min-w-0 truncate">{level.label}</span>
                        <span
                          className="rounded-full border px-2 py-0.5 text-[8px] uppercase tracking-[0.18em]"
                          style={{
                            color: active ? DROP_TEXT : DROP_TEXT_SOFT,
                            background: active ? TEAL_ROW : DROP_SURFACE,
                            borderColor: active
                              ? "rgba(52,211,153,0.24)"
                              : "rgba(255,255,255,0.08)",
                          }}
                        >
                          {level.shortLabel}
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        </DropdownPanel>
      )}
    </div>
  );
}

/* ── Migration tracking toggle + sub-filters ── */
function MovebankChip({
  active,
  onToggle,
  windowDays,
  onWindowChange,
}: {
  active: boolean;
  onToggle: () => void;
  windowDays: number | null;
  onWindowChange: (days: number | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useClickOutside(ref, () => setOpen(false), open);

  const WINDOW_OPTIONS: { label: string; value: number | null }[] = [
    { label: "All time", value: null },
    { label: "1 year", value: 365 },
    { label: "90 days", value: 90 },
    { label: "30 days", value: 30 },
  ];

  const chipLabel = active ? "Migration · ON" : "Migration";

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1 px-1.5 py-0.5 rounded-sm transition-colors cursor-pointer"
        style={{
          background: active ? TEAL_BG : "transparent",
          color: active ? TEAL : TEAL_DIM,
        }}
      >
        <span className="truncate">{chipLabel}</span>
        <svg
          width="6"
          height="6"
          viewBox="0 0 6 6"
          fill="currentColor"
          style={{ opacity: 0.5 }}
        >
          <path d="M0 1.5 L3 4.5 L6 1.5" />
        </svg>
      </button>

      {open && (
        <DropdownPanel className={`right-0 ${FILTER_BAR_DROPDOWN_WIDTH}`}>
          {/* Enable / disable toggle */}
          <div className="px-1.5 py-1.5" style={{ borderBottom: DROP_DIVIDER }}>
            <button
              type="button"
              onClick={() => { onToggle(); }}
              className="flex w-full items-center justify-between gap-2 rounded-xl px-3 py-2 text-left transition-colors hover:bg-white/5"
              style={{
                ...getDropdownItemStyle(active),
              }}
            >
              <span>{active ? "Tracking enabled" : "Enable tracking"}</span>
              <span
                className="w-8 h-4 rounded-full flex items-center px-0.5 transition-all shrink-0"
                style={{ background: active ? "rgba(52,211,153,0.55)" : DROP_SURFACE_STRONG }}
              >
                <span
                  className="w-3 h-3 rounded-full transition-all"
                  style={{
                    background: active ? TEAL : "rgba(255,255,255,0.4)",
                    transform: active ? "translateX(16px)" : "translateX(0)",
                  }}
                />
              </span>
            </button>
          </div>

          {/* Time window — single-select full-width rows */}
          <div className="px-1.5 py-1.5">
            <div className="px-2 pb-1 font-mono uppercase tracking-[0.16em]" style={{ color: TEAL_DIM, fontSize: DROP_SECTION_FONT_SIZE }}>
              Time window
            </div>
            {WINDOW_OPTIONS.map((opt) => {
              const isActive = windowDays === opt.value;
              return (
                <button
                  key={opt.label}
                  type="button"
                  onClick={() => onWindowChange(opt.value)}
                  className="flex w-full items-center justify-between gap-2 rounded-xl px-3 py-2 text-left transition-colors hover:bg-white/5"
                  style={getDropdownItemStyle(isActive)}
                >
                  <span>{opt.label}</span>
                  {isActive && (
                    <svg width="10" height="10" viewBox="0 0 10 10" fill="none" style={{ color: TEAL }}>
                      <path d="M1.5 5L4 7.5L8.5 2.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  )}
                </button>
              );
            })}
          </div>

        </DropdownPanel>
      )}
    </div>
  );
}

/* ── Environmental profiling dropdown ── */
function EnvironmentalProfilingChip({
  activeEarthEngineOverlay,
  onSelectEarthEngineOverlay,
  spatialPartitionMode,
}: {
  activeEarthEngineOverlay: EarthEngineOverlayId | null;
  onSelectEarthEngineOverlay: (overlay: EarthEngineOverlayId | null) => void;
  spatialPartitionMode: EarthEngineVisualizationMode;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useClickOutside(ref, () => setOpen(false), open);

  const hasActive = activeEarthEngineOverlay !== null;
  const activeOverlayLabel = activeEarthEngineOverlay
    ? (EARTH_ENGINE_OVERLAYS.find(
        (overlay) => overlay.id === activeEarthEngineOverlay,
      )?.shortLabel ?? "Env")
    : null;
  const activeViewLabel =
    EARTH_ENGINE_VISUALIZATION_META[spatialPartitionMode]?.shortLabel ??
    "Admin";

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1 px-1.5 py-0.5 rounded-sm transition-colors cursor-pointer"
        style={{
          background: hasActive ? TEAL_BG : "transparent",
          color: hasActive ? TEAL : TEAL_DIM,
        }}
      >
        <span className="truncate max-w-[180px]">
          Environment
          {activeOverlayLabel
            ? ` · ${activeOverlayLabel} · ${activeViewLabel}`
            : ""}
        </span>
        <svg
          width="6"
          height="6"
          viewBox="0 0 6 6"
          fill="currentColor"
          style={{ opacity: 0.5 }}
        >
          <path d="M0 1.5 L3 4.5 L6 1.5" />
        </svg>
      </button>

      {open && (
        <DropdownPanel className={`right-0 ${FILTER_BAR_DROPDOWN_WIDTH}`}>
          <div className="overflow-y-auto p-1.5" style={{ maxHeight: 260 }}>
            {hasActive && (
              <button
                type="button"
                onClick={() => onSelectEarthEngineOverlay(null)}
                className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left font-mono transition-colors hover:bg-white/5"
                style={{ fontSize: DROP_ITEM_FONT_SIZE, color: DROP_TEXT_DIM }}
              >
                <span
                  className="w-4 h-4 rounded-sm border flex items-center justify-center flex-shrink-0 text-[9px] leading-none"
                  style={{
                    borderColor: "rgba(255,255,255,0.12)",
                    background: DROP_SURFACE,
                  }}
                >
                  ✕
                </span>
                <span>None (clear lens)</span>
              </button>
            )}
            {EARTH_ENGINE_OVERLAYS.map((overlay) => {
              const active = activeEarthEngineOverlay === overlay.id;
              return (
                <button
                  key={overlay.id}
                  type="button"
                  onClick={() =>
                    onSelectEarthEngineOverlay(active ? null : overlay.id)
                  }
                  className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left font-mono transition-colors hover:bg-white/5"
                  style={getDropdownItemStyle(active, 10)}
                >
                  <span
                    className="w-4 h-4 rounded-sm border flex items-center justify-center flex-shrink-0 text-[9px] leading-none"
                    style={{
                      borderColor: active
                        ? "rgba(52,211,153,0.5)"
                        : "rgba(255,255,255,0.15)",
                      background: active
                        ? "rgba(52,211,153,0.16)"
                        : "rgba(255,255,255,0.02)",
                    }}
                  >
                    <span
                      style={{
                        color: active ? TEAL : "rgba(255,255,255,0.22)",
                      }}
                    >
                      {active ? "✓" : ""}
                    </span>
                  </span>
                  <span className="min-w-0 flex-1 flex items-center justify-between gap-2">
                    <span className="block leading-tight truncate">
                      {overlay.label}
                    </span>
                    <span
                      className="px-1.5 py-0.5 rounded-full text-[8px] uppercase tracking-[0.18em] flex-shrink-0"
                      style={{
                        color: active
                          ? DROP_TEXT
                          : DROP_TEXT_SOFT,
                        background: active
                          ? "rgba(52,211,153,0.18)"
                          : DROP_SURFACE,
                        border: `1px solid ${active ? "rgba(52,211,153,0.22)" : "rgba(255,255,255,0.08)"}`,
                      }}
                    >
                      {overlay.shortLabel}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </DropdownPanel>
      )}
    </div>
  );
}

/* ── Weather overlay dropdown (mirrors the explore/weather visual) ── */
const WEATHER_OVERLAY_CHIP_OPTIONS: {
  id: LiveWeatherOverlay;
  label: string;
  short: string;
}[] = [
  { id: "wind", label: "Wind", short: "Wind" },
  { id: "temperature", label: "Temperature", short: "Temp" },
  { id: "cloud", label: "Cloud cover", short: "Cloud" },
  { id: "precipitation", label: "Precipitation", short: "Rain" },
  { id: "drought", label: "Drought (SPI/SPEI)", short: "Drought" },
];

function WeatherChip({
  activeWeatherOverlay,
  onSelectWeatherOverlay,
}: {
  activeWeatherOverlay: LiveWeatherOverlay | null;
  onSelectWeatherOverlay: (overlay: LiveWeatherOverlay | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useClickOutside(ref, () => setOpen(false), open);

  const hasActive = activeWeatherOverlay !== null;
  const activeLabel = activeWeatherOverlay
    ? (WEATHER_OVERLAY_CHIP_OPTIONS.find((o) => o.id === activeWeatherOverlay)?.short ?? "On")
    : null;

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1 px-1.5 py-0.5 rounded-sm transition-colors cursor-pointer"
        style={{
          background: hasActive ? TEAL_BG : "transparent",
          color: hasActive ? TEAL : TEAL_DIM,
        }}
      >
        <span className="truncate max-w-[180px]">
          Weather{activeLabel ? ` · ${activeLabel}` : ""}
        </span>
        <svg width="6" height="6" viewBox="0 0 6 6" fill="currentColor" style={{ opacity: 0.5 }}>
          <path d="M0 1.5 L3 4.5 L6 1.5" />
        </svg>
      </button>

      {open && (
        <DropdownPanel className={`right-0 ${FILTER_BAR_DROPDOWN_WIDTH}`}>
          <div className="overflow-y-auto p-1.5" style={{ maxHeight: 260 }}>
            {hasActive && (
              <button
                type="button"
                onClick={() => onSelectWeatherOverlay(null)}
                className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left font-mono transition-colors hover:bg-white/5"
                style={{ fontSize: DROP_ITEM_FONT_SIZE, color: DROP_TEXT_DIM }}
              >
                <span
                  className="w-4 h-4 rounded-sm border flex items-center justify-center flex-shrink-0 text-[9px] leading-none"
                  style={{ borderColor: "rgba(255,255,255,0.12)", background: DROP_SURFACE }}
                >
                  ✕
                </span>
                <span>None (weather off)</span>
              </button>
            )}
            {WEATHER_OVERLAY_CHIP_OPTIONS.map((option) => {
              const active = activeWeatherOverlay === option.id;
              return (
                <button
                  key={option.id}
                  type="button"
                  onClick={() => onSelectWeatherOverlay(active ? null : option.id)}
                  className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left font-mono transition-colors hover:bg-white/5"
                  style={getDropdownItemStyle(active, 10)}
                >
                  <span
                    className="w-4 h-4 rounded-sm border flex items-center justify-center flex-shrink-0 text-[9px] leading-none"
                    style={{
                      borderColor: active ? "rgba(52,211,153,0.5)" : "rgba(255,255,255,0.15)",
                      background: active ? "rgba(52,211,153,0.16)" : "rgba(255,255,255,0.02)",
                    }}
                  >
                    <span style={{ color: active ? TEAL : "rgba(255,255,255,0.22)" }}>
                      {active ? "✓" : ""}
                    </span>
                  </span>
                  <span className="min-w-0 flex-1 flex items-center justify-between gap-2">
                    <span className="block leading-tight truncate">{option.label}</span>
                    <span
                      className="px-1.5 py-0.5 rounded-full text-[8px] uppercase tracking-[0.18em] flex-shrink-0"
                      style={{
                        color: active ? DROP_TEXT : DROP_TEXT_SOFT,
                        background: active ? "rgba(52,211,153,0.18)" : DROP_SURFACE,
                        border: `1px solid ${active ? "rgba(52,211,153,0.22)" : "rgba(255,255,255,0.08)"}`,
                      }}
                    >
                      {option.short}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </DropdownPanel>
      )}
    </div>
  );
}

function HotspotsChip({
  activeSource,
  onSelectSource,
}: {
  activeSource: HotspotSource | null;
  onSelectSource: (source: HotspotSource | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useClickOutside(ref, () => setOpen(false), open);

  const hasActive = activeSource !== null;

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1 px-1.5 py-0.5 rounded-sm transition-colors cursor-pointer"
        style={{
          background: hasActive ? TEAL_BG : "transparent",
          color: hasActive ? TEAL : TEAL_DIM,
        }}
      >
        <span className="truncate max-w-[220px]">Hotspots</span>
        <svg
          width="6"
          height="6"
          viewBox="0 0 6 6"
          fill="currentColor"
          style={{ opacity: 0.5 }}
        >
          <path d="M0 1.5 L3 4.5 L6 1.5" />
        </svg>
      </button>

      {open && (
        <DropdownPanel className={`right-0 ${FILTER_BAR_DROPDOWN_WIDTH}`}>
          <div className="overflow-y-auto p-1.5" style={{ maxHeight: 320 }}>
            <div
              className="px-3 pt-1 pb-1.5 uppercase tracking-wider"
              style={{ fontSize: 9, color: DROP_TEXT_SOFT }}
            >
              Source
            </div>
            {HOTSPOT_SOURCE_OPTIONS.map((option) => {
              const active = activeSource === option.value;
              return (
                <button
                  key={option.label}
                  type="button"
                  onClick={() => {
                    onSelectSource(option.value);
                    if (option.value === null) setOpen(false);
                  }}
                  className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left font-mono transition-colors hover:bg-white/5"
                  style={getDropdownItemStyle(active, 10)}
                >
                  <span
                    className="w-4 h-4 rounded-sm border flex items-center justify-center flex-shrink-0 text-[9px] leading-none"
                    style={{
                      borderColor: active
                        ? "rgba(52,211,153,0.5)"
                        : "rgba(255,255,255,0.15)",
                      background: active ? TEAL_BG : DROP_SURFACE,
                    }}
                  >
                    {active ? "✓" : ""}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate">{option.label}</span>
                    <span
                      className="block truncate pt-0.5"
                      style={{ color: DROP_TEXT_SOFT }}
                    >
                      {option.detail}
                    </span>
                  </span>
                </button>
              );
            })}

          </div>
        </DropdownPanel>
      )}
    </div>
  );
}

/* ── Species multi-select dropdown ── */
function SpeciesChip({
  options,
  active,
  onToggle,
}: {
  options: string[];
  active: string[];
  onToggle: (sp: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  useClickOutside(
    ref,
    () => {
      setOpen(false);
      setSearch("");
    },
    open,
  );

  const filtered = useMemo(
    () =>
      search
        ? options.filter((s) => s.toLowerCase().includes(search.toLowerCase()))
        : options,
    [options, search],
  );

  const allActive = active.length === 0;
  const label = allActive
    ? "Species"
    : active.length === 1
      ? active[0]
      : `${active.length} species`;

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1 px-1.5 py-0.5 rounded-sm transition-colors cursor-pointer"
        style={{
          background: allActive ? "transparent" : TEAL_BG,
          color: allActive ? TEAL_DIM : TEAL,
        }}
      >
        <span className="truncate max-w-[110px]">{label}</span>
        <svg
          width="6"
          height="6"
          viewBox="0 0 6 6"
          fill="currentColor"
          style={{ opacity: 0.5 }}
        >
          <path d="M0 1.5 L3 4.5 L6 1.5" />
        </svg>
      </button>

      {open && (
        <DropdownPanel className={`left-1/2 ${FILTER_BAR_DROPDOWN_WIDTH} -translate-x-1/2`}>
          <div className="px-3 py-2.5" style={{ borderBottom: DROP_DIVIDER }}>
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search species…"
              className="w-full rounded-xl border px-3 py-2 font-mono outline-none placeholder:text-white/30"
              style={{
                fontSize: DROP_ITEM_FONT_SIZE,
                color: DROP_TEXT_DIM,
                borderColor: "rgba(255,255,255,0.08)",
                background: DROP_SURFACE,
              }}
              autoFocus
            />
          </div>
          <div className="overflow-y-auto p-1.5" style={{ maxHeight: 200 }}>
            {filtered.map((sp) => {
              const checked = active.includes(sp);
              return (
                <button
                  key={sp}
                  type="button"
                  onClick={() => onToggle(sp)}
                  className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-left font-mono transition-colors hover:bg-white/5"
                  style={{
                    ...getDropdownItemStyle(checked, 10),
                  }}
                >
                  <span
                    className="w-3.5 h-3.5 rounded-[4px] border flex items-center justify-center flex-shrink-0"
                    style={{
                      borderColor: checked
                        ? "rgba(52,211,153,0.5)"
                        : "rgba(255,255,255,0.12)",
                      background: checked
                        ? "rgba(52,211,153,0.2)"
                        : DROP_SURFACE,
                    }}
                  >
                    {checked && (
                      <svg
                        width="8"
                        height="8"
                        viewBox="0 0 8 8"
                        fill="none"
                        stroke={TEAL}
                        strokeWidth="1.5"
                      >
                        <path d="M1.5 4 L3 5.5 L6.5 2" />
                      </svg>
                    )}
                  </span>
                  <span className="truncate">{sp}</span>
                </button>
              );
            })}
            {filtered.length === 0 && (
              <div
                className="px-3 py-2 font-mono"
                style={{ fontSize: DROP_ITEM_FONT_SIZE, color: DROP_TEXT_SOFT }}
              >
                No match
              </div>
            )}
          </div>
        </DropdownPanel>
      )}
    </div>
  );
}

export function HeroMap({
  onFilterChange,
  focusedStationId,
  onStationSelect,
  onGridSelect,
  flyFocus,
}: {
  onFilterChange?: (f: {
    types: string[];
    species: string[];
    minQuality: number;
    showStations: boolean;
  }) => void;
  focusedStationId?: string | null;
  onStationSelect?: (stationId: string | null) => void;
  onGridSelect?: (gridCellId: string | null) => void;
  flyFocus?: { lat: number; lon: number } | null;
}) {
  const mapRef = useRef<SatelliteMapRef>(null);

  const [vs, setVs] = useState(() => ({
    latitude: 52.13,
    longitude: 5.29,
    zoom: 7.0,
  }));

  const handleViewStateChange = useCallback((next: MapViewState) => {
    const rounded = {
      latitude: Number(next.latitude.toFixed(2)),
      longitude: Number(next.longitude.toFixed(2)),
      zoom: Number(next.zoom.toFixed(1)),
    };

    setVs((prev) =>
      prev.latitude === rounded.latitude &&
      prev.longitude === rounded.longitude &&
      prev.zoom === rounded.zoom
        ? prev
        : rounded,
    );
  }, []);

  const handleReset = useCallback(() => {
    mapRef.current?.resetView();
  }, []);

  /* ── Filter state ── */
  const [allStations, setAllStations] = useState<Station[]>([]);
  const [activeTypes, setActiveTypes] = useState<string[]>(STATION_TYPE_IDS);
  const [activeSpecies, setActiveSpecies] = useState<string[]>([]);
  const [minQuality, setMinQuality] = useState(0);
  const [showStations, setShowStations] = useState(true);
  const [showMovebank, setShowMovebank] = useState(false);
  const [movebankWindowDays, setMovebankWindowDays] = useState<number | null>(null);
  /** Populated from live GeoJSON once tracks are fetched — avoids hardcoded species list */
  const [availableMovebankSpecies, setAvailableMovebankSpecies] = useState<string[]>([]);
  const [activeEarthEngineOverlay, setActiveEarthEngineOverlay] =
    useState<EarthEngineOverlayId | null>(null);
  const [weatherOverlay, setWeatherOverlay] =
    useState<LiveWeatherOverlay | null>(null);
  const [activeHotspotSource, setActiveHotspotSource] =
    useState<HotspotSource | null>(null);
  // Hotspot overlay grain is independent of the spatial-partition base layer,
  // because each source only carries data at certain grains (bioacoustic is
  // grid-only). Default to grid so the first real source (Bioacoustic) renders
  // out of the box.
  const [hotspotGrain, setHotspotGrain] = useState<HotspotGrain>("grid");
  // Always render the newest available hotspot week for the selected source.
  const hotspotWeek = LATEST_WEEK_VALUE;
  const [activeSkin, setActiveSkin] = useState<MapSkinId>("satellite");
  const [spatialPartitionMode, setSpatialPartitionMode] =
    useState<EarthEngineVisualizationMode>("municipality");
  // Tessellation to restore when a hotspot is unticked — while a source is
  // active, the base tessellation auto-follows it (grid / municipality).
  const prevPartitionModeRef = useRef<EarthEngineVisualizationMode | null>(null);

  // Derive unique species from all stations' top_species;
  // when migration tracking is active, also merge Movebank species so the
  // shared SpeciesChip can filter both station detections and migration tracks.
  const speciesOptions = useMemo(() => {
    const set = new Set<string>();
    for (const s of allStations) {
      for (const sp of s.stats.top_species ?? []) {
        if (TARGET_SPECIES.has(sp.name)) set.add(sp.name);
      }
    }
    if (showMovebank) {
      for (const sp of availableMovebankSpecies) set.add(sp);
    }
    return Array.from(set).sort();
  }, [allStations, showMovebank, availableMovebankSpecies]);

  const toggleType = useCallback((type: string) => {
    setActiveTypes((prev) => {
      if (prev.includes(type)) {
        const next = prev.filter((t) => t !== type);
        return next.length === 0 ? prev : next;
      }
      return [...prev, type];
    });
  }, []);

  const toggleSpecies = useCallback((sp: string) => {
    setActiveSpecies((prev) =>
      prev.includes(sp) ? prev.filter((s) => s !== sp) : [...prev, sp],
    );
  }, []);

  const selectAllTypes = useCallback(() => {
    setActiveTypes(STATION_TYPE_IDS);
  }, []);

  const resetStationFilters = useCallback(() => {
    setActiveTypes(STATION_TYPE_IDS);
    setMinQuality(0);
    setShowStations(true);
  }, []);

  const handleEarthEngineOverlayChange = useCallback(
    (overlay: EarthEngineOverlayId | null) => {
      setActiveEarthEngineOverlay(overlay);
      if (overlay) {
        setActiveHotspotSource(null);
      }
    },
    [],
  );

  const handleHotspotSourceChange = useCallback(
    (source: HotspotSource | null) => {
      setActiveHotspotSource(source);
      if (source) {
        setActiveEarthEngineOverlay(null);
        // Grain + base tessellation follow the source automatically. Bioacoustic
        // carries grid-cell signal, so it renders on the grid tessellation.
        // Remember the prior tessellation so unticking can restore it.
        const gridBased = source === "bioacoustic";
        setHotspotGrain(gridBased ? "grid" : "admin_area");
        setSpatialPartitionMode((prev) => {
          if (prevPartitionModeRef.current === null) {
            prevPartitionModeRef.current = prev;
          }
          return gridBased ? "grid" : "municipality";
        });
      } else {
        // Untick → restore the tessellation that was active before selecting.
        if (prevPartitionModeRef.current !== null) {
          setSpatialPartitionMode(prevPartitionModeRef.current);
          prevPartitionModeRef.current = null;
        }
        setHotspotGrain("grid");
      }
    },
    [],
  );

  // Notify parent of filter changes
  useEffect(() => {
    onFilterChange?.({
      types: activeTypes,
      species: activeSpecies,
      minQuality,
      showStations,
    });
  }, [activeTypes, activeSpecies, minQuality, onFilterChange, showStations]);

  return (
    <div className="relative flex flex-col items-center justify-center h-full select-none hero-bg overflow-hidden">
      {/* ───── Scope assembly ───── */}
      <div className="relative scope-size my-auto">
        {/* Ring 3 — outermost perimeter */}
        <div className="absolute -inset-10 rounded-full border border-dashed border-brand-500/[0.07]" />

        {/* Ring 2 — middle zone */}
        <div className="absolute -inset-5 rounded-full border border-brand-500/[0.12]" />

        {/* Ring 1.5 — inner accent */}
        <div className="absolute -inset-1.5 rounded-full border border-brand-500/[0.18]" />

        {/* ── Tick marks ── */}
        {TICKS.map((deg) => (
          <div
            key={`t-${deg}`}
            className="absolute top-1/2 left-1/2 pointer-events-none"
            style={{
              width: "1px",
              height: `${tickHeight(deg)}px`,
              backgroundColor: `rgba(16, 185, 129, ${tickOpacity(deg)})`,
              transform: `translate(-50%, -50%) rotate(${deg}deg) translateY(calc(-1 * (var(--scope) / 2 + 20px)))`,
            }}
          />
        ))}

        {/* ── Cardinal direction labels ── */}
        {CARDINALS.map(({ label, deg }) => (
          <span
            key={label}
            className="absolute top-1/2 left-1/2 text-[9px] font-semibold tracking-[0.25em] text-brand-500/25 pointer-events-none"
            style={{
              transform: `translate(-50%, -50%) rotate(${deg}deg) translateY(calc(-1 * (var(--scope) / 2 + 32px))) rotate(-${deg}deg)`,
            }}
          >
            {label}
          </span>
        ))}

        {/* ── Degree labels at 30° intervals ── */}
        {DEGREE_LABELS.map((deg) => (
          <span
            key={`dl-${deg}`}
            className="absolute top-1/2 left-1/2 text-[7px] font-mono tracking-widest text-brand-500/15 pointer-events-none"
            style={{
              transform: `translate(-50%, -50%) rotate(${deg}deg) translateY(calc(-1 * (var(--scope) / 2 + 28px))) rotate(-${deg}deg)`,
            }}
          >
            {String(deg).padStart(3, "0")}
          </span>
        ))}

        {/* ── Rotating radar sweep ── */}
        <div
          className="absolute -inset-5 rounded-full pointer-events-none z-20 animate-radar-scan"
          style={{
            background:
              "conic-gradient(from 0deg, transparent 0deg, rgba(16,185,129,0.10) 20deg, transparent 60deg)",
            maskImage: "radial-gradient(circle, transparent 90%, black 93%)",
            WebkitMaskImage:
              "radial-gradient(circle, transparent 90%, black 93%)",
          }}
        />

        {/* ── Glow halo ── */}
        <div className="absolute -inset-px rounded-full animate-glow-pulse pointer-events-none" />

        {/* ═══════ Circular map viewport ═══════ */}
        <div className="w-full h-full rounded-full overflow-hidden border border-brand-500/20 relative z-10">
          <SatelliteMap
            ref={mapRef}
            onViewStateChange={handleViewStateChange}
            activeStationTypes={activeTypes}
            activeSpecies={activeSpecies}
            minQuality={minQuality}
            showStations={showStations}
            focusedStationId={focusedStationId}
            onStationSelect={onStationSelect}
            onGridSelect={onGridSelect}
            flyFocus={flyFocus}
            showMovebank={showMovebank}
            movebankSpeciesFilter={activeSpecies.length > 0 ? activeSpecies : undefined}
            movebankWindowDays={movebankWindowDays}
            skin={activeSkin}
            earthEngineOverlay={activeEarthEngineOverlay}
            spatialPartitionMode={spatialPartitionMode}
            hotspotSource={activeHotspotSource}
            hotspotGrain={hotspotGrain}
            hotspotWeek={hotspotWeek}
            weatherOverlay={weatherOverlay}
            onStationsLoaded={setAllStations}
            onTracksLoaded={setAvailableMovebankSpecies}
          />
        </div>

        {/* ── Signal noise (faint static through the scope lens) ── */}
        <svg
          className="absolute inset-0 w-full h-full pointer-events-none z-[19] rounded-full"
          style={{ opacity: 0.03, mixBlendMode: "overlay" }}
          aria-hidden="true"
        >
          <filter id="scope-noise">
            <feTurbulence
              type="fractalNoise"
              baseFrequency="0.65"
              numOctaves="3"
              stitchTiles="stitch"
            />
          </filter>
          <rect width="100%" height="100%" filter="url(#scope-noise)" />
        </svg>

        {/* ── Scanlines overlay (CRT effect) ── */}
        <div className="absolute inset-0 rounded-full pointer-events-none z-20 opacity-30 mix-blend-overlay scanlines" />

        {/* ── Crosshair reticle ── */}
        <div className="absolute inset-0 rounded-full pointer-events-none z-20 overflow-hidden">
          <div className="absolute top-1/2 left-[8%] right-[8%] h-px -translate-y-1/2 bg-brand-400/[0.15]" />
          <div className="absolute left-1/2 top-[8%] bottom-[8%] w-px -translate-x-1/2 bg-brand-400/[0.15]" />
          <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-1.5 h-1.5 rounded-full bg-zinc-400 shadow-[0_0_8px_rgba(161,161,170,0.45)]" />
        </div>

        {/* ── Teal lens tint ── */}
        <div
          className="absolute inset-0 rounded-full pointer-events-none z-20"
          style={{
            background:
              "radial-gradient(circle, transparent 20%, rgba(6, 78, 59, 0.15) 100%)",
          }}
        />

        {/* ── Deep Vignette ── */}
        <div
          className="absolute inset-0 rounded-full pointer-events-none z-20"
          style={{
            boxShadow: "inset 0 0 120px 50px rgba(17,17,17,0.7)",
          }}
        />
      </div>

      {/* ───── Floating filter bar ───── */}
      <div className="absolute bottom-6 left-1/2 -translate-x-1/2 z-30 flex items-center gap-x-3 px-4 py-1.5 bg-surface-primary/80 backdrop-blur-md border border-brand-500/20 rounded-full text-[10px] font-mono text-brand-100 shadow-[0_0_15px_rgba(16,185,129,0.1)]">
        {/* Coordinates + Zoom */}
        <span className="whitespace-nowrap shrink-0" style={{ color: "rgba(52,211,153,0.4)" }}>
          {vs.latitude.toFixed(2)}&deg;N&#8194;{vs.longitude.toFixed(2)}&deg;E
          <span className="text-brand-500/30 mx-1">&middot;</span>Z
          {vs.zoom.toFixed(1)}
        </span>

        <span className="text-brand-500/20">│</span>

        <SpatialPartitionChip
          activeMode={spatialPartitionMode}
          onSelectMode={setSpatialPartitionMode}
        />

        <span className="text-brand-500/20">│</span>

        <SkinChip
          activeSkin={activeSkin}
          onSelectSkin={setActiveSkin}
        />

        {/* filter disabled
        <span className="text-brand-500/20">│</span>
        <StationsChip
          activeTypes={activeTypes}
          onToggleType={toggleType}
          onSelectAllTypes={selectAllTypes}
          quality={minQuality}
          onQualityChange={setMinQuality}
          showStations={showStations}
          onShowStationsChange={setShowStations}
          onReset={resetStationFilters}
        />
        */}

        {/* filter disabled
        <span className="text-brand-500/20">│</span>
        <SpeciesChip
          options={speciesOptions}
          active={activeSpecies}
          onToggle={toggleSpecies}
        />
        */}

        <span className="text-brand-500/20">│</span>

        {/* Migration tracking */}
        <MovebankChip
          active={showMovebank}
          onToggle={() => setShowMovebank((v) => !v)}
          windowDays={movebankWindowDays}
          onWindowChange={setMovebankWindowDays}
        />

        <span className="text-brand-500/20">│</span>

        {/* Hotspot overlays */}
        <HotspotsChip
          activeSource={activeHotspotSource}
          onSelectSource={handleHotspotSourceChange}
        />

        <span className="text-brand-500/20">│</span>

        {/* Weather overlay */}
        <WeatherChip
          activeWeatherOverlay={weatherOverlay}
          onSelectWeatherOverlay={setWeatherOverlay}
        />

        {/* filter disabled
        <span className="text-brand-500/20">│</span>
        <EnvironmentalProfilingChip
          activeEarthEngineOverlay={activeEarthEngineOverlay}
          onSelectEarthEngineOverlay={handleEarthEngineOverlayChange}
          spatialPartitionMode={spatialPartitionMode}
        />
        */}

        {/* Reset view */}
        <span className="text-brand-500/20">│</span>
        <button
          onClick={handleReset}
          className="flex items-center justify-center w-5 h-5 rounded-full text-brand-400 hover:text-brand-300 hover:bg-brand-500/10 transition-all cursor-pointer group"
          title="Reset to Netherlands view"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="12"
            height="12"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="group-hover:-rotate-90 transition-transform duration-300"
          >
            <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
            <path d="M3 3v5h5" />
          </svg>
        </button>
      </div>
    </div>
  );
}
