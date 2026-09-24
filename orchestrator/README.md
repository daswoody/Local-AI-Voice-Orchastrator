# Voice-Orchestrator (Mikro-Phasen 1.7, 1.7b-d, 1.11)

Siehe `../docs/heim-ai-projektspezifikation.md` fuer den Gesamtkontext.
Die Nachbar-Services der Voice-Pipeline liegen im selben Repo:
`../stt-service` (1.8, faster-whisper), `../tts-piper` (1.9, Filler),
`../tts-xtts` (1.10, Hauptstimme), `../tts-breeze` (v1.17, Test-Engine
Breeze TTS 2, eigener Deploy).

## Admin-Panel (/admin)

Verwaltungsoberflaeche unter `https://<domain>/admin/` (statisches
Vanilla-JS, kein Build-Step - Entscheidung siehe Spezifikation 4.14).
Login mit einem Tier-3-Konto; beim allerersten Start wird der Seed-Admin
aus `ADMIN_USERNAME`/`ADMIN_PASSWORD` in die leere DB geschrieben.

| Bereich | Funktion |
|---|---|
| Modelle | In LiteLLM registrierte Modelle anzeigen und das aktive Modell setzen (LM Studio laedt per JIT beim ersten Request, Entladen per Idle-TTL) |
| Sprachausgabe | TTS-Engine der gesprochenen Antworten waehlen (XTTS / Piper / Breeze TTS 2) mit Live-Status; Probehoeren pro Engine + Stimme mit Latenzmessung (Vergleich auf der echten Hardware); optionale Breeze-Sprechanweisung |
| GPUs | Karten mit Name, Compute-Capability und live belegtem VRAM; pro Dienst die Karte waehlen (CPU / GPU 0 / GPU 1). STT und XTTS laden ihr Modell dabei zur Laufzeit neu - kein Container-Neustart. Piper ist CPU-only, LM Studio laeuft auf dem Host und wird dort eingestellt (4.2) |
| Charakter | Globaler System-Prompt; pro Nutzer ueberschreibbar (Nutzer-Formular) |
| Nutzer | Anlegen/Bearbeiten/Loeschen, Tier 1-3, Standard-Stimme, Charakter-Override |
| Stimmen | Anlegen + WAV-Sample-Upload (landet im XTTS-Voices-Volume, kein docker cp mehr); Transkript des Samples (fuer Breeze), beim Upload per Whisper vorgeschlagen und editierbar |
| Filler & Trigger | Eigene Trigger (Nachdenken/Suche/Tool inkl. Tool-Muster wie `Calendar-*`), Filler mit Titel+Text, Engine pro Filler (XTTS = Nutzerstimme, Piper = feste Stimme/robust, Breeze = Test), "Alle generieren" rendert vor, pro Stimme Play-Button zum Probehoeren und &#8635; zum Neu-Generieren nur dieser Stimme, Filter-Chips nach Trigger |
| Agenten | Spezial-Agenten (4.16) mit eigener ID, Beschreibung, System-Prompt und eigenem LiteLLM-Modell; erscheinen der Haupt-KI als Tool `agent-<id>` - z. B. Websuche/Coding an Cloud-Modelle delegieren. Reservierte ID `code-card`: schreibt automatisch die HTML-Layouts fuer Karten ohne passendes Template |
| Karten | Layout-Templates (4.12) anlegen/bearbeiten/loeschen, Version zaehlt automatisch hoch; Format JSON (Layout-Baum) oder HTML (Fragment mit `{{data.*}}`-Platzhaltern, sandboxed gerendert) |

### GPUs verteilen (zwei Karten)

Unter "GPUs" siehst du beide Karten mit belegtem VRAM und weist jedem
Dienst eine zu. Ein Wechsel laedt das Modell auf der neuen Karte neu
(einige Sekunden) - der Container bleibt laufen, und die Zuweisung wird
nach einem Neustart automatisch wiederhergestellt.

Bewaehrte Aufteilung bei 11 GB + 8 GB:

| Dienst | Karte | Warum |
|---|---|---|
| LLM (LM Studio) | grosse Karte | groesster und am staerksten schwankender Verbrauch |
| STT (Whisper Medium) | kleine Karte | ~1,5-3 GB, laeuft nur waehrend der Transkription |
| XTTS | kleine Karte | ~3 GB, zusammen mit Whisper passen beide in 8 GB |
| Piper | CPU (fest) | sub-sekundenschnell auch ohne GPU |

**Wichtig:** Die Karte fuer das LLM stellst du in **LM Studio** ein, nicht
hier - LM Studio laeuft auf dem Host und ist fuer den Orchestrator nicht
erreichbar. Das Panel zeigt den Eintrag nur zur Orientierung; den
VRAM-Verbrauch siehst du trotzdem in der Kartenuebersicht.

Zeigt ein Dienst "weicht ab", hat er die zugewiesene Karte nicht bekommen
(zu wenig freies VRAM) und ist auf CPU ausgewichen - dann eine andere
Karte waehlen oder in LM Studio Platz schaffen. Fehlt die VRAM-Anzeige
ganz, hat der Orchestrator-Container keinen GPU-Zugriff: den
`devices`-Block mit `capabilities: [utility]` in der Compose pruefen und
neu deployen.

Ablauf fuer die erste Stimme: Stimme anlegen -> Sample hochladen (6-30s
sauberes Deutsch) -> unter "Filler & Trigger" bei jedem Filler "Audio
generieren" klicken. Ab dann spielt der Orchestrator Filler in der
XTTS-Stimme aus dem Cache; ohne generiertes Audio faellt er auf Piper
zurueck, ohne Piper laeuft die Antwort einfach ohne Filler.

**Nach dem Generieren kurz probehoeren:** In der Audio-Spalte steht pro
Stimme ein Play-Button und daneben &#8635; - das generiert NUR diese Stimme
neu, gelungene andere Stimmen bleiben erhalten. Die Generierung prueft
selbst, ob die Sprechdauer zum Text passt (Zeitlupe/angehaengte Laute),
wuerfelt Ausreisser bis zu zweimal neu und meldet, wenn es trotzdem
auffaellig bleibt. Klingt ein Filler dauerhaft kaputt, die Engine dieses
Fillers auf **Piper** umstellen (robust und ohne GPU, klingt dafuer anders
als die Hauptantwort). Achtung: Ein Wechsel der Engine oder des Textes
verwirft vorhandenes Audio - danach neu generieren.

Es laeuft immer nur eine Generierung (die Buttons sind solange gesperrt):
XTTS verfaelscht sich bei parallelen Synthesen gegenseitig - das war die
Ursache fuer Fetzen und Zeitlupe bis v1.15. **Nach dem Update auf v1.16
alle Filler einmal neu generieren**, aeltere Aufnahmen koennen noch
betroffen sein.

## Sprachausgabe: TTS-Engine wechseln (v1.17)

Unter **Sprachausgabe** waehlst du, welche Engine die gesprochenen
Antworten erzeugt - server-weit, genau wie das aktive LLM unter "Modelle".
Die Wahl greift ab dem naechsten Sprach-Turn, ohne Neustart.

| Engine | Stimme | Hardware | Wofuer |
|---|---|---|---|
| XTTS-v2 (Default) | klont die Nutzerstimme aus dem Sample, Deutsch | ~3 GB VRAM | bewaehrte Hauptstimme |
| Piper | eine feste deutsche Stimme | CPU | robuster Notbetrieb, z. B. wenn die GPUs fuer einen LLM-Test gebraucht werden |
| Breeze TTS 2 | klont aus Sample **+ exaktem Transkript**, sonst eingebaute Stimme | ~7,7 GB VRAM | Test-Engine (eigener Container, s. u.) |

Wie es funktioniert:

- **Gemeinsame Schnittstelle:** Jede Engine liefert `(Samplerate, PCM16-Chunk)`,
  deshalb kennen Antwort-Pipeline, Filler-Generierung und Probehoeren nur die
  Engine-ID (`services/tts_engines.py`). Eine weitere Engine ist ein Client
  mit `stream()` plus ein Registry-Eintrag.
- **Sicherheitsnetz:** Faellt eine andere Engine als XTTS aus, bevor Audio
  geflossen ist (Container gestoppt, Modell laedt noch, belegt), spricht XTTS
  die Antwort. Nach dem ersten Chunk wird nicht mehr gewechselt (sonst
  doppeltes Audio).
- **Probehoeren & vergleichen:** Testsatz + Stimme waehlen, "Anhoeren" -
  ohne Fallback, damit du wirklich die gewaehlte Engine hoerst. Das Panel
  zeigt die Zeit bis zur ersten Sekunde Audio, die Gesamtdauer und den
  Echtzeitfaktor (< 1 = schneller als Echtzeit).
- **Filler** behalten ihre eigene Engine (Filler & Trigger). Fuer eine
  einheitliche Stimme dort dieselbe Engine waehlen und neu generieren.

### Breeze TTS 2 testen

Vorab, weil es die Erwartung praegt: Laut [offiziellem Repo](https://github.com/breezeblue-ai/breeze-tts)
spricht das Open-Weight-Modell **nur Englisch und Chinesisch** - deutsche
Antworten koennen mit Akzent oder falsch ausgesprochen klingen. Die Gewichte
stehen unter der *BreezeBlue Research and Non-Commercial License* (privat ok,
kommerziell nicht). Die Cloud-API von breezeblue.ai (mehr Sprachen) ist
bewusst **nicht** angebunden: Jede Antwort wuerde das Haus verlassen.

1. **Deploy anlegen:** Coolify -> New Resource -> Docker Compose, dasselbe
   Repo, Base Directory `/orchestrator`, Compose-Datei
   `docker-compose.breeze.yml`. Getrennt vom Voice-Stack, weil der Container
   das Modell beim Start laedt und dann dauerhaft ~7,7 GB VRAM belegt - er
   soll nur laufen, solange du testest.
2. **Karte waehlen:** Env-Variable `BREEZE_GPU` (Index wie im GPU-Panel,
   Default `0`). Bei 11 GB + 8 GB ist die grosse Karte die realistische Wahl
   (auf 8 GB bleibt neben ~7,7 GB praktisch nichts) - vorher im GPU-Panel
   XTTS/STT auf die andere Karte schieben und das LLM in LM Studio dort
   entladen bzw. umziehen.
3. **Deployen:** Der erste Start laedt die Gewichte (mehrere GB) ins Volume
   `breeze-models`; danach laedt der Server das Modell (`/health` meldet so
   lange 503, das Panel zeigt "laedt Modell..."). Verlangt Hugging Face eine
   Lizenz-Zustimmung, bricht der Download mit einem Hinweis ab: auf der
   Modellseite zustimmen und `HF_TOKEN` setzen.
4. **Transkripte pflegen:** Breeze klont eine Stimme nur mit dem exakten
   Wortlaut des Samples. Unter **Stimmen** bei vorhandenen Samples
   "Transkribieren" klicken (Whisper schlaegt den Text vor) und ggf. unter
   "Bearbeiten" korrigieren. Ohne Transkript spricht Breeze mit seiner
   eingebauten Stimme.
5. **Probehoeren:** Sprachausgabe -> Probehoeren, Engine "Breeze TTS 2",
   denselben Satz auch mit XTTS - Klang und Latenz direkt vergleichen.
   Optional eine Sprechanweisung setzen ("Voice Direction", z. B. *Speak in a
   warm, calm tone.*).
6. **Aktivieren**, wenn es ueberzeugt - oder den Breeze-Deploy in Coolify
   stoppen, um das VRAM wieder freizugeben (XTTS bleibt aktiv bzw. springt
   ein).

Grenzen des offiziellen Servers: genau **ein** Request zur Zeit (weitere
bekommen 409 - der Orchestrator wartet bis ~5 s, danach springt XTTS ein)
und die Referenz wird bei jedem Request neu kodiert. Das offizielle
Dockerfile baut FlashAttention (nur ab Ampere) - `tts-breeze/` verzichtet
darauf, der Server rechnet ohnehin "eager". Ob die RTX 2080 Ti (Turing, ohne
natives bf16) Breeze schnell genug rechnet, zeigt erst der Test - genau
dafuer misst das Probehoeren den Echtzeitfaktor.

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

`scripts/dev_fake_services.py` stellt LiteLLM, STT, Piper, XTTS und Breeze auf
einem Port nach (inkl. simulierter LLM-Latenz, damit die Filler-Logik sichtbar
wird; der Fake-Breeze klingt beim Voice-Cloning tiefer als mit eingebauter
Stimme, so hoert man im Probehoeren, welcher Modus gegriffen hat):

```bash
# Terminal 1: Fake-Backends
uv run python scripts/dev_fake_services.py

# Terminal 2: Orchestrator dagegen starten
LITELLM_BASE_URL=http://127.0.0.1:9100/llm \
STT_BASE_URL=http://127.0.0.1:9100/stt \
PIPER_BASE_URL=http://127.0.0.1:9100/piper \
XTTS_BASE_URL=http://127.0.0.1:9100/xtts \
BREEZE_BASE_URL=http://127.0.0.1:9100/breeze \
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

## HTTPS / Browser sagt "Nicht sicher"

TLS terminiert am Coolify-Traefik, nicht in der App. Wenn die Seite unter
der HTTPS-Domain laeuft, aber als "Nicht sicher" markiert wird, liegt es
praktisch immer am Zertifikat - so grenzt man es ein:

1. **Schloss-Symbol -> Zertifikat anzeigen.** Steht dort als Aussteller
   "TRAEFIK DEFAULT CERT", liefert Traefik sein Self-Signed-Fallback aus,
   weil Let's Encrypt kein Zertifikat ausstellen konnte.
2. **Ursache bei LAN-Setups:** Der Standard-Weg (HTTP-01-Challenge)
   verlangt, dass Let's Encrypt die Domain von aussen auf Port 80
   erreicht. Zeigt der DNS-Eintrag auf die private IP (192.168.x.x),
   schlaegt das zwangslaeufig fehl.
3. **Loesung: DNS-01-Challenge ueber die Hetzner-DNS-API** - dabei weist
   Traefik den Domain-Besitz per TXT-Record nach, die VM muss NICHT
   oeffentlich erreichbar sein. Coolify hat dafuer eine offizielle
   Anleitung: https://coolify.io/docs/knowledge-base/proxy/traefik/dns-challenge
   Kurzfassung: In Coolify -> Server -> Proxy die Traefik-Konfiguration
   um einen certificatesresolver mit `dnsChallenge.provider=hetzner`
   erweitern und den Hetzner-DNS-API-Token als Env-Variable in den
   Proxy-Container geben (`HETZNER_API_KEY`; neuere Traefik/lego-Versionen
   akzeptieren auch `HETZNER_API_TOKEN` - im Zweifel beide setzen).
   Damit ist auch ein Wildcard-Zertifikat (`*.eure-domain`) moeglich,
   das alle kuenftigen internen Services abdeckt.
4. Danach in Coolify beim Orchestrator-Service pruefen, dass die Domain
   den neuen Certresolver nutzt, und neu deployen.

**Wenn das Zertifikat gueltig ist (Let's Encrypt im Viewer), der Browser
aber trotzdem "Nicht sicher" zeigt:**
- Browser komplett neu starten bzw. frisches Tab/Inkognito - manche
  Browser halten den Sicherheitszustand einer Seite fest, wenn das
  Zertifikat erst waehrend der Sitzung ausgetauscht wurde.
- DevTools (F12) -> Tab "Security"/"Sicherheit" oeffnen: dort steht der
  EXAKTE Grund (Zertifikat, veraltetes TLS, Mixed Content) - das ist die
  verbindliche Diagnose statt Raten.
- Zertifikats-KETTE pruefen (wichtig fuer die Android-App - Android
  laedt fehlende Zwischenzertifikate NICHT selbst nach, Desktop-Browser
  teils schon):
  ```bash
  openssl s_client -connect ai.preuss.app:443 -servername ai.preuss.app -showcerts </dev/null | grep -E "s:|i:|Verify"
  # Erwartet: 2 Zertifikate (Leaf + Let's-Encrypt-Intermediate) und "Verify return code: 0 (ok)"
  ```
  Alternativ https://www.ssllabs.com/ssltest/ - "Chain issues" muss
  "None" sein (funktioniert nur, wenn die Domain oeffentlich erreichbar
  ist; bei LAN-only-IP den openssl-Weg aus dem LAN nutzen).

**Server-Adresse fuer die Android-App:** die nackte Basis-URL
`https://ai.preuss.app` - ohne Pfad, ohne Port. Die App haengt selbst
`/v1/health`, `/v1/auth/login` usw. an und leitet `wss://` fuer den
Stream daraus ab. `/admin` ist NUR das Web-Panel fuer den Browser.

Der frueher direkt veroeffentlichte Port 8000 ist aus der Compose-Datei
entfernt - Klartext-HTTP haette Login-Tokens unverschluesselt uebertragen
und war eine zweite Quelle fuer "Nicht sicher"-Warnungen. Die App-Clients
nutzen die HTTPS-Domain (`https://` bzw. `wss://`).

## Troubleshooting: Stimmgenerierung schlaegt fehl

Fehlerbild `peer closed connection without sending complete message body`
= der XTTS-Container ist WAEHREND der Generierung gestorben. Seit dem
Stream-Priming (erster Audio-Chunk wird vor der Antwort erzeugt) kommen
Fehler beim Modell-Laden/Sample-Einlesen stattdessen als Klartext-500 an;
bleibt der Abriss, auf der VM pruefen:

```bash
docker logs heimai-tts-xtts --tail 100   # Traceback? CUDA out of memory?
dmesg | grep -i -E "oom|killed process"  # RAM-OOM-Kill? (16-GB-VM ist eng)
nvidia-smi                               # VRAM: LM-Studio-Modell + XTTS <= 11 GB?
docker inspect -f '{{.RestartCount}}' heimai-tts-xtts  # >0 = Container stirbt
```

Typische Ursachen und Abhilfen:
- **VRAM voll:** grosses LM-Studio-Modell + XTTS (~3 GB) passen nicht
  gleichzeitig -> kleineres LLM aktivieren oder LM-Studio-TTL abwarten.
- **RAM-OOM:** XTTS/torch braucht mehrere GB Prozess-RAM -> pruefen, was
  parallel laeuft (Embedding-Sidecar, Whisper); ggf. XTTS zuerst alleine
  testen.
- **Sample-Datei:** WAV mit ~6-30s sauberem Sprechmaterial verwenden;
  sehr lange Samples treiben die Latents-Berechnung in den Speicher.

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

Die Frame- und REST-Formate sind gegen `docs/PROTOCOL.md` des App-Repos
abgeglichen (Stand Branch claude/sharp-pascal-r1vi0y): Audio-Feld `data`,
Tool-Frames `call_id`/`ok`/`result`, Manifest im hello-Feld `tools`,
`session`-Frame nach Connect, Auth per Authorization-Header.

## Antwort-Modalitaeten

- **Text rein -> Text raus:** getippte Eingaben bekommen NIE Server-Audio.
- **Audio rein -> Sprachantwort + Details im Chat:** gesprochene Eingaben
  (Push-to-Talk, Realtime, Assist) bekommen Audio; lange Antworten werden
  fuer die Sprachausgabe per zweitem LLM-Call auf max. zwei Saetze
  gekuerzt (`VOICE_SUMMARY_MAX_CHARS`, Default 280; abschaltbar per
  `VOICE_SUMMARY_ENABLED`), waehrend der volle Text als assistant_text im
  Chat steht.

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
