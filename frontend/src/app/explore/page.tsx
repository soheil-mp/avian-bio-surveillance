import { measurePageRender } from "@/lib/performance/server";
import { Space_Grotesk } from "next/font/google";
import { Suspense } from "react";
import ExploreClientBootstrap from "./ExploreClientBootstrap";
import ExploreTabNav from "./ExploreTabNav";
import ExploreWeatherTab from "./ExploreWeatherTab";
import FilterPanel from "./FilterPanel";
import WeatherFilterPanel from "./WeatherFilterPanel";
import { loadExploreBootstrapPayload, loadExploreFilterOptions } from "./data";

export const dynamic = "force-dynamic";

const exploreTitleFont = Space_Grotesk({
  subsets: ["latin"],
  weight: ["500", "700"],
});

export default async function ExplorePage({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  return measurePageRender(async () => {
    const params = await searchParams;
    const monitorPanelHeight = 780;
    const requestedTab = Array.isArray(params.tab) ? params.tab[0] : params.tab;
    const activeTab = requestedTab === "weather" ? "weather" : "inventory";
    const initialQuery = new URLSearchParams();

    let speciesOpts: string[] = [];
    let stationOpts: string[] = [];
    let locationOpts: string[] = [];
    let initialPayload = null;

    if (activeTab === "inventory") {
      const options = await loadExploreFilterOptions();
      initialPayload = await loadExploreBootstrapPayload(params);
      speciesOpts = options.species;
      stationOpts = options.stations;
      locationOpts = options.locations;
    }

    for (const [key, value] of Object.entries(params)) {
      if (Array.isArray(value)) {
        for (const item of value) {
          initialQuery.append(key, item);
        }
        continue;
      }

      if (typeof value === "string") {
        initialQuery.set(key, value);
      }
    }

    return (
      <div className="h-full w-full overflow-x-hidden overflow-y-auto">
        <style dangerouslySetInnerHTML={{ __html: `
          .explore-tabs-grid,
          .explore-panel-grid {
            grid-template-columns: minmax(0, 1fr);
          }
          .explore-tabs-spacer {
            display: none;
          }
          .explore-tabs-content {
            grid-column: 1 / -1;
          }
          .explore-tabs-nav {
            display: grid;
            width: 100%;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 8px;
          }
          @media (min-width: 1100px) {
            .explore-tabs-grid,
            .explore-panel-grid {
              grid-template-columns: 264px minmax(0, 1fr) 264px;
            }
            .explore-tabs-spacer {
              display: block;
            }
            .explore-tabs-content {
              grid-column: 2 / 4;
            }
            .explore-tabs-nav {
              display: inline-flex;
              width: auto;
              gap: 10px;
            }
            .explore-tab-button {
              width: auto;
              min-width: 156px;
            }
            .explore-primary-panel {
              grid-column: 2 / 4;
            }
          }
        ` }} />

        <div className="relative mx-auto max-w-[1600px] px-6 pb-8 pt-10 sm:px-8 lg:px-10 xl:px-12">
          <header className="mb-8 px-1">
            <h1
              className={`${exploreTitleFont.className} text-[31px] font-semibold leading-none tracking-[-0.045em]`}
              style={{ color: "rgba(255,255,255,0.94)" }}
            >
              Explore
            </h1>
          </header>

          <section className="space-y-0 px-1">
            <div className="explore-tabs-grid grid gap-x-5 gap-y-3">
              <div className="explore-tabs-spacer" />
              <div className="explore-tabs-content relative z-10 -mb-[2px]">
                <ExploreTabNav activeTab={activeTab} />
              </div>
            </div>

            <div className="explore-panel-grid grid items-stretch gap-5">
              {activeTab === "inventory" ? (
                <>
                  <div className="min-h-0" style={{ height: monitorPanelHeight }}>
                    <Suspense fallback={null}>
                      <FilterPanel
                        speciesOpts={speciesOpts}
                        stationOpts={stationOpts}
                        locationOpts={locationOpts}
                      />
                    </Suspense>
                  </div>
                  <div className="explore-primary-panel min-h-0" style={{ height: monitorPanelHeight }}>
                    <Suspense
                      fallback={
                        <div
                          className="flex h-full items-center justify-center rounded-2xl border"
                          style={{
                            background: 'rgba(20,20,20,0.96)',
                            borderColor: 'rgba(255,255,255,0.08)',
                          }}
                        >
                          <span
                            className="font-mono text-[11px] tracking-widest uppercase"
                            style={{ color: "rgba(255,255,255,0.2)" }}
                          >
                            Loading detections
                          </span>
                        </div>
                      }
                    >
                      <ExploreClientBootstrap
                        initialQueryString={initialQuery.toString()}
                        initialPayload={initialPayload}
                      />
                    </Suspense>
                  </div>
                </>
              ) : (
                <>
                  <div className="min-h-0" style={{ height: monitorPanelHeight }}>
                    <WeatherFilterPanel />
                  </div>
                  <div
                    className="explore-primary-panel min-h-0 overflow-hidden rounded-2xl"
                    style={{
                      height: monitorPanelHeight,
                      background: 'rgba(20,20,20,0.96)',
                      border: '1px solid rgba(255,255,255,0.08)',
                      borderTopColor: 'transparent',
                    }}
                  >
                    <ExploreWeatherTab />
                  </div>
                </>
              )}
            </div>
          </section>
        </div>
      </div>
    );
  }, { routePath: "/explore" });
}
