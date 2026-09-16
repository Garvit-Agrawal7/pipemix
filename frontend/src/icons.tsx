import type { DeviceKind } from "./types";

type P = { size?: number };

// Every glyph is lifted verbatim from the Direction B mockups; the default
// size is the box the mockup draws it at.
const base = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeLinecap: "round",
  strokeLinejoin: "round",
} as const;

const sm = { ...base, viewBox: "0 0 20 20" } as const;

export const Logo = ({ size = 30 }: P) => (
  <svg className="mark" width={size} height={size} {...base} strokeWidth={1.8}>
    <circle cx="5" cy="12" r="2.4" />
    <path d="M7.2 11.2 15.2 6.6" />
    <path d="M7.2 12.8 15.2 17.4" />
    <circle cx="17.6" cy="5.4" r="2.4" />
    <circle cx="17.6" cy="18.6" r="2.4" />
  </svg>
);

export const IconOutputs = ({ size = 21 }: P) => (
  <svg width={size} height={size} {...base} strokeWidth={1.8}>
    <path d="M4 9.5v5h3.5L13 19V5L7.5 9.5H4z" />
    <path d="M16.5 9.8a3.6 3.6 0 0 1 0 4.4" />
    <path d="M19.3 7.2a7.4 7.4 0 0 1 0 9.6" />
  </svg>
);

export const IconApps = ({ size = 21 }: P) => (
  <svg width={size} height={size} {...base} strokeWidth={1.8}>
    <circle cx="4.6" cy="12" r="1.9" />
    <path d="M6.5 12h4.2l3.6-5h3.4" />
    <path d="M10.7 12l3.6 5h3.4" />
    <path d="M16.4 4.6 18.9 7l-2.5 2.4" />
    <path d="M16.4 14.6 18.9 17l-2.5 2.4" />
  </svg>
);

export const IconPower = ({ size = 20 }: P) => (
  <svg width={size} height={size} {...base} strokeWidth={1.8}>
    <path d="M12 3.5v8.2" />
    <path d="M6.8 6.9a7.6 7.6 0 1 0 10.4 0" />
  </svg>
);

export const IconHeadphones = ({ size = 21 }: P) => (
  <svg width={size} height={size} {...base} strokeWidth={1.6}>
    <path d="M4 15v-3a8 8 0 0 1 16 0v3" />
    <rect x="2.2" y="13.6" width="4.6" height="6.4" rx="2.2" />
    <rect x="17.2" y="13.6" width="4.6" height="6.4" rx="2.2" />
  </svg>
);

export const IconDisplay = ({ size = 21 }: P) => (
  <svg width={size} height={size} {...base} strokeWidth={1.6}>
    <rect x="2.5" y="4" width="19" height="13" rx="2.2" />
    <path d="M8.5 20.5h7" />
    <path d="M12 17v3.5" />
  </svg>
);

export const IconUsb = ({ size = 21 }: P) => (
  <svg width={size} height={size} {...base} strokeWidth={1.6}>
    <circle cx="12" cy="20.2" r="1.4" />
    <path d="M12 18.8V4.2" />
    <path d="m12 8.6 3.2 3.2v4" />
    <path d="M12 12.2 8.8 15v2.4" />
    <path d="M9.6 3.2h4.8L12 6.2z" />
  </svg>
);

export const IconSpeaker = ({ size = 21 }: P) => (
  <svg width={size} height={size} {...base} strokeWidth={1.6}>
    <rect x="5.5" y="2.6" width="13" height="18.8" rx="2.6" />
    <circle cx="12" cy="15.2" r="3.2" />
    <circle cx="12" cy="7.4" r="1.2" />
  </svg>
);

export const IconWave = ({ size = 21 }: P) => (
  <svg width={size} height={size} {...base} strokeWidth={1.6}>
    <path d="M4 11v2" />
    <path d="M8 7.5v9" />
    <path d="M12 4.5v15" />
    <path d="M16 8.5v7" />
    <path d="M20 10.5v3" />
  </svg>
);

export const IconVolume = ({ size = 17 }: P) => (
  <svg width={size} height={size} {...base} strokeWidth={1.7}>
    <path d="M4 9.5v5h3.5L13 19V5L7.5 9.5H4z" />
    <path d="M16.5 9.8a3.6 3.6 0 0 1 0 4.4" />
  </svg>
);

export const IconMuted = ({ size = 17 }: P) => (
  <svg width={size} height={size} {...base} strokeWidth={1.7}>
    <path d="M4 9.5v5h3.5L13 19V5L7.5 9.5H4z" />
    <path d="m16.5 10 4 4" />
    <path d="m20.5 10-4 4" />
  </svg>
);

// Points down; rotate 180deg for the open state.
export const IconChevron = ({ size = 13 }: P) => (
  <svg width={size} height={size} {...sm} strokeWidth={2}>
    <path d="m5 8 5 5 5-5" />
  </svg>
);

export const IconCheck = ({ size = 14 }: P) => (
  <svg width={size} height={size} {...sm} strokeWidth={2.2}>
    <path d="m4 10.5 3.6 3.6L16 5.5" />
  </svg>
);

export const IconPlus = ({ size = 14 }: P) => (
  <svg width={size} height={size} {...sm} strokeWidth={2}>
    <path d="M10 4.5v11M4.5 10h11" />
  </svg>
);

export const IconTrash = ({ size = 14 }: P) => (
  <svg width={size} height={size} {...sm} strokeWidth={1.9}>
    <path d="M4 6h12" />
    <path d="M8 6V4.2h4V6" />
    <path d="M5.6 6l.8 9.4h7.2L14.4 6" />
  </svg>
);

export const IconRefresh = ({ size = 13 }: P) => (
  <svg width={size} height={size} {...sm} strokeWidth={1.8}>
    <path d="M10 3.2a6.8 6.8 0 1 1-6.2 4" />
    <path d="M3.4 3.4v3.6h3.6" />
  </svg>
);

export const IconSignal = ({ size = 19 }: P) => (
  <svg width={size} height={size} {...base} strokeWidth={1.7}>
    <path d="M3 9.5a13 13 0 0 1 18 0" />
    <path d="M6.5 13a8.4 8.4 0 0 1 11 0" />
    <path d="M10 16.4a3.4 3.4 0 0 1 4 0" />
    <path d="M12 20h.01" />
  </svg>
);

// The crossed-split glyph in the empty state.
export const IconSplit = ({ size = 34 }: P) => (
  <svg width={size} height={size} {...base} strokeWidth={1.3}>
    <path d="m7 7 10 10" />
    <path d="M12 3.5 17 8l-5 4.5v-9z" />
    <path d="M12 20.5 17 16l-5-4.5v9z" />
    <path d="m7 16 10-10" />
  </svg>
);

export function kindIcon(kind: DeviceKind) {
  switch (kind) {
    case "bluetooth": return IconHeadphones;
    case "hdmi":      return IconDisplay;
    case "usb":       return IconUsb;
    default:          return IconSpeaker;
  }
}
