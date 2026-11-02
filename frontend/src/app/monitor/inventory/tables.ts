import {
    Bird, Cloud, Skull, Binoculars, Navigation, Satellite,
    Activity, BarChart3, ShieldCheck,
} from 'lucide-react';

export type TableDef = {
    key: string;
    name: string;
    desc: string;
    icon: React.ComponentType<{ size?: number; style?: React.CSSProperties }>;
    accentRgb: string;
    orderCol: string;
    type: 'Hypertable' | 'Regular';
};

export const TABLE_DEFS: TableDef[] = [
    /* ── Hypertables (time-series) ───────────────────────── */
    {
        key: 'birdweather_tbl',
        name: 'BirdWeather Detections',
        desc: 'Acoustic bird detections from BirdWeather stations across the Netherlands',
        icon: Bird,
        accentRgb: '52,211,153',
        orderCol: 'detected_at',
        type: 'Hypertable',
    },
    {
        key: 'knmi_tbl',
        name: 'KNMI Weather Observations',
        desc: 'Hourly weather measurements from KNMI weather stations',
        icon: Cloud,
        accentRgb: '56,189,248',
        orderCol: 'observed_at',
        type: 'Hypertable',
    },
    {
        key: 'ebird_tbl',
        name: 'eBird Observations',
        desc: 'Citizen-science bird observations from the eBird platform',
        icon: Binoculars,
        accentRgb: '129,140,248',
        orderCol: 'obs_dt',
        type: 'Hypertable',
    },
    {
        key: 'movebank_tbl',
        name: 'Movebank Tracking Events',
        desc: 'GPS/sensor tracking events for tagged birds from Movebank studies',
        icon: Navigation,
        accentRgb: '244,114,182',
        orderCol: 'timestamp',
        type: 'Hypertable',
    },

    /* ── Regular tables ──────────────────────────────────── */
    {
        key: 'sovon_tbl',
        name: 'Sovon Mortality Records',
        desc: 'Bird mortality data collected by Sovon surveys',
        icon: Skull,
        accentRgb: '251,191,36',
        orderCol: 'observation_date',
        type: 'Regular',
    },
    {
        key: 'google_earth_engine_tbl',
        name: 'Earth Engine Base Grid Profiles',
        desc: 'Canonical Earth Engine feature facts on the 5 km Netherlands base grid; all derived grid products read from this main table',
        icon: Satellite,
        accentRgb: '74,222,128',
        orderCol: 'observation_date',
        type: 'Regular',
    },
    {
        key: 'grid_cells_tbl',
        name: 'Canonical Grid Cells',
        desc: 'Shared Netherlands tessellation registry with authoritative grid-cell geometry for map overlays and gridded data products',
        icon: Satellite,
        accentRgb: '34,197,94',
        orderCol: 'grid_cell_id',
        type: 'Regular',
    },
    {
        key: 'seismic_tbl',
        name: 'Seismic Events',
        desc: 'Earthquake and seismic event records from KNMI seismology',
        icon: Activity,
        accentRgb: '253,224,71',
        orderCol: 'event_time',
        type: 'Regular',
    },
    {
        key: 'birdweather_stations_tbl',
        name: 'Station Summary',
        desc: 'Pre-aggregated per-station statistics, quality scores, and species counts',
        icon: BarChart3,
        accentRgb: '167,139,250',
        orderCol: 'last_detection',
        type: 'Regular',
    },
    {
        key: 'data_quality_tbl',
        name: 'Data Quality Results',
        desc: 'Quality audit metrics and run summaries from automated data quality checks',
        icon: ShieldCheck,
        accentRgb: '45,212,191',
        orderCol: 'created_at',
        type: 'Regular',
    },
];

/** Allowed table keys for SQL interpolation safety */
export const VALID_TABLE_KEYS = TABLE_DEFS.map((t) => t.key);
