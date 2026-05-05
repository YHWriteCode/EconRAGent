import type { SVGProps } from "react";

type IconName =
  | "chat"
  | "chevronRight"
  | "database"
  | "discover"
  | "globe"
  | "graph"
  | "memory"
  | "search"
  | "settings"
  | "upload";

const paths: Record<IconName, JSX.Element> = {
  chat: (
    <>
      <path d="M5 6.5h14v9H9l-4 3v-12Z" />
      <path d="M8 10h8" />
      <path d="M8 13h5" />
    </>
  ),
  chevronRight: <path d="m9 6 6 6-6 6" />,
  database: (
    <>
      <ellipse cx="12" cy="5.5" rx="7" ry="3" />
      <path d="M5 5.5v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6" />
      <path d="M5 11.5v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6" />
    </>
  ),
  discover: (
    <>
      <circle cx="12" cy="12" r="7" />
      <path d="m14.5 9.5-1.8 3.2-3.2 1.8 1.8-3.2 3.2-1.8Z" />
    </>
  ),
  globe: (
    <>
      <circle cx="12" cy="12" r="8" />
      <path d="M4 12h16" />
      <path d="M12 4c2 2.1 3 4.8 3 8s-1 5.9-3 8" />
      <path d="M12 4c-2 2.1-3 4.8-3 8s1 5.9 3 8" />
    </>
  ),
  graph: (
    <>
      <circle cx="6" cy="7" r="2.5" />
      <circle cx="18" cy="7" r="2.5" />
      <circle cx="12" cy="18" r="2.5" />
      <path d="m8.2 8.4 7.6 7.2" />
      <path d="m15.8 8.4-7.6 7.2" />
      <path d="M8.5 7h7" />
    </>
  ),
  memory: (
    <>
      <path d="M8 5h8a3 3 0 0 1 3 3v8a3 3 0 0 1-3 3H8a3 3 0 0 1-3-3V8a3 3 0 0 1 3-3Z" />
      <path d="M9 9h6v6H9z" />
      <path d="M9 2v3" />
      <path d="M15 2v3" />
      <path d="M9 19v3" />
      <path d="M15 19v3" />
      <path d="M2 9h3" />
      <path d="M2 15h3" />
      <path d="M19 9h3" />
      <path d="M19 15h3" />
    </>
  ),
  search: (
    <>
      <circle cx="11" cy="11" r="6" />
      <path d="m16 16 4 4" />
    </>
  ),
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 3v3" />
      <path d="M12 18v3" />
      <path d="m4.2 7.5 2.6 1.5" />
      <path d="m17.2 15 2.6 1.5" />
      <path d="m19.8 7.5-2.6 1.5" />
      <path d="m6.8 15-2.6 1.5" />
    </>
  ),
  upload: (
    <>
      <path d="M12 16V4" />
      <path d="m7 9 5-5 5 5" />
      <path d="M5 18h14" />
    </>
  ),
};

export function Icon({
  name,
  className = "",
  ...props
}: SVGProps<SVGSVGElement> & { name: IconName }) {
  return (
    <svg
      aria-hidden="true"
      className={`app-icon ${className}`.trim()}
      fill="none"
      focusable="false"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.8"
      viewBox="0 0 24 24"
      {...props}
    >
      {paths[name]}
    </svg>
  );
}
