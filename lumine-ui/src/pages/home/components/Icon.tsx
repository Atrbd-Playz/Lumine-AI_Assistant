import type { IconName } from "../types";

/** Small inline icon set. Keeping icons here makes buttons easy to scan and replace. */
export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  const common = { width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.7, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
  switch (name) {
    case "home": return <svg {...common}><path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V10Z" /><path d="M9 21v-7h6v7" /></svg>;
    case "spark": return <svg {...common}><path d="m12 2 1.9 6.1L20 10l-6.1 1.9L12 18l-1.9-6.1L4 10l6.1-1.9L12 2Z" /><path d="m19 16 .8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8L19 16Z" /></svg>;
    case "check": return <svg {...common}><path d="m5 12 4.2 4L19 6" /></svg>;
    case "clock": return <svg {...common}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></svg>;
    case "settings": return <svg {...common}><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5v.2h-4v-.2a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1-2.8-2.8.1-.1A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.5-1H3v-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1 2.8-2.8.1.1a1.7 1.7 0 0 0 1.8.3 1.7 1.7 0 0 0 1-1.5V3h4v.2a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1 2.8 2.8-.1.1a1.7 1.7 0 0 0-.3 1.8 1.7 1.7 0 0 0 1.5 1h.2v4h-.2a1.7 1.7 0 0 0-1.5 1Z" /></svg>;
    case "mic": return <svg {...common}><rect x="8.5" y="2" width="7" height="13" rx="3.5" /><path d="M5 11.5a7 7 0 0 0 14 0M12 18.5V22M8.5 22h7" /></svg>;
    case "send": return <svg {...common}><path d="m21 3-8.5 18-2.2-7.3L3 11.5 21 3Z" /><path d="m10.3 13.7 4.1-4.1" /></svg>;
    case "stop": return <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2" /></svg>;
    case "more": return <svg {...common}><circle cx="5" cy="12" r=".8" fill="currentColor" /><circle cx="12" cy="12" r=".8" fill="currentColor" /><circle cx="19" cy="12" r=".8" fill="currentColor" /></svg>;
    case "music": return <svg {...common}><path d="M9 18V5l11-2v13" /><circle cx="6" cy="18" r="3" /><circle cx="17" cy="16" r="3" /></svg>;
    case "bell": return <svg {...common}><path d="M18 9a6 6 0 0 0-12 0c0 7-3 8-3 8h18s-3-1-3-8" /><path d="M10 21h4" /></svg>;
    case "activity": return <svg {...common}><path d="M3 12h4l2-6 4 12 2-6h6" /></svg>;
    case "waveform": return <svg {...common}><path d="M4 12v2M8 8v8M12 5v14M16 8v8M20 10v4" /></svg>;
    case "trash": return <svg {...common}><path d="M4 7h16M10 11v6M14 11v6M6 7l1 14h10l1-14M9 7V4h6v3" /></svg>;
    case "reset": return <svg {...common}><path d="M4 8a8 8 0 1 1 1.8 8.8" /><path d="M4 4v4h4" /></svg>;
    case "close": return <svg {...common}><path d="m6 6 12 12M18 6 6 18" /></svg>;
  }
}

export function Mark() {
  return <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>;
}
