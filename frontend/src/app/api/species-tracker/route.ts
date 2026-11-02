import { unstable_cache } from "next/cache";
import { NextRequest, NextResponse } from "next/server";

import { TARGET_SPECIES_BY_BIRDWEATHER_ID } from "@/config/target-species";
import { query } from "@/lib/db";
import {
    activeScoreRowsCte,
    loadFeaturePayloadsForSpecies,
} from "@/lib/modeling-db";
import { withMeasuredApiRoute } from "@/lib/performance/server";
import { createSnapshotCache } from "@/lib/swr-snapshot";

import {
    buildMortalityByYear,
    aggregateSeasonalPresenceRows,
    calculatePopulationActivityMetrics,
    lastCompleteUtcYear,
    selectPopulationLoadResult,
    type SeasonalPresenceYear,
} from "./helpers";

export type SpeciesTrackerGrain = "day" | "week" | "month";

export interface SpeciesTrackerSpeciesOption {
    speciesId: string;
    commonName: string;
    scientificName: string | null;
    color: string | null;
    imageUrl: string | null;
    thumbnailUrl: string | null;
    silhouetteUrl: string | null;
    isTarget: boolean;
    totalDetections: number;
}

export interface SpeciesTrackerPopulationPoint {
    bucketStart: string;
    label: string;
    detections: number;
    activeDays: number;
    activeStations: number;
    communityActiveDays: number;
    communityActiveStations: number;
    stationDayEffort: number;
    detectionsPerActiveStationDay: number | null;
    activityIndex: number;
    historicalAverageDetections: number | null;
    historicalAverageActiveDays: number | null;
    historicalAverageActiveStations: number | null;
    historicalAverageCommunityActiveStations: number | null;
    historicalAverageActivityIndex: number | null;
}

export interface SpeciesTrackerMortalityPoint {
    month: number;
    label: string;
    count: number;
    historicalAverageCount: number | null;
}

export interface SpeciesTrackerMortalityYearSeries {
    year: number;
    series: SpeciesTrackerMortalityPoint[];
}

export interface SpeciesTrackerMortality {
    scope: "species" | "network";
    source: "sovon";
    year: number | null;
    series: SpeciesTrackerMortalityPoint[];
    availableYears: number[];
    byYear: SpeciesTrackerMortalityYearSeries[];
}

export interface SpeciesTrackerMortalitySources {
    sovon: SpeciesTrackerMortality;
}

export type SpeciesTrackerSeasonalPresenceYear = SeasonalPresenceYear;

export interface SpeciesTrackerAnomalySeriesPoint {
    periodStart: string;
    amber: number;
    red: number;
    scoredCells: number;
    avgScore: number | null;
    avgBioacousticScore: number | null;
    avgBioacousticAnomalyScore: number | null;
    avgBioacousticOutlierShare: number | null;
    avgPopulationScore: number | null;
}

export interface SpeciesTrackerHotspot {
    stationId: string;
    stationName: string;
    lat: number | null;
    lon: number | null;
    detections: number;
    activeDays: number;
    avgConfidence: number | null;
    lastDetectedAt: string | null;
}

export interface SpeciesTrackerSummary {
    currentYearDetections: number;
    historicalAverageDetections: number | null;
    currentYearActiveDays: number;
    historicalAverageActiveDays: number | null;
    currentYearActiveStations: number;
    historicalAverageActiveStations: number | null;
    detectionsPerActiveDay: number | null;
    temporalOccupancyPct: number | null;
    stationPresencePct: number | null;
    relativeAbundancePct: number | null;
    yoyVsHistoricalPct: number | null;
    lastDetectedAt: string | null;
    firstDetectedThisYear: string | null;
    passageDays: number | null;
}

export interface SpeciesTrackerActivity {
    confidenceMean: number | null;
    confidenceP10: number | null;
    confidenceP50: number | null;
    confidenceP90: number | null;
    highConfidencePct: number | null;
    hourly: Array<{ hour: number; detections: number }>;
    backgroundLabelMix: Record<string, number>;
    seasonalPresence: SpeciesTrackerSeasonalPresenceYear[];
}

export interface SpeciesTrackerContext {
    avgStationAvailability90dPct: number | null;
    avgStationLifetimeAvailabilityPct: number | null;
    avgDaysSinceLast: number | null;
    occupiedStations: number;
    modelingStations: number;
    occupiedGridCells: number;
    meanTemperatureC: number | null;
    meanHumidityPct: number | null;
    meanWindMs: number | null;
    totalRainMm: number | null;
    meanCooccurringRichness: number | null;
    anomalySeries: SpeciesTrackerAnomalySeriesPoint[];
}

export interface SpeciesTrackerDetail {
    species: SpeciesTrackerSpeciesOption;
    summary: SpeciesTrackerSummary;
    population: {
        series: SpeciesTrackerPopulationPoint[];
        hotspots: SpeciesTrackerHotspot[];
    };
    mortality: SpeciesTrackerMortality;
    mortalitySources: SpeciesTrackerMortalitySources;
    activity: SpeciesTrackerActivity;
    context: SpeciesTrackerContext;
}

export interface SpeciesTrackerResponse {
    asOf: string | null;
    grain: SpeciesTrackerGrain;
    speciesId: string | null;
    speciesList: SpeciesTrackerSpeciesOption[];
    detail: SpeciesTrackerDetail | null;
    empty: boolean;
}

type JsonRecord = Record<string, unknown>;

interface GrainSql {
    keyExpression: string;
    labelExpression: string;
}

interface CurrentYearPopulationSummary {
    year: number | null;
    currentYearDetections: number;
    currentYearActiveDays: number;
    currentYearActiveStations: number;
    firstDetectedThisYear: string | null;
    lastDetectedAt: string | null;
}

interface PopulationLoadResult {
    series: SpeciesTrackerPopulationPoint[];
    summary: CurrentYearPopulationSummary;
}

function numberOrNull(value: unknown): number | null {
    if (value === null || value === undefined) return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
}

function numberOrZero(value: unknown): number {
    return numberOrNull(value) ?? 0;
}

function integerOrZero(value: unknown): number {
    return Math.trunc(numberOrZero(value));
}

function textOrNull(value: unknown): string | null {
    return typeof value === "string" && value.length > 0 ? value : null;
}

function jsonRecord(value: unknown): JsonRecord {
    if (value && typeof value === "object" && !Array.isArray(value)) {
        return value as JsonRecord;
    }
    return {};
}

function jsonRecordsArray(value: unknown): JsonRecord[] {
    if (!Array.isArray(value)) return [];
    return value.filter((entry): entry is JsonRecord => !!entry && typeof entry === "object" && !Array.isArray(entry));
}

function roundedAverage(values: Array<number | null>): number | null {
    const finiteValues = values.filter((value): value is number => Number.isFinite(value));
    if (!finiteValues.length) return null;
    const average = finiteValues.reduce((sum, value) => sum + value, 0) / finiteValues.length;
    return Math.round(average * 10) / 10;
}

function yearFromDate(value: unknown): number | null {
    const text = textOrNull(value);
    if (!text) return null;
    const year = Number(text.slice(0, 4));
    return Number.isFinite(year) ? year : null;
}

function buildSpecies(row: JsonRecord): SpeciesTrackerSpeciesOption {
    const speciesInfo = jsonRecord(row.species_info);
    const speciesId = textOrNull(row.species_id) ?? "unknown";
    const targetSpecies = TARGET_SPECIES_BY_BIRDWEATHER_ID.get(speciesId);
    const commonName =
        textOrNull(row.common_name) ??
        textOrNull(speciesInfo.common_name) ??
        targetSpecies?.english ??
        `Species ${speciesId}`;
    const scientificName =
        textOrNull(row.scientific_name) ??
        textOrNull(speciesInfo.scientific_name) ??
        targetSpecies?.scientific ??
        null;
    const imageUrl =
        textOrNull(row.species_image_url) ??
        textOrNull(speciesInfo.image_url) ??
        textOrNull(row.species_thumbnail_url) ??
        textOrNull(speciesInfo.thumbnail_url);
    const thumbnailUrl =
        textOrNull(row.species_thumbnail_url) ??
        textOrNull(speciesInfo.thumbnail_url) ??
        textOrNull(row.species_image_url) ??
        textOrNull(speciesInfo.image_url);
    const silhouetteUrl =
        textOrNull(row.phylopic_svg_url) ??
        textOrNull(row.phylopic_thumb_url) ??
        null;

    return {
        speciesId,
        commonName,
        scientificName,
        color: textOrNull(row.species_color) ?? textOrNull(speciesInfo.species_color),
        imageUrl,
        thumbnailUrl,
        silhouetteUrl,
        isTarget: speciesInfo.is_target_species === true || TARGET_SPECIES_BY_BIRDWEATHER_ID.has(speciesId),
        totalDetections: integerOrZero(row.total_detections),
    };
}

function grainSql(grain: SpeciesTrackerGrain): GrainSql {
    switch (grain) {
        case "day":
            return {
                keyExpression: "EXTRACT(DOY FROM period_start)::int",
                labelExpression: "to_char(period_start, 'Mon DD')",
            };
        case "week":
            return {
                keyExpression: "EXTRACT(WEEK FROM period_start)::int",
                labelExpression: "'W' || lpad(EXTRACT(WEEK FROM period_start)::int::text, 2, '0')",
            };
        case "month":
        default:
            return {
                keyExpression: "EXTRACT(MONTH FROM period_start)::int",
                labelExpression: "to_char(period_start, 'Mon')",
            };
    }
}

function observationPeriodStartSql(grain: SpeciesTrackerGrain, column: string): string {
    switch (grain) {
        case "day":
            return column;
        case "week":
            return `date_trunc('week', ${column}::timestamp)::date`;
        case "month":
        default:
            return `date_trunc('month', ${column}::timestamp)::date`;
    }
}

function targetYearDetectionCtesSql(): string {
    return `
        target_year AS (
            SELECT make_date($3::int, 1, 1) AS year_start
        ), cagg_rows AS (
            SELECT
                bucket::date AS observation_date,
                species_id,
                station_id,
                SUM(detection_count)::bigint AS detections
            FROM birdweather_daily_cagg_tbl, target_year
            WHERE bucket >= target_year.year_start
              AND bucket < target_year.year_start + INTERVAL '1 year'
            GROUP BY bucket::date, species_id, station_id
        ), cagg_coverage AS (
            SELECT MAX(observation_date) AS max_observation_date
            FROM cagg_rows
        ), raw_tail AS (
            SELECT
                observation_date,
                species_id,
                station_id,
                COUNT(*)::bigint AS detections
            FROM birdweather_tbl, target_year, cagg_coverage
            WHERE observation_date >= target_year.year_start
              AND observation_date < target_year.year_start + INTERVAL '1 year'
              AND observation_date > COALESCE(cagg_coverage.max_observation_date, target_year.year_start - INTERVAL '1 day')
            GROUP BY observation_date, species_id, station_id
        ), detection_rows AS (
            SELECT observation_date, species_id, station_id, detections
            FROM cagg_rows
            UNION ALL
            SELECT observation_date, species_id, station_id, detections
            FROM raw_tail
        )
    `;
}

function historicalActivityIndexSql(specAlias: string, communityAlias: string): string {
    const effort = `NULLIF(${communityAlias}.active_days * ${communityAlias}.active_station_count, 0)`;
    const rate = `(${specAlias}.total_detections::double precision / ${effort})`;
    return `
        ROUND((
            100.0 * (
                GREATEST(0.0, LEAST(1.0, COALESCE(${specAlias}.relative_abundance_pct, 0) / 100.0)) +
                GREATEST(0.0, LEAST(1.0, COALESCE(${specAlias}.temporal_occupancy_pct, 0) / 100.0)) +
                GREATEST(0.0, LEAST(1.0, COALESCE(${specAlias}.station_presence_pct, 0) / 100.0)) +
                GREATEST(0.0, LEAST(1.0, COALESCE(${rate} / NULLIF(${rate} + 1.0, 0), 0)))
            ) / 4.0
        )::numeric, 1)::double precision
    `;
}

function normalizeBackgroundMix(value: unknown): Record<string, number> {
    const mix = jsonRecord(value);
    return Object.fromEntries(
        Object.entries(mix)
            .map(([label, share]) => [label, numberOrNull(share)] as const)
            .filter((entry): entry is readonly [string, number] => entry[1] !== null),
    );
}

function hourlySeries(value: unknown): Array<{ hour: number; detections: number }> {
    const hourlyCounts = jsonRecord(jsonRecord(value).hourly_detection_counts);
    return Array.from({ length: 24 }, (_, hour) => ({
        hour,
        detections: integerOrZero(hourlyCounts[String(hour)]),
    }));
}

async function loadSpeciesList(): Promise<SpeciesTrackerSpeciesOption[]> {
    // Scope the picker to the configured target species only; without this the
    // list shows every species detected in the network (e.g. "Species 89").
    const targetSpeciesIds = Array.from(TARGET_SPECIES_BY_BIRDWEATHER_ID.keys());
    const result = await query<JsonRecord>(`
        WITH network_years AS (
            SELECT species_id, period_start, total_detections, species_info
            FROM species_tracker_tbl
            WHERE row_kind = 'species'
              AND location_type = 'network'
              AND location_id = 'nl'
              AND period_grain = 'year'
              AND species_id IS NOT NULL
              AND species_id = ANY($1::text[])
        ), ranked AS (
            SELECT
                species_id,
                SUM(total_detections)::bigint AS total_detections,
                (ARRAY_AGG(species_info ORDER BY period_start DESC))[1] AS species_info
            FROM network_years
            GROUP BY species_id
        )
        SELECT ranked.species_id,
               ranked.total_detections,
               ranked.species_info,
               sp.common_name,
               sp.scientific_name,
               sp.species_color,
               sp.species_image_url,
               sp.species_thumbnail_url,
               sp.phylopic_svg_url,
               sp.phylopic_thumb_url
        FROM ranked
        LEFT JOIN species_tbl sp ON sp.species_id = ranked.species_id
        ORDER BY total_detections DESC, species_id ASC
    `, [targetSpeciesIds]);
    return result.rows.map(buildSpecies);
}

function mapPopulationSeriesEntry(row: JsonRecord): SpeciesTrackerPopulationPoint {
    const detections = integerOrZero(row.detections);
    const activeDays = integerOrZero(row.active_days);
    const activeStations = integerOrZero(row.active_stations);
    const communityActiveDays = integerOrZero(row.community_active_days);
    const communityActiveStations = integerOrZero(row.community_active_stations);
    const metrics = calculatePopulationActivityMetrics({
        detections,
        activeDays,
        activeStations,
        communityDetections: numberOrZero(row.community_detections),
        communityActiveDays,
        communityActiveStations,
        periodDayCount: integerOrZero(row.period_day_count),
        stationDayEffort: numberOrNull(row.station_day_effort) ?? undefined,
    });

    return {
        label: textOrNull(row.label) ?? "-",
        bucketStart: textOrNull(row.bucket_start) ?? "",
        detections,
        activeDays,
        activeStations,
        communityActiveDays,
        communityActiveStations,
        stationDayEffort: metrics.stationDayEffort,
        detectionsPerActiveStationDay: metrics.detectionsPerActiveStationDay,
        activityIndex: metrics.activityIndex,
        historicalAverageDetections: numberOrNull(row.avg_detections),
        historicalAverageActiveDays: numberOrNull(row.avg_active_days),
        historicalAverageActiveStations: numberOrNull(row.avg_active_stations),
        historicalAverageCommunityActiveStations: numberOrNull(row.avg_community_active_stations),
        historicalAverageActivityIndex: numberOrNull(row.avg_activity_index),
    };
}

function buildPopulationLoadResult(row: JsonRecord): PopulationLoadResult {
    return {
        series: jsonRecordsArray(row.series).map(mapPopulationSeriesEntry),
        summary: {
            year: yearFromDate(row.year_start),
            currentYearDetections: integerOrZero(row.total_detections),
            currentYearActiveDays: integerOrZero(row.active_days),
            currentYearActiveStations: integerOrZero(row.active_stations),
            firstDetectedThisYear: textOrNull(row.first_detected),
            lastDetectedAt: textOrNull(row.last_detected),
        },
    };
}

interface ArtifactPopulationBucket {
    bucketStart: string;
    label: string;
    periodDayCount: number;
    detections: number;
    activeStations: number;
    communityDetections: number;
    communityActiveStations: number;
    stationDayEffort: number;
    activeDaysByWeek: Map<string, number>;
    communityActiveDaysByWeek: Map<string, number>;
}

function emptyPopulationLoadResult(year: number | null = null): PopulationLoadResult {
    return {
        series: [],
        summary: {
            year,
            currentYearDetections: 0,
            currentYearActiveDays: 0,
            currentYearActiveStations: 0,
            firstDetectedThisYear: null,
            lastDetectedAt: null,
        },
    };
}

function daysInUtcMonth(year: number, monthIndex: number): number {
    return new Date(Date.UTC(year, monthIndex + 1, 0)).getUTCDate();
}

function artifactBucketStart(row: JsonRecord, grain: SpeciesTrackerGrain): string | null {
    const periodStart = textOrNull(row.period_start);
    if (!periodStart) return null;
    if (grain === "month") {
        const monthStart = textOrNull(row.month_start);
        return monthStart ?? `${periodStart.slice(0, 7)}-01`;
    }
    return periodStart.slice(0, 10);
}

function artifactBucketLabel(bucketStart: string, grain: SpeciesTrackerGrain): string {
    const parsed = new Date(`${bucketStart}T00:00:00Z`);
    if (!Number.isFinite(parsed.getTime())) return bucketStart;
    if (grain === "month") {
        return parsed.toLocaleDateString("en-GB", { month: "short", timeZone: "UTC" });
    }
    if (grain === "week") {
        const week = Number.isFinite(parsed.getTime())
            ? Math.ceil((((parsed.getTime() - Date.UTC(parsed.getUTCFullYear(), 0, 1)) / 86_400_000) + 1) / 7)
            : 0;
        return `W${String(week).padStart(2, "0")}`;
    }
    return parsed.toLocaleDateString("en-GB", { month: "short", day: "2-digit", timeZone: "UTC" });
}

function artifactPeriodDayCount(bucketStart: string, grain: SpeciesTrackerGrain, row: JsonRecord): number {
    if (grain === "month") {
        const year = Number(bucketStart.slice(0, 4));
        const month = Number(bucketStart.slice(5, 7));
        return Number.isFinite(year) && Number.isFinite(month) ? daysInUtcMonth(year, month - 1) : 30;
    }
    return Math.max(1, integerOrZero(row.period_day_count) || 7);
}

function updateMaxMapValue(map: Map<string, number>, key: string, value: number): void {
    map.set(key, Math.max(map.get(key) ?? 0, value));
}

function buildArtifactPopulationPoint(
    bucket: ArtifactPopulationBucket,
    historicalAverageActivityIndex: number | null = null,
): SpeciesTrackerPopulationPoint {
    const activeDays = Math.min(
        bucket.periodDayCount,
        Array.from(bucket.activeDaysByWeek.values()).reduce((sum, value) => sum + value, 0),
    );
    const communityActiveDays = Math.min(
        bucket.periodDayCount,
        Array.from(bucket.communityActiveDaysByWeek.values()).reduce((sum, value) => sum + value, 0),
    );
    const metrics = calculatePopulationActivityMetrics({
        detections: bucket.detections,
        activeDays,
        activeStations: bucket.activeStations,
        communityDetections: bucket.communityDetections,
        communityActiveDays,
        communityActiveStations: bucket.communityActiveStations,
        periodDayCount: bucket.periodDayCount,
        stationDayEffort: bucket.stationDayEffort,
    });

    return {
        bucketStart: bucket.bucketStart,
        label: bucket.label,
        detections: Math.trunc(bucket.detections),
        activeDays,
        activeStations: Math.trunc(bucket.activeStations),
        communityActiveDays,
        communityActiveStations: Math.trunc(bucket.communityActiveStations),
        stationDayEffort: metrics.stationDayEffort,
        detectionsPerActiveStationDay: metrics.detectionsPerActiveStationDay,
        activityIndex: metrics.activityIndex,
        historicalAverageDetections: null,
        historicalAverageActiveDays: null,
        historicalAverageActiveStations: null,
        historicalAverageCommunityActiveStations: null,
        historicalAverageActivityIndex,
    };
}

async function loadPopulationDataFromModelingDb(
    speciesId: string,
    grain: SpeciesTrackerGrain,
    targetYear: number,
): Promise<PopulationLoadResult> {
    try {
        const rows = await loadFeaturePayloadsForSpecies(speciesId);
        const bucketsByYear = new Map<number, Map<string, ArtifactPopulationBucket>>();

        for (const row of rows) {
            if (String(row.species_id ?? "") !== speciesId) continue;
            const year = Number(String(row.period_start ?? "").slice(0, 4));
            if (!Number.isFinite(year)) continue;

            const bucketStart = artifactBucketStart(row, grain);
            if (!bucketStart) continue;
            if (!bucketsByYear.has(year)) {
                bucketsByYear.set(year, new Map());
            }
            const yearBuckets = bucketsByYear.get(year)!;
            if (!yearBuckets.has(bucketStart)) {
                yearBuckets.set(bucketStart, {
                    bucketStart,
                    label: artifactBucketLabel(bucketStart, grain),
                    periodDayCount: artifactPeriodDayCount(bucketStart, grain, row),
                    detections: 0,
                    activeStations: 0,
                    communityDetections: 0,
                    communityActiveStations: 0,
                    stationDayEffort: 0,
                    activeDaysByWeek: new Map(),
                    communityActiveDaysByWeek: new Map(),
                });
            }

            const bucket = yearBuckets.get(bucketStart)!;
            const periodStart = String(row.period_start ?? bucketStart);
            const communityActiveDays = integerOrZero(row.community_active_days);
            const communityActiveStations = integerOrZero(row.community_active_station_count);
            bucket.detections += numberOrZero(row.total_detections);
            bucket.activeStations += numberOrZero(row.species_station_count);
            bucket.communityDetections += numberOrZero(row.community_total_detections);
            bucket.communityActiveStations += communityActiveStations;
            bucket.stationDayEffort += communityActiveDays * communityActiveStations;
            updateMaxMapValue(bucket.activeDaysByWeek, periodStart, integerOrZero(row.active_days));
            updateMaxMapValue(bucket.communityActiveDaysByWeek, periodStart, communityActiveDays);
        }

        const currentBuckets = bucketsByYear.get(targetYear);
        if (!currentBuckets?.size) {
            return emptyPopulationLoadResult(targetYear);
        }

        const historicalBuckets = Array.from(bucketsByYear.entries())
            .filter(([year]) => year >= targetYear - 5 && year < targetYear)
            .flatMap(([, yearBuckets]) => Array.from(yearBuckets.values()));
        const historicalActivityByBucket = new Map<string, number[]>();
        for (const bucket of historicalBuckets) {
            const point = buildArtifactPopulationPoint(bucket);
            const bucketKey = grain === "month" ? bucket.bucketStart.slice(5, 7) : bucket.label;
            const values = historicalActivityByBucket.get(bucketKey) ?? [];
            values.push(point.activityIndex);
            historicalActivityByBucket.set(bucketKey, values);
        }

        const series = Array.from(currentBuckets.values())
            .sort((a, b) => a.bucketStart.localeCompare(b.bucketStart))
            .map((bucket) => {
                const bucketKey = grain === "month" ? bucket.bucketStart.slice(5, 7) : bucket.label;
                const historicalValues = historicalActivityByBucket.get(bucketKey) ?? [];
                const historicalAverageActivityIndex = historicalValues.length > 0
                    ? Math.round((historicalValues.reduce((sum, value) => sum + value, 0) / historicalValues.length) * 10) / 10
                    : null;
                return buildArtifactPopulationPoint(bucket, historicalAverageActivityIndex);
            });

        return {
            series,
            summary: {
                year: targetYear,
                currentYearDetections: series.reduce((sum, point) => sum + point.detections, 0),
                currentYearActiveDays: Math.max(0, ...series.map((point) => point.activeDays)),
                currentYearActiveStations: Math.max(0, ...series.map((point) => point.activeStations)),
                firstDetectedThisYear: series[0]?.bucketStart ?? null,
                lastDetectedAt: series.at(-1)?.bucketStart ?? null,
            },
        };
    } catch {
        return emptyPopulationLoadResult(targetYear);
    }
}

async function loadFreshPopulationData(
    speciesId: string,
    grain: SpeciesTrackerGrain,
    targetYear: number,
): Promise<PopulationLoadResult> {
    const expressions = grainSql(grain);
    const periodStartExpression = observationPeriodStartSql(grain, "observation_date");
    const currentKeyExpression = expressions.keyExpression.replaceAll("period_start", "species_rows.period_start");
    const currentLabelExpression = expressions.labelExpression.replaceAll("period_start", "species_rows.period_start");
    const historicalActivityIndex = historicalActivityIndexSql("species_tracker", "community_tracker");
    const result = await query<JsonRecord>(`
        WITH ${targetYearDetectionCtesSql()}, species_rows AS (
            SELECT
                ${periodStartExpression} AS period_start,
                SUM(detections)::bigint AS total_detections,
                COUNT(DISTINCT observation_date)::int AS active_days,
                COUNT(DISTINCT station_id)::int AS species_station_count
            FROM detection_rows
            WHERE species_id = $1
            GROUP BY period_start
        ), community_rows AS (
            SELECT
                ${periodStartExpression} AS period_start,
                SUM(detections)::bigint AS community_detections,
                COUNT(DISTINCT observation_date)::int AS community_active_days,
                COUNT(DISTINCT station_id)::int AS community_active_stations
            FROM detection_rows
            GROUP BY period_start
        ), current_series AS (
            SELECT
                ${currentKeyExpression} AS bucket_key,
                ${currentLabelExpression} AS label,
                species_rows.period_start AS bucket_start,
                (species_rows.period_end - species_rows.period_start + 1)::int AS period_day_count,
                species_rows.total_detections::bigint AS detections,
                species_rows.active_days::int AS active_days,
                COALESCE(species_rows.species_station_count, 0)::int AS active_stations,
                COALESCE(community_rows.community_detections, 0)::bigint AS community_detections,
                COALESCE(community_rows.community_active_days, 0)::int AS community_active_days,
                COALESCE(community_rows.community_active_stations, 0)::int AS community_active_stations
            FROM (
                SELECT
                    period_start,
                    CASE
                        WHEN $2 = 'day' THEN period_start
                        WHEN $2 = 'week' THEN period_start + INTERVAL '6 days'
                        ELSE (period_start + INTERVAL '1 month - 1 day')
                    END::date AS period_end,
                    total_detections,
                    active_days,
                    species_station_count
                FROM species_rows
            ) species_rows
            LEFT JOIN community_rows USING (period_start)
            JOIN target_year ON TRUE
            WHERE species_rows.period_start >= target_year.year_start
              AND species_rows.period_start < target_year.year_start + INTERVAL '1 year'
        ), historical AS (
            SELECT
                ${expressions.keyExpression.replaceAll("period_start", "species_tracker.period_start")} AS bucket_key,
                AVG(species_tracker.total_detections)::double precision AS avg_detections,
                AVG(species_tracker.active_days)::double precision AS avg_active_days,
                AVG(COALESCE(species_tracker.species_station_count, 0))::double precision AS avg_active_stations,
                AVG(COALESCE(community_tracker.active_station_count, species_tracker.active_station_count, 0))::double precision AS avg_community_active_stations,
                AVG(${historicalActivityIndex})::double precision AS avg_activity_index
            FROM species_tracker_tbl species_tracker
            JOIN target_year ON TRUE
            LEFT JOIN species_tracker_tbl community_tracker
              ON community_tracker.row_kind = 'community'
             AND community_tracker.location_type = species_tracker.location_type
             AND community_tracker.location_id = species_tracker.location_id
             AND community_tracker.period_grain = species_tracker.period_grain
             AND community_tracker.period_start = species_tracker.period_start
            WHERE species_tracker.row_kind = 'species'
              AND species_tracker.location_type = 'network'
              AND species_tracker.location_id = 'nl'
              AND species_tracker.species_id = $1
              AND species_tracker.period_grain = $2
              AND species_tracker.period_start < target_year.year_start
            GROUP BY bucket_key
        ), summary AS (
            SELECT
                target_year.year_start::text AS year_start,
                COALESCE(SUM(detection_rows.detections), 0)::bigint AS total_detections,
                COUNT(DISTINCT detection_rows.observation_date)::int AS active_days,
                COUNT(DISTINCT detection_rows.station_id)::int AS active_stations,
                MIN(detection_rows.observation_date)::text AS first_detected,
                MAX(detection_rows.observation_date)::text AS last_detected
            FROM target_year
            LEFT JOIN detection_rows
              ON detection_rows.species_id = $1
            GROUP BY target_year.year_start
        ), series_payload AS (
            SELECT COALESCE(
                json_agg(
                    json_build_object(
                        'label', current_series.label,
                        'bucket_start', current_series.bucket_start::text,
                        'period_day_count', current_series.period_day_count,
                        'detections', current_series.detections,
                        'active_days', current_series.active_days,
                        'active_stations', current_series.active_stations,
                        'community_detections', current_series.community_detections,
                        'community_active_days', current_series.community_active_days,
                        'community_active_stations', current_series.community_active_stations,
                        'avg_detections', historical.avg_detections,
                        'avg_active_days', historical.avg_active_days,
                        'avg_active_stations', historical.avg_active_stations,
                        'avg_community_active_stations', historical.avg_community_active_stations,
                        'avg_activity_index', historical.avg_activity_index
                    )
                    ORDER BY current_series.bucket_start
                ),
                '[]'::json
            ) AS series
            FROM current_series
            LEFT JOIN historical USING (bucket_key)
        )
        SELECT
            summary.year_start,
            summary.total_detections,
            summary.active_days,
            summary.active_stations,
            summary.first_detected,
            summary.last_detected,
            series_payload.series
        FROM summary
        CROSS JOIN series_payload
    `, [speciesId, grain, targetYear]);

    return buildPopulationLoadResult(result.rows[0] ?? {});
}

async function loadPopulationDataFromTracker(
    speciesId: string,
    grain: SpeciesTrackerGrain,
    targetYear: number,
): Promise<PopulationLoadResult> {
    const expressions = grainSql(grain);
    const speciesKeyExpression = expressions.keyExpression.replaceAll("period_start", "species_tracker.period_start");
    const historicalActivityIndex = historicalActivityIndexSql("species_tracker", "community_tracker");
    const result = await query<JsonRecord>(`
        WITH preferred_year AS (
            SELECT make_date($3::int, 1, 1) AS year_start
        ), selected_year AS (
            SELECT COALESCE(
                (
                    SELECT tracker.period_start
                    FROM species_tracker_tbl tracker
                    CROSS JOIN preferred_year
                    WHERE tracker.row_kind = 'species'
                      AND tracker.location_type = 'network'
                      AND tracker.location_id = 'nl'
                      AND tracker.period_grain = 'year'
                      AND tracker.species_id = $1
                      AND tracker.period_start = preferred_year.year_start
                    LIMIT 1
                ),
                (
                    SELECT MAX(tracker.period_start)
                    FROM species_tracker_tbl tracker
                    WHERE tracker.row_kind = 'species'
                      AND tracker.location_type = 'network'
                      AND tracker.location_id = 'nl'
                      AND tracker.period_grain = 'year'
                      AND tracker.species_id = $1
                )
            ) AS year_start
        ), current_series AS (
            SELECT
                ${speciesKeyExpression} AS bucket_key,
                ${expressions.labelExpression.replaceAll("period_start", "species_tracker.period_start")} AS label,
                species_tracker.period_start AS bucket_start,
                species_tracker.period_day_count::int AS period_day_count,
                species_tracker.total_detections::bigint AS detections,
                species_tracker.active_days::int AS active_days,
                COALESCE(species_tracker.species_station_count, 0)::int AS active_stations,
                COALESCE(community_tracker.total_detections, 0)::bigint AS community_detections,
                COALESCE(community_tracker.active_days, species_tracker.period_day_count, 0)::int AS community_active_days,
                COALESCE(community_tracker.active_station_count, species_tracker.active_station_count, 0)::int AS community_active_stations
            FROM species_tracker_tbl species_tracker
            JOIN selected_year ON TRUE
            LEFT JOIN species_tracker_tbl community_tracker
              ON community_tracker.row_kind = 'community'
             AND community_tracker.location_type = species_tracker.location_type
             AND community_tracker.location_id = species_tracker.location_id
             AND community_tracker.period_grain = species_tracker.period_grain
             AND community_tracker.period_start = species_tracker.period_start
            WHERE selected_year.year_start IS NOT NULL
              AND species_tracker.row_kind = 'species'
              AND species_tracker.location_type = 'network'
              AND species_tracker.location_id = 'nl'
              AND species_tracker.species_id = $1
              AND species_tracker.period_grain = $2
              AND species_tracker.period_start >= selected_year.year_start
              AND species_tracker.period_start < selected_year.year_start + INTERVAL '1 year'
        ), historical AS (
            SELECT
                ${speciesKeyExpression} AS bucket_key,
                AVG(species_tracker.total_detections)::double precision AS avg_detections,
                AVG(species_tracker.active_days)::double precision AS avg_active_days,
                AVG(COALESCE(species_tracker.species_station_count, 0))::double precision AS avg_active_stations,
                AVG(COALESCE(community_tracker.active_station_count, species_tracker.active_station_count, 0))::double precision AS avg_community_active_stations,
                AVG(${historicalActivityIndex})::double precision AS avg_activity_index
            FROM species_tracker_tbl species_tracker
            JOIN selected_year ON TRUE
            LEFT JOIN species_tracker_tbl community_tracker
              ON community_tracker.row_kind = 'community'
             AND community_tracker.location_type = species_tracker.location_type
             AND community_tracker.location_id = species_tracker.location_id
             AND community_tracker.period_grain = species_tracker.period_grain
             AND community_tracker.period_start = species_tracker.period_start
            WHERE selected_year.year_start IS NOT NULL
              AND species_tracker.row_kind = 'species'
              AND species_tracker.location_type = 'network'
              AND species_tracker.location_id = 'nl'
              AND species_tracker.species_id = $1
              AND species_tracker.period_grain = $2
              AND species_tracker.period_start < selected_year.year_start
            GROUP BY bucket_key
        ), summary AS (
            SELECT
                tracker.period_start::text AS year_start,
                tracker.total_detections::bigint AS total_detections,
                tracker.active_days::int AS active_days,
                COALESCE(tracker.species_station_count, 0)::int AS active_stations,
                tracker.first_detection_at::text AS first_detected,
                tracker.last_detection_at::text AS last_detected
            FROM species_tracker_tbl tracker
            JOIN selected_year
              ON selected_year.year_start = tracker.period_start
            WHERE tracker.row_kind = 'species'
              AND tracker.location_type = 'network'
              AND tracker.location_id = 'nl'
              AND tracker.period_grain = 'year'
              AND tracker.species_id = $1
        ), series_payload AS (
            SELECT COALESCE(
                json_agg(
                    json_build_object(
                        'label', current_series.label,
                        'bucket_start', current_series.bucket_start::text,
                        'period_day_count', current_series.period_day_count,
                        'detections', current_series.detections,
                        'active_days', current_series.active_days,
                        'active_stations', current_series.active_stations,
                        'community_detections', current_series.community_detections,
                        'community_active_days', current_series.community_active_days,
                        'community_active_stations', current_series.community_active_stations,
                        'avg_detections', historical.avg_detections,
                        'avg_active_days', historical.avg_active_days,
                        'avg_active_stations', historical.avg_active_stations,
                        'avg_community_active_stations', historical.avg_community_active_stations,
                        'avg_activity_index', historical.avg_activity_index
                    )
                    ORDER BY current_series.bucket_start
                ),
                '[]'::json
            ) AS series
            FROM current_series
            LEFT JOIN historical USING (bucket_key)
        )
        SELECT
            summary.year_start,
            summary.total_detections,
            summary.active_days,
            summary.active_stations,
            summary.first_detected,
            summary.last_detected,
            series_payload.series
        FROM summary
        CROSS JOIN series_payload
    `, [speciesId, grain, targetYear]);

    return buildPopulationLoadResult(result.rows[0] ?? {});
}

const cachedLatestNetworkYear = unstable_cache(
    async (): Promise<number | null> => {
        const result = await query<{ year_start: string | null }>(`
            SELECT MAX(period_start)::text AS year_start
            FROM species_tracker_tbl
            WHERE row_kind = 'species'
              AND location_type = 'network'
              AND location_id = 'nl'
              AND period_grain = 'year'
        `);
        return yearFromDate(result.rows[0]?.year_start ?? null);
    },
    ["species-tracker-latest-network-year-v1"],
    { revalidate: 900 },
);

// The population stream's "current year". Driven by the latest year actually
// present in the pre-aggregated species_tracker_tbl (capped at the last complete
// calendar year), not a blind now-minus-one. When the aggregate has not been
// rebuilt for the newest calendar year yet, this keeps the dashboard on the most
// recent fully-aggregated year instead of requesting a year only the raw
// hypertable has — which would otherwise force loadFreshPopulationData's
// multi-minute real-time continuous-aggregate scan on every load. The year is
// derived with the same yearFromDate(period_start) convention the tracker loader
// uses for summary.year, so the two always agree and the step-2 fast path below
// matches.
async function resolvePopulationTargetYear(): Promise<number> {
    const calendarYear = lastCompleteUtcYear();
    const latestAggregatedYear = await cachedLatestNetworkYear();
    return latestAggregatedYear == null
        ? calendarYear
        : Math.min(calendarYear, latestAggregatedYear);
}

async function loadPopulationData(
    speciesId: string,
    grain: SpeciesTrackerGrain,
): Promise<PopulationLoadResult> {
    const targetYear = await resolvePopulationTargetYear();

    // 1. Fast path: pre-computed modeling artifacts for the target year.
    const artifactResult = await loadPopulationDataFromModelingDb(speciesId, grain, targetYear);
    if (artifactResult.summary.year === targetYear && artifactResult.series.length > 0) {
        return artifactResult;
    }

    // 2. Pre-aggregated species_tracker_tbl. If it already covers the target
    //    year, serve it directly — there is no reason to recompute the same year
    //    from the raw hypertable. (Its query prefers the target year and only
    //    falls back to an older year when the target year is genuinely absent.)
    const trackerResult = await loadPopulationDataFromTracker(speciesId, grain, targetYear);
    if (trackerResult.summary.year === targetYear && trackerResult.series.length > 0) {
        return selectPopulationLoadResult(targetYear, artifactResult, trackerResult);
    }

    // 3. Neither pre-aggregated source covers the target year, so recompute the
    //    population stream straight from the raw birdweather hypertable to surface
    //    detections the aggregates have not captured yet. This is the one
    //    genuinely expensive query in the route; it now runs only on a true
    //    pre-aggregation gap rather than eagerly on every artifact miss. The
    //    selection precedence is unchanged: artifact(target) > fresh(target) >
    //    any-with-series, tracker as the final fallback.
    const freshResult = await loadFreshPopulationData(speciesId, grain, targetYear);
    return selectPopulationLoadResult(
        targetYear,
        selectPopulationLoadResult(targetYear, artifactResult, freshResult),
        trackerResult,
    );
}

function buildMortalityResult(
    rows: JsonRecord[],
    scope: "species" | "network",
    source: "sovon",
): SpeciesTrackerMortality {
    const monthlyByYear = new Map<number, Map<number, number>>();
    for (const row of rows) {
        const yearValue = numberOrNull(row.year);
        const monthValue = numberOrNull(row.month);
        if (yearValue === null || monthValue === null) continue;
        const yearKey = Math.trunc(yearValue);
        const monthKey = Math.trunc(monthValue);
        if (monthKey < 1 || monthKey > 12) continue;
        if (!monthlyByYear.has(yearKey)) monthlyByYear.set(yearKey, new Map());
        const monthMap = monthlyByYear.get(yearKey)!;
        monthMap.set(
            monthKey,
            (monthMap.get(monthKey) ?? 0) + integerOrZero(row.mortality_count),
        );
    }
    const aggregated = buildMortalityByYear(monthlyByYear, null);
    return {
        scope,
        source,
        year: aggregated.year,
        series: aggregated.series,
        availableYears: aggregated.availableYears,
        byYear: aggregated.byYear,
    };
}

async function loadMortalitySeries(speciesId: string, commonName: string): Promise<SpeciesTrackerMortality> {
    const sovonId = TARGET_SPECIES_BY_BIRDWEATHER_ID.get(speciesId)?.sovonId ?? null;
    const speciesResult = await query<JsonRecord>(`
        WITH selected_species AS (
            SELECT $1::int AS sovon_species_id, $2::text AS common_name
        )
        SELECT
            sovon.year::int AS year,
            sovon.month::int AS month,
            SUM(sovon.dead_bird_count)::bigint AS mortality_count
        FROM sovon_tbl sovon
        CROSS JOIN selected_species species
        WHERE (
                species.sovon_species_id IS NOT NULL
            AND sovon.sovon_species_id = species.sovon_species_id
            )
           OR (
                species.sovon_species_id IS NULL
            AND lower(trim(sovon.species_english)) = lower(trim(species.common_name))
            )
        GROUP BY sovon.year, sovon.month
        ORDER BY sovon.year, sovon.month
    `, [sovonId, commonName]);

    // Per-selected-species only: if this species has no SOVON dead-bird
    // records, show its empty state rather than falling back to an all-species
    // NL-network total.
    return buildMortalityResult(speciesResult.rows, "species", "sovon");
}

async function loadHotspots(speciesId: string): Promise<SpeciesTrackerHotspot[]> {
    const result = await query<JsonRecord>(`
        WITH latest_year AS (
            SELECT date_trunc('year', MAX(period_start)::timestamp)::date AS year_start
            FROM species_tracker_tbl
            WHERE row_kind = 'species'
              AND location_type = 'network'
              AND location_id = 'nl'
              AND period_grain = 'year'
              AND species_id = $1
        )
        SELECT
            COALESCE(station.station_id, tracker.station_id) AS station_id,
            COALESCE(station.station_name, station.station_location, tracker.station_id) AS name,
            station.lat,
            station.lon,
            tracker.total_detections::bigint AS detections,
            tracker.active_days::int AS active_days,
            tracker.confidence_mean AS mean_confidence,
            tracker.last_detection_at::text AS last_detected
        FROM latest_year
        JOIN species_tracker_tbl tracker
          ON tracker.period_start = latest_year.year_start
         AND tracker.row_kind = 'species'
         AND tracker.location_type = 'station'
         AND tracker.period_grain = 'year'
         AND tracker.species_id = $1
        LEFT JOIN birdweather_stations_tbl station
          ON station.station_id = tracker.station_id
        ORDER BY tracker.total_detections DESC, tracker.active_days DESC, tracker.location_id ASC
        LIMIT 40
    `, [speciesId]);

    return result.rows.map((row) => ({
        stationId: textOrNull(row.station_id) ?? "unknown",
        stationName: textOrNull(row.name) ?? "Unknown station",
        lat: numberOrNull(row.lat),
        lon: numberOrNull(row.lon),
        detections: integerOrZero(row.detections),
        activeDays: integerOrZero(row.active_days),
        avgConfidence: numberOrNull(row.mean_confidence),
        lastDetectedAt: textOrNull(row.last_detected),
    }));
}

async function loadStationContext(speciesId: string): Promise<JsonRecord> {
    const result = await query<JsonRecord>(`
        WITH latest_year AS (
            SELECT date_trunc('year', MAX(period_start)::timestamp)::date AS year_start
            FROM species_tracker_tbl
            WHERE row_kind = 'species'
              AND location_type = 'network'
              AND location_id = 'nl'
              AND period_grain = 'year'
              AND species_id = $1
        )
        SELECT
            AVG(tracker.station_availability_90d_pct)::double precision AS mean_availability_90d_pct,
            AVG(tracker.station_lifetime_availability_pct)::double precision AS mean_lifetime_availability_pct,
            AVG(tracker.station_days_since_last)::double precision AS mean_days_since_last,
            COUNT(*)::int AS occupied_station_count,
            COUNT(*) FILTER (WHERE tracker.station_availability_90d_pct >= 60)::int AS modeling_station_count,
            COUNT(DISTINCT tracker.grid_cell_id)::int AS occupied_grid_cell_count
        FROM latest_year
        JOIN species_tracker_tbl tracker
          ON tracker.period_start = latest_year.year_start
         AND tracker.row_kind = 'species'
         AND tracker.location_type = 'station'
         AND tracker.period_grain = 'year'
         AND tracker.species_id = $1
        LEFT JOIN birdweather_stations_tbl station
          ON station.station_id = tracker.station_id
    `, [speciesId]);
    return result.rows[0] ?? {};
}

async function loadSeasonalPresence(
    speciesId: string,
): Promise<SpeciesTrackerSeasonalPresenceYear[]> {
    const result = await query<JsonRecord>(`
        SELECT
            species_id,
            location_type,
            location_id,
            period_start::text AS period_start,
            temporal_occupancy_pct,
            station_presence_pct,
            relative_abundance_pct
        FROM species_tracker_tbl
        WHERE row_kind = 'species'
          AND location_type = 'network'
          AND location_id = 'nl'
          AND period_grain = 'month'
          AND species_id = $1
        ORDER BY period_start ASC
    `, [speciesId]);

    return aggregateSeasonalPresenceRows(result.rows, speciesId, lastCompleteUtcYear(), 4);
}

// Monthly anomaly summary for this species across the active score layer: for
// each species/location/week, the newest completed scoring run wins. This lets
// species-specific reruns update one species without hiding older scores for
// every other species.
async function loadAnomalySeries(
    speciesId: string,
): Promise<SpeciesTrackerAnomalySeriesPoint[]> {
    try {
        const result = await query<{
            month_start: string;
            amber: number | string;
            red: number | string;
            scored_cells: number | string;
            avg_score: number | string | null;
            avg_bioacoustic_score: number | string | null;
            avg_bioacoustic_anomaly_score: number | string | null;
            avg_bioacoustic_outlier_share: number | string | null;
            avg_population_score: number | string | null;
        }>(
            `
            WITH ${activeScoreRowsCte(["scores.species_id = $1"])}
            SELECT
                date_trunc('month', period_start)::date::text AS month_start,
                COUNT(*) FILTER (WHERE alert_level = 'amber')::int AS amber,
                COUNT(*) FILTER (WHERE alert_level = 'red')::int AS red,
                COUNT(*)::int AS scored_cells,
                AVG(model_anomaly_score)::double precision AS avg_score,
                AVG(embedding_drift_score)::double precision AS avg_bioacoustic_score,
                AVG(embedding_anomaly_score)::double precision AS avg_bioacoustic_anomaly_score,
                AVG(embedding_outlier_share)::double precision AS avg_bioacoustic_outlier_share,
                AVG(population_anomaly_score)::double precision AS avg_population_score
            FROM active_score_rows
            GROUP BY 1
            ORDER BY 1
            `,
            [speciesId],
        );
        return result.rows.map((row) => {
            const avg = row.avg_score == null ? null : Number(row.avg_score);
            const bioacoustic =
                row.avg_bioacoustic_score == null ? null : Number(row.avg_bioacoustic_score);
            const bioacousticAnomaly =
                row.avg_bioacoustic_anomaly_score == null
                    ? null
                    : Number(row.avg_bioacoustic_anomaly_score);
            const bioacousticOutlier =
                row.avg_bioacoustic_outlier_share == null
                    ? null
                    : Number(row.avg_bioacoustic_outlier_share);
            const population =
                row.avg_population_score == null ? null : Number(row.avg_population_score);
            return {
                periodStart: row.month_start,
                amber: integerOrZero(row.amber),
                red: integerOrZero(row.red),
                scoredCells: integerOrZero(row.scored_cells),
                avgScore: avg != null && Number.isFinite(avg) ? avg : null,
                avgBioacousticScore:
                    bioacoustic != null && Number.isFinite(bioacoustic)
                        ? bioacoustic
                        : null,
                avgBioacousticAnomalyScore:
                    bioacousticAnomaly != null && Number.isFinite(bioacousticAnomaly)
                        ? bioacousticAnomaly
                        : null,
                avgBioacousticOutlierShare:
                    bioacousticOutlier != null && Number.isFinite(bioacousticOutlier)
                        ? bioacousticOutlier
                        : null,
                avgPopulationScore:
                    population != null && Number.isFinite(population) ? population : null,
            };
        });
    } catch {
        return [];
    }
}

interface SpeciesDetailBase {
    yearRows: JsonRecord[];
    hotspots: SpeciesTrackerHotspot[];
    sovonMortality: SpeciesTrackerMortality;
    stationContext: JsonRecord;
    seasonalPresence: SpeciesTrackerSeasonalPresenceYear[];
    anomalySeries: SpeciesTrackerAnomalySeriesPoint[];
}

async function loadSpeciesDetailBase(
    species: SpeciesTrackerSpeciesOption,
): Promise<SpeciesDetailBase> {
    const [
        yearRowsResult,
        hotspots,
        sovonMortality,
        stationContext,
        seasonalPresence,
        anomalySeries,
    ] = await Promise.all([
        query<JsonRecord>(`
            SELECT
                period_start::text,
                period_end::text,
                total_detections::bigint,
                active_days::int,
                active_station_count::int,
                COALESCE(species_station_count, 0)::int AS species_station_count,
                detections_per_active_day,
                temporal_occupancy_pct,
                relative_abundance_pct,
                station_presence_pct,
                confidence_mean,
                confidence_p10,
                confidence_p50,
                confidence_p90,
                high_confidence_pct,
                processed_clip_pct,
                first_detection_at::text,
                last_detection_at::text,
                peak_hour_utc,
                peak_hour_share_pct,
                nocturnal_share_pct,
                dawn_share_pct,
                dusk_share_pct,
                background_label_mix,
                knmi_temp_mean_c,
                knmi_precip_mm,
                knmi_humidity_mean_pct,
                knmi_wind_max_ms,
                species_richness,
                extras
            FROM species_tracker_tbl
            WHERE row_kind = 'species'
              AND location_type = 'network'
              AND location_id = 'nl'
              AND period_grain = 'year'
              AND species_id = $1
            ORDER BY period_start ASC
        `, [species.speciesId]),
        loadHotspots(species.speciesId),
        loadMortalitySeries(species.speciesId, species.commonName),
        loadStationContext(species.speciesId),
        loadSeasonalPresence(species.speciesId),
        loadAnomalySeries(species.speciesId),
    ]);

    return {
        yearRows: yearRowsResult.rows,
        hotspots,
        sovonMortality,
        stationContext,
        seasonalPresence,
        anomalySeries,
    };
}

function assembleSpeciesDetail(
    species: SpeciesTrackerSpeciesOption,
    base: SpeciesDetailBase,
    populationData: PopulationLoadResult,
): SpeciesTrackerDetail {
    const {
        yearRows,
        hotspots,
        sovonMortality,
        stationContext,
        seasonalPresence,
        anomalySeries,
    } = base;
    const series = populationData.series;
    const currentYearSummary = populationData.summary;

    const currentYearRow = yearRows[yearRows.length - 1] ?? {};
    const currentYear = currentYearSummary.year ?? yearFromDate(currentYearRow.period_start) ?? new Date().getUTCFullYear();
    const historicalRows = yearRows.filter((row) => (yearFromDate(row.period_start) ?? currentYear) < currentYear);
    const currentYearDetections = currentYearSummary.currentYearDetections || integerOrZero(currentYearRow.total_detections);
    const currentYearActiveDays = currentYearSummary.currentYearActiveDays || integerOrZero(currentYearRow.active_days);
    const currentYearActiveStations = currentYearSummary.currentYearActiveStations || integerOrZero(currentYearRow.species_station_count);
    const firstDetection = currentYearSummary.firstDetectedThisYear ?? textOrNull(currentYearRow.first_detection_at);
    const lastDetection = currentYearSummary.lastDetectedAt ?? textOrNull(currentYearRow.last_detection_at);
    const detectionsPerActiveDay = currentYearActiveDays > 0
        ? Math.round((currentYearDetections / currentYearActiveDays) * 10) / 10
        : numberOrNull(currentYearRow.detections_per_active_day);
    const detectionTrendPct = (() => {
        const historicalAverage = roundedAverage(historicalRows.map((row) => numberOrNull(row.total_detections)));
        if (!historicalAverage) return null;
        return Math.round(((currentYearDetections - historicalAverage) / historicalAverage) * 1000) / 10;
    })();
    const passageDays = firstDetection && lastDetection
        ? Math.max(
            1,
            Math.ceil((new Date(lastDetection).getTime() - new Date(firstDetection).getTime()) / 86_400_000) + 1,
        )
        : null;

    const historicalAverage = {
        detections: roundedAverage(historicalRows.map((row) => numberOrNull(row.total_detections))),
        activeDays: roundedAverage(historicalRows.map((row) => numberOrNull(row.active_days))),
        activeStations: roundedAverage(historicalRows.map((row) => numberOrNull(row.species_station_count))),
    };

    return {
        species,
        summary: {
            currentYearDetections,
            historicalAverageDetections: historicalAverage.detections,
            currentYearActiveDays,
            historicalAverageActiveDays: historicalAverage.activeDays,
            currentYearActiveStations,
            historicalAverageActiveStations: historicalAverage.activeStations,
            detectionsPerActiveDay,
            temporalOccupancyPct: numberOrNull(currentYearRow.temporal_occupancy_pct),
            stationPresencePct: numberOrNull(currentYearRow.station_presence_pct),
            relativeAbundancePct: numberOrNull(currentYearRow.relative_abundance_pct),
            yoyVsHistoricalPct: detectionTrendPct,
            lastDetectedAt: lastDetection,
            firstDetectedThisYear: firstDetection,
            passageDays,
        },
        population: {
            series,
            hotspots,
        },
        mortality: sovonMortality,
        mortalitySources: {
            sovon: sovonMortality,
        },
        activity: {
            confidenceMean: numberOrNull(currentYearRow.confidence_mean),
            confidenceP10: numberOrNull(currentYearRow.confidence_p10),
            confidenceP50: numberOrNull(currentYearRow.confidence_p50),
            confidenceP90: numberOrNull(currentYearRow.confidence_p90),
            highConfidencePct: numberOrNull(currentYearRow.high_confidence_pct),
            hourly: hourlySeries(currentYearRow.extras),
            backgroundLabelMix: normalizeBackgroundMix(currentYearRow.background_label_mix),
            seasonalPresence,
        },
        context: {
            meanTemperatureC: numberOrNull(currentYearRow.knmi_temp_mean_c),
            meanHumidityPct: numberOrNull(currentYearRow.knmi_humidity_mean_pct),
            meanWindMs: numberOrNull(currentYearRow.knmi_wind_max_ms),
            totalRainMm: numberOrNull(currentYearRow.knmi_precip_mm),
            avgStationAvailability90dPct: numberOrNull(stationContext.mean_availability_90d_pct),
            avgStationLifetimeAvailabilityPct: numberOrNull(stationContext.mean_lifetime_availability_pct),
            avgDaysSinceLast: numberOrNull(stationContext.mean_days_since_last),
            meanCooccurringRichness: numberOrNull(currentYearRow.species_richness),
            occupiedStations: integerOrZero(stationContext.occupied_station_count),
            modelingStations: integerOrZero(stationContext.modeling_station_count),
            occupiedGridCells: integerOrZero(stationContext.occupied_grid_cell_count),
            anomalySeries,
        },
    };
}

const cachedSpeciesList = unstable_cache(loadSpeciesList, ["species-tracker-network-list-v1"], {
    revalidate: 900,
});

const cachedSpeciesBase = unstable_cache(
    async (speciesId: string, modelingVersion: string) => {
        void modelingVersion;
        const speciesList = await cachedSpeciesList();
        const selectedSpecies = speciesList.find((candidate) => candidate.speciesId === speciesId) ?? speciesList[0];
        if (!selectedSpecies) return null;
        return { species: selectedSpecies, base: await loadSpeciesDetailBase(selectedSpecies) };
    },
    ["species-tracker-network-detail-base-v4"],
    { revalidate: 900 },
);

// Only the population stream depends on grain, so it is cached separately from
// the grain-independent base. This stops the client's grain warm-up (day/week/
// month) from recomputing the shared base queries three times per species.
const cachedPopulation = unstable_cache(
    async (speciesId: string, grain: SpeciesTrackerGrain) => loadPopulationData(speciesId, grain),
    ["species-tracker-network-population-v1"],
    { revalidate: 900 },
);

async function loadSpeciesDetailCached(
    requestedSpeciesId: string,
    grain: SpeciesTrackerGrain,
    modelingVersion: string,
): Promise<SpeciesTrackerDetail | null> {
    const speciesList = await cachedSpeciesList();
    const resolvedSpecies =
        speciesList.find((candidate) => candidate.speciesId === requestedSpeciesId) ?? speciesList[0];
    if (!resolvedSpecies) return null;
    const [baseBundle, populationData] = await Promise.all([
        cachedSpeciesBase(resolvedSpecies.speciesId, modelingVersion),
        cachedPopulation(resolvedSpecies.speciesId, grain),
    ]);
    if (!baseBundle) return null;
    return assembleSpeciesDetail(baseBundle.species, baseBundle.base, populationData);
}

async function loadSpeciesTrackerModelingVersion(speciesId: string): Promise<string> {
    const result = await query<{
        max_score_row_id: string | null;
        max_generated_at: string | null;
    }>(
        `
        SELECT
            MAX(scores.anomaly_row_id)::text AS max_score_row_id,
            MAX(scores.computed_at)::text AS max_generated_at
        FROM species_week_anomaly_tbl scores
        WHERE scores.species_id = $1
        `,
        [speciesId],
    );
    const row = result.rows[0];
    return `${row?.max_score_row_id ?? "0"}:${row?.max_generated_at ?? "none"}`;
}

function parseGrain(value: string | null): SpeciesTrackerGrain {
    return value === "day" || value === "week" ? value : "month";
}

// Disk-backed stale-while-revalidate over the assembled response, keyed by
// species + grain. The first request for a key blocks once on the cold query;
// every later request — including after a dev recompile or a prod restart — is
// served instantly from the on-disk snapshot and revalidated in the background.
// 15-min freshness mirrors the unstable_cache revalidate used by the loaders.
const trackerSnapshotCache = createSnapshotCache<SpeciesTrackerResponse>({
    name: "species-tracker-v5",
    freshMs: 15 * 60_000,
    isEmpty: (response) => response.empty || response.speciesList.length === 0,
});

const handleGet = async (request: NextRequest) => {
    try {
        const speciesParam = request.nextUrl.searchParams.get("species_id") ?? "";
        const grain = parseGrain(
            request.nextUrl.searchParams.get("grain") ??
            request.nextUrl.searchParams.get("period_grain"),
        );
        const speciesList = await cachedSpeciesList();
        const requestedSpecies = speciesParam || speciesList[0]?.speciesId || "";
        const modelingVersion = requestedSpecies
            ? await loadSpeciesTrackerModelingVersion(requestedSpecies)
            : "no-species";
        const cacheKey = `${requestedSpecies || "default"}::${grain}::${modelingVersion}::v5`;
        const response = await trackerSnapshotCache.get(
            cacheKey,
            async (): Promise<SpeciesTrackerResponse> => {
                const detail = requestedSpecies
                    ? await loadSpeciesDetailCached(requestedSpecies, grain, modelingVersion)
                    : null;
                return {
                    asOf: detail?.summary.lastDetectedAt ?? null,
                    grain,
                    speciesId: detail?.species.speciesId ?? null,
                    speciesList,
                    detail,
                    empty: speciesList.length === 0,
                };
            },
        );

        return NextResponse.json(response, {
            headers: { "Cache-Control": "public, s-maxage=900, stale-while-revalidate=900" },
        });
    } catch (error) {
        console.error("Failed to load species tracker", error);
        return NextResponse.json({ error: "Failed to load species tracker" }, { status: 500 });
    }
};

export const GET = withMeasuredApiRoute(handleGet);
