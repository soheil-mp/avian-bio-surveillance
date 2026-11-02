"use client";

import Image from "next/image";
import { useEffect, useState } from "react";

export default function SpeciesAvatar({
  thumbnailUrl,
  imageUrl,
  speciesLabel,
}: {
  thumbnailUrl: string | null;
  imageUrl: string | null;
  speciesLabel: string;
}) {
  const [thumbnailFailed, setThumbnailFailed] = useState(false);
  const [imageFailed, setImageFailed] = useState(false);

  useEffect(() => {
    setThumbnailFailed(false);
    setImageFailed(false);
  }, [thumbnailUrl, imageUrl]);

  const src =
    thumbnailUrl && !thumbnailFailed
      ? thumbnailUrl
      : imageUrl && !imageFailed
        ? imageUrl
        : null;

  if (!src) {
    return (
      <span
        className="text-[10px] font-mono font-bold"
        style={{ color: "rgba(255,255,255,0.4)" }}
      >
        {speciesLabel.slice(0, 2).toUpperCase()}
      </span>
    );
  }

  return (
    <Image
      src={src}
      alt={speciesLabel}
      fill
      unoptimized
      sizes="40px"
      className="object-cover transition-transform duration-500 group-hover:scale-110"
      onError={() => {
        if (src === thumbnailUrl) {
          setThumbnailFailed(true);
          return;
        }

        setImageFailed(true);
      }}
    />
  );
}