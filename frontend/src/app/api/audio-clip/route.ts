import { NextRequest, NextResponse } from 'next/server';
import { presignedGetObjectUrl } from '@/lib/minio';
import { withMeasuredApiRoute } from '@/lib/performance/server';

type MinioError = Error & { code?: string };

/**
 * GET /api/audio-clip?bucket=raw-audio&path=2025-01-05/abc_42.flac&start=1.2&end=4.5
 *
 * Redirects to a short-lived browser-facing MinIO URL so the browser fetches
 * audio and preview assets directly instead of proxying the object through
 * the Next.js app.
 *
 * Typical processed artifact paths:
 * - ?bucket=processed-audio&path=birdweather/<date>/<detection_id>/clip.flac
 * - ?bucket=processed-audio&path=birdweather/<date>/<detection_id>/waveform.png
 */
const handleGet = async (req: NextRequest) => {
    const { searchParams } = req.nextUrl;
    const bucket = searchParams.get('bucket') || 'raw-audio';
    const objectPath = searchParams.get('path');
    const proxyBytes = searchParams.get('raw') === '1';

    if (!objectPath) {
        return NextResponse.json({ error: 'Missing path param' }, { status: 400 });
    }

    try {
        const signedUrl = await presignedGetObjectUrl(bucket, objectPath, 10 * 60);
        // Same-origin byte proxy (raw=1): stream the object through Next so the
        // browser can READ the bytes for Web Audio decoding (the live, on-the-fly
        // spectrogram). The normal path 307-redirects to MinIO, which serves no
        // CORS headers and lives on a different origin, so a direct cross-origin
        // fetch of the redirect target is blocked from reading the response body.
        if (proxyBytes) {
            const upstream = await fetch(signedUrl);
            if (!upstream.ok || !upstream.body) {
                return NextResponse.json(
                    { error: 'Not found' },
                    { status: upstream.status === 404 ? 404 : 502 },
                );
            }
            return new NextResponse(upstream.body, {
                status: 200,
                headers: {
                    'Content-Type': upstream.headers.get('content-type') ?? 'application/octet-stream',
                    'Cache-Control': 'public, max-age=300, stale-while-revalidate=300',
                },
            });
        }
        return NextResponse.redirect(signedUrl, {
            headers: {
                'Cache-Control': 'public, max-age=300, stale-while-revalidate=300',
            },
        });
    } catch (error: unknown) {
        const minioError = error as MinioError;

        if (minioError.code === 'NoSuchKey' || minioError.code === 'NoSuchBucket') {
            return NextResponse.json({ error: 'Not found' }, { status: 404 });
        }
        if (minioError.code === 'ECONNREFUSED' || minioError.code === 'ENOTFOUND' || minioError.code === 'ETIMEDOUT') {
            console.error('audio-clip error', minioError);
            return NextResponse.json({ error: 'Storage unavailable' }, { status: 503 });
        }
        console.error('audio-clip error', minioError);
        return NextResponse.json({ error: 'Internal error' }, { status: 500 });
    }
};

export const GET = withMeasuredApiRoute(handleGet);
