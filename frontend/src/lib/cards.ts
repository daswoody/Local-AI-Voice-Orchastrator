// Karten-System (docs/CARDS.md): Layout-Templates kommen vom Server
// (/v1/cards/layouts), werden in localStorage gecacht (hoehere Version
// gewinnt) und von CardView.svelte rekursiv gerendert.

import type { CardEnvelope, CardLayout } from "./api";
import { fetchCardLayouts } from "./api";

const CACHE_KEY = "heimai.cardLayouts";

interface LayoutCache {
  version: number;
  layouts: Record<string, CardLayout>;
}

let cache: LayoutCache = loadCache();

function loadCache(): LayoutCache {
  try {
    return JSON.parse(localStorage.getItem(CACHE_KEY) || "") as LayoutCache;
  } catch {
    return { version: 0, layouts: {} };
  }
}

/** Beim App-Start: Templates nachziehen (since_version-Poll, 4.12). */
export async function syncLayouts(): Promise<void> {
  try {
    const result = await fetchCardLayouts(cache.version);
    if (result.layouts.length === 0 && result.version === cache.version) return;
    for (const layout of result.layouts) {
      const existing = cache.layouts[layout.card_type];
      if (!existing || layout.layout_version > existing.layout_version) {
        cache.layouts[layout.card_type] = layout;
      }
    }
    cache.version = Math.max(cache.version, result.version);
    localStorage.setItem(CACHE_KEY, JSON.stringify(cache));
  } catch (error) {
    console.warn("Karten-Layouts nicht aktualisierbar:", error);
  }
}

/** Fallback generic laut CARDS.md: unbekannte Typen rendern data.headline
 *  + data.body, so kann der Server neue Typen vor dem Layout einfuehren. */
const GENERIC_FALLBACK: Record<string, unknown> = {
  component: "column",
  children: [
    { component: "text", text: "{{data.headline}}", style: "title" },
    { component: "text", text: "{{data.body}}", style: "body" },
  ],
};

export function layoutFor(cardType: string): Record<string, unknown> {
  return (
    cache.layouts[cardType]?.root ?? cache.layouts["generic"]?.root ?? GENERIC_FALLBACK
  );
}

/** {{pfad}}-Bindings gegen die Envelope aufloesen ({type,title,data});
 *  in list-item_template relativ zum Listeneintrag (scope). */
export function resolveBindings(
  template: string,
  envelope: CardEnvelope,
  scope?: unknown,
): string {
  return template.replace(/\{\{([^}]+)\}\}/g, (_, path: string) => {
    const value = resolvePath(path.trim(), envelope, scope);
    return value === undefined || value === null ? "" : String(value);
  });
}

export function resolvePath(path: string, envelope: CardEnvelope, scope?: unknown): unknown {
  const segments = path.split(".");
  // Im Listen-Scope (item_template) beziehen sich Pfade auf den Eintrag
  // selbst ({{day}}); data./type/title erreichen weiterhin die Envelope.
  let current: unknown =
    scope !== undefined && !["data", "type", "title"].includes(segments[0])
      ? scope
      : (envelope as unknown);
  for (const segment of segments) {
    if (current === undefined || current === null) return undefined;
    current = (current as Record<string, unknown>)[segment];
  }
  return current;
}

/** Icon-Namen aus CARDS.md -> Emoji (bewusst simpel; die Windows/Web-UI
 *  braucht keine Icon-Font, und Emojis sind auf allen Plattformen da). */
export const ICONS: Record<string, string> = {
  home: "🏠",
  alarm: "⏰",
  calendar: "📅",
  check: "✅",
  cloud: "☁️",
  mail: "✉️",
  light: "💡",
  location: "📍",
  music: "🎵",
  navigation: "🧭",
  notification: "🔔",
  person: "👤",
  phone: "📞",
  star: "⭐",
  thermostat: "🌡️",
  timer: "⏱️",
  rain: "🌧️",
  sun: "☀️",
};
