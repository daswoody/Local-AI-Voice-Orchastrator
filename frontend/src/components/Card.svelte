<script lang="ts">
  // Huelle einer Karte: Rahmen + Titel + Template-Aufloesung; die eigentliche
  // Struktur rendert CardView rekursiv aus dem Layout-JSON.
  import type { CardEnvelope } from "../lib/api";
  import { layoutFor } from "../lib/cards";
  import CardView from "./CardView.svelte";

  let {
    card,
    ontool = undefined,
  }: {
    card: CardEnvelope;
    ontool?: (tool: string, args: Record<string, unknown>) => void;
  } = $props();

  const root = $derived(layoutFor(card.type));
</script>

<div class="card">
  {#if card.title}<div class="title">{card.title}</div>{/if}
  <CardView node={root} envelope={card} {ontool} />
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
</style>
