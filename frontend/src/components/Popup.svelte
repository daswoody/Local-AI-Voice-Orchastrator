<script lang="ts">
  // Antwort-Popup ueber allen Anwendungen (Anforderung 2.5): eigenes
  // rahmenloses Topmost-Fenster der Shell, laedt #/popup?id=<n>. Zeigt
  // Text und/oder Karte, schliesst nach 12 s automatisch - ausser der
  // Nutzer pinnt es an (📌).
  import { onMount } from "svelte";
  import type { CardEnvelope } from "../lib/api";
  import { syncLayouts } from "../lib/cards";
  import { closePopup, getPopupPayload, pinPopup } from "../lib/shell";
  import Card from "./Card.svelte";

  const id = new URLSearchParams(location.hash.split("?")[1] ?? "").get("id") ?? "";

  let title = $state("Heim-AI");
  let body = $state("");
  let card: CardEnvelope | null = $state(null);
  let pinned = $state(false);
  let remaining = $state(12);
  let timer: ReturnType<typeof setInterval> | undefined;

  onMount(async () => {
    await syncLayouts();
    const raw = await getPopupPayload(id);
    if (raw) {
      const payload = JSON.parse(raw) as { title?: string; body?: string; card?: CardEnvelope };
      title = payload.title || "Heim-AI";
      body = payload.body || "";
      card = payload.card ?? null;
    }
    timer = setInterval(() => {
      if (pinned) return;
      remaining -= 1;
      if (remaining <= 0) void close();
    }, 1000);
    return () => clearInterval(timer);
  });

  async function close() {
    clearInterval(timer);
    await closePopup(id);
  }

  async function pin() {
    pinned = true;
    await pinPopup(id);
  }
</script>

<div class="popup">
  <div class="head">
    <span class="title">🏠 {title}</span>
    <span class="head-actions">
      {#if !pinned}
        <button class="ghost" title="Anpinnen (bleibt sichtbar)" onclick={pin}>📌</button>
      {/if}
      <button class="ghost" title="Schließen" onclick={close}>✕</button>
    </span>
  </div>
  {#if body}<div class="body">{body}</div>{/if}
  {#if card}
    <div class="card-wrap"><Card {card} /></div>
  {/if}
  {#if !pinned}
    <div class="timer"><div class="timer-bar" style:width={`${(remaining / 12) * 100}%`}></div></div>
  {/if}
</div>

<style>
  :global(body) {
    background: transparent !important;
    overflow: hidden;
  }
  .popup {
    margin: 4px;
    background: rgba(20, 25, 31, 0.97);
    border: 1px solid var(--border);
    border-radius: 14px;
    box-shadow: 0 6px 24px rgba(0, 0, 0, 0.5);
    display: flex;
    flex-direction: column;
    max-height: calc(100vh - 8px);
  }
  .head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 8px 4px 14px;
  }
  .title {
    font-weight: 600;
    font-size: 14px;
  }
  .head-actions {
    display: flex;
  }
  .head-actions button {
    padding: 2px 6px;
  }
  .body {
    padding: 2px 14px 10px;
    font-size: 14px;
    line-height: 1.45;
    overflow-y: auto;
    white-space: pre-wrap;
  }
  .card-wrap {
    padding: 0 10px 10px;
    overflow-y: auto;
  }
  .timer {
    height: 3px;
    background: var(--bg-input);
    border-radius: 0 0 14px 14px;
    overflow: hidden;
  }
  .timer-bar {
    height: 100%;
    background: var(--primary);
    transition: width 1s linear;
  }
</style>
