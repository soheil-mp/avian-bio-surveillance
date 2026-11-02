import { HomeContent } from "@/components/home/HomeContent";
import { measurePageRender } from "@/lib/performance/server";

// DB is not available at build time, so opt out of static prerendering.
export const dynamic = "force-dynamic";

export default async function HomePage() {
  // The live-detection feed is fetched client-side from
  // /api/home/live-detections so a cold TimescaleDB query plan can never stall
  // the initial page paint. The map and species tracker already load their data
  // client-side, so the shell renders immediately and each panel streams in.
  return measurePageRender(async () => <HomeContent />, { routePath: "/" });
}
