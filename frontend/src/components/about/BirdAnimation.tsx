export function BirdAnimation() {
  return (
    <div className="relative h-full w-full overflow-hidden bg-[#050505]">
      <iframe
        src="https://pages.cs.wisc.edu/~lizy/mrdoob-three.js-ef5f05d/examples/canvas_geometry_birds.html"
        title="Animated flock of birds"
        loading="lazy"
        className="absolute left-0 w-full border-0"
        style={{
          top: "-52px",
          height: "calc(100% + 52px)",
          filter: "invert(1) contrast(1.06) brightness(0.94)",
        }}
      />
    </div>
  );
}