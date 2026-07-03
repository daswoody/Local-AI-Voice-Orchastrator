# Voice-Orchestrator (Mikro-Phasen 1.7, 1.7b-d, 1.11)

Siehe `../docs/heim-ai-projektspezifikation.md` fuer den Gesamtkontext.
Die Nachbar-Services der Voice-Pipeline liegen im selben Repo:
`../stt-service` (1.8, faster-whisper), `../tts-piper` (1.9, Filler),
`../tts-xtts` (1.10, Hauptstimme).

## Admin-Panel (/admin)

Verwaltungsoberflaeche unter `https://<domain>/admin/` (statisches
Vanilla-JS, kein Build-Step - Entscheidung siehe Spezifikation 4.14).
Login mit einem Tier-3-Konto; beim allerersten Start wird der Seed-Admin
aus `ADMIN_USERNAME`/`ADMIN_PASSWORD` in die leere DB geschrieben.

| Bereich | Funktion |
|---|---|
| Modelle | LM-Studio-Modelle anzeigen, laden/entladen (Hot-Swap, native REST-API >= 0.4.0) |
| Charakter | Globaler System-Prompt; pro Nutzer ueberschreibbar (Nutzer-Formular) |
| Nutzer | Anlegen/Bearbeiten/Loeschen, Tier 1-3, Standard-Stimme, Charakter-Override |
| Stimmen | Anlegen + WAV-Sample-Upload (landet im XTTS-Voices-Volume, kein docker cp mehr) |
| Filler & Trigger | Eigene Trigger (Nachdenken/Suche/Tool inkl. Tool-Muster wie `Calendar-*`), Filler mit Titel+Text, "Audio generieren" rendert sie per XTTS pro Stimme vor |
| Karten | Layout-Templates (4.12) anlegen/bearbeiten/loeschen, Version zaehlt automatisch hoch |

Ablauf fuer die erste Stimme: Stimme anlegen -> Sample hochladen (6-30s
sauberes Deutsch) -> unter "Filler & Trigger" bei jedem Filler "Audio
generieren" klicken. Ab dann spielt der Orchestrator Filler in der
XTTS-Stimme aus dem Cache; ohne generiertes Audio faellt er auf Piper
zurueck, ohne Piper laeuft die Antwort einfach ohne Filler.

## 1. Lokal testen (ohne echtes LiteLLM/Weaviate)

```bash
cd orchestrator
uv sync --extra dev
uv run pytest
```

Die Tests mocken LiteLLM und Weaviate, pruefen also nur die Orchestrator-Logik
selbst (Login, Karten-Versionierung, WebSocket-Roundtrip).

Server manuell starten und interaktiv ausprobieren:

```bash
uv run uvicorn orchestrator.main:app --reload
```

Dann im Browser **http://localhost:8000/docs** oeffnen - FastAPI generiert dort
automatisch eine Swagger-UI, in der sich `/v1/health`, `/v1/auth/login`,
`/v1/voices` und `/v1/cards/layouts` direkt per Klick testen lassen, ohne
`curl`.

Der WebSocket laesst sich damit nicht testen (Swagger kann kein WS). Dafuer:

```bash
uv run python scripts/manual_ws_test.py "Wie spaet ist es?"
```

Ohne echtes LiteLLM/Weaviate schlaegt der LLM-Call fehl (Verbindung
verweigert) - das ist erwartet, solange `.env` nicht auf eure echte
Infrastruktur zeigt. Die Weaviate-Abfrage selbst scheitert dabei bewusst
"weich": Sie wird geloggt und uebersprungen, der Rest des Flows laeuft weiter.

### Kompletter Voice-Loop ohne GPU (Fake-Backends)

`scripts/dev_fake_services.py` stellt LiteLLM, STT, Piper und XTTS auf einem
Port nach (inkl. simulierter LLM-Latenz, damit die Filler-Logik sichtbar wird):

```bash
# Terminal 1: Fake-Backends
uv run python scripts/dev_fake_services.py

# Terminal 2: Orchestrator dagegen starten
LITELLM_BASE_URL=http://127.0.0.1:9100/llm \
STT_BASE_URL=http://127.0.0.1:9100/stt \
PIPER_BASE_URL=http://127.0.0.1:9100/piper \
XTTS_BASE_URL=http://127.0.0.1:9100/xtts \
WEAVIATE_URL=http://127.0.0.1:9100/weaviate-gibtsnicht \
FILLER_DELAY_MS=800 \
uv run uvicorn orchestrator.main:app --port 8000

# Terminal 3: Audio-Roundtrip fahren
uv run python scripts/manual_audio_ws_test.py
```

Erwartete Frame-Reihenfolge: `transcript` -> `audio_chunk` (Filler, VOR dem
Text!) -> `assistant_text` -> `audio_chunk`(s) -> `audio_end` -> `done`.

## 2. Mit Docker lokal bauen (Vorab-Check vor Coolify)

```bash
docker build -t heimai-orchestrator .
docker run --rm -p 8000:8000 --env-file .env heimai-orchestrator
curl http://localhost:8000/v1/health
```

Falls kein Docker lokal verfuegbar ist: Coolify baut das Image beim Deploy
ohnehin selbst aus demselben `Dockerfile` - dieser Schritt ist nur ein
schnellerer Vorab-Check.

## 3. Deployment via Coolify

Empfohlen: **Docker-Compose-Resource** (nicht die einfache "Application"),
weil `docker-compose.yml` explizit das bestehende `ai-lab`-Netzwerk einbindet
(4.6/4.7 der Spezifikation) - so kann der Orchestrator LiteLLM/Weaviate ueber
ihre Compose-Service-Namen erreichen, genau wie die anderen Container.

1. **Coolify -> New Resource -> Docker Compose**, GitHub-Repo
   `daswoody/Local-AI-Voice-Orchastrator` verbinden, Branch waehlen.
2. **Base Directory** auf `/orchestrator` setzen (dort liegen
   `docker-compose.yml` und `Dockerfile`).
3. **Netzwerk-Name pruefen:** `docker network ls` auf der AI-VM ausfuehren
   und nachsehen, wie das Netzwerk heisst, in dem `litellm`/`weaviate` laufen.
   Falls es nicht `ai-lab` heisst, in `docker-compose.yml` anpassen.
4. **Environment Variables** in der Coolify-UI setzen (siehe `.env.example`).
   Fuer `LITELLM_BASE_URL` reicht alternativ auch `http://litellm.ai.lab`
   (per Traefik, ohne Port - siehe Sektion 7/8 der Spezifikation), falls das
   gemeinsame Docker-Netzwerk aus irgendeinem Grund nicht klappt. Fuer
   Weaviate gibt es aktuell keinen `*.ai.lab`-Hostnamen - entweder das
   Netzwerk gemeinsam nutzen oder in Coolify/nginx-proxy-manager einen
   Eintrag dafuer anlegen.
5. **Port:** 8000 (intern), Coolify vergibt Domain/Reverse-Proxy automatisch.
6. **Deploy.** Danach von aussen testen:
   ```bash
   curl https://<eure-coolify-domain>/v1/health
   uv run python scripts/manual_ws_test.py "Test" --url wss://<eure-coolify-domain>/v1/assistant/stream
   ```
7. **Aus der Android-App testen:** Server-Auswahl-Screen -> die Coolify-Domain
   eintragen -> Health-Check sollte gruen sein -> Login (`ADMIN_USERNAME`/
   `ADMIN_PASSWORD` aus der `.env`) -> Text-Chat ausprobieren.

## Hinweise fuer den Voice-Stack auf der VM

- Die Compose-Datei deployt alle vier Services zusammen; STT und XTTS
  reservieren die GPU (`deploy.resources`). Dafuer muss das
  **nvidia-container-toolkit** auf der AI-VM installiert sein - Fehlerbild
  sonst: "could not select device driver nvidia". Uebergangsweise laeuft
  Whisper auch per `WHISPER_DEVICE=cpu` (+ `WHISPER_COMPUTE_TYPE=int8`,
  GPU-Block entfernen); XTTS ist auf CPU praktisch unbenutzbar.
- XTTS braucht **deutsche Sample-WAVs** im `xtts-voices`-Volume, benannt
  `{voice_id}.wav` (z. B. `default-de-female.wav`) - ca. 6-30s sauberes
  Sprechmaterial, offener Punkt der Spezifikation. Ohne Sample antwortet
  `/v1/synthesize` mit 404 und der Orchestrator faellt auf Text +
  App-TTS-Fallback zurueck.
- Modell-Downloads passieren beim ersten Start in die Volumes
  (Whisper: faster-whisper-Download, Piper: HuggingFace via Entrypoint,
  XTTS: Coqui-Downloader, TOS via ENV zugestimmt) - der erste Start
  dauert entsprechend.

## Tool-Calling (Mikro-Phase 1.12)

Der Antwort-Pfad ist ein Agent-Loop (LangGraph: retrieve -> agent -> tools
-> agent ...) mit drei Tool-Quellen:

- **Server-Tools** ueber LiteLLMs MCP-Gateway (`LITELLM_MCP_URL`, Default
  `http://litellm:4000/mcp`): alles, was dort registriert ist (z. B.
  mcp-time), steht dem LLM automatisch zur Verfuegung. Gateway nicht
  erreichbar = Turn laeuft ohne Server-Tools weiter, kein Fehler.
- **Geraete-Tools** aus dem `hello`-Manifest der App (4.13): der Orchestrator
  schickt `tool_call` ueber den WebSocket, die App antwortet mit
  `tool_result` (Timeout `DEVICE_TOOL_TIMEOUT_S`, Default 60s wegen
  moeglicher Bestaetigungs-Dialoge).
- **show_card** (builtin): das LLM kann parallel zur Antwort eine Karte
  (4.12) in die App pushen; verfuegbare Kartentypen werden ihm aus der DB
  in die Tool-Beschreibung gereicht.

Die Admin-definierten **Tool-Trigger** (1.7d) feuern jetzt: Vor einem
Tool-Call spielt der Orchestrator den passenden Filler (spezifisches
Muster wie `Calendar-*` schlaegt den generischen), hoechstens ein Filler
pro Turn.

**Wichtig - gegen `docs/PROTOCOL.md` im App-Repo verifizieren** (war aus
dieser Umgebung nicht abrufbar): die Feldnamen der Frames
`tool_call` (`{type, id, name, arguments}`),
`tool_result` (`{type, id, result}`) und
`card` (`{type: "card", card: {type, version, title?, data}}`).

## Bekannte Einschraenkungen dieses Stands

- `/v1/voices` liefert die Stimmen aus der DB; die IDs muessen zu den
  Sample-WAVs im xtts-voices-Volume passen (Upload via Admin-Panel).
- `transcript` kommt nur als final, nicht partial - Streaming-STT ist ein
  spaeterer Ausbau, die App zeigt das Transkript dann eben erst nach dem
  Sprechende.
- Barge-in (`interrupt`) stoppt die Server-Seite; bereits gesendete
  Audio-Frames muss die App selbst aus ihrem Player werfen.
- `search`-Trigger feuern erst, wenn Web-Search (1.6) als eigener Schritt
  existiert; Tool- und Nachdenk-Trigger sind aktiv.
