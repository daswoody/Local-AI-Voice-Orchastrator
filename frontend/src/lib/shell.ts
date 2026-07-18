// Bruecke zur Windows-Shell (Tauri 2). Die Shell injiziert window.__TAURI__
// (withGlobalTauri); im normalen Browser fehlen die nativen Faehigkeiten,
// alle Aufrufe degradieren dann zu No-Ops. So bleibt die UI EIN Artefakt
// fuer beide Welten ("voll zentral", Phase 2.5).

export type IndicatorState = "hidden" | "listening" | "thinking" | "speaking";

export interface ShellInfo {
  shell_version: string;
  api_version: number;
  platform: string;
}

interface TauriGlobal {
  core: { invoke: (cmd: string, args?: Record<string, unknown>) => Promise<unknown> };
  event: {
    listen: (
      event: string,
      handler: (event: { payload: unknown }) => void,
    ) => Promise<() => void>;
  };
}

const tauri: TauriGlobal | undefined = (window as never as Record<string, TauriGlobal>)[
  "__TAURI__"
];

export const inShell = tauri !== undefined;

async function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T | null> {
  if (!tauri) return null;
  try {
    return (await tauri.core.invoke(cmd, args)) as T;
  } catch (error) {
    console.warn(`Shell-Befehl ${cmd} fehlgeschlagen:`, error);
    return null;
  }
}

export const shellInfo = () => invoke<ShellInfo>("get_shell_info");

/** Schwebender Voice-Indikator oben mittig (eigenes Topmost-Fenster).
 *  Key indicatorState: Tauri mappt camelCase-Args auf snake_case-Parameter
 *  (indicator_state in commands.rs). */
export const setIndicator = (state: IndicatorState) =>
  invoke("set_indicator", { indicatorState: state });

/** Antwort-Popup ueber allen Anwendungen, anpinnbar (eigenes Fenster,
 *  rendert dieselbe Karten-UI unter #/popup). */
export const popupCard = (payload: {
  title: string;
  body: string;
  card?: unknown;
}) => invoke("popup_card", { payload: JSON.stringify(payload) });

/** Vom Popup-Fenster selbst aufgerufen. */
export const getPopupPayload = (id: string) =>
  invoke<string>("get_popup_payload", { id });
export const closePopup = (id: string) => invoke("close_popup", { id });
export const pinPopup = (id: string) => invoke("pin_popup", { id });

/** Desktop-Screenshot (PNG, Base64) - Phase 2.5 Screenshot-Flow.
 *  Wirft bei Fehlern (statt still null zu liefern), damit die echte
 *  Ursache - z. B. eine IPC-Ablehnung - beim Nutzer ankommt. */
export async function captureScreenshot(): Promise<{ b64: string; mime: string }> {
  if (!tauri) throw new Error("Screenshot ist nur in der Windows-App verfuegbar");
  return (await tauri.core.invoke("capture_screenshot")) as { b64: string; mime: string };
}

/** Globale Hotkeys registrieren; Aktionen kommen als "hotkey"-Event zurueck. */
export const setHotkeys = (hotkeys: { chat: string; talk: string; screenshot: string }) =>
  invoke("set_hotkeys", { hotkeys });

export const setAutostart = (enabled: boolean) => invoke("set_autostart", { enabled });

/** Wake Word (openWakeWord/ONNX in der Shell) ein-/ausschalten. */
export const setWakeWord = (enabled: boolean, threshold: number) =>
  invoke("set_wake_word", { enabled, threshold });

/** Hauptfenster in den Vordergrund holen (z. B. nach Hotkey/Wake Word). */
export const showMainWindow = () => invoke("show_main_window");

/** Events der Shell (Hotkeys, Wake Word, Indikator-Zustand) abonnieren. */
export function onShellEvent(
  event: "hotkey" | "wake-word" | "indicator-state",
  handler: (payload: unknown) => void,
): void {
  tauri?.event.listen(event, (e) => handler(e.payload));
}
