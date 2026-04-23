import { useState } from "react";

import type { DiscoverSourceEntry } from "../types";

interface SourcePillProps {
  source: DiscoverSourceEntry;
}

export function SourcePill({ source }: SourcePillProps) {
  const [broken, setBroken] = useState(false);
  const initials = (source.label || source.domain || "?").slice(0, 1).toUpperCase();

  return (
    <a
      className="source-pill"
      href={source.url}
      rel="noreferrer"
      target="_blank"
    >
      <span className="favicon">
        {!broken && source.favicon_url ? (
          <img
            alt={source.domain ?? initials}
            src={source.favicon_url}
            onError={() => setBroken(true)}
          />
        ) : (
          initials
        )}
      </span>
      <span>{source.domain || source.url}</span>
    </a>
  );
}
