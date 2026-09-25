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
| GPUs | Karten mit Name, Compute-Capability und live belegtem VRAM; pro Dienst die Karte waehlen (CPU / GPU 0 / GPU 1), XTTS auch ganz ausschalten ("Aus"). STT und XTTS laden ihr Modell dabei zur Laufzeit neu - kein Container-Neustart. Piper ist CPU-only, LM Studio laeuft auf dem Host und wird dort eingestellt (4.2) |
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

**XTTS ausschalten (v1.19):** In der Zeile von XTTS "Aus (Modell entladen)"
waehlen - z. B. um fuer einen Breeze-Test Platz auf der Karte zu schaffen.
Der Dienst entlaedt sein Modell (~3 GB), der Container laeuft weiter (der
Orchestrator hat bewusst keinen Docker-Zugriff, 4.2). Die Einstellung
ueberlebt Neustarts. Einige hundert MB (CUDA-Kontext) gibt erst
`docker restart heimai-tts-xtts` frei. Regeln: Ausschalten geht nur, wenn
unter Sprachausgabe eine andere Engine aktiv ist; faellt die aus, springt
Piper statt XTTS ein. Vorhandene XTTS-Filler bleiben abspielbar, neue
lassen sich erst nach dem Einschalten erzeugen. Wieder an: eine Karte
waehlen.

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
| Breeze TTS 2 | klont aus Sample **+ exaktem Transkript**, sonst eingebaute Stimme | ~7,7 GB (PyTorch) bzw. ~4 GB (Breeze-TTS-2.cpp) | Test-Engine (eigener Server, s. u.) |

Wie es funktioniert:

- **Gemeinsame Schnittstelle:** Jede Engine liefert `(Samplerate, PCM16-Chunk)`,
  deshalb kennen Antwort-Pipeline, Filler-Generierung und Probehoeren nur die
  Engine-ID (`services/tts_engines.py`). Eine weitere Engine ist ein Client
  mit `stream()` plus ein Registry-Eintrag.
- **Sicherheitsnetz:** Faellt eine andere Engine als XTTS aus, bevor Audio
  geflossen ist (Container gestoppt, Modell laedt noch, belegt), spricht XTTS
  die Antwort - bzw. Piper, solange XTTS unter GPUs ausgeschaltet ist. Nach
  dem ersten Chunk wird nicht mehr gewechselt (sonst doppeltes Audio).
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

Es gibt zwei Server-Varianten mit **derselben HTTP-Schnittstelle** - der
Orchestrator merkt keinen Unterschied, im Panel wird nur die Adresse
eingetragen (Sprachausgabe -> Breeze-Server):

| | A: offizieller PyTorch-Server | B: Breeze-TTS-2.cpp |
|---|---|---|
| Ordner / Compose | `tts-breeze/`, `docker-compose.breeze.yml` | `tts-breeze-cpp/`, `docker-compose.breeze-cpp.yml` - oder nativ (B2) |
| Adresse im Panel | `http://tts-breeze:7860` (Standard) | `http://tts-breeze-cpp:7860` bzw. `http://<ip>:7860` |
| VRAM | ~7,7 GB (bf16) | ~4 GB (Q8_0), ~3 GB (Q4_K) |
| Tempo | offen: die 2080 Ti (Turing) kann bf16 nicht nativ | laut Projekt ~1,25x Echtzeit auf einer RTX 3060 (Q8_0) |
| Gewichte | `BreezeBlue/breeze-tts-2`, ~6,5 GB | `HoppouAI/Breeze-TTS-2.cpp`, Q8_0 ~3,3 GB |
| Erster Build | grosses Image (PyTorch + CUDA-Bibliotheken) | CUDA-Kompilierung, dauert |
| Herkunft | offizieller Code | Community-Portierung auf ggml, jung |

Ein Unterschied im Verhalten: Ohne Sprechanweisung konditioniert B auf ein
eingebautes "Speak clearly and naturally.", A klont dann ohne Anweisung.
Beide nehmen genau **einen** Request zur Zeit an (weitere bekommen 409, der
Orchestrator wartet bis ~5 s, danach springt XTTS ein) und kodieren die
Referenz bei jedem Request neu.

#### Vorbereitung (beide Varianten)

1. **Karte planen** (GPU-Panel): A braucht ~7,7 GB am Stueck - auf 11 GB +
   8 GB heisst das die grosse Karte freiraeumen (XTTS/STT auf die andere
   Karte, das LLM in LM Studio entladen bzw. umziehen). B passt mit ~4 GB
   auch auf die 8-GB-Karte neben Whisper - aber nicht zusaetzlich neben
   XTTS: Beim Klonen braucht Breeze kurzzeitig mehr als im Leerlauf (langes
   Sample = mehr). Fuer den Test XTTS unter GPUs ausschalten ("Aus") oder
   auf die andere Karte legen - vorher Breeze unter Sprachausgabe aktivieren.
2. **Transkripte pflegen:** Breeze klont eine Stimme nur mit dem exakten
   Wortlaut des Samples. Unter **Stimmen** bei vorhandenen Samples
   "Transkribieren" klicken (Whisper schlaegt den Text vor) und unter
   "Bearbeiten" korrigieren. Ohne Transkript spricht Breeze mit seiner
   eingebauten Stimme. Das Sample geht als mono 16 Bit/24 kHz an Breeze -
   auch 24-Bit-Aufnahmen, die Breeze-TTS-2.cpp sonst als Stille liest.

#### Variante A: offizieller PyTorch-Server (Docker)

1. Coolify -> New Resource -> Docker Compose: dasselbe Repo und derselbe
   Branch wie der Voice-Stack, **Base Directory `/`** und Docker Compose
   Location `/orchestrator/docker-compose.breeze.yml`. Nicht `/orchestrator`
   als Base Directory: Coolify nimmt es als Projektordner und sucht den
   Build-Context `tts-breeze/` dann unter `orchestrator/` (Deploy bricht ab
   mit `unable to prepare context: path ".../orchestrator/tts-breeze" not
   found`).
2. Environment Variables: `BREEZE_GPU` = Index der Karte (wie im GPU-Panel,
   Default `0`); `HF_TOKEN` nur, falls Hugging Face eine Lizenz-Zustimmung
   verlangt (auf der Modellseite zustimmen, Lese-Token anlegen).
3. Advanced -> Build arguments -> "Managed manually in Dockerfile". Sonst
   schreibt Coolify jede Variable als `ARG` ins Dockerfile: Eine geaenderte
   Laufzeit-Einstellung wie `BREEZE_GPU` loest dann einen kompletten Neubau
   aus, und `HF_TOKEN` landet in der Build-History des Images.
4. Deploy. Der erste Start laedt die Gewichte (~6,5 GB) ins Volume
   `breeze-models`, danach das Modell auf die Karte - das Panel zeigt so
   lange "laedt Modell...". Verfolgen mit `docker logs -f heimai-tts-breeze`.
5. Im Panel unter Sprachausgabe -> Breeze-Server nichts eintragen (Standard
   `http://tts-breeze:7860`) - die Tabelle zeigt Breeze als "erreichbar".

#### Variante B1: Breeze-TTS-2.cpp im Container

1. Coolify -> New Resource -> Docker Compose: gleiches Repo/Branch, **Base
   Directory `/`** und Docker Compose Location
   `/orchestrator/docker-compose.breeze-cpp.yml` (warum nicht
   `/orchestrator`: siehe A).
2. Environment Variables:
   - `BREEZE_GPU` = Index der Karte (Default `0`)
   - `BREEZE_CUDA_ARCHS` = Compute-Capability der Karte(n) ohne Punkt, z. B.
     `75` fuer die 2080 Ti allein (baut am schnellsten). Die Karte aus
     `BREEZE_GPU` muss dabei sein (fehlt z. B. `61` fuer eine Pascal-Karte,
     bricht der Server mit "no kernel image is available" ab). Nachsehen mit
     `nvidia-smi --query-gpu=index,name,compute_cap --format=csv`. Default
     `61;75;86;89` deckt Pascal bis Ada ab.
   - optional `BREEZE_GGUF_QUANT` = `q8_0` (Default, empfohlen) oder `q4_k`
     (~3 GB, etwas schlechter), `HF_TOKEN` wie bei A, `BREEZE_BUILD_JOBS`
     (Default 4 - kleiner, falls der VM beim Build der RAM ausgeht)
   - `BREEZE_CUDA_ARCHS` und `BREEZE_BUILD_JOBS` brauchen "Available during
     build", alle anderen nur zur Laufzeit.
3. Advanced -> Build arguments -> "Managed manually in Dockerfile" (wie bei
   A) - hier besonders wichtig, sonst kompiliert jede geaenderte Variable
   CUDA neu.
4. Deploy. Der Build kompiliert ggml mit CUDA (hier gemessen: ~30 Minuten fuer
   die vier Standard-Architekturen auf 4 Kernen, mit nur `75` deutlich
   schneller). Der erste Start laedt die GGUF-Datei ins Volume
   `breeze-cpp-models`.
   `docker logs -f heimai-tts-breeze-cpp` zeigt am Ende
   `backend: CUDA0, sample rate: 24000` und `listening on http://0.0.0.0:7860`.
   Steht dort `backend: CPU`, hat der Container keine GPU bekommen.
5. Im Panel unter Sprachausgabe -> Breeze-Server `http://tts-breeze-cpp:7860`
   eintragen und speichern.

#### Variante B2: Breeze-TTS-2.cpp nativ (ohne Docker)

Fuer einen Rechner ausserhalb von Coolify - die VM selbst oder ein anderer
Linux-Rechner (hier Ubuntu 24.04) mit NVIDIA-Karte. Fertige Binaries gibt es
(noch) nicht; gebaut wird mit dem Vulkan-Backend, das nur den NVIDIA-Treiber
braucht (hier getestet: ~3 Minuten auf 4 Kernen):

```bash
sudo apt install build-essential cmake ninja-build git libvulkan-dev glslc spirv-headers vulkan-tools
vulkaninfo --summary        # muss die NVIDIA-Karte auflisten (sonst fehlt der Vulkan-Teil des Treibers)
git clone https://github.com/HoppouAI/Breeze-TTS-2.cpp.git && cd Breeze-TTS-2.cpp
git checkout a5436642d4c64304b398ceeda9b8fce4577bfdb1   # derselbe Stand wie im Container
git submodule update --init --recursive
cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF
cmake --build build --target breeze-server -j4
```

`-DBUILD_SHARED_LIBS=OFF` macht `build/breeze-server` zu einem eigenstaendigen
Binary, das man auch verschieben kann. Mit installiertem CUDA-Toolkit geht
statt Vulkan `-DBREEZE_CUDA=ON -DBREEZE_VULKAN=OFF` - fuer CUDA ist aber
Variante B1 der einfachere Weg (Toolkit steckt im Build-Image).

Gewichte: auf https://huggingface.co/HoppouAI/Breeze-TTS-2.cpp/tree/main die
`...q8_0.gguf` (ohne `-dd` im Namen) laden, z. B.
`wget https://huggingface.co/HoppouAI/Breeze-TTS-2.cpp/resolve/main/<datei>`.

Starten - der Server hat **keine Authentifizierung**, deshalb nur an eine
interne Adresse binden:

```bash
# Auf der VM selbst: an das Gateway des ai-lab-Netzes - dann erreichen ihn nur Container
docker network inspect ai-lab -f '{{(index .IPAM.Config 0).Gateway}}'    # z. B. 172.18.0.1
./build/breeze-server <datei>.gguf --host 172.18.0.1 --port 7860 --ws-port -1
# Auf einem anderen Rechner: an dessen LAN-IP, Port 7860 per Firewall nur fuer die VM freigeben
```

Im Panel dann `http://172.18.0.1:7860` bzw. `http://<lan-ip>:7860` eintragen.
Damit der Server den Neustart ueberlebt, z. B. als systemd-Dienst:

```ini
# /etc/systemd/system/breeze-tts.service
[Unit]
Description=Breeze-TTS-2.cpp
After=network-online.target docker.service

[Service]
WorkingDirectory=/opt/Breeze-TTS-2.cpp
ExecStart=/opt/Breeze-TTS-2.cpp/build/breeze-server /opt/Breeze-TTS-2.cpp/<datei>.gguf --host 172.18.0.1 --port 7860 --ws-port -1
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`sudo systemctl enable --now breeze-tts`. Unter Windows baut das Projekt mit
MSVC oder mingw plus Vulkan SDK (siehe `docs/build.md` im Projekt), der
Server-Aufruf ist derselbe.

#### Testen und aktivieren (alle Varianten)

1. Sprachausgabe -> Probehoeren: Engine "Breeze TTS 2" und denselben Satz mit
   XTTS - Klang und Latenz direkt vergleichen (Echtzeitfaktor unter 1 =
   schneller als Echtzeit). Optional eine Sprechanweisung setzen ("Voice
   Direction", z. B. *Speak in a warm, calm tone.*).
2. **Aktivieren**, wenn es ueberzeugt. Zum Aufhoeren den Breeze-Deploy in
   Coolify stoppen (bzw. den nativen Dienst) - das VRAM ist dann wieder frei,
   bis dahin bzw. danach spricht XTTS.

**Panel zeigt Breeze als "nicht erreichbar"?** Die Meldung nennt die Ursache:

- *Server nicht gefunden* - den Namen aus der eingetragenen Adresse kennt das
  Netzwerk nicht: Der Breeze-Deploy laeuft (noch) nicht oder die Adresse
  stimmt nicht. In Coolify pruefen, ob die Resource existiert und fertig
  deployt ist (der erste Build dauert). Auf der VM:
  ```bash
  docker ps --filter name=heimai-tts-breeze          # laeuft ein Breeze-Container (A oder B1)?
  docker network inspect ai-lab --format '{{range .Containers}}{{.Name}} {{end}}'
  docker exec heimai-orchestrator python -c "import socket; print(socket.gethostbyname('tts-breeze'))"
  ```
- *nimmt keine Verbindungen an* - der Container/Rechner ist da, der Server
  aber (noch) nicht: `docker logs -f heimai-tts-breeze` bzw.
  `heimai-tts-breeze-cpp` (nativ: die Konsole von breeze-server) zeigen
  Download, Modell-Laden oder den Fehler (z. B. Lizenz-Zustimmung ->
  `HF_TOKEN`, CUDA out of memory -> Karte freiraeumen).
- *laedt Modell...* - einfach warten, danach steht dort "erreichbar".

**Probehoeren meldet "Verbindung mitten in der Synthese abgebrochen"?**
Breeze hat die Antwort begonnen und ist dann abgestuerzt (Container startet
neu). Den Grund zeigen die letzten Log-Zeilen direkt danach:

```bash
docker logs --tail 40 heimai-tts-breeze-cpp      # bzw. heimai-tts-breeze
nvidia-smi                                        # VRAM der Breeze-Karte
```

- `out of memory` / `cudaMalloc failed` / `failed to allocate` - die Karte
  ist voll: XTTS unter GPUs ausschalten oder umziehen, das LLM entladen, ein
  kuerzeres Sample (~10 s) nehmen oder `BREEZE_GPU` auf die freiere Karte.
- `no kernel image is available` - `BREEZE_CUDA_ARCHS` enthaelt die Karte
  aus `BREEZE_GPU` nicht (Compute-Capability pruefen, neu bauen).

Aktivieren geht trotzdem (nach Rueckfrage) - bis Breeze antwortet, spricht
XTTS (bzw. Piper, solange XTTS ausgeschaltet ist).

Zu Variante A: Das offizielle Dockerfile baut FlashAttention (laeuft erst ab
Ampere) - `tts-breeze/` verzichtet darauf, der Server rechnet ohnehin
"eager". Ob die 2080 Ti ohne natives bf16 schnell genug ist, zeigt der
Echtzeitfaktor im Probehoeren; ist sie zu langsam, ist B die Alternative.

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
2. **Base Directory** `/` lassen und **Docker Compose Location**
   `/orchestrator/docker-compose.yml` eintragen. Coolify nimmt das Base
   Directory als Projektordner (`--project-directory`), und alle
   Build-Contexts in der Compose-Datei sind relativ zum Repo-Root.
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
