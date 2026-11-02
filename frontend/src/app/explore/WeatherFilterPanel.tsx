'use client';

import { ChevronDown, RotateCcw, SlidersHorizontal } from 'lucide-react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useEffect, useRef, useState } from 'react';

import {
  WEATHER_OVERLAY_OPTIONS,
  resolveWeatherOverlay,
  type WeatherOverlay,
  resolveWeatherSelectedDate,
} from './weather';

const FILTER_SURFACE = {
  panel: 'rgba(20,20,20,0.96)',
  panelAlt: 'rgba(16,16,16,0.96)',
  panelDeep: 'rgba(10,10,10,0.98)',
  border: 'rgba(255,255,255,0.08)',
  borderSoft: 'rgba(255,255,255,0.05)',
} as const;

const CONTROL_TEXT = 'rgba(255,255,255,0.72)';

type WeatherFilters = {
  weatherDate: string;
};

const EMPTY_FILTERS: WeatherFilters = {
  weatherDate: '',
};

const WEATHER_OVERLAY_SELECT_OPTIONS = [
  { value: 'none', label: 'None' },
  ...WEATHER_OVERLAY_OPTIONS,
] as const;

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <p
      className="mb-1 block font-mono tracking-[0.18em] uppercase"
      style={{ fontSize: 10, color: 'rgba(255,255,255,0.30)' }}
    >
      {children}
    </p>
  );
}

function resolvePanelWeatherDate(searchParams: Pick<URLSearchParams, 'get'>) {
  return searchParams.get('weatherDate')
    ?? (searchParams.get('weatherDateFrom') || searchParams.get('weatherDateTo')
      ? resolveWeatherSelectedDate(
          null,
          searchParams.get('weatherDateFrom'),
          searchParams.get('weatherDateTo'),
        )
      : '');
}

function writeWeatherOverlay(
  params: URLSearchParams,
  nextOverlay: WeatherOverlay | null,
) {
  if (nextOverlay === null) {
    params.set('weatherOverlay', 'none');
    return;
  }

  if (nextOverlay === 'wind') {
    params.delete('weatherOverlay');
    return;
  }

  params.set('weatherOverlay', nextOverlay);
}

function OverlaySelect({
  value,
  onChange,
}: {
  value: WeatherOverlay | null;
  onChange: (value: WeatherOverlay | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const currentLabel = value === null
    ? 'None'
    : WEATHER_OVERLAY_OPTIONS.find((overlay) => overlay.value === value)?.label ?? 'Wind';

  useEffect(() => {
    if (!open) {
      return;
    }

    const handler = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };

    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  return (
    <div ref={ref} className="relative w-full">
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        className="w-full flex items-center justify-between gap-3 rounded-xl px-2.5 py-1.5 text-left transition-all duration-200"
        style={{
          fontSize: 13,
          background: open ? FILTER_SURFACE.panel : FILTER_SURFACE.panelAlt,
          border: `1px solid ${FILTER_SURFACE.border}`,
          color: CONTROL_TEXT,
          boxShadow: open ? 'inset 0 1px 0 rgba(255,255,255,0.04)' : 'none',
        }}
      >
        <span className="truncate">{currentLabel}</span>
        <ChevronDown
          size={10}
          style={{
            color: 'rgba(255,255,255,0.28)',
            transform: open ? 'rotate(180deg)' : undefined,
            transition: 'transform 0.15s',
          }}
        />
      </button>

      {open ? (
        <div
          className="absolute z-50 top-full left-0 right-0 mt-1.5 rounded-xl p-1.5 shadow-2xl"
          style={{
            background: FILTER_SURFACE.panelDeep,
            border: `1px solid ${FILTER_SURFACE.border}`,
            boxShadow: '0 20px 70px rgba(0,0,0,0.46)',
          }}
        >
          <div className="flex flex-col gap-1">
            {WEATHER_OVERLAY_SELECT_OPTIONS.map((option) => {
              const optionValue = option.value === 'none' ? null : option.value;
              const isSelected = value === optionValue;

              return (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => {
                    onChange(optionValue);
                    setOpen(false);
                  }}
                  className="w-full rounded-xl px-2.5 py-1.5 text-left transition-colors truncate block"
                  style={{
                    fontSize: 14,
                    color: 'rgba(255,255,255,0.82)',
                    background: isSelected ? 'rgba(255,255,255,0.08)' : FILTER_SURFACE.panelAlt,
                    border: `1px solid ${FILTER_SURFACE.border}`,
                  }}
                  onMouseEnter={(event) => {
                    event.currentTarget.style.background = 'rgba(255,255,255,0.06)';
                  }}
                  onMouseLeave={(event) => {
                    event.currentTarget.style.background = isSelected
                      ? 'rgba(255,255,255,0.08)'
                      : FILTER_SURFACE.panelAlt;
                  }}
                >
                  {option.label}
                </button>
              );
            })}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default function WeatherFilterPanel() {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const currentWeatherDate = resolvePanelWeatherDate(searchParams);
  const activeOverlay = resolveWeatherOverlay(searchParams.get('weatherOverlay'));
  const currentFilters = {
    weatherDate: currentWeatherDate,
  } satisfies WeatherFilters;
  const [filters, setFilters] = useState<WeatherFilters>(currentFilters);

  useEffect(() => {
    setFilters({ weatherDate: currentWeatherDate });
  }, [currentWeatherDate]);

  const buildParams = useCallback((nextFilters: WeatherFilters) => {
    const nextParams = new URLSearchParams(searchParams.toString());

    nextParams.set('tab', 'weather');
    nextParams.delete('weatherMetric');
    nextParams.delete('weatherDateFrom');
    nextParams.delete('weatherDateTo');

    if (nextFilters.weatherDate) {
      nextParams.set('weatherDate', nextFilters.weatherDate);
    } else {
      nextParams.delete('weatherDate');
    }

    return nextParams;
  }, [searchParams]);

  const currentQuery = buildParams(currentFilters).toString();
  const nextQuery = buildParams(filters).toString();
  const hasPendingChanges = nextQuery !== currentQuery;
  const activeCount = filters.weatherDate ? 1 : 0;

  const updateOverlay = useCallback((nextOverlay: WeatherOverlay | null) => {
    const nextParams = new URLSearchParams(searchParams.toString());
    nextParams.set('tab', 'weather');
    writeWeatherOverlay(nextParams, nextOverlay);
    router.replace(`${pathname}?${nextParams.toString()}`);
  }, [pathname, router, searchParams]);

  const apply = useCallback(() => {
    if (!hasPendingChanges) {
      return;
    }

    router.replace(`${pathname}?${nextQuery}`);
  }, [hasPendingChanges, nextQuery, pathname, router]);

  const reset = useCallback(() => {
    setFilters(EMPTY_FILTERS);

    const resetParams = buildParams(EMPTY_FILTERS);
    resetParams.delete('weatherOverlay');
    const resetQuery = resetParams.toString();
    if (resetQuery !== currentQuery) {
      router.replace(`${pathname}?${resetQuery}`);
    }
  }, [buildParams, currentQuery, pathname, router]);

  const inputCls = [
    'w-full rounded-xl px-2.5 py-1.5 transition-all duration-200',
    'bg-transparent placeholder-white/20',
    'border border-white/[0.08] focus:outline-none focus:ring-0',
  ].join(' ');

  const dateInputStyle = {
    fontSize: 13,
    fontFamily: 'inherit',
    fontWeight: 400,
    background: FILTER_SURFACE.panelAlt,
    color: CONTROL_TEXT,
    WebkitTextFillColor: CONTROL_TEXT,
  } as const;

  return (
    <>
      <div
        className="w-[264px] h-full min-h-0 shrink-0 flex flex-col overflow-hidden rounded-2xl p-3 z-10"
        style={{
          background: FILTER_SURFACE.panel,
          border: `1px solid ${FILTER_SURFACE.border}`,
          boxShadow: '0 18px 40px rgba(0,0,0,0.28)',
        }}
      >
        <div className="mb-3 flex items-start justify-between gap-3 shrink-0">
          <div>
            <div
              className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.24em]"
              style={{ color: 'rgba(255,255,255,0.42)' }}
            >
              <SlidersHorizontal size={12} />
              Filters
            </div>
            {activeCount > 0 ? (
              <p className="mt-1 text-xs leading-relaxed" style={{ color: 'rgba(255,255,255,0.42)' }}>
                {activeCount} active filter{activeCount === 1 ? '' : 's'}
              </p>
            ) : null}
          </div>
        </div>

        <div
          className="flex-1 overflow-y-auto custom-scrollbar pr-1"
          style={{ display: 'flex', flexDirection: 'column', gap: 10 }}
        >
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <div>
              <SectionLabel>Date</SectionLabel>
              <input
                type="date"
                className={`${inputCls} filterDateInput [color-scheme:dark]`}
                value={filters.weatherDate}
                onChange={(event) => setFilters((current) => ({
                  ...current,
                  weatherDate: event.target.value,
                }))}
                style={dateInputStyle}
              />
            </div>
            <div>
              <SectionLabel>Overlay</SectionLabel>
              <OverlaySelect value={activeOverlay} onChange={updateOverlay} />
            </div>
          </div>
        </div>

        <div
          className="mt-3 grid shrink-0 grid-cols-2 gap-2 border-t pt-3"
          style={{ borderColor: FILTER_SURFACE.borderSoft }}
        >
          <button
            type="button"
            onClick={apply}
            disabled={!hasPendingChanges}
            className="inline-flex items-center justify-center rounded-xl px-3 py-2 text-sm font-medium transition-all duration-200"
            style={{
              background: hasPendingChanges ? 'rgba(255,255,255,0.10)' : FILTER_SURFACE.panelAlt,
              border: `1px solid ${hasPendingChanges ? 'rgba(255,255,255,0.16)' : FILTER_SURFACE.border}`,
              color: hasPendingChanges ? 'rgba(255,255,255,0.92)' : 'rgba(255,255,255,0.46)',
              cursor: hasPendingChanges ? 'pointer' : 'default',
            }}
          >
            Apply
          </button>
          <button
            type="button"
            onClick={reset}
            className="inline-flex items-center justify-center gap-2 rounded-xl px-3 py-2 text-sm font-medium transition-all duration-200"
            style={{
              background: FILTER_SURFACE.panelAlt,
              border: `1px solid ${FILTER_SURFACE.border}`,
              color: 'rgba(255,255,255,0.68)',
            }}
          >
            <RotateCcw size={13} />
            Reset
          </button>
        </div>
      </div>
      <style jsx>{`
        .filterDateInput::-webkit-datetime-edit,
        .filterDateInput::-webkit-datetime-edit-text,
        .filterDateInput::-webkit-datetime-edit-month-field,
        .filterDateInput::-webkit-datetime-edit-day-field,
        .filterDateInput::-webkit-datetime-edit-year-field {
          color: ${CONTROL_TEXT};
          font-family: inherit;
        }

        .filterDateInput::-webkit-calendar-picker-indicator {
          opacity: 0.45;
        }
      `}</style>
    </>
  );
}