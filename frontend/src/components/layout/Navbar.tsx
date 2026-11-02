"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Map,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";

type NavItem = {
  href: string;
  label: string;
  icon: LucideIcon;
  match: (pathname: string) => boolean;
};

const NAV_ITEMS: readonly NavItem[] = [
  { href: "/", label: "Live Map", icon: Map, match: (pathname) => pathname === "/" },
  {
    href: "/alerts",
    label: "Alerts",
    icon: TriangleAlert,
    match: (pathname) => pathname.startsWith("/alerts"),
  },
];

function NavButton({
  href,
  label,
  icon: Icon,
  isActive,
  compact = false,
}: {
  href: string;
  label: string;
  icon: LucideIcon;
  isActive: boolean;
  compact?: boolean;
}) {
  return (
    <Link
      href={href}
      prefetch={false}
      aria-label={label}
      aria-current={isActive ? "page" : undefined}
      title={label}
      className={[
        "group relative flex items-center justify-center rounded-[18px] border transition-all duration-200 ease-out",
        compact ? "h-10 min-w-10 px-2.5" : "h-11 w-11",
        isActive
          ? "border-white/7 bg-[linear-gradient(180deg,rgba(76,70,65,0.92),rgba(53,48,45,0.94))] text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.09),0_10px_24px_rgba(0,0,0,0.22)]"
          : "border-transparent bg-transparent text-white/45 hover:border-white/6 hover:bg-white/[0.035] hover:text-white/78",
      ].join(" ")}
    >
      {isActive && (
        <span className="pointer-events-none absolute inset-0 rounded-[18px] bg-[linear-gradient(180deg,rgba(255,255,255,0.08),rgba(255,255,255,0.015))]" />
      )}
      <Icon
        size={compact ? 18 : 19}
        strokeWidth={isActive ? 2 : 1.8}
        className="relative z-10 transition-transform duration-200 group-hover:scale-[1.04]"
      />
      <span className="sr-only">{label}</span>
    </Link>
  );
}

export function Navbar() {
  const pathname = usePathname();

  return (
    <>
      <aside className="hidden h-full shrink-0 select-none md:flex md:w-[88px] md:flex-col md:items-center md:pt-[4.75rem] md:pb-2">
        <Link
          href="/"
          prefetch={false}
          aria-label="Avian Biosurveillance home"
          className="flex h-14 w-14 items-center justify-center rounded-[20px] border border-white/7 bg-[linear-gradient(180deg,rgba(25,24,23,0.98),rgba(17,17,17,0.98))] shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_14px_30px_rgba(0,0,0,0.2)] transition-colors duration-200 hover:brightness-[1.04]"
        >
          <Image
            src="/icons/logo.png"
            alt="Avian Biosurveillance"
            width={36}
            height={36}
            priority
            className="brightness-0 invert"
          />
        </Link>

        <nav
          aria-label="Primary"
          className="sidebar relative mt-6 flex flex-col items-center gap-2.5 rounded-[30px] border border-white/5 bg-[linear-gradient(180deg,rgba(25,24,23,0.98),rgba(17,17,17,0.98))] px-2.5 py-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.035),0_28px_80px_rgba(0,0,0,0.22)]"
        >
          {NAV_ITEMS.map(({ href, label, icon, match }) => (
            <NavButton
              key={href}
              href={href}
              label={label}
              icon={icon}
              isActive={match(pathname)}
            />
          ))}
        </nav>
      </aside>

      <div className="md:hidden">
        <div className="flex items-center gap-3">
          <Link
            href="/"
            prefetch={false}
            aria-label="Avian Biosurveillance home"
            className="flex h-11 w-11 shrink-0 items-center justify-center rounded-[16px] border border-white/7 bg-[linear-gradient(180deg,rgba(25,24,23,0.98),rgba(17,17,17,0.98))] shadow-[inset_0_1px_0_rgba(255,255,255,0.08)]"
          >
            <Image
              src="/icons/logo.png"
              alt="Avian Biosurveillance"
              width={28}
              height={28}
              priority
              className="brightness-0 invert"
            />
          </Link>

          <nav
            aria-label="Primary"
            className="sidebar relative flex min-w-0 flex-1 items-center gap-2 overflow-x-auto rounded-[24px] border border-white/5 bg-[linear-gradient(180deg,rgba(25,24,23,0.98),rgba(17,17,17,0.98))] px-3 py-2 pr-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.035),0_24px_70px_rgba(0,0,0,0.2)]"
          >
            {NAV_ITEMS.map(({ href, label, icon, match }) => (
              <NavButton
                key={href}
                href={href}
                label={label}
                icon={icon}
                isActive={match(pathname)}
                compact
              />
            ))}
          </nav>
        </div>
      </div>
    </>
  );
}
