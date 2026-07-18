<script lang="ts">
  // Einstellungen: alles, was den Nutzer betrifft (Anforderung 2.5) -
  // Stimme (Server), Mikrofon-Modus (PTT vs. Realtime), Hotkeys, Wake Word,
  // TTS-Fallback, Autostart. Shell-Optionen erscheinen nur in der Windows-App.
  import { onMount } from "svelte";
  import { listVoices } from "../lib/api";
  import { app, applySettings } from "../lib/session.svelte";
  import { inShell } from "../lib/shell";

  let voices: { id: string; name: string }[] = $state([]);
  // Kopie bearbeiten, erst "Speichern" wendet an (inkl. Shell-Sync).
  let draft = $state({ ...app.settings, hotkeys: { ...app.settings.hotkeys } });
  let saved = $state(false);

  onMount(async () => {
    try {
      voices = (await listVoices()).voices;
    } catch {
      voices = [];
    }
  });

  function save(event: SubmitEvent) {
    event.preventDefault();
    applySettings({ ...draft, hotkeys: { ...draft.hotkeys } });
    saved = true;
    setTimeout(() => (saved = false), 2000);
  }
</script>

<form class="settings" onsubmit={save}>
  <h2>Einstellungen</h2>

  <section>
    <h3>AI &amp; Stimme</h3>
    <label for="voice">Stimme</label>
    <select id="voice" bind:value={draft.voiceId}>
      <option value="">Standard (vom Server)</option>
      {#each voices as voice (voice.id)}
        <option value={voice.id}>{voice.name}</option>
      {/each}
    </select>
    <p class="hint">Der Charakter (System-Prompt) wird zentral im <a href="/admin/" target="_blank">Admin-Panel</a> verwaltet.</p>

    <label for="micmode">Sprachmodus</label>
    <select id="micmode" bind:value={draft.micMode}>
      <option value="ptt">Push-to-Talk (Taste halten)</option>
      <option value="realtime">Realtime Talk (Dauer-Mikrofon mit Sprechpausen-Erkennung)</option>
    </select>

    <label for="silence">Realtime: Stille bis Äußerungs-Ende (ms)</label>
    <input id="silence" type="number" min="300" max="3000" step="100" bind:value={draft.vadSilenceMs} />

    <label class="check">
      <input type="checkbox" bind:checked={draft.ttsFallback} />
      Antwort lokal vorlesen, wenn der Server kein Audio liefert (TTS-Fallback)
    </label>
  </section>

  {#if inShell}
    <section>
      <h3>Hotkeys (global)</h3>
      <label for="hk-chat">Chat öffnen</label>
      <input id="hk-chat" bind:value={draft.hotkeys.chat} />
      <label for="hk-talk">Realtime Voice starten/stoppen</label>
      <input id="hk-talk" bind:value={draft.hotkeys.talk} />
      <label for="hk-shot">Screenshot an die AI senden</label>
      <input id="hk-shot" bind:value={draft.hotkeys.screenshot} />
      <p class="hint">Syntax: z. B. <code>CommandOrControl+Shift+Space</code>, <code>Alt+F9</code></p>
    </section>

    <section>
      <h3>Wake Word &amp; System</h3>
      <label class="check">
        <input type="checkbox" bind:checked={draft.wakeWordEnabled} />
        Wake Word aktiv (openWakeWord, läuft lokal in der Windows-App)
      </label>
      <label for="ww-model">Wake Word</label>
      <select id="ww-model" bind:value={draft.wakeWordModel}>
        <option value="hey_jarvis">„Hey Jarvis" (Standard)</option>
        <option value="alexa">„Alexa"</option>
        <option value="hey_mycroft">„Hey Mycroft"</option>
        <option value="custom">Eigenes Modell (aus dem Modell-Ordner)</option>
      </select>
      <p class="hint">Die Modelle sind in der Windows-App enthalten — kein Download nötig. Eigene Modelle: <code>.onnx</code> nach <code>%APPDATA%\de.heimai.windows\openwakeword</code> legen und „Eigenes Modell" wählen.</p>
      <label for="ww-threshold">Wake-Word-Empfindlichkeit (0–1, höher = strenger)</label>
      <input id="ww-threshold" type="number" min="0.1" max="0.95" step="0.05" bind:value={draft.wakeWordThreshold} />
      <label class="check">
        <input type="checkbox" bind:checked={draft.autostart} />
        Mit Windows starten (im System-Tray)
      </label>
    </section>
  {/if}

  <div class="actions">
    <button class="primary" type="submit">Speichern</button>
    {#if saved}<span class="saved">✓ Gespeichert</span>{/if}
  </div>
</form>

<style>
  .settings {
    max-width: 560px;
    margin: 0 auto;
    padding: 26px 20px 40px;
    overflow-y: auto;
    height: 100%;
  }
  h2 {
    margin: 0 0 8px;
  }
  section {
    background: var(--bg-raised);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px 18px;
    margin-top: 14px;
  }
  h3 {
    margin: 0;
    font-size: 15px;
  }
  .check {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--text);
    font-size: 14px;
    margin-top: 14px;
  }
  .check input {
    width: auto;
  }
  .hint {
    color: var(--text-muted);
    font-size: 12px;
    margin: 6px 0 0;
  }
  .hint a {
    color: var(--primary);
  }
  code {
    background: var(--bg-input);
    padding: 1px 5px;
    border-radius: 4px;
  }
  .actions {
    margin-top: 18px;
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .saved {
    color: var(--ok);
  }
</style>
