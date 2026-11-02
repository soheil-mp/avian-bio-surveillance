import { BirdAnimation } from "@/components/about/BirdAnimation";

export default function AboutPage() {
  return (
    <div className="relative h-full overflow-hidden bg-[#050505]">
      <div className="absolute inset-0">
        <BirdAnimation />
      </div>

      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_center,rgba(0,0,0,0.08),rgba(0,0,0,0.52)_58%,rgba(0,0,0,0.84)_100%)]" />
      <div className="pointer-events-none absolute inset-0 bg-[linear-gradient(180deg,rgba(0,0,0,0.32),rgba(0,0,0,0.12)_35%,rgba(0,0,0,0.48)_100%)]" />

      <div className="relative z-10 flex h-full items-center justify-center px-6 py-10 md:px-12">
        <section className="mx-auto flex max-w-3xl flex-col items-center gap-5 text-center font-mono">
          <h1
            className="text-5xl font-semibold tracking-[-0.05em] text-white sm:text-6xl md:text-7xl"
          >
            About us
          </h1>
          <p className="max-w-2xl text-base leading-8 text-white/70 sm:text-lg">
            Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.
          </p>
        </section>
      </div>
    </div>
  );
}