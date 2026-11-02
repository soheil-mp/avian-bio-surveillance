import { NextResponse } from 'next/server';
import { query } from '@/lib/db';
import { withMeasuredApiRoute } from '@/lib/performance/server';

/**
 * GET /api/phylopic?name=Turdus+merula
 *
 * Resolves a scientific name into a stable-ish PhyloPic silhouette by:
 * 1) discovering the current API build,
 * 2) searching matching taxonomic nodes,
 * 3) following the node's primary image, and
 * 4) falling back to image-name search when needed.
 *
 * Response: { svgUrl, thumbUrl, attribution, uuid }
 */

type PhyloPicResult = {
    svgUrl: string | null;
    thumbUrl: string | null;
    attribution: string;
    uuid: string;
};

type PhyloPicLink = {
    href?: string;
    title?: string;
};

type PhyloPicNodeNamePart = {
    text?: string;
};

type PhyloPicNode = {
    uuid?: string;
    names?: PhyloPicNodeNamePart[][];
    _links?: {
        self?: PhyloPicLink;
        primaryImage?: PhyloPicLink;
        images?: PhyloPicLink;
    };
};

type PhyloPicImage = {
    uuid?: string;
    attribution?: string;
    _links?: {
        self?: PhyloPicLink;
        vectorFile?: PhyloPicLink;
        sourceFile?: PhyloPicLink;
        thumbnailFiles?: PhyloPicLink[];
        specificNode?: PhyloPicLink;
        nodes?: PhyloPicLink[];
    };
};

const API_BASE = 'https://api.phylopic.org';
const JSON_HEADERS = {
    Accept: 'application/vnd.phylopic.v2+json, application/json',
};
const CACHE_HEADERS = {
    'Cache-Control': 'public, max-age=86400, s-maxage=86400, stale-while-revalidate=43200',
};
const EMPTY_RESULT: PhyloPicResult = {
    svgUrl: null,
    thumbUrl: null,
    attribution: '',
    uuid: '',
};
const BUILD_CACHE_TTL_MS = 6 * 60 * 60 * 1000;
const FAILURE_CACHE_TTL_MS = 60 * 60 * 1000;

/* In-memory caches survive for the server process lifetime. */
const cache = new Map<string, PhyloPicResult>();
const inFlightRequests = new Map<string, Promise<PhyloPicResult>>();
const failedRequests = new Map<string, number>();
let buildCache: { value: number | null; fetchedAt: number } = {
    value: null,
    fetchedAt: 0,
};

function normalizeName(value: string): string {
    return value
        .normalize('NFD')
        .replace(/[\u0300-\u036f]/g, '')
        .replace(/[^a-z0-9\s-]/gi, ' ')
        .replace(/\s+/g, ' ')
        .trim()
        .toLowerCase();
}

function getTitleCandidates(entity: PhyloPicNode | PhyloPicImage): string[] {
    const candidates = new Set<string>();
    const imageLinks = entity._links as PhyloPicImage['_links'] | undefined;

    const addCandidate = (value: string | undefined | null) => {
        if (typeof value === 'string' && value.trim()) {
            candidates.add(value.trim());
        }
    };

    addCandidate(entity._links?.self?.title);

    if ('names' in entity && Array.isArray(entity.names)) {
        for (const group of entity.names) {
            for (const part of group) {
                addCandidate(part?.text);
            }
        }
    }

    if (imageLinks) {
        addCandidate(imageLinks.specificNode?.title);
        for (const link of imageLinks.nodes ?? []) {
            addCandidate(link?.title);
        }
    }

    return Array.from(candidates);
}

function scoreCandidateMatch(target: string, candidates: string[]): number {
    const normalizedTarget = normalizeName(target);
    const targetGenus = normalizedTarget.split(' ')[0];
    let bestScore = -1;

    for (const candidate of candidates) {
        const normalizedCandidate = normalizeName(candidate);
        if (!normalizedCandidate) {
            continue;
        }
        if (normalizedCandidate === normalizedTarget) {
            return 1000;
        }
        if (normalizedCandidate.startsWith(normalizedTarget)) {
            bestScore = Math.max(bestScore, 700);
            continue;
        }
        if (normalizedCandidate.includes(normalizedTarget)) {
            bestScore = Math.max(bestScore, 500);
            continue;
        }
        if (normalizedCandidate === targetGenus) {
            bestScore = Math.max(bestScore, 250);
        } else if (normalizedCandidate.startsWith(targetGenus)) {
            bestScore = Math.max(bestScore, 150);
        }
    }

    return bestScore;
}

function pickBestMatch<T extends PhyloPicNode | PhyloPicImage>(items: T[], target: string): T | null {
    if (items.length === 0) {
        return null;
    }

    return items
        .map((item) => ({
            item,
            score: scoreCandidateMatch(target, getTitleCandidates(item)),
        }))
        .sort((a, b) => b.score - a.score)[0]?.item ?? items[0];
}

function extractEmbeddedItems<T>(payload: unknown): T[] {
    if (
        typeof payload === 'object'
        && payload !== null
        && '_embedded' in payload
        && typeof payload._embedded === 'object'
        && payload._embedded !== null
        && 'items' in payload._embedded
        && Array.isArray(payload._embedded.items)
    ) {
        return payload._embedded.items as T[];
    }
    return [];
}

function imageToResult(image: PhyloPicImage | null | undefined): PhyloPicResult {
    if (!image) {
        return { ...EMPTY_RESULT };
    }

    return {
        svgUrl: image._links?.vectorFile?.href || image._links?.sourceFile?.href || null,
        thumbUrl: image._links?.thumbnailFiles?.[0]?.href || null,
        attribution: image.attribution || '',
        uuid: image.uuid || '',
    };
}

function isTimeoutError(error: unknown): boolean {
    return error instanceof DOMException
        ? error.name === 'TimeoutError'
        : typeof error === 'object'
            && error !== null
            && 'name' in error
            && error.name === 'TimeoutError';
}

function hasFreshFailure(name: string): boolean {
    const expiresAt = failedRequests.get(name);
    if (!expiresAt) {
        return false;
    }

    if (expiresAt <= Date.now()) {
        failedRequests.delete(name);
        return false;
    }

    return true;
}

async function persistCachedResult(name: string, result: PhyloPicResult): Promise<void> {
    cache.set(name, result);

    try {
        await query(
            `WITH target_species AS (
                 SELECT species_id
                 FROM   species_tbl
                 WHERE  lower(scientific_name) = $1
                     OR lower(common_name) = $1
                 ORDER  BY CASE
                            WHEN lower(scientific_name) = $1 THEN 0
                            ELSE 1
                          END
                 LIMIT  1
             )
             UPDATE species_tbl AS s
             SET    phylopic_svg_url     = $2,
                    phylopic_thumb_url   = $3,
                    phylopic_attribution = COALESCE($4, ''),
                    phylopic_uuid        = COALESCE($5, ''),
                    phylopic_fetched_at  = NOW()
             FROM   target_species t
             WHERE  s.species_id = t.species_id`,
            [
                name,
                result.svgUrl,
                result.thumbUrl,
                result.attribution || null,
                result.uuid || null,
            ],
        );
    } catch (err) {
        console.warn('PhyloPic DB cache write failed:', err);
    }
}

async function getCurrentBuild(forceRefresh = false): Promise<number> {
    const isFresh = buildCache.value !== null && (Date.now() - buildCache.fetchedAt) < BUILD_CACHE_TTL_MS;
    if (!forceRefresh && isFresh) {
        return buildCache.value!;
    }

    const res = await fetch(`${API_BASE}/`, {
        headers: JSON_HEADERS,
        signal: AbortSignal.timeout(8000),
    });

    if (!res.ok) {
        if (buildCache.value !== null) {
            return buildCache.value;
        }
        throw new Error(`Failed to load PhyloPic build: ${res.status}`);
    }

    const json = await res.json() as { build?: number };
    if (typeof json.build !== 'number') {
        if (buildCache.value !== null) {
            return buildCache.value;
        }
        throw new Error('PhyloPic build number missing from API root response.');
    }

    buildCache = {
        value: json.build,
        fetchedAt: Date.now(),
    };

    return json.build;
}

async function fetchPhyloPicJson(pathOrUrl: string, forceRefreshBuild = false): Promise<unknown | null> {
    const build = await getCurrentBuild(forceRefreshBuild);
    const url = new URL(pathOrUrl, API_BASE);
    url.searchParams.set('build', String(build));

    const res = await fetch(url.toString(), {
        headers: JSON_HEADERS,
        signal: AbortSignal.timeout(8000),
    });

    if ((res.status === 400 || res.status === 410) && !forceRefreshBuild) {
        return fetchPhyloPicJson(pathOrUrl, true);
    }

    if (!res.ok) {
        return null;
    }

    return res.json();
}

async function searchNodes(name: string): Promise<PhyloPicNode[]> {
    const payload = await fetchPhyloPicJson(`/nodes?filter_name=${encodeURIComponent(name)}&page=0&embed_items=true`);
    return extractEmbeddedItems<PhyloPicNode>(payload);
}

async function searchImages(name: string): Promise<PhyloPicImage[]> {
    const payload = await fetchPhyloPicJson(`/images?filter_name=${encodeURIComponent(name)}&page=0&embed_items=true`);
    return extractEmbeddedItems<PhyloPicImage>(payload);
}

async function fetchNodePrimaryImage(node: PhyloPicNode): Promise<PhyloPicImage | null> {
    const primaryImageHref = node._links?.primaryImage?.href;
    if (primaryImageHref) {
        const payload = await fetchPhyloPicJson(primaryImageHref);
        if (payload && typeof payload === 'object') {
            return payload as PhyloPicImage;
        }
    }

    const nodeImagesHref = node._links?.images?.href;
    if (!nodeImagesHref) {
        return null;
    }

    const listUrl = new URL(nodeImagesHref, API_BASE);
    listUrl.searchParams.set('page', '0');
    listUrl.searchParams.set('embed_items', 'true');

    const payload = await fetchPhyloPicJson(listUrl.toString());
    const items = extractEmbeddedItems<PhyloPicImage>(payload);
    return items[0] ?? null;
}

async function resolveImageForName(name: string): Promise<PhyloPicResult> {
    const attempts = Array.from(new Set([
        name,
        name.split(' ')[0],
    ].filter(Boolean)));

    for (const attempt of attempts) {
        const nodes = await searchNodes(attempt);
        const bestNode = pickBestMatch(nodes, name);
        if (bestNode) {
            const image = await fetchNodePrimaryImage(bestNode);
            const result = imageToResult(image);
            if (result.svgUrl || result.thumbUrl) {
                return result;
            }
        }

        const images = await searchImages(attempt);
        const bestImage = pickBestMatch(images, name);
        const fallbackResult = imageToResult(bestImage);
        if (fallbackResult.svgUrl || fallbackResult.thumbUrl) {
            return fallbackResult;
        }
    }

    return { ...EMPTY_RESULT };
}

const handleGet = async (request: Request) => {
    const { searchParams } = new URL(request.url);
    const name = searchParams.get('name')?.trim().toLowerCase();

    if (!name) {
        return NextResponse.json({ ...EMPTY_RESULT }, { headers: CACHE_HEADERS });
    }

    // L1 cache: in-process Map (per Next.js worker).
    const cached = cache.get(name);
    if (cached) {
        return NextResponse.json(cached, { headers: CACHE_HEADERS });
    }

        // L2 cache: species_tbl PhyloPic columns. Survives worker restarts and is
        // shared across all Next.js processes; populated lazily on miss.
    try {
        const dbHit = await query<{
            svg_url: string | null;
            thumb_url: string | null;
            attribution: string | null;
            uuid: string | null;
        }>(
                        `SELECT phylopic_svg_url AS svg_url,
                                        phylopic_thumb_url AS thumb_url,
                                        phylopic_attribution AS attribution,
                                        phylopic_uuid AS uuid
                         FROM   species_tbl
                         WHERE  (lower(scientific_name) = $1 OR lower(common_name) = $1)
                             AND  phylopic_fetched_at IS NOT NULL
                         ORDER  BY CASE
                                                WHEN lower(scientific_name) = $1 THEN 0
                                                ELSE 1
                                            END
                         LIMIT  1`,
            [name],
        );
        if (dbHit.rows.length > 0) {
            const row = dbHit.rows[0];
            const result: PhyloPicResult = {
                svgUrl: row.svg_url,
                thumbUrl: row.thumb_url,
                attribution: row.attribution ?? '',
                uuid: row.uuid ?? '',
            };
            cache.set(name, result);
            return NextResponse.json(result, { headers: CACHE_HEADERS });
        }
    } catch (err) {
        // DB unavailable: fall through to upstream lookup.
        console.warn('PhyloPic DB cache lookup failed:', err);
    }

    if (hasFreshFailure(name)) {
        return NextResponse.json({ ...EMPTY_RESULT }, { headers: CACHE_HEADERS });
    }

    // L3: upstream PhyloPic API.
    let pendingRequest = inFlightRequests.get(name);
    if (!pendingRequest) {
        pendingRequest = resolveImageForName(name);
        inFlightRequests.set(name, pendingRequest);
    }

    try {
        const result = await pendingRequest;
        failedRequests.delete(name);
        await persistCachedResult(name, result);
        return NextResponse.json(result, { headers: CACHE_HEADERS });
    } catch (err) {
        failedRequests.set(name, Date.now() + FAILURE_CACHE_TTL_MS);

        if (isTimeoutError(err)) {
            console.warn(`PhyloPic API timed out for \"${name}\". Serving cached empty fallback.`);
        } else {
            console.error('PhyloPic API error:', err);
        }

        return NextResponse.json({ ...EMPTY_RESULT }, { headers: CACHE_HEADERS });
    } finally {
        if (inFlightRequests.get(name) === pendingRequest) {
            inFlightRequests.delete(name);
        }
    }
};

export const GET = withMeasuredApiRoute(handleGet);
