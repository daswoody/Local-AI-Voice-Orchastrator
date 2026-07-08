# Protokoll-Erweiterungen Phase 2.5 (Windows-App / zentrale UI)

> Ergänzt `docs/PROTOCOL.md` im App-Repo (`Android-AI-Assistant-App`).
> Alle Erweiterungen sind **additiv und abwärtskompatibel**: die bestehende
> Android-App ignoriert unbekannte Frame-Typen (verifiziert in
> `AssistantSession.kt`) und muss nicht angepasst werden. Beim nächsten
> Update von `PROTOCOL.md` diese Abschnitte dort einpflegen.

## 1. Zentrale Chat-Historie

Der Server besitzt die Gespräche; Clients sind Ansichten (Entscheidung
Phase 2.5: "voll zentral"). Jeder Turn über den WebSocket wird
serverseitig persistiert (SQLite), inklusive der Karten des Turns.
Bild-Rohdaten (`image_input`) werden **nicht** gespeichert, nur markiert.

### Neue REST-Endpoints (Bearer-Token erforderlich)

- `GET /v1/conversations` → `{ "conversations": [ { id, title,
  device_name, message_count, created_at, updated_at } ] }`
  (nur eigene Gespräche; auch Tier 3 sieht keine fremden)
- `GET /v1/conversations/{id}` → Gespräch inkl. `messages[]`
  `{ id, role: user|assistant, content, cards[], has_image, created_at }`
- `DELETE /v1/conversations/{id}` → `{ "ok": true }`

Gast-/Satelliten-Sessions ohne Login werden persistiert (Retention/RAG,
Phase 5: nachträgliche Sprecher-Zuordnung), sind aber über REST nicht
abrufbar.

### WebSocket

- **Neues Server→Client-Frame** `conversation`:
  `{ "type": "conversation", "conversation_id": "<hex>" }` — gesendet,
  sobald das Gespräch angelegt wurde (lazy beim ersten Input) bzw. als
  Bestätigung einer Fortsetzung direkt nach dem `hello`.
- **`hello` erweitert** um optionales Feld `conversation_id`: setzt ein
  bestehendes eigenes Gespräch fort (Server prüft Besitz; ungültige IDs
  werden ignoriert und es wird ein neues Gespräch angelegt).

## 2. Bild-Eingabe (`image_input`)

Neues Client→Server-Frame für Screenshot-/Bild-Analyse (Phase 2.5/3):

```json
{ "type": "image_input", "data": "<Base64 PNG/JPEG>",
  "mime": "image/png", "text": "Was siehst du?", "speak": false }
```

- `text` optional (leer → "Beschreibe, was du siehst")
- `speak: true` → Antwort kommt zusätzlich als TTS-`audio_chunk`-Stream
  (für sprachgetriebene Flows)
- Größenlimit serverseitig (`max_image_b64_bytes`, Default 12 MB), bei
  Überschreitung `{"type":"error","message":"Bild zu gross"}`
- Das Bild geht als multimodale user-Message an LiteLLM; das aktive
  Modell muss Vision können (Gemma 4 E4B: ja)

## 3. Bild-Ergebnisse von Geräte-Tools

Konvention der Geräte-Tool-Bridge (4.13): liefert ein `tool_result` als
`result` ein JSON-Objekt `{ "image_b64": "<Base64>", "mime": "image/png" }`,
reicht der Orchestrator das Bild als multimodale user-Message in den
Agent-Loop (statt Base64 in den Tool-Text zu kippen). Damit funktioniert
der Screenshot-Dialog aus der Spez:

```
Nutzer:  "Hey AI, kannst du mir hier helfen?"
LLM:     ruft Geräte-Tool capture_screenshot auf
Windows: Screenshot → tool_result {image_b64, mime}
LLM:     sieht das Bild, antwortet (+ ggf. Detail-Karte via show_card)
```

Die Windows-App meldet dafür im `hello`-Manifest das Tool
`capture_screenshot` (parameterlos, nicht sensitiv) an.

## 4. Zentrale User-Web-UI unter `/app`

- Der Orchestrator liefert die User-UI (Svelte, `frontend/`) unter
  `GET /app/` aus — Windows-Shell und Browser laden dieselbe UI live vom
  Server; ein Server-Deploy aktualisiert alle Clients.
- `GET /app/version.json` → `{ "ui_version": N, "shell_api_version": N }`.
  `shell_api_version` deklariert, welche Shell-Befehls-API die UI
  erwartet; eine ältere Windows-Shell zeigt bei Mismatch ihre
  Bootstrap-Seite mit Update-Hinweis statt einer kaputten UI.
- Browser-WebSockets können keine `Authorization`-Header setzen; die
  Web-UI nutzt den bestehenden `?token=`-Query-Fallback des Streams.

## 5. Shell-Befehle der Windows-App (UI ↔ Shell, Tauri-IPC)

Nicht Teil des Server-Protokolls, aber der Vollständigkeit halber — die
UI ruft in der Windows-Shell auf: `get_shell_info`, `set_indicator`,
`popup_card` / `get_popup_payload` / `pin_popup` / `close_popup`,
`capture_screenshot`, `set_hotkeys`, `set_autostart`, `set_wake_word`,
`show_main_window`. Events Shell→UI: `hotkey {action}`, `wake-word`,
`indicator-state`. Versioniert über `shell_api_version` (aktuell 1).
