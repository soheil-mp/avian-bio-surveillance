'use client';

import { usePathname, useRouter, useSearchParams } from 'next/navigation';

const TAB_SURFACE = {
  panel: '#141414',
  border: '#232323',
} as const;

const TABS = [
  { value: 'inventory', label: 'Bioacoustic' },
  { value: 'weather', label: 'Weather' },
] as const;

type ExploreTab = (typeof TABS)[number]['value'];

function buildTabHref(
  pathname: string,
  searchParams: { toString(): string },
  tab: ExploreTab,
) {
  const nextParams = new URLSearchParams(searchParams.toString());

  if (tab === 'inventory') {
    nextParams.delete('tab');
  } else {
    nextParams.set('tab', tab);
  }

  const nextQuery = nextParams.toString();
  return nextQuery ? `${pathname}?${nextQuery}` : pathname;
}

export default function ExploreTabNav({ activeTab }: { activeTab: ExploreTab }) {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();

  return (
    <div
      className="explore-tabs-nav relative max-w-full rounded-[24px] p-1 pb-0"
      style={{ background: 'rgba(255,255,255,0.012)' }}
    >
      {TABS.map((tab) => {
        const active = activeTab === tab.value;
        const href = buildTabHref(pathname, searchParams, tab.value);

        return (
          <button
            key={tab.value}
            type="button"
            onClick={() => router.replace(href)}
            className={active
              ? 'explore-tab-button relative z-20 min-w-0 rounded-t-2xl rounded-b-none px-4 pb-3 pt-2.5 text-left transition-all duration-150'
              : 'explore-tab-button relative min-w-0 rounded-2xl px-3.5 py-2 text-left transition-all duration-150'}
            style={{
              marginBottom: active ? -1 : 0,
              transform: active ? 'translateY(1px)' : 'translateY(0)',
              background: active ? TAB_SURFACE.panel : 'rgba(14,14,14,0.78)',
              border: `1px solid ${TAB_SURFACE.border}`,
              borderBottomColor: active ? TAB_SURFACE.panel : TAB_SURFACE.border,
              boxShadow: active
                ? '0 12px 24px rgba(0,0,0,0.18), inset 0 1px 0 rgba(255,255,255,0.02)'
                : 'inset 0 1px 0 rgba(255,255,255,0.01)',
              color: active ? 'rgba(255,255,255,0.92)' : 'rgba(255,255,255,0.56)',
            }}
          >
            <div className="font-mono text-[10px] uppercase tracking-[0.24em]">{tab.label}</div>
          </button>
        );
      })}
    </div>
  );
}