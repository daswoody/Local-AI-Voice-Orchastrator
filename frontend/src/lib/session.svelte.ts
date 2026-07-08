// Globaler App-Zustand (Svelte-5-Runes) + Assistant-Logik: WebSocket-Session,
// Mikrofon (PTT/Realtime/Assist), Audio-Wiedergabe, Karten, Geraete-Tools
// (capture_screenshot ueber die Shell) und Popups/Indikator der Shell.

import type { CardEnvelope } from "./api";
import { storedUser } from "./api";
import { MicCapture, StreamPlayer, VoiceActivity } from "./audio";
import * as shell from "./shell";
import { loadSettings, saveSettings, type AppSettings } from "./settings";
import { AssistantSession, type DeviceToolSpec } from "./ws";

export interface UiMessage {
  id: number;
  role: "user" | "assistant" | "card";
  text: string;
  card?: CardEnvelope;
  final: boolean;
}

export const app = $state({
  messages: [] as UiMessage[],
  partialTranscript: "",
  conversationId: undefined as string | undefined,
  listening: false,
  thinking: false,
  speaking: false,
  realtime: false,
  micLevel: 0,
  error: "",
  settings: loadSettings(),
});

let nextId = 1;
let session: AssistantSession | null = null;
let audioThisTurn = false; // Eingabe kam per Mikrofon (fuer TTS-Fallback)
let serverAudioThisTurn = false;
let assistOneShot = false; // Wake-Word-/Hotkey-Assist: nach einem Turn beenden
let currentAssistantText = "";

const player = new StreamPlayer((speaking) => {
  app.speaking = speaking;
  updateIndicator();
});

const vad = new VoiceActivity(0.015, app.settings.vadSilenceMs);
let utteranceActive = false;

const mic = new MicCapture(
  (pcm) => {
    if (utteranceActive) session?.sendAudioChunk(pcm);
  },
  (rms) => {
    app.micLevel = rms;
    if (!app.realtime) return;
    const change = vad.update(rms);
    if (change === "start") {
      // Barge-in: Nutzer spricht, waehrend die Antwort laeuft.
      if (app.speaking || app.thinking) {
        player.stop();
        session?.interrupt();
      }
      utteranceActive = true;
      app.listening = true;
      updateIndicator();
    } else if (change === "end" && utteranceActive) {
      utteranceActive = false;
      app.listening = false;
      app.thinking = true;
      audioThisTurn = true;
      session?.endAudio();
      updateIndicator();
    }
  },
);

function updateIndicator(): void {
  if (!shell.inShell) return;
  const state = app.listening
    ? "listening"
    : app.thinking
      ? "thinking"
      : app.speaking
        ? "speaking"
        : "hidden";
  void shell.setIndicator(state);
}

function deviceName(): string {
  return shell.inShell ? "Windows-PC" : "Browser";
}

/** Geraete-Tools der Windows-Shell (4.13). Im Browser: keine. */
function deviceTools(): DeviceToolSpec[] {
  if (!shell.inShell) return [];
  return [
    {
      name: "capture_screenshot",
      description:
        "Nimmt einen Screenshot des Windows-Desktops auf und haengt ihn als Bild an, " +
        "damit du siehst, was der Nutzer gerade sieht. Nutze das Tool, wenn der Nutzer " +
        "Hilfe zu etwas auf seinem Bildschirm braucht (Spiel, Anwendung, Fehlermeldung).",
      parameters: { type: "object", properties: {} },
      sensitive: false,
    },
  ];
}

async function handleToolCall(
  callId: string,
  name: string,
  _args: Record<string, unknown>,
): Promise<void> {
  if (name === "capture_screenshot") {
    try {
      const shot = await shell.captureScreenshot();
      // Konvention der Bild-Tool-Ergebnisse (Server: _extract_image_data_url)
      session?.sendToolResult(callId, true, { image_b64: shot.b64, mime: shot.mime });
    } catch (error) {
      session?.sendToolResult(callId, false, `Screenshot fehlgeschlagen: ${error}`);
    }
    return;
  }
  session?.sendToolResult(callId, false, `Unbekanntes Tool: ${name}`);
}

function pushMessage(message: Omit<UiMessage, "id">): number {
  const id = nextId++;
  app.messages.push({ ...message, id });
  return id;
}

let assistantMsgId: number | null = null;

function onAssistantText(text: string, final: boolean): void {
  if (final) {
    const full = text || currentAssistantText;
    if (assistantMsgId === null) {
      assistantMsgId = pushMessage({ role: "assistant", text: full, final: true });
    } else {
      const msg = app.messages.find((m) => m.id === assistantMsgId);
      if (msg) {
        msg.text = full;
        msg.final = true;
      }
    }
    notifyIfHidden(full);
    currentAssistantText = "";
    assistantMsgId = null;
  } else {
    currentAssistantText += text;
    if (assistantMsgId === null) {
      assistantMsgId = pushMessage({ role: "assistant", text: currentAssistantText, final: false });
    } else {
      const msg = app.messages.find((m) => m.id === assistantMsgId);
      if (msg) msg.text = currentAssistantText;
    }
  }
}

/** Ist die App im Tray (Fenster versteckt), erscheinen Antworten als
 *  anpinnbares Popup ueber allen Anwendungen (Anforderung 2.5). */
function notifyIfHidden(text: string, card?: CardEnvelope): void {
  if (!shell.inShell || !document.hidden) return;
  void shell.popupCard({
    title: "Heim-AI",
    body: text.length > 300 ? `${text.slice(0, 300)}…` : text,
    card,
  });
}

function ttsFallback(): void {
  // TTS-Fallback-Regel (4.13): nur bei Mikrofon-Eingabe ohne Server-Audio.
  if (!app.settings.ttsFallback || !audioThisTurn || serverAudioThisTurn) return;
  const last = [...app.messages].reverse().find((m) => m.role === "assistant");
  if (!last?.text) return;
  const utterance = new SpeechSynthesisUtterance(last.text);
  utterance.lang = "de-DE";
  speechSynthesis.speak(utterance);
}

export async function ensureSession(): Promise<void> {
  if (session?.connected) return;
  session = new AssistantSession({
    onTranscript: (text, final) => {
      if (final) {
        app.partialTranscript = "";
        if (text.trim()) pushMessage({ role: "user", text, final: true });
      } else {
        app.partialTranscript = text;
      }
    },
    onAssistantText,
    onAudioChunk: (pcm, rate) => {
      serverAudioThisTurn = true;
      app.thinking = false;
      player.play(pcm, rate);
      updateIndicator();
    },
    onAudioEnd: () => undefined,
    onCard: (card) => {
      pushMessage({ role: "card", text: "", card, final: true });
      notifyIfHidden(card.title || "Neue Karte", card);
    },
    onToolCall: (callId, name, args) => void handleToolCall(callId, name, args),
    onConversation: (conversationId) => {
      app.conversationId = conversationId;
    },
    onDone: () => {
      app.thinking = false;
      ttsFallback();
      audioThisTurn = false;
      serverAudioThisTurn = false;
      updateIndicator();
      if (assistOneShot && !utteranceActive) {
        assistOneShot = false;
        void stopRealtime();
      }
    },
    onError: (message) => {
      app.error = message;
      app.thinking = false;
      updateIndicator();
    },
    onClose: () => {
      app.thinking = false;
      app.listening = false;
    },
  });

  await session.connect({
    mode: app.realtime ? "talk" : "chat",
    voiceId: app.settings.voiceId || undefined,
    conversationId: app.conversationId,
    deviceName: deviceName(),
    tools: deviceTools(),
  });
}

export function closeSession(): void {
  session?.close();
  session = null;
}

export async function sendText(text: string): Promise<void> {
  if (!text.trim()) return;
  await ensureSession();
  pushMessage({ role: "user", text, final: true });
  app.thinking = true;
  audioThisTurn = false;
  session!.sendText(text);
}

/** Push-to-Talk: Taste/Button halten. */
export async function startPtt(): Promise<void> {
  await ensureSession();
  player.stop();
  session!.interrupt();
  await mic.start();
  utteranceActive = true;
  audioThisTurn = true;
  app.listening = true;
  updateIndicator();
}

export function stopPtt(): void {
  if (!utteranceActive) return;
  utteranceActive = false;
  mic.stop();
  app.listening = false;
  app.thinking = true;
  session?.endAudio();
  updateIndicator();
}

/** Realtime Talk: Dauer-Mikrofon, VAD segmentiert die Aeusserungen. */
export async function startRealtime(oneShot = false): Promise<void> {
  if (app.realtime) return;
  assistOneShot = oneShot;
  app.realtime = true;
  vad.reset();
  await ensureSession();
  await mic.start();
  updateIndicator();
}

export async function stopRealtime(): Promise<void> {
  app.realtime = false;
  app.listening = false;
  utteranceActive = false;
  mic.stop();
  updateIndicator();
}

export async function toggleRealtime(): Promise<void> {
  if (app.realtime) await stopRealtime();
  else await startRealtime();
}

/** Screenshot-Flow auf Kommando (Hotkey/Button): Bild aufnehmen und mit
 *  optionaler Frage als image_input schicken. */
export async function sendScreenshot(question: string): Promise<void> {
  let shot: { b64: string; mime: string };
  try {
    shot = await shell.captureScreenshot();
  } catch (error) {
    app.error = error instanceof Error ? error.message : String(error);
    return;
  }
  await ensureSession();
  pushMessage({
    role: "user",
    text: question.trim() ? `📸 ${question}` : "📸 Screenshot gesendet",
    final: true,
  });
  app.thinking = true;
  session!.sendImage(shot.b64, shot.mime, question, audioThisTurn);
}

export function newChat(): void {
  closeSession();
  app.messages = [];
  app.conversationId = undefined;
  app.partialTranscript = "";
  app.error = "";
}

export function loadIntoChat(
  conversationId: string,
  history: { role: string; content: string; cards: CardEnvelope[] }[],
): void {
  closeSession();
  app.messages = [];
  app.conversationId = conversationId;
  for (const message of history) {
    if (message.content) {
      pushMessage({
        role: message.role === "user" ? "user" : "assistant",
        text: message.content,
        final: true,
      });
    }
    for (const card of message.cards) {
      pushMessage({ role: "card", text: "", card, final: true });
    }
  }
}

export function applySettings(settings: AppSettings): void {
  app.settings = settings;
  saveSettings(settings);
  if (shell.inShell) {
    void shell.setHotkeys(settings.hotkeys);
    void shell.setAutostart(settings.autostart);
    void shell.setWakeWord(settings.wakeWordEnabled, settings.wakeWordThreshold);
  }
}

/** Shell-Events (Hotkeys, Wake Word) verdrahten - einmal beim App-Start. */
export function initShellIntegration(): void {
  if (!shell.inShell) return;
  void shell.setHotkeys(app.settings.hotkeys);
  void shell.setWakeWord(app.settings.wakeWordEnabled, app.settings.wakeWordThreshold);

  shell.onShellEvent("hotkey", (payload) => {
    const action = (payload as { action: string }).action;
    if (action === "chat") {
      void shell.showMainWindow();
    } else if (action === "talk") {
      void toggleRealtime();
    } else if (action === "screenshot") {
      void sendScreenshot("");
    }
  });

  shell.onShellEvent("wake-word", () => {
    // Wake Word -> Assist-Modus: einmal zuhoeren, antworten, fertig.
    if (!app.realtime && storedUser()) void startRealtime(true);
  });
}
