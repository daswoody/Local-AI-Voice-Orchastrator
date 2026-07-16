<script lang="ts">
  // Chat-Ansicht: Verlauf, Texteingabe, Push-to-Talk, Realtime-Toggle,
  // Screenshot-Button (in der Windows-Shell).
  import { app, sendText, sendScreenshot, startPtt, stopPtt, toggleRealtime } from "../lib/session.svelte";
  import { inShell } from "../lib/shell";
  import Card from "./Card.svelte";

  let input = $state("");
  let scroller: HTMLElement | undefined = $state();

  $effect(() => {
    // Bei neuen Nachrichten ans Ende scrollen.
    void app.messages.length;
    void app.partialTranscript;
    requestAnimationFrame(() => scroller?.scrollTo({ top: scroller.scrollHeight }));
  });

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    const text = input;
    input = "";
    await sendText(text);
  }

  async function screenshot() {
    const question = input;
    input = "";
    await sendScreenshot(question);
  }
</script>

<div class="chat">
  <div class="messages" bind:this={scroller}>
    {#if app.messages.length === 0}
      <div class="empty">
        <div class="empty-icon">💬</div>
        <p>Neues Gespräch — tippe eine Nachricht oder halte die Mikrofon-Taste gedrückt.</p>
      </div>
    {/if}
    {#each app.messages as message (message.id)}
      {#if message.role === "card" && message.card}
        <div class="bubble-row assistant"><Card card={message.card} /></div>
      {:else if message.role === "tools" && message.tools}
        <!-- Was tut die KI gerade? Tool-/Agenten-Aufrufe des Turns (v1.12.1) -->
        <div class="bubble-row assistant">
          <div class="tool-chips">
            {#each message.tools as activity}
              <span class="tool-chip {activity.status}">
                <span class="tool-icon">{activity.tool.startsWith("agent-") ? "🤖" : "🔧"}</span>
                {activity.tool.startsWith("agent-")
                  ? `Agent: ${activity.tool.slice(6)}`
                  : activity.tool}
                <span class="tool-status">
                  {activity.status === "running" ? "…" : activity.status === "error" ? "⚠" : "✓"}
                </span>
              </span>
            {/each}
          </div>
        </div>
      {:else}
        <div class="bubble-row {message.role}">
          <div class="bubble {message.role}" class:pending={!message.final}>{message.text}</div>
        </div>
      {/if}
    {/each}
    {#if app.partialTranscript}
      <div class="bubble-row user"><div class="bubble user pending">{app.partialTranscript}</div></div>
    {/if}
    {#if app.thinking}
      <div class="bubble-row assistant"><div class="bubble assistant pending">…</div></div>
    {/if}
  </div>

  {#if app.error}
    <div class="error" role="alert">
      {app.error}
      <button class="ghost" onclick={() => (app.error = "")}>×</button>
    </div>
  {/if}

  <form class="input-row" onsubmit={submit}>
    <button
      type="button"
      class="mic"
      class:active={app.listening}
      title="Push-to-Talk: gedrückt halten"
      onpointerdown={() => void startPtt()}
      onpointerup={stopPtt}
      onpointerleave={stopPtt}
    >🎙️</button>
    <button
      type="button"
      class="mic"
      class:active={app.realtime}
      title="Realtime Talk umschalten"
      onclick={() => void toggleRealtime()}
    >{app.realtime ? "⏹" : "🔊"}</button>
    {#if inShell}
      <button type="button" class="mic" title="Screenshot senden" onclick={screenshot}>📸</button>
    {/if}
    <input placeholder="Nachricht an Heim-AI…" bind:value={input} />
    <button class="primary" type="submit">Senden</button>
  </form>
</div>

<style>
  .chat {
    display: flex;
    flex-direction: column;
    height: 100%;
  }
  .messages {
    flex: 1;
    overflow-y: auto;
    padding: 20px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .empty {
    margin: auto;
    text-align: center;
    color: var(--text-muted);
  }
  .empty-icon {
    font-size: 40px;
  }
  .bubble-row {
    display: flex;
  }
  .bubble-row.user {
    justify-content: flex-end;
  }
  .bubble {
    max-width: 70%;
    padding: 10px 14px;
    border-radius: var(--radius);
    white-space: pre-wrap;
    line-height: 1.45;
  }
  .bubble.user {
    background: var(--primary-dim);
  }
  .bubble.assistant {
    background: var(--bg-raised);
    border: 1px solid var(--border);
  }
  .bubble.pending {
    opacity: 0.7;
  }
  .tool-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .tool-chip {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 12px;
    color: var(--text-muted);
    background: var(--bg-raised);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 3px 10px;
  }
  .tool-chip.running {
    animation: chip-pulse 1.2s ease-in-out infinite;
  }
  .tool-chip.error {
    border-color: var(--danger);
    color: var(--danger);
  }
  .tool-icon {
    font-size: 13px;
  }
  .tool-status {
    opacity: 0.8;
  }
  @keyframes chip-pulse {
    50% {
      opacity: 0.55;
    }
  }
  .error {
    margin: 0 20px;
    padding: 8px 12px;
    background: color-mix(in srgb, var(--danger) 15%, transparent);
    border: 1px solid var(--danger);
    border-radius: var(--radius);
    color: var(--danger);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .input-row {
    display: flex;
    gap: 8px;
    padding: 14px 20px 18px;
  }
  .input-row input {
    flex: 1;
  }
  .mic {
    font-size: 17px;
    padding: 8px 12px;
    user-select: none;
  }
  .mic.active {
    background: var(--danger);
    border-color: var(--danger);
  }
</style>
