<script lang="ts">
  // Schwebender Voice-/Record-Indikator (Anforderung 2.5): laeuft in einem
  // eigenen rahmenlosen Topmost-Fenster der Shell (oben mittig). Die Shell
  // schickt den Zustand als "indicator-state"-Event.
  import { onShellEvent } from "../lib/shell";

  let state: "listening" | "thinking" | "speaking" = $state("listening");

  onShellEvent("indicator-state", (payload) => {
    if (payload !== "hidden") state = payload as typeof state;
  });

  const LABELS = { listening: "Ich höre zu…", thinking: "Moment…", speaking: "" } as const;
</script>

<div class="indicator {state}">
  <div class="dot"></div>
  {#if LABELS[state]}<span>{LABELS[state]}</span>{/if}
  <div class="bars" class:animated={state === "speaking" || state === "listening"}>
    <i></i><i></i><i></i><i></i>
  </div>
</div>

<style>
  :global(body) {
    background: transparent !important;
    overflow: hidden;
  }
  .indicator {
    display: flex;
    align-items: center;
    gap: 10px;
    justify-content: center;
    height: 40px;
    margin: 4px;
    padding: 0 16px;
    border-radius: 999px;
    background: rgba(16, 20, 24, 0.92);
    border: 1px solid var(--border);
    color: var(--text);
    font-size: 13px;
    box-shadow: 0 4px 18px rgba(0, 0, 0, 0.45);
  }
  .dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--danger);
    animation: pulse 1.2s infinite;
  }
  .thinking .dot {
    background: #f0b429;
  }
  .speaking .dot {
    background: var(--primary);
    animation: none;
  }
  .bars {
    display: flex;
    gap: 3px;
    align-items: flex-end;
    height: 16px;
  }
  .bars i {
    width: 3px;
    height: 6px;
    background: var(--primary);
    border-radius: 2px;
  }
  .bars.animated i {
    animation: bounce 0.9s infinite ease-in-out;
  }
  .bars i:nth-child(2) {
    animation-delay: 0.15s;
  }
  .bars i:nth-child(3) {
    animation-delay: 0.3s;
  }
  .bars i:nth-child(4) {
    animation-delay: 0.45s;
  }
  @keyframes pulse {
    50% {
      opacity: 0.35;
    }
  }
  @keyframes bounce {
    50% {
      height: 16px;
    }
  }
</style>
