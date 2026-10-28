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

## 6. Tool-Aktivität (`tool_activity`, additiv — v1.12.1)

Server → Client, rein informativ: zeigt an, welches Tool bzw. welcher
Agent (Tool-Name-Präfix `agent-`) gerade läuft. Clients, die das Frame
nicht kennen (aktuelle Android-App), ignorieren es folgenlos.

```json
{ "type": "tool_activity", "tool": "agent-websuche", "status": "running" }
{ "type": "tool_activity", "tool": "agent-websuche", "status": "done" }
```

`status`: `running` → `done` | `error`. `show_card` erzeugt bewusst kein
Aktivitäts-Frame (die Karte selbst ist die Anzeige).

Persistenz: Der Endzustand des Turns hängt als `tools`-Liste
(`[{tool, status}]`) an der Assistant-Message in
`GET /v1/conversations/{id}` — der Verlauf zeigt damit dieselben Chips
wie der Live-Turn.

## 7. Auth: Geräte-Tokens + strikte Token-Prüfung am WebSocket (v1.13.1)

- `POST /v1/auth/login` liefert jetzt ein **langlebiges Geräte-Token**
  (Prefix `hda_`, kein Ablauf) statt eines 7-Tage-JWT. Für Clients ändert
  sich nichts an der Nutzung (Bearer-Header wie bisher); alte JWTs bleiben
  bis zu ihrem Ablauf gültig. Tokens sind im Admin-Panel pro Gerät
  widerrufbar; ein Passwort-Reset widerruft alle Geräte des Nutzers.
- **WebSocket:** Ein mitgeschicktes, aber ungültiges/widerrufenes Token
  führt jetzt zu einem `error`-Frame („Token ungueltig oder abgelaufen -
  bitte neu anmelden") und **Close mit Code 4401** — vorher wurde still
  auf Gast heruntergestuft. Clients sollten auf Close 4401 mit einem
  Re-Login-Dialog reagieren. Verbindungen **ohne** Token bleiben als
  Gast-Session erlaubt (Satelliten-Szenario).

## 8. Hintergrund-Agenten (additiv — v1.29)

Agenten-Aufrufe (`agent-<slug>`, außer `code-card`) blockieren den Turn
nicht mehr: Der Server startet den Agenten neben dem Gespräch, der Turn
endet wie gewohnt mit `done`, und das Ergebnis fließt in eine spätere
Antwort ein.

**`tool_activity` mit `job_id`:** Der Start kommt im Turn mit dem neuen
Status `background`, das Ende irgendwann später — auch zwischen zwei
Turns — als `done` bzw. `error` mit derselben `job_id`:

```json
{ "type": "tool_activity", "tool": "agent-routen", "status": "background", "job_id": "<hex>" }
{ "type": "tool_activity", "tool": "agent-routen", "status": "done", "job_id": "<hex>" }
```

Clients aktualisieren den Chip mit dieser `job_id`, egal in welchem Turn
er steht, und verwerfen das End-Frame auch nach einem `interrupt` nicht
(es gehört zu keinem Turn). In `GET /v1/conversations/{id}` trägt der
Chip in `tools` ebenfalls die `job_id` und nach dem Agenten-Ende den
Endstatus (`error` auch bei Verbindungsabbruch — dann wird der Agent
abgebrochen).

**Proaktive Meldung:** Sagt der Nutzer nach dem Agenten-Ende eine Weile
nichts (Admin-Panel, Default 20 s, 0 = nie), schickt der Server von sich
aus einen Turn: `assistant_text` (ggf. mit Deltas), `audio_chunk`s, wenn
die letzte Eingabe gesprochen war, und `done` — **ohne** vorherige
Nutzereingabe. Clients müssen also Antworten außerhalb eines eigenen
Requests annehmen (Web-UI: ja; Android-App: prüfen, sonst im Panel 0
einstellen). Jede Eingabe (`text_input`, `audio_chunk`, `audio_end`,
`image_input`) hält die proaktive Meldung an; das Ergebnis kommt dann in
die Antwort auf diese Eingabe. In der Historie steht die proaktive
Meldung als Assistant-Message ohne User-Message davor.
