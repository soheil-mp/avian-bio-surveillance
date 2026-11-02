import type { Metadata } from "next";
import "./globals.css";
import { Navbar } from "@/components/layout/Navbar";
import { PerformanceClientBootstrap } from "@/components/performance/PerformanceClientBootstrap";

export const metadata: Metadata = {
  title: "Avian Biosurveillance",
  description:
    "Acoustic monitoring dashboard for wildlife disease surveillance in the Netherlands",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="flex h-screen overflow-hidden bg-[#0c0c0c] p-2 text-text-primary md:p-3">
        <PerformanceClientBootstrap />
        <main className="relative flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-[32px] border border-white/[0.04] bg-[#0c0c0c] shadow-[inset_0_1px_0_rgba(255,255,255,0.018),0_20px_56px_rgba(0,0,0,0.22)] md:flex-row">
          <Navbar />
          <div className="relative min-h-0 min-w-0 flex-1 overflow-hidden">
            {children}
          </div>
        </main>
      </body>
    </html>
  );
}
