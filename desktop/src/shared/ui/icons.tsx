/**
 * The drawn marks the window uses, in one place.
 *
 * Inline SVG rather than an icon font or a package: a dozen strokes cost less
 * than a dependency, and a packaged window must not reach anywhere for them.
 * Every one takes `currentColor`, so its colour is the text's around it.
 */

import type { SVGProps } from "react";

type Props = SVGProps<SVGSVGElement>;

const base = { fill: "none", "aria-hidden": true } as const;

export const LogoMark = (props: Props) => (
  <svg viewBox="0 0 24 24" {...base} {...props}>
    <circle cx="12" cy="12" r="9.4" stroke="currentColor" strokeWidth="1.6" />
    <ellipse cx="12" cy="12" rx="4.4" ry="9.4" stroke="currentColor" strokeWidth="1.6" />
    <path d="M2.6 12h18.8" stroke="currentColor" strokeWidth="1.6" />
  </svg>
);

export const PanelIcon = (props: Props) => (
  <svg viewBox="0 0 20 20" {...base} {...props}>
    <rect x="2.5" y="3.5" width="15" height="13" rx="3" stroke="currentColor" strokeWidth="1.5" />
    <path d="M8 3.5v13" stroke="currentColor" strokeWidth="1.5" />
  </svg>
);

export const PlusIcon = (props: Props) => (
  <svg viewBox="0 0 14 14" {...base} {...props}>
    <path d="M7 2v10M2 7h10" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
  </svg>
);

export const SearchIcon = (props: Props) => (
  <svg viewBox="0 0 16 16" {...base} {...props}>
    <circle cx="7.2" cy="7.2" r="4.6" stroke="currentColor" strokeWidth="1.5" />
    <path d="M10.8 10.8 14 14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
  </svg>
);

export const ChevronDown = (props: Props) => (
  <svg viewBox="0 0 12 12" {...base} {...props}>
    <path
      d="M3 4.5 6 7.5l3-3"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  </svg>
);

export const ChevronRight = (props: Props) => (
  <svg viewBox="0 0 12 12" {...base} {...props}>
    <path
      d="M4 2.5 8 6l-4 3.5"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  </svg>
);

export const WarningIcon = (props: Props) => (
  <svg viewBox="0 0 14 14" {...base} {...props}>
    <path d="M7 1.6 12.6 12H1.4L7 1.6Z" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
    <path d="M7 5.6v2.8M7 10.2v.1" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
  </svg>
);

export const ArrowUpIcon = (props: Props) => (
  <svg viewBox="0 0 16 16" {...base} {...props}>
    <path
      d="M8 13.2V3M8 3 3.6 7.4M8 3l4.4 4.4"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  </svg>
);

export const CopyIcon = (props: Props) => (
  <svg viewBox="0 0 16 16" {...base} {...props}>
    <rect x="5" y="5" width="8.5" height="8.5" rx="2" stroke="currentColor" strokeWidth="1.4" />
    <path
      d="M11 5V4a1.5 1.5 0 0 0-1.5-1.5H4A1.5 1.5 0 0 0 2.5 4v5.5A1.5 1.5 0 0 0 4 11h1"
      stroke="currentColor"
      strokeWidth="1.4"
    />
  </svg>
);

export const CheckIcon = (props: Props) => (
  <svg viewBox="0 0 14 14" {...base} {...props}>
    <path
      d="M3 7.3 5.7 10 11 4.4"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  </svg>
);

export const StopIcon = (props: Props) => (
  <svg viewBox="0 0 14 14" {...base} {...props}>
    <rect x="3.5" y="3.5" width="7" height="7" rx="1.6" fill="currentColor" />
  </svg>
);

export const GearIcon = (props: Props) => (
  <svg viewBox="0 0 16 16" {...base} {...props}>
    <circle cx="8" cy="8" r="2.2" stroke="currentColor" strokeWidth="1.4" />
    <path
      d="M8 1.8v1.6M8 12.6v1.6M14.2 8h-1.6M3.4 8H1.8M12.4 3.6l-1.1 1.1M4.7 11.3l-1.1 1.1M12.4 12.4l-1.1-1.1M4.7 4.7 3.6 3.6"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
    />
  </svg>
);

export const CloseIcon = (props: Props) => (
  <svg viewBox="0 0 14 14" {...base} {...props}>
    <path d="M3.4 3.4 10.6 10.6M10.6 3.4 3.4 10.6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
  </svg>
);

export const BackIcon = (props: Props) => (
  <svg viewBox="0 0 16 16" {...base} {...props}>
    <path
      d="M13 8H3.4M3.4 8 7.2 4.2M3.4 8l3.8 3.8"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  </svg>
);

export const FolderIcon = (props: Props) => (
  <svg viewBox="0 0 16 16" {...base} {...props}>
    <path
      d="M1.8 4.4a1.6 1.6 0 0 1 1.6-1.6h2.3l1.3 1.6h5.6a1.6 1.6 0 0 1 1.6 1.6v5.6a1.6 1.6 0 0 1-1.6 1.6H3.4a1.6 1.6 0 0 1-1.6-1.6V4.4Z"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinejoin="round"
    />
  </svg>
);

export const BookIcon = (props: Props) => (
  <svg viewBox="0 0 16 16" {...base} {...props}>
    <path
      d="M2.6 3a1 1 0 0 1 1-1H8v12H3.6a1 1 0 0 1-1-1V3ZM8 2h4.4a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H8"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinejoin="round"
    />
  </svg>
);

export const SparkIcon = (props: Props) => (
  <svg viewBox="0 0 16 16" {...base} {...props}>
    <path
      d="M8 1.8 9.5 6 13.8 7.5 9.5 9 8 13.2 6.5 9 2.2 7.5 6.5 6 8 1.8Z"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinejoin="round"
    />
  </svg>
);

export const BoxIcon = (props: Props) => (
  <svg viewBox="0 0 16 16" {...base} {...props}>
    <path
      d="M8 1.9 13.7 5v6L8 14.1 2.3 11V5L8 1.9Z"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinejoin="round"
    />
    <path d="M2.3 5 8 8.1 13.7 5M8 8.1v6" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
  </svg>
);

export const PlugIcon = (props: Props) => (
  <svg viewBox="0 0 16 16" {...base} {...props}>
    <path d="M5.6 1.9v3M10.4 1.9v3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    <path
      d="M3.6 4.9h8.8v3a4.4 4.4 0 0 1-4.4 4.4 4.4 4.4 0 0 1-4.4-4.4v-3Z"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinejoin="round"
    />
    <path d="M8 12.3v1.8" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
  </svg>
);

export const PeopleIcon = (props: Props) => (
  <svg viewBox="0 0 14 14" {...base} {...props}>
    <circle cx="5.2" cy="4.8" r="2.1" stroke="currentColor" strokeWidth="1.3" />
    <path d="M1.6 11.6c.4-2 1.9-3.2 3.6-3.2s3.2 1.2 3.6 3.2" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
    <path d="M9.4 3a2 2 0 0 1 0 3.8M10.4 8.6c1 .4 1.7 1.5 2 3" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
  </svg>
);

export const ComposeIcon = (props: Props) => (
  <svg viewBox="0 0 20 20" {...base} {...props}>
    <path
      d="M9.2 3.6H6a2.4 2.4 0 0 0-2.4 2.4v8A2.4 2.4 0 0 0 6 16.4h8a2.4 2.4 0 0 0 2.4-2.4v-3.2"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
    />
    <path
      d="M14.3 2.9a1.7 1.7 0 0 1 2.4 2.4l-6.2 6.2-3.1.7.7-3.1 6.2-6.2Z"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinejoin="round"
    />
  </svg>
);

export const DotsIcon = (props: Props) => (
  <svg viewBox="0 0 16 16" {...base} {...props}>
    <circle cx="3.4" cy="8" r="1.3" fill="currentColor" />
    <circle cx="8" cy="8" r="1.3" fill="currentColor" />
    <circle cx="12.6" cy="8" r="1.3" fill="currentColor" />
  </svg>
);

export const PencilIcon = (props: Props) => (
  <svg viewBox="0 0 16 16" {...base} {...props}>
    <path
      d="M10.9 2.6a1.6 1.6 0 0 1 2.3 2.3L5.6 12.5l-3 .8.8-3 7.5-7.7Z"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinejoin="round"
    />
  </svg>
);

export const TrashIcon = (props: Props) => (
  <svg viewBox="0 0 16 16" {...base} {...props}>
    <path
      d="M2.6 4.2h10.8M6.4 4.2V2.8h3.2v1.4"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
    <path
      d="M3.9 4.2l.7 8.5a1.4 1.4 0 0 0 1.4 1.3h4a1.4 1.4 0 0 0 1.4-1.3l.7-8.5"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinejoin="round"
    />
  </svg>
);
