// Client-/Shell-Einstellungen (localStorage). Server-seitige Dinge wie die
// Stimmenliste kommen per REST; hier liegt nur, was das Geraet betrifft.

export interface AppSettings {
  voiceId: string;
  // Push-to-Talk (Taste halten) oder Realtime Talk (Dauer-Mikrofon + VAD)
  micMode: "ptt" | "realtime";
  hotkeys: { chat: string; talk: string; screenshot: string };
  ttsFallback: boolean;
  wakeWordEnabled: boolean;
  wakeWordThreshold: number;
  autostart: boolean;
  vadSilenceMs: number;
}

const KEY = "heimai.settings";

export const DEFAULT_SETTINGS: AppSettings = {
  voiceId: "",
  micMode: "ptt",
  // Tauri-Accelerator-Syntax; in den Einstellungen aenderbar.
  hotkeys: {
    chat: "CommandOrControl+Shift+Space",
    talk: "CommandOrControl+Shift+T",
    screenshot: "CommandOrControl+Shift+S",
  },
  ttsFallback: true,
  wakeWordEnabled: false,
  wakeWordThreshold: 0.5,
  autostart: false,
  vadSilenceMs: 900,
};

export function loadSettings(): AppSettings {
  try {
    return { ...DEFAULT_SETTINGS, ...JSON.parse(localStorage.getItem(KEY) || "{}") };
  } catch {
    return { ...DEFAULT_SETTINGS };
  }
}

export function saveSettings(settings: AppSettings): void {
  localStorage.setItem(KEY, JSON.stringify(settings));
}
