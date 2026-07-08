// REST-Client gegen den Orchestrator (docs/PROTOCOL.md). Die UI wird vom
// selben Server ausgeliefert (/app), daher sind alle Pfade relativ -
// funktioniert identisch im Browser und in der Windows-Shell.

export interface UserInfo {
  name: string;
  tier: number;
}

export interface ConversationSummary {
  id: string;
  title: string;
  device_name: string;
  message_count: number;
  created_at: string;
  updated_at: string;
}

export interface HistoryMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  cards: CardEnvelope[];
  has_image: boolean;
  created_at: string;
}

export interface CardEnvelope {
  type: string;
  version?: number;
  title?: string;
  data: Record<string, unknown>;
}

export interface CardLayout {
  card_type: string;
  layout_version: number;
  root: Record<string, unknown>;
}

let authToken: string | null = localStorage.getItem("heimai.token");

export function token(): string | null {
  return authToken;
}

export function setToken(value: string | null): void {
  authToken = value;
  if (value === null) localStorage.removeItem("heimai.token");
  else localStorage.setItem("heimai.token", value);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init.headers as Record<string, string>),
  };
  if (authToken) headers["Authorization"] = `Bearer ${authToken}`;
  const response = await fetch(path, { ...init, headers });
  if (response.status === 401) {
    // Token abgelaufen -> zurueck zum Login (App reagiert auf das Event).
    setToken(null);
    window.dispatchEvent(new CustomEvent("heimai:logout"));
    throw new Error("Sitzung abgelaufen");
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || detail.error || `HTTP ${response.status}`);
  }
  return response.json();
}

export async function login(
  username: string,
  password: string,
  deviceName: string,
): Promise<{ token: string; user: UserInfo }> {
  const result = await request<{ token: string; user: UserInfo }>("/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password, device_name: deviceName }),
  });
  setToken(result.token);
  localStorage.setItem("heimai.user", JSON.stringify(result.user));
  return result;
}

export function storedUser(): UserInfo | null {
  const raw = localStorage.getItem("heimai.user");
  return raw ? (JSON.parse(raw) as UserInfo) : null;
}

export function logout(): void {
  setToken(null);
  localStorage.removeItem("heimai.user");
}

export const listVoices = () =>
  request<{ voices: { id: string; name: string; language: string }[] }>("/v1/voices");

export const listConversations = () =>
  request<{ conversations: ConversationSummary[] }>("/v1/conversations");

export const getConversation = (id: string) =>
  request<{ id: string; title: string; messages: HistoryMessage[] }>(
    `/v1/conversations/${id}`,
  );

export const deleteConversation = (id: string) =>
  request<{ ok: boolean }>(`/v1/conversations/${id}`, { method: "DELETE" });

export const fetchCardLayouts = (sinceVersion: number) =>
  request<{ version: number; layouts: CardLayout[] }>(
    `/v1/cards/layouts?since_version=${sinceVersion}`,
  );
