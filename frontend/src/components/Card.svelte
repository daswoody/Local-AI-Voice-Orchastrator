<script lang="ts">
  // Huelle einer Karte: Rahmen + Titel + Template-Aufloesung; die eigentliche
  // Struktur rendert CardView rekursiv aus dem Layout-JSON. HTML-Karten
  // (4.12 v1.12: KI-geschrieben oder gespeichertes HTML-Layout) laufen
  // stattdessen in einem sandboxed iframe - ohne allow-same-origin kommt
  // das HTML nicht an Token/localStorage der App heran.
  import type { CardEnvelope } from "../lib/api";
  import { htmlFor, layoutFor } from "../lib/cards";
  import CardView from "./CardView.svelte";

  let {
    card,
    ontool = undefined,
  }: {
    card: CardEnvelope;
    ontool?: (tool: string, args: Record<string, unknown>) => void;
  } = $props();

  const html = $derived(htmlFor(card));
  const root = $derived(layoutFor(card.type));
  const frameHeight = $derived.by(() => {
    const raw = Number(card.data?.height);
    return Number.isFinite(raw) && raw > 0 ? Math.min(raw, 800) : 320;
  });
  const srcdoc = $derived(
    html === null
      ? ""
      : `<!doctype html><meta charset="utf-8"><style>body{margin:0;font-family:system-ui,sans-serif;color-scheme:light dark}</style>${html}`,
  );
</script>

<div class="card">
  {#if card.title}<div class="title">{card.title}</div>{/if}
  {#if html !== null}
    <iframe
      class="html-card"
      sandbox="allow-scripts"
      srcdoc={srcdoc}
      style:height={`${frameHeight}px`}
      title={card.title ?? "Karte"}
    ></iframe>
  {:else}
    <CardView node={root} envelope={card} {ontool} />
  {/if}
</div>

<style>
  .card {
    background: var(--bg-raised);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 14px;
    max-width: 420px;
  }
  .title {
    font-size: 13px;
    color: var(--text-muted);
    margin-bottom: 8px;
  }
  .html-card {
    border: none;
    width: 100%;
    display: block;
    background: transparent;
  }
</style>
