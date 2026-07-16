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

  // Hoehe an den Inhalt anpassen: Ohne same-origin kann der Parent nicht in
  // das iframe schauen - deshalb meldet ein injiziertes Mini-Script die
  // Inhaltshoehe per postMessage. Zuordnung ueber eine pro Karte eindeutige
  // ID im Payload (robuster als contentWindow-Vergleiche, gerade bei
  // mehreren Karten im Verlauf).
  const frameId = `card-${Math.random().toString(36).slice(2)}`;
  let frameHeight = $state(60);

  $effect(() => {
    const requested = Number(card.data?.height);
    if (Number.isFinite(requested) && requested > 0) {
      frameHeight = Math.min(requested, 800);
      return;
    }
    const onMessage = (event: MessageEvent) => {
      const data = event.data as { heimaiCard?: string; height?: unknown } | null;
      if (data?.heimaiCard !== frameId) return;
      const height = Number(data.height);
      if (Number.isFinite(height) && height > 0) {
        frameHeight = Math.min(Math.max(Math.ceil(height), 24), 800);
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  });

  // Wrapper um das Karten-HTML: transparenter Hintergrund, App-Textfarbe
  // (dunkles UI), Breiten-Guards gegen zu breit geratene Layouts (feste
  // Breiten schlagen sonst als Scrollbalken durch) und der Hoehen-Reporter.
  const srcdoc = $derived(
    html === null
      ? ""
      : `<!doctype html><meta charset="utf-8"><style>` +
        `html,body{margin:0;background:transparent;overflow-x:hidden}` +
        `body{font-family:system-ui,sans-serif;color:#e8edf2;line-height:1.45}` +
        `*{box-sizing:border-box}` +
        `body *{max-width:100% !important}` +
        `table{width:100%;border-collapse:collapse}` +
        `img{max-width:100%}` +
        `</style>${html}<script>` +
        `var send=function(){parent.postMessage({heimaiCard:"${frameId}",height:document.documentElement.scrollHeight},"*")};` +
        `addEventListener("load",send);` +
        `if(window.ResizeObserver){new ResizeObserver(send).observe(document.documentElement);}` +
        `setTimeout(send,50);send();` +
        `<\/script>`,
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
    color-scheme: normal;
  }
</style>
