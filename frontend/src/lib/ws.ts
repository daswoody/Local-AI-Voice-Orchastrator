// WebSocket-Session gegen /v1/assistant/stream (docs/PROTOCOL.md).
// Browser-WebSockets koennen keine Authorization-Header setzen, deshalb
// nutzt die Web-UI den ?token=-Fallback des Orchestrators.

import type { CardEnvelope } from "./api";
import { token } from "./api";

export type Mode = "chat" | "talk" | "assist";

export interface SessionCallbacks {
  onTranscript?: (text: string, final: boolean) => void;
  onAssistantText?: (text: string, final: boolean) => void;
  onAudioChunk?: (pcm: Int16Array, sampleRate: number) => void;
  onAudioEnd?: () => void;
  onCard?: (card: CardEnvelope) => void;
  onToolCall?: (callId: string, name: string, args: Record<string, unknown>) => void;
  onConversation?: (conversationId: string) => void;
  onDone?: () => void;
  onError?: (message: string) => void;
  onClose?: () => void;
}

export interface DeviceToolSpec {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  sensitive: boolean;
}

function b64ToInt16(b64: string): Int16Array {
  const raw = atob(b64);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return new Int16Array(bytes.buffer);
}

export function int16ToB64(pcm: Int16Array): string {
  const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  let binary = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

export class AssistantSession {
  private ws: WebSocket | null = null;
  private callbacks: SessionCallbacks;

  constructor(callbacks: SessionCallbacks) {
    this.callbacks = callbacks;
  }

  get connected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  connect(options: {
    mode: Mode;
    voiceId?: string;
    conversationId?: string;
    deviceName: string;
    tools: DeviceToolSpec[];
  }): Promise<void> {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const query = token() ? `?token=${encodeURIComponent(token()!)}` : "";
    const url = `${scheme}://${location.host}/v1/assistant/stream${query}`;

    return new Promise((resolve, reject) => {
      const ws = new WebSocket(url);
      this.ws = ws;

      ws.onopen = () => {
        this.send({
          type: "hello",
          mode: options.mode,
          voice_id: options.voiceId,
          conversation_id: options.conversationId,
          device: {
            platform: "web",
            name: options.deviceName,
            app_version: "0.1.0",
          },
          capabilities: { audio_in: true, audio_out: true, cards: true },
          tools: options.tools,
        });
        resolve();
      };
      ws.onerror = () => reject(new Error("WebSocket-Verbindung fehlgeschlagen"));
      ws.onclose = () => this.callbacks.onClose?.();
      ws.onmessage = (event) => this.dispatch(JSON.parse(event.data));
    });
  }

  close(): void {
    this.ws?.close();
    this.ws = null;
  }

  private send(frame: Record<string, unknown>): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(frame));
  }

  sendText(text: string): void {
    this.send({ type: "text_input", text });
  }

  sendAudioChunk(pcm: Int16Array): void {
    this.send({ type: "audio_chunk", data: int16ToB64(pcm) });
  }

  endAudio(): void {
    this.send({ type: "audio_end" });
  }

  /** Screenshot-/Bild-Analyse (Protokoll-Erweiterung 2.5). */
  sendImage(b64: string, mime: string, text: string, speak: boolean): void {
    this.send({ type: "image_input", data: b64, mime, text, speak });
  }

  interrupt(): void {
    this.send({ type: "interrupt" });
  }

  sendToolResult(callId: string, ok: boolean, result: unknown): void {
    this.send({ type: "tool_result", call_id: callId, ok, result });
  }

  private dispatch(frame: Record<string, never>): void {
    switch (frame["type"] as string) {
      case "transcript":
        this.callbacks.onTranscript?.(frame["text"] ?? "", frame["final"] ?? false);
        break;
      case "assistant_text":
        this.callbacks.onAssistantText?.(frame["text"] ?? "", frame["final"] ?? false);
        break;
      case "audio_chunk":
        this.callbacks.onAudioChunk?.(
          b64ToInt16(frame["data"] ?? ""),
          (frame["sample_rate"] as number) ?? 24000,
        );
        break;
      case "audio_end":
        this.callbacks.onAudioEnd?.();
        break;
      case "card":
        this.callbacks.onCard?.(frame["card"]);
        break;
      case "tool_call":
        this.callbacks.onToolCall?.(
          frame["call_id"],
          frame["name"],
          (frame["arguments"] as Record<string, unknown>) ?? {},
        );
        break;
      case "conversation":
        this.callbacks.onConversation?.(frame["conversation_id"]);
        break;
      case "done":
        this.callbacks.onDone?.();
        break;
      case "error":
        this.callbacks.onError?.(frame["message"] ?? "Unbekannter Fehler");
        break;
      // session + unbekannte Frames: bewusst ignorieren (vorwaertskompatibel,
      // gleiches Verhalten wie die Android-App).
    }
  }
}
