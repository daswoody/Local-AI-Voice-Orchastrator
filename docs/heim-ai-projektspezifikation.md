# Heim-AI Projektspezifikation

> **Single Source of Truth** für das Heim-AI-Projekt. Dieses Dokument soll am Anfang jeder Claude-Session als Kontext mitgegeben werden, damit Claude den Projektstand und alle Entscheidungen kennt.

---

## 1. Projekt-Zielsetzung

Aufbau einer privaten, lokal gehosteten Heim-AI mit folgenden Kernfähigkeiten:
- Sprachinteraktion über Wake Word (lokale Spracherkennung)
- Hochwertige, deutsche TTS mit niedriger Latenz
- Multi-Tool-Orchestrierung (Kalender, Notizen, Dokumente, Smart Home, etc.)
- Sprecher-Identifikation für rollenbasierte Rechte (Admin / User / Gast)
- Komplexe Inhalte werden parallel zur Sprachantwort als Modal/Karten in begleitender App angezeigt
- Screenshot-Analyse für PC/Gaming-Unterstützung
- Discord Voice Bot (finale Ausbaustufe)
- **Zentrale Admin-Oberfläche** zur Verwaltung von Modellen, Charakteren/System-Prompts, Stimmen und Karten-Layouts — zugleich die Rechte-Verwaltungs-UI des Projekts (NEU in v1.6, siehe 4.14)

Charakter des Projekts: **Lernprojekt** – schrittweise Umsetzung mit erklärendem Vorgehen. *Ausnahme Phase 2 (beschlossen in v1.5): Hier zählte das Endergebnis – die Android-App wurde komplett generiert statt schrittweise erarbeitet. Die Codebasis ist dafür ausführlich kommentiert und dokumentiert.*

---

## 2. Hardware

| Komponente | Spezifikation |
|---|---|
| Heimserver | 12-Core CPU, NVIDIA RTX 2080 Ti **(11 GB VRAM)** |
| Hypervisor | Proxmox |
| AI-VM | Ubuntu 24.04.4 LTS mit aktivem GPU-Passthrough, **16 GB RAM** (Ziel langfristig: 128 GB) |
| Webserver (RZ) | 2 Cores, 4 GB RAM (Headscale, optional Exit-Node, später Endpoints) |
| Audio-Endpoints | Android-Smartphones mit eigener App (Phase 2 ✅) / später: ESP32-basierte Raumstationen |
| Audio-Output | Sony STR-DN1060 AV-Receiver (Spotify Connect-fähig) |

**VRAM-Budget ist der kritische Constraint** – muss bei jeder Komponentenwahl berücksichtigt werden. RAM ist aktuell ebenfalls eng und Teil jeder Entscheidung.

---

## 3. Bestehender Software-Stack

### Auf der AI-VM (192.168.2.105)
| Tool | Zweck im Projekt | Status |
|---|---|---|
| LM Studio | LLM-Hosting mit GPU | ✅ läuft, erreichbar via `lmstudio.ai.lab` und IP:1234 |
| LiteLLM | LLM-Gateway + **MCP-Gateway** | ✅ läuft (Container `litellm`, ai-lab Netzwerk) |
| Open WebUI | Chat-UI für Tests + komplexe Inhaltsanzeige (initial) | ✅ läuft (v0.9.5) |
| n8n | Tool-Integrationen, Routinen, Webhooks, **Retention-Cleanup** | ✅ läuft |
| Langflow | Optional: visuelle LLM-Flow-Prototypen | ✅ läuft |
| Weaviate | Vektor-DB für RAG / Erinnerungsspeicher | ✅ läuft (v1.26.4), 4 Collections angelegt, mit lokalem Embedding-Sidecar |
| t2v-transformers | Embedding-Sidecar für Weaviate (intfloat/multilingual-e5-base, CPU) | ✅ läuft |
| mcp-time | MCP-Tool-Server (Time/Timezone) | ✅ läuft (theo01/mcp-time, `mcp-time:8080`) |
| Postgres | DB für LiteLLM Storage | ✅ läuft |
| local-registry | Lokale Docker-Registry für Custom-Images (Port 5000) | ✅ läuft (`--restart=always`) |

### Auf Container 100/101/102 etc.
| Container | Service |
|---|---|
| 100 | Home Assistant (VM, Thermostate) |
| 101 | pihole (DHCP, lokaler DNS – `*.ai.lab` → AI-VM) |
| 102 | n8n |
| 103 | nginx proxy manager |
| 104 | Languagetool |

### Auf dem Webserver (RZ)
| Tool | Status |
|---|---|
| SearXNG | ✅ läuft (Meta-Suche) |
| Affine | ✅ läuft |
| Authentik | ✅ läuft |
| Umami | ✅ läuft |
| n8n | ✅ läuft (zweite Instanz für externe Hooks) |
| Headscale | ⏳ noch nicht installiert – geplant |

### Eigene Codebasen
| Repo | Inhalt | Status |
|---|---|---|
| `Android-AI-Assistant-App` (GitHub: daswoody) | Android-App (Phase 2: Kotlin + Compose), Protokoll-Vertrag (`docs/PROTOCOL.md`), Karten-Format (`docs/CARDS.md`), CI-Workflow für APK-Build | ✅ Code komplett, CI grün, Debug-APK als Actions-Artifact (`heimai-debug-apk`) |
| `Local-AI-Voice-Orchastrator` (GitHub: daswoody) (**NEU in v1.6**) | Monorepo: `orchestrator/` (FastAPI + LangGraph, 1.7+1.11), `stt-service/` (faster-whisper, 1.8), `tts-piper/` (Filler, 1.9), `tts-xtts/` (Hauptstimme, 1.10), später `admin-frontend/` (1.7c) — siehe 4.14 | 🚧 1.7–1.11 code-seitig fertig (v1.7), VM-Validierung offen |

---

## 4. Architektur-Entscheidungen (final)

### 4.1 Voice-Pipeline-Layout

```
[Android-App / ESP32-Satellit]
   │ Wake Word lokal erkannt (Android: Porcupine)
   ▼
[Audio-Stream via WebSocket /v1/assistant/stream — JSON-Frames, Base64-PCM16/16k]
   ▼
[STT-Service] ─── Whisper (Medium oder Large-v3, je nach VRAM-Plan)
   ▼
[Voice-Orchestrator] (eigener Python-Service mit LangGraph)
   │ ├─ Routing-Entscheidung
   │ ├─ Filler-Audio ausspielen ("Lass mich nachdenken...")
   │ ├─ Tool-Calls (über MCP-Gateway in LiteLLM)
   │ ├─ Geräte-Tool-Calls (zurück an die App, siehe 4.13)
   │ ├─ RAG-Abfrage (Weaviate)
   │ └─ LLM-Anfrage (über LiteLLM)
   ▼
[TTS-Service] ─── Piper (Filler) + XTTS-v2 (Hauptantwort)
   ▼
[Audio zurück an Endpoint] + parallel: Card-Push an App (siehe 4.12)
```

### 4.2 Modell-Strategie (VRAM-Budget)

**Aktuelles Modell für Tests:** Gemma 4 E4B (Google, multimodal, mit Thinking-Mode und nativem Function-Calling). Finale Modellwahl wird später basierend auf Speicherbedarf der anderen Komponenten getroffen.

**Default-Konfiguration** (alle Komponenten gleichzeitig aktiv, Zielwert):
- LLM: ~5 GB (Modell TBD)
- STT: Whisper Medium (~1.5 GB)
- TTS: XTTS-v2 (~3 GB)
- Buffer / Speaker-Embeddings (~1.5 GB)
- **Summe: ~11 GB** (knapp, aber machbar)

**Embedding für RAG:** läuft bewusst **außerhalb der GPU** auf CPU (siehe 4.8), um das VRAM-Budget nicht zu belasten.

**Modell-Hot-Swap (NEU in v1.6):** Siehe 4.14 — LM Studio bietet eine native REST-API zum Laden/Entladen, die der Orchestrator kapselt.

### 4.3 Filler-Phrasen-Strategie

| Trigger | Phrasen-Typ |
|---|---|
| Web-Search / RAG-Abfrage | "Lass mich kurz nachdenken", "Hm…", "Gib mir einen Augenblick" |
| Tool-Call (Kalender, Notes, etc.) | "Ich schaue eben nach", "Lass mich kurz nachschauen" |

Filler werden **vor** der eigentlichen Antwort über Piper (Sub-Sekunden-Latenz) ausgespielt, während im Hintergrund die LLM-Antwort generiert wird. *Für die App ist das transparent: Filler und Hauptantwort kommen als ein zusammenhängender `audio_chunk`-Stream.*

### 4.4 Sicherheits-/Rechtekonzept (Tiered Security)

| Tier | Voraussetzung | Erlaubte Aktionen |
|---|---|---|
| **Tier 1 – Gast** | Keine Stimm-Erkennung | Web-Suche, allgemeine Konversation, Wetter, News |
| **Tier 2 – User (Frau)** | Stimme erkannt | + Kalender, Notes, Smart Home, Musik, RAG-Lesen |
| **Tier 3 – Admin (du)** | Stimme erkannt + Gerätekontext | + Systemeinstellungen, kritische Steuerung, RAG-Schreiben |

Ergänzung: **Kritische Aktionen** (z. B. Türschloss, Heizung extrem) erfordern explizite App-Bestätigung, unabhängig vom Tier.

**Umsetzung in der Android-App:** Geräte-Tools tragen ein `sensitive`-Flag. Sensible Tools (z. B. Benachrichtigungen auslesen) erfordern eine Bestätigung per Dialog in der App — außer der Nutzer aktiviert in den Rechte-Einstellungen "entsperrtes Gerät genügt" (dann prüft die App nur, dass der Keyguard nicht aktiv ist). Der Login liefert das Tier des Users mit (`user.tier`); die serverseitige Tier-Durchsetzung pro Tool kommt in Phase 4/5. Der App-Bestätigungsdialog ist gleichzeitig der vorgesehene Mechanismus für die "kritischen Aktionen" oben.

**Defense in depth bei Knowledge-Collections:** Die Trennung in `PrivateKnowledge` / `GeneralKnowledge` / `WebKnowledge` ist nicht nur logisch, sondern auch sicherheitsrelevant. Der Voice-Orchestrator routet Tier-1-Anfragen API-seitig gar nicht erst gegen `PrivateKnowledge` – zusätzlich zu eventuellen Filtern. Das verhindert, dass ein Code-Bug bei der Filter-Konstruktion private Daten leakt.

**Admin-Frontend-Zugriff (NEU in v1.6):** Alle `/v1/admin/*`-Routen (siehe 4.14) erfordern Tier 3 — dieselbe Middleware wie oben beschrieben, keine Sonderlogik.

### 4.5 Web-Search / Crawling

- **SearXNG** auf dem Webserver für Meta-Suche
- **Firecrawl lokal** auf dem Heimserver (volle Performance)
- **Headscale Exit-Node** auf dem Webserver für Firecrawl-Routing (saubere IP, Split-Tunneling – nicht aller Heim-Traffic)

### 4.6 MCP-Architektur

**LiteLLM ist die zentrale MCP-Quelle.** Alle MCP-Server werden in LiteLLM registriert. Clients (heute Open WebUI, später Voice-Orchestrator) sprechen mit LiteLLM's MCP-Gateway-Endpoint `/mcp/`, der die Tools aller registrierten Server aggregiert.

**Vorteile:**
- Ein einziger MCP-Endpoint für alle Clients
- Permission-Management über LiteLLM Virtual Keys
- Beim Hinzufügen neuer MCP-Server müssen Clients nicht angepasst werden
- Saubere Trennung: MCP-Server kennen nur Tools, LLM kennt nur Sprache, LiteLLM verbindet beides

**Hostname-Konvention im ai-lab-Netzwerk:** Coolify vergibt UUIDs an Container-Namen, der **kurze Compose-Service-Name funktioniert aber als DNS-Alias** (z. B. `mcp-time`, `litellm`, `weaviate`). MCP-Server werden im Compose mit kurzen, sprechenden Namen versehen.

**Tool-Naming:** LiteLLM prefixt Tools automatisch mit dem Server-Namen (z. B. `Time-current_time`, `Time-relative_time`), damit Tool-Namen über mehrere Server eindeutig bleiben.

**Sonderfall Geräte-Tools:** Die Tools der Android-App (App öffnen, Navigation, Benachrichtigungen, …) sind **session-gebunden** — sie existieren nur, solange das jeweilige Gerät verbunden ist, und ein Aufruf muss zum richtigen Gerät zurückgeroutet werden. Sie laufen daher NICHT über die LiteLLM-MCP-Registry, sondern werden vom Orchestrator pro WebSocket-Verbindung dynamisch als Tools beim LLM registriert (Manifest kommt in der `hello`-Nachricht, siehe 4.13). Das ist eine bewusste, dokumentierte Ausnahme vom MCP-First-Prinzip.

**Verhältnis zu „Skills" (NEU in v1.6):** Siehe Begriffsklärung in 4.14 — MCP-Tool-Server-Verwaltung bleibt vollständig bei LiteLLM, daran ändert sich durch das Admin-Frontend nichts.

### 4.7 Custom-Image-Pipeline & lokale Docker-Registry

Für eigene Docker-Images (z. B. Embedding-Sidecar, später eigene MCP-Server) nutzen wir eine **lokale Docker-Registry auf der AI-VM** statt Images nur lokal getagged zu lassen.

**Begründung:**
- Coolify räumt periodisch "unbenutzte" Images auf. Lokal gebaute Images ohne Container-Referenz können dabei verschwinden
- `pull_policy: never` im Compose ist fragil und nicht konsistent von allen Coolify-Versionen unterstützt
- Eine lokale Registry ist die saubere, offizielle Lösung für dieses Pattern

**Registry-Setup:**
- Container `local-registry` mit Image `registry:2`
- Lauscht auf Port 5000 der AI-VM
- Persistenter Storage unter `/opt/docker-registry`
- `--restart=always` – läuft nach VM-Neustart automatisch wieder

**Build-Konvention:**
- Custom-Image-Quellen liegen unter `~/docker-builds/<image-name>/` auf der AI-VM
- Jedes Verzeichnis enthält ein `Dockerfile`
- Workflow: `docker build` → `docker tag <name>:local localhost:5000/<name>:latest` → `docker push localhost:5000/<name>:latest`
- Im Compose referenziert als `image: 'localhost:5000/<name>:latest'`

**Erstes Beispiel:** `t2v-e5-base` (siehe 4.8). **Künftig auch:** das Orchestrator+Admin-Frontend-Image (4.14).

### 4.8 Embedding-Pipeline für RAG

**Modell:** `intfloat/multilingual-e5-base`
- 768 Dimensionen
- Max. 512 Token Input (514 Position-Embeddings inkl. 2 Spezial-Tokens)
- XLM-RoBERTa-Architektur, mehrsprachig (Deutsch sehr gut)
- Bewusst **base** statt **large** wegen RAM-Budget der VM (16 GB)

**Deployment-Pattern: Sidecar-Container**
- Embedding läuft in eigenem Container (`t2v-transformers`), separat von Weaviate
- Weaviate ruft den Sidecar über `TRANSFORMERS_INFERENCE_API=http://t2v-transformers:8080` auf
- Vorteil: Modell kann unabhängig von Weaviate neu gestartet / ausgetauscht werden
- Sidecar bewusst **nicht** im ai-lab-Netzwerk: nur Weaviate spricht mit ihm

**CPU statt GPU:**
- Bewusste Entscheidung wegen VRAM-Knappheit
- e5-base auf CPU: ~80-150 ms pro Embedding – für RAG (nicht-Echtzeit-Pfad) absolut ausreichend
- ENV: `ENABLE_CUDA=0`

**Image-Build:**
- Basis-Image: `semitechnologies/transformers-inference:custom` (von Weaviate)
- Dockerfile zwei Zeilen: `FROM ...:custom` + `RUN MODEL_NAME=intfloat/multilingual-e5-base ./download.py`
- Image-Größe: ~10 GB on-disk (großer Anteil ist CUDA-Runtime im Basis-Image, ungenutzt)
- Build-Pfad: `~/docker-builds/t2v-e5-base/`
- Registry-Tag: `localhost:5000/t2v-e5-base:latest`

**Healthcheck-Hinweis:** Das Inference-Image enthält **kein `wget`**. Healthchecks müssen über Python erfolgen:
```yaml
test:
  - CMD
  - python3
  - '-c'
  - 'import urllib.request,sys; sys.exit(0 if urllib.request.urlopen("http://localhost:8080/.well-known/ready").status==204 else 1)'
```

**Modellwechsel-Strategie:** Embeddings sind modellgebunden. Bei einem späteren Wechsel auf z. B. `multilingual-e5-large` (sobald die VM mehr RAM hat) müssen Collections gedroppt und neu indexiert werden. Da die Datenmengen im Heim-AI-Kontext überschaubar bleiben, ist das kein blockierendes Problem.

### 4.9 RAG-Verhalten und Tuning-Pfad

Aus den Such-Tests in 1.5b haben wir empirisch gelernt, wie sich das e5-base-Modell auf unseren deutschen Test-Daten verhält. Das definiert den Tuning-Pfad für den späteren Voice-Orchestrator.

**Empirische Beobachtungen:**
- Semantische Suche funktioniert: Synonyme werden erkannt ("sieden" findet "kochen"), anders formulierte Anfragen finden inhaltlich passende Treffer (z. B. "Norditalien" findet "Garda-See")
- **Hochbias bei Certainty-Werten:** e5-base liefert für nahezu jede Anfrage Werte im engen Band 0,85–0,95. Auch komplett unzusammenhängende Anfragen ("Wie programmiere ich in Rust?" gegen Wasser-/Kochen-Wissen) erreichen 0,90+
- → **Absolute Schwellwerte sind unbrauchbar.** Nur die relative Reihenfolge der Treffer ist verlässlich
- Trennschärfe bei sehr kurzen Anfragen ist begrenzt – richtige Treffer landen manchmal auf Platz 2 oder 3 statt 1

**Tuning-Pfad (Implementierung in Voice-Orchestrator, Phase 1.7+):**

1. **e5-Präfixe nutzen** – das Modell erwartet `query: <Frage>` bei Suchen und `passage: <Inhalt>` beim Schreiben. Verbessert Retrieval messbar. Voice-Orchestrator legt Inhalte mit `passage:`-Prefix in Weaviate ab und sucht mit `query:`-Prefix
2. **Relative Filterung** statt absoluter Schwellwerte. Konkret: Top-K Ergebnisse holen, dann nur die behalten, deren Certainty mindestens X% **über dem Durchschnitt der gesamten Top-K** liegt (z. B. >5%). Damit wird die Hochbias-Eigenschaft umschifft
3. **Hybrid Search evaluieren** – Weaviate unterstützt nativ `hybrid`-Queries (Vektor + BM25-Keyword). Für Anfragen mit konkreten Begriffen wie Eigennamen, Fachwörtern oder Produktnamen kann das die Trennschärfe deutlich verbessern. Im Voice-Orchestrator pro Query-Typ entscheiden

**Bewusste Nicht-Aktion jetzt:** Wir migrieren die Test-Daten in 1.5b **nicht** auf Präfixe. Sie sind explizite Test-Inhalte und werden später ohnehin verworfen. Die Präfix-Konvention startet ab Voice-Orchestrator.

### 4.10 Zeitzonen-Konvention

Alle Timestamps in Weaviate werden in **UTC** gespeichert (ISO 8601 mit `Z`-Suffix, z. B. `2026-05-15T08:30:00Z`).

**Begründung:**
- Sommerzeit-/Winterzeit-Eindeutigkeit (`02:30 Berlin` existiert am Umstellungstag zweimal, UTC nie)
- Sortierung und Vergleichbarkeit unabhängig von Zeitzonen-Wechseln
- Cleanup-Logik (n8n) arbeitet zuverlässig mit `now() - X` ohne Spezialfälle

**Konvertierung in `Europe/Berlin`** erfolgt an der Grenze zwischen Code und User: Voice-Orchestrator beim Anzeigen, n8n-Workflows beim Lesen für User-Output. Beim **Schreiben** wandelt der Voice-Orchestrator User-Eingaben aus Berlin-Zeit in UTC um, bevor sie in Weaviate landen. *Die Android-App speichert lokale Verlaufs-Timestamps als Epoch-Millis und zeigt sie in Gerätezeit an — konsistent mit dieser Regel.*

### 4.11 App-Tech-Stack (Mikro-Phase 2.1 abgeschlossen)

**Entscheidung Android: Kotlin nativ + Jetpack Compose** (statt Flutter/React Native).

Begründung — die Kern-Anforderungen der App sind tief im Android-System verankert, Cross-Platform-Frameworks würden für jeden Punkt Plugin-/Plattformkanal-Krücken brauchen:
- **System-Assistent:** `VoiceInteractionService` (App als "Digitaler Assistent" auswählbar) gibt es nur nativ
- **Wake Word im Hintergrund:** Microphone-Foreground-Service mit korrektem Service-Typ und Battery-Verhalten
- **Overlay-Popup** über anderen Apps (Assistant-Modus)
- **NotificationListenerService**, Intent-basierte Geräte-Tools, später ggf. AccessibilityService

**Windows (Phase 2.5): separater nativer Client** (Empfehlung: .NET/WinUI oder Tauri — finale Entscheidung bei 2.5). Die "zentrale Karten-Definition" wird NICHT über ein gemeinsames UI-Framework gelöst, sondern über das **plattformneutrale Karten-Format** (4.12): beide Apps interpretieren dasselbe JSON.

**Wake-Word-Engine: Picovoice Porcupine** (entschieden, war offener Punkt):
- Kostenlos für Personal Use, sehr geringer CPU-/Akku-Verbrauch, stabile Android-Integration (Maven Central: `ai.picovoice:porcupine-android`)
- Benötigt einen **AccessKey** (console.picovoice.ai), der in den App-Einstellungen hinterlegt wird
- Built-in-Keywords wählbar (Computer, Jarvis, Porcupine, Bumblebee, Terminator); **eigene Phrase** erfordert ein über die Picovoice-Konsole trainiertes `.ppn`-Modell
- Die App kapselt die Engine hinter einem `WakeWordEngine`-Interface, damit später openWakeWord/microWakeWord (ESP32-Parität) nachgerüstet werden kann

**App-interne Architektur-Eckpunkte:** Single-Module Kotlin/Compose-Projekt, manueller DI-Container (bewusst kein Hilt — weniger Build-Magie), Room für Verlauf + Layout-Cache, DataStore für Settings, OkHttp für REST + WebSocket, kotlinx.serialization. APK-Build über GitHub Actions (Debug-Signatur, Sideload-fähig).

### 4.12 Karten-System & zentrale Karten-Verwaltung

Karten ("Skills") sind zweischichtig getrennt:

- **CardEnvelope** `{type, version, title?, data}` — wird vom Orchestrator zur Laufzeit parallel zur Sprachantwort über die WebSocket-Verbindung gepusht. `data` ist frei strukturiert.
- **LayoutTemplate** `{card_type, layout_version, root}` — deklaratives, **plattformneutrales** Layout-JSON. Komponenten: `column`, `row`, `text`, `image`, `icon`, `divider`, `spacer`, `badge`, `progress`, `button`, `list`; Daten-Anbindung über `{{data.*}}`-Bindings; Aktionen `open_url` und `device_tool`. Android interpretiert es mit Compose, Windows (Phase 2.5) mit eigenem Renderer — dasselbe JSON.

**Card-Layout-Server (zentrale Verwaltung):** Endpoint `GET /v1/cards/layouts?since_version=N` des Voice-Orchestrators, Vertrag fixiert in `docs/PROTOCOL.md` + `docs/CARDS.md` im App-Repo. Globale Versionsnummer zählt bei jeder Layout-Änderung hoch; die App pollt beim Start, cacht Templates in Room (höhere `layout_version` gewinnt) und fällt offline auf mitgelieferte Asset-Templates zurück.

**Konsequenzen:**
- **Neue Karten-Layouts erreichen die Clients ohne App-Update** ("Karte deployen = Skill deployen")
- Unbekannte Kartentypen rendern über ein `generic`-Template (`data.headline` + `data.body`) — der Server kann neue Typen einführen, bevor das Layout verteilt ist
- Mitgelieferte Templates: `generic`, `weather`, `list`, `calendar`, `navigation`, `notifications_summary`, `media`

**Verwaltungs-UI (NEU in v1.6):** Bisher war die "zentrale Verwaltung" nur als Server-Endpunkt gedacht (Templates von Hand als JSON gepflegt). Das Admin-Frontend (4.14) ergänzt einen CRUD-Editor darüber: `POST/PUT/DELETE /v1/admin/cards/layouts`, JSON-Paste-Eingabe, Bearbeiten, Löschen. Das Verhalten des Read-Pfads (Versionierung, Fallback) bleibt unverändert.

**HTML-Karten (NEU in v1.12):** Neben den Layout-JSON-Templates gibt es Karten als **HTML** — in zwei Formen:

1. **Gespeicherte HTML-Layouts:** Im Karten-Editor wählbar (Format „JSON" oder „HTML"). Ein HTML-Layout ist ein HTML-Fragment mit denselben `{{data.*}}`-Bindings wie die JSON-Templates; der Client löst die Bindings auf und rendert das Ergebnis **sandboxed** (Web-UI: `<iframe sandbox>` ohne same-origin — kein Zugriff auf Token/Session). Die Verteilung läuft unverändert über `GET /v1/cards/layouts` (additive Felder `format` + `html`; für Alt-Clients wird als `root` das generic-Fallback-Layout mitgeliefert, sie rendern also `data.headline`/`data.body` statt kaputtzugehen).
2. **KI-geschriebene Ad-hoc-Karten:** Das `show_card`-Tool akzeptiert ein neues Feld `html`. Findet das LLM keinen passenden Kartentyp, **schreibt es selbst eine HTML-Karte** — die Tool-Beschreibung fordert es dazu auf und verweist auf den Coding-Agenten (4.16) als Layout-Autor. Die Envelope ist dann `{type: "html", data: {html: "..."}}`; die Web-UI rendert sie sandboxed, die Android-App fällt protokollkonform auf die generic-Karte zurück, bis sie einen HTML-Renderer (WebView) nachzieht.

Sicherheitsrahmen: HTML kommt nur aus zwei Quellen — Admin (Tier 3, Editor) oder dem eigenen LLM zur Laufzeit. Die Sandbox (kein same-origin, kein Zugriff auf `localStorage`/Cookies) ist die eigentliche Grenze; Scripts im HTML sind erlaubt (kleine Interaktivität), laufen aber isoliert.

**Robustheit (v1.12.1, nach erstem Praxistest):** Modelle halten sich nicht zuverlässig an das Argument-Schema — `show_card` sammelt das HTML deshalb tolerant ein (`html`-Feld, `data.html` oder `data` als String) und liefert bei `card_type: "html"` ohne HTML ein **korrigierbares Fehler-Ergebnis** an das LLM zurück, statt eine leere Karte zu pushen. Die Tool-Beschreibung verbietet zusätzlich ausdrücklich, das HTML im Antworttext zu wiederholen (dort erscheint es als roher Code).

**Feinschliff nach drittem Praxistest (v1.12.3):** Drei beobachtete Restprobleme behoben. (1) *Leere Karten bei bekanntem Typ:* Modelle wählen gern `generic`, legen aber eigene Keys statt `headline`/`body` in `data` — die Delegations-Regel greift jetzt auch dann (generic ohne headline/body ⇒ `code-card`). (2) *Nie wieder leere Karten, auch ohne Agent:* Ist kein `code-card`-Agent konfiguriert, werden die `data`-Keys serverseitig zu `headline`/`body` aufbereitet (Schlüssel-Wert-Zeilen) statt Fehler-Pingpong mit dem Modell. (3) *Karten-Spam:* Manche Modelle rufen `show_card` 2–3× mit denselben Daten auf — identische Envelopes werden pro Turn nur einmal gepusht (Wiederholung bekommt einen Hinweis als Tool-Ergebnis). Dazu Rendering-Fixes der Web-UI: HTML-Karten-iframe passt seine Höhe per postMessage an den Inhalt an (kein weißer Leerraum), Wrapper mit transparentem Hintergrund + App-Textfarbe; der `code-card`-Arbeitsauftrag verlangt transparenten Hintergrund und verbietet Titel-Wiederholung.

**Karten-Agent `code-card` (v1.12.2, nach zweitem Praxistest):** Kleine lokale Modelle scheitern in der Praxis daran, komplettes HTML in Tool-Argumente zu packen (Karte kam weiter leer an). Deshalb ist das HTML-Schreiben jetzt **delegiert statt erbeten**: Existiert ein aktiver Agent mit der reservierten ID **`code-card`** (4.16), ruft `show_card` ihn **automatisch serverseitig** auf, sobald kein fertiges HTML mitkommt — bei `card_type: "html"` immer, bei unbekannten Kartentypen, wenn auch das generic-Fallback (`data.headline`/`data.body`) leer bliebe. Das Haupt-LLM liefert nur noch Titel + Daten; der Agent (typisch ein Cloud-Modell) baut das Layout, Markdown-Zäune werden entfernt, und die Delegation erscheint als `tool_activity`-Chip (`agent-code-card`). Der Agenten-Lauf dafür ist bewusst tool-los (ein LLM-Call). Ohne konfigurierten `code-card`-Agent gilt weiter v1.12.1 (LLM liefert das HTML selbst bzw. bekommt Fehler-Feedback).

### 4.13 App ↔ Orchestrator-Protokoll

Verbindlicher Vertrag in `docs/PROTOCOL.md` (App-Repo). **Die App ist fertig gebaut — der Orchestrator implementiert diese Schnittstelle nach.** Kurzfassung:

**REST:**
- `GET /v1/health` — Erreichbarkeits-Check (Server-Auswahl-Screen)
- `POST /v1/auth/login` `{username, password, device_name}` → `{token, user:{name, tier}}` (Bearer-Token für alles Weitere)
- `GET /v1/voices` — Stimmen für die Stimmauswahl
- `GET /v1/cards/layouts?since_version=N` — Karten-Layouts (4.12)

**WebSocket `/v1/assistant/stream`** (JSON-Frames; Audio als Base64-PCM16 — Client sendet 16 kHz für Whisper, Server antwortet mit `sample_rate`-Angabe, typisch 24 kHz XTTS):
- Client → Server: `hello` (mit Mode chat|talk|assist, voice_id und **Geräte-Tool-Manifest**), `text_input`, `audio_chunk`/`audio_end`, `interrupt` (Barge-in), `tool_result`
- Server → Client: `transcript` (partial/final), `assistant_text` (Streaming-Deltas + final), `audio_chunk`/`audio_end`, `card`, `tool_call`, `done`, `error`

**Geräte-Tool-Bridge:** App meldet im `hello` ihre Tools an (`open_app`, `navigate_to`, `dial_number`, `compose_email`, `create_contact`, `web_search`, `set_alarm`, `read_notifications`); das LLM ruft sie über `tool_call` auf, die App führt lokal aus und antwortet mit `tool_result`. `read_notifications` liefert Rohdaten — die Zusammenfassung formuliert das LLM (idealerweise zusätzlich als `notifications_summary`-Karte).

**TTS-Fallback-Regel:** Kommt bis zur `done`-Nachricht kein Server-Audio im Turn, liest die App den Antwort-Text per Android-On-Device-TTS vor (in den Einstellungen abschaltbar).

**Antwort-Modalitäten & Sprach-Kurzfassung (NEU in v1.10):** Der Orchestrator entscheidet die Ausgabe-Modalität nach der Eingabe-Modalität: **Text rein → Text raus** (kein Server-Audio, egal in welchem Modus — die Assistenz liest getippte Chats nicht ungefragt vor), **Audio rein → Sprachantwort + Details im Chat**. Bei langen Antworten (> `VOICE_SUMMARY_MAX_CHARS`, Default 280) wird für die Sprachausgabe per zweitem, kleinem LLM-Call eine **Kurzfassung** (max. zwei gesprochene Sätze) erzeugt; der vollständige Text steht parallel als `assistant_text` im Chat/Popup. Schlägt die Kurzfassung fehl, wird der volle Text gesprochen. Abschaltbar per `VOICE_SUMMARY_ENABLED`.

**Protokoll-Verifikation (v1.10):** Die Frame- und REST-Formate des Orchestrators sind gegen das echte `docs/PROTOCOL.md` des App-Repos abgeglichen (Audio-Feld `data`, Tool-Frames mit `call_id`/`ok`/`result`, Geräte-Manifest im hello-Feld `tools` inkl. `sensitive`, `session`-Frame nach Connect, Auth per `Authorization`-Header auch am WebSocket, `/v1/voices` → `{voices: []}`, `/v1/cards/layouts` → `{version, layouts}`).

**Tool-Aktivitäts-Anzeige (additiv, NEU in v1.12.1):** Neues Server→Client-Frame `tool_activity` `{tool, status: running|done|error}` meldet live, welches Tool bzw. welcher Agent (`agent-*`) gerade arbeitet; `show_card` bleibt bewusst still (die Karte ist die Anzeige). Der Endzustand des Turns wird als `tools`-Liste an der Assistant-Message persistiert (`GET /v1/conversations/{id}`), sodass Chat- UND Voice-Verlauf dieselben Aktivitäts-Chips zeigen wie der Live-Turn. Die Web-UI rendert Chips (🔧 Tool / 🤖 Agent, pulsierend solange running); die Android-App ignoriert das unbekannte Frame protokollkonform. Dokumentiert in `docs/protocol-additions-2.5.md` (Abschnitt 6).

### 4.14 Admin-Frontend & Charakter-/Rechte-Verwaltung (NEU in v1.6)

**Zweck:** Zentrale Verwaltungsoberfläche für den Admin (Tier 3) — Modelle wechseln, Charakter/System-Prompt definieren, Stimmen anlegen/importieren/zuweisen, Karten-Layouts pflegen. Übernimmt damit auch die Rolle des Rechte-Verwaltungs-UIs für Phase 4/5.

**Repo & Deployment:** Kein eigenes Repo — lebt im selben Repo wie der Voice-Orchestrator, um die Protokoll-Drift-Problematik (App-Repo vs. Orchestrator-Repo, siehe 4.13) nicht ein zweites Mal einzuführen. Begründung: gleiche Auth, gleiche DB, gleicher Deploy-Prozess; auf einer 16-GB-RAM-VM lohnt ein separater Service für einen einzigen Admin-Nutzer den Overhead nicht.

**Frontend-Tech-Entscheidung (v1.8, ersetzt den Vite-Plan aus v1.6):** Statisches **Vanilla-JS ohne Build-Step** unter `orchestrator/src/orchestrator/static/admin/` (index.html + app.js + styles.css), von FastAPI unter `/admin` ausgeliefert. Begründung: kein Node-Toolchain im Deploy, kein Framework-Churn, für den Umfang eines Admin-Panels (6 Views, ein Nutzer) völlig ausreichend — und passt zum Lernprojekt-Charakter. Revisionierbar, falls die UI deutlich wächst. Admin-Frontend nutzt denselben Login (`POST /v1/auth/login`) wie die Apps; alle `/v1/admin/*`-Routen erfordern Tier 3 (4.4), die statischen Seiten selbst sind harmlos.

**Persistenz (v1.8):** SQLite (stdlib, bewusst ohne ORM) in einem `/data`-Volume: `users`, `app_settings` (Charakter), `voices`, `filler_triggers`, `fillers`, `card_layouts`. Seeds beim ersten Start: Admin aus `.env`, Standard-Trigger/-Filler (4.3), die sieben Karten-Templates (4.12), zwei Stimmen-Platzhalter.

**Begriffsklärung „Skills":** Im Projekt wurde der Begriff bisher doppelt verwendet (4.12: "Karten (‚Skills')" vs. 1.13: Tool-Integrationen). Ab v1.6 gilt: **Skills** sind Ablauf-Definitionen (in Anlehnung an Claude Skills), die beschreiben, wie der Orchestrator auf bestimmte Situationen reagiert, und dabei auf **MCP-Tools** und **Karten** zurückgreifen. Die Skill-Logik selbst lebt als LangGraph-Flow im Orchestrator (ab 1.12/1.13) — sie wird nicht über das Admin-Frontend verwaltet. Das Admin-Frontend verwaltet nur die darin referenzierten **Karten** (CRUD-Editor, siehe 4.12). MCP-Tool-Server bleiben ausschließlich über LiteLLM registriert (4.6) — daran ändert sich nichts.

**Karten-Editor:** Siehe 4.12 — `GET/POST/PUT/DELETE /v1/admin/cards`, JSON-Paste-Eingabe (kein visueller Drag&Drop-Editor in v1), Liste/Bearbeiten/Löschen bestehender Templates. Jede Änderung erhöht die globale Layout-Version.

**Charakter/System-Prompt:** Ein globaler Default-System-Prompt, der pro Nutzer überschrieben werden kann. Datenmodell siehe Minimal-User-Modell (Mikro-Phase 1.7b): Feld `system_prompt_override` (nullable — leer heißt "nutzt den globalen Default"). Kein Feld für Modell-Bindung pro Nutzer — Modellwahl ist server-weit, da VRAM keine mehreren gleichzeitig geladenen LLMs erlaubt.

**Stimmen-Verwaltung:** `GET/POST/DELETE /v1/admin/voices` + `POST /v1/admin/voices/{id}/sample` (WAV-Upload). Das Voices-Volume ist zwischen Orchestrator und XTTS-Service geteilt — ein Upload aus dem Panel landet direkt dort, wo das Voice-Cloning es erwartet (kein `docker cp`). Zuweisung an Nutzer über `default_voice_id`. `GET /v1/voices` (App-seitig, 4.13) bleibt unverändert.

**Filler-System (NEU in v1.8, ersetzt Piper-Live-Filler als Primärweg):** Filler werden im Admin-Panel angelegt (Titel, gesprochener Text, Trigger) und per XTTS **vorab** pro Stimme generiert (Cache-Matrix Filler × Stimme im `/data`-Volume). Vorteile: identische Stimme für Filler und Hauptantwort (kein hörbarer Piper→XTTS-Stimmbruch) und noch niedrigere Latenz (Datei abspielen statt Synthese). **Trigger sind admin-definierbar:** Art `thinking` (LLM langsam) / `search` (RAG/Web) / `tool` (vor Tool-Calls, optional eingeschränkt per fnmatch-Muster wie `Calendar-*`). Der Orchestrator kennt den Wartegrund selbst und wählt zufällig aus den passenden Fillern; spezifische Tool-Muster schlagen den generischen `*`-Trigger. Fallback-Kette zur Laufzeit: vorgeneriertes XTTS-Audio → Piper-Live-Synthese mit dem Filler-Text → kein Filler. `search`/`tool`-Trigger feuern, sobald Tool-Routing (1.12) bzw. Web-Search (1.6) existieren; bis dahin ist `thinking` der aktive Pfad. Piper (1.9) bleibt als Fallback im Stack, kann perspektivisch entfallen.

**Per-Filler-Delay (NEU in v1.11.2):** Jeder Filler hat ein eigenes `delay_ms` (Admin-Panel, Default 1200, 0 = sofort): Der Orchestrator wählt den Filler zuerst aus und lässt dann dessen Delay **gegen die laufende Arbeit rennen** — ist die LLM-Antwort (thinking) bzw. das Tool (tool) vor Ablauf fertig, entfällt der Filler. Damit blockieren Filler keine schnellen Antworten mehr; träge Aktionen bekommen weiterhin ihre Überbrückung. Das globale `FILLER_DELAY_MS` dient nur noch als Fallback für Datensätze ohne Wert.

**Modell-Wechsel (korrigiert in v1.9.1 — läuft über LiteLLM, nicht LM Studio direkt):** Der ursprüngliche v1.6-Plan (Orchestrator ruft LM Studios native REST-API für Load/Unload) verletzte das 4.6-Prinzip „LiteLLM ist der einzige LLM-Zugang" und hätte einen zweiten Integrationspunkt geschaffen. Stattdessen: Das Admin-Panel listet die **in LiteLLM registrierten Modelle** (`GET /v1/models`) und setzt per „Aktivieren" das **aktive Modell** (`app_settings.active_model`), das der Orchestrator ab sofort für alle LLM-Calls verwendet (pro Call aufgelöst, Fallback `.env`-`LITELLM_MODEL`). Das physische VRAM-Management übernimmt LM Studio selbst: JIT-Loading lädt ein Modell beim ersten Request, Idle-TTL/Auto-Evict entlädt ungenutzte (Standard 60 Min; in LM Studio konfigurierbar). Erster Request nach einem Wechsel ist entsprechend langsam — bewusste, akzeptierte Admin-Aktion. Neue Modelle müssen zuerst in LiteLLM registriert werden, dann erscheinen sie im Panel. Quellen: [LM Studio Idle TTL and Auto-Evict](https://lmstudio.ai/docs/developer/core/ttl-and-auto-evict).

**Auth & Sicherheit:** Alle `/v1/admin/*`-Routen erfordern Tier 3 — dieselbe Middleware wie in 4.4 beschrieben, keine Sonderlogik.

### 4.15 Windows-App & zentrale User-UI (NEU in v1.11 — Architektur "voll zentral")

**Entscheidung (ersetzt die offene Wahl aus 4.11/2.5):** Die Windows-App ist eine **dünne native Shell (Tauri 2, Rust)** um eine **zentrale User-Web-UI**, die der Orchestrator selbst ausliefert. Kein separater UI-Codebestand pro Plattform mehr nötig:

- **User-Web-UI** (`frontend/`, Vite + Svelte 5, ~27 kB gzip) wird unter `GET /app` ausgeliefert (Multi-Stage-Dockerfile baut sie mit; Muster wie `/admin`). Windows-Shell UND jeder Browser laden dieselbe UI **live vom Server** — ein Deploy aktualisiert alle Clients ("UI deployen = App updaten", konsequente Fortführung von 4.12). Enthält: Login, Chat mit PTT + Realtime Talk (WebAudio 16k, Energie-VAD, Barge-in), TTS-Streaming-Player, Karten-Renderer (CARDS.md-Layout-JSON, dritte Implementierung neben Compose), Einstellungen.
- **Zentrale Chat-Historie:** Der Server besitzt die Gespräche (SQLite: `conversations`/`messages` inkl. Karten des Turns); REST `GET/DELETE /v1/conversations[/{id}]`, WebSocket-Frame `conversation` + `hello.conversation_id` zum Fortsetzen. Alle Geräte sehen dieselbe Historie; Gast-/Satelliten-Sessions werden persistiert (Phase 5: nachträgliche Sprecher-Zuordnung), sind aber nicht abrufbar. Android stellt später additiv um (Room wird Cache statt Quelle).
- **Windows-Shell** (Repo `Windows-AI-Assistant-App`) liefert nur, was ein Browser nicht kann: System-Tray/Autostart, globale Hotkeys, **anpinnbare Antwort-Popups über allen Anwendungen** (rahmenlose Topmost-Fenster mit dem Web-Karten-Renderer — Windows-Toasts wären nicht anpinnbar), schwebender Voice-Indikator oben mittig, Desktop-Screenshot, Wake Word (openWakeWord-ONNX-Port der Android-Engine, cpal + ort). Gebündelt ist nur eine Bootstrap-Seite (Server-Auswahl + Versions-Check).
- **Versionierung Shell↔UI:** `GET /app/version.json` deklariert `shell_api_version`; eine ältere Shell bleibt bei Mismatch auf der Bootstrap-Seite ("App-Update nötig") statt eine inkompatible UI zu laden. Befehls-/Event-Vertrag: `docs/protocol-additions-2.5.md`.
- **Screenshot-Flow (zieht 3.2 vor):** Neues `image_input`-Frame (Base64 + optionale Frage, optional `speak`) geht multimodal an LiteLLM (Gemma 4 E4B kann Vision); zusätzlich meldet die Shell `capture_screenshot` als Geräte-Tool an — Tool-Ergebnisse mit `{image_b64, mime}` reicht der Orchestrator als multimodale user-Message in den Agent-Loop ("Hey AI, hilf mir hier" → LLM macht selbst den Screenshot).

### 4.16 Agenten-System (NEU in v1.12)

**Zweck:** Admin-definierbare **Agenten** sind spezialisierte LLM-Läufe mit eigenem Namen, eigener ID, eigener Beschreibung, eigenem System-Prompt und **eigenem Modell** (aus LiteLLM, wie beim Haupt-Modell 4.6). Der Orchestrator (das Haupt-LLM) delegiert Teilaufgaben an sie. Kernmotivation: **Lastverteilung** — für weniger persönliche Aufgaben (z. B. Websuche, Coding) kann ein Cloud-Modell mit gezieltem Prompt arbeiten, während die persönliche Heim-KI lokal bleibt; das entlastet die 11-GB-Karte.

**Datenmodell:** Tabelle `agents` (`slug` = ID für das LLM, `name`, `description`, `system_prompt`, `model`, `enabled`). CRUD über `GET/POST/PUT/DELETE /v1/admin/agents` + Admin-Panel-View „Agenten" (Modell-Dropdown aus LiteLLM, wie 4.14 Modell-Wechsel).

**Anbindung an den Agent-Loop:** Jeder aktive Agent erscheint dem Haupt-LLM als **Tool** `agent-<slug>` mit der Admin-Beschreibung als Tool-Beschreibung und einem einzigen Parameter `task` (die Aufgabenstellung in eigenen Worten). Das Haupt-LLM wählt den Agenten also genau wie jedes andere Tool anhand der Beschreibung — keine Sonderlogik, kein zusätzliches Routing. Damit greifen automatisch auch die bestehenden Mechanismen: Tool-Filler (`agent-*` als fnmatch-Muster möglich, 4.14), Iterations-Obergrenze, Fehler-als-Tool-Ergebnis.

**Ausführung:** Ein Agenten-Aufruf ist ein eigener, in sich geschlossener LLM-Lauf **über LiteLLM** (nie direkt, 4.6) mit dem Modell und System-Prompt des Agenten. Der Agent bekommt dabei die **Server-Tools** (MCP-Gateway) angeboten und darf sie in einer eigenen kleinen Tool-Schleife nutzen (gleiche Iterations-Obergrenze) — so kann ein Websuche-Agent ein Such-MCP-Tool aufrufen, sobald eines registriert ist (1.6). **Keine** Geräte-Tools und kein `show_card` im Agenten-Lauf: die sind session-gebunden und bleiben Sache des Haupt-LLMs, das die Agent-Antwort als Tool-Ergebnis erhält und selbst entscheidet, was es dem Nutzer zeigt (z. B. der Coding-Agent liefert HTML, das Haupt-LLM zeigt es per `show_card` an, 4.12).

**Abgrenzung zu Skills (4.14):** Skills sind Ablauf-Definitionen im Orchestrator-Code; Agenten sind delegierte LLM-Läufe mit eigener Konfiguration. Ein Skill kann perspektivisch Agenten nutzen.

**Reservierte Agent-ID `code-card` (v1.12.2):** Ein aktiver Agent mit dieser ID wird von `show_card` automatisch als Karten-Layouter genutzt (Details in 4.12) — der einzige Agent mit serverseitiger Sonderrolle; alle anderen werden ausschließlich vom Haupt-LLM per Tool-Auswahl aufgerufen.

---

## 5. Phasen-Roadmap mit Mikro-Phasen

> Jede Mikro-Phase liefert ein eigenständig testbares Ergebnis. Reihenfolge ist verbindlich (Abhängigkeiten). **Ausnahme (v1.5): Phase 2 (App-Seite) wurde vorgezogen — die App ist fertig und definiert den Vertrag, den der Orchestrator (1.7+) implementiert. Ausnahme (v1.6): 1.7b nimmt einen Minimal-Ausschnitt aus Phase 4.1 vorweg (Begründung analog zu Phase 2).**

### **PHASE 1 – Infrastruktur & Basis-AI** (Foundation)

#### ✅ Mikro-Phase 1.1: Baseline-Check & Dokumentation – ERLEDIGT
- IST-Stand aller installierten Tools dokumentiert (siehe Abschnitt 3)
- Netzwerk-Topologie skizziert (Baseline-Datei vorhanden)

#### ✅ Mikro-Phase 1.2: LM Studio + LiteLLM-Integration validieren – ERLEDIGT
- LM Studio im Server-Modus mit Gemma 4 E4B
- LiteLLM erreichbar via `http://litellm:4000` (Container) bzw. `http://192.168.2.105:4000` und `http://litellm.ai.lab`
- Testabfrage via curl funktioniert

#### ✅ Mikro-Phase 1.3: Open WebUI an LiteLLM koppeln – ERLEDIGT
- Open WebUI v0.9.5 verbindet sich mit LiteLLM-Backend, Chat funktioniert

#### ✅ Mikro-Phase 1.4: Erstes MCP-Tool integrieren – ERLEDIGT (v1.2)
- `mcp-time` deployed, LiteLLM als MCP-Gateway konfiguriert, 5 Time-Tools aggregiert
- Tool-Calling End-to-End validiert (curl, `/v1/responses` und `/v1/chat/completions`)
- **Erfolgskriterium erfüllt: "Das LLM kann ein externes Tool aufrufen."**

#### ✅ Mikro-Phase 1.5a: Weaviate-Infrastruktur mit lokalem Embedding – ERLEDIGT (v1.3)
- Lokale Registry, Custom-Image `t2v-e5-base`, Weaviate nur mit `text2vec-transformers`, Cross-Container-Erreichbarkeit validiert
- **Erfolgskriterium erfüllt: "Weaviate ist mit lokaler Embedding-Pipeline produktionsbereit."**

#### ✅ Mikro-Phase 1.5b: Schemas anlegen und semantische Suche validieren – ERLEDIGT (v1.4)
- Vier Collections (`Conversations`, `PrivateKnowledge`, `GeneralKnowledge`, `WebKnowledge`), Test-Daten, `nearText`-Suche validiert (6 Testfälle)
- Beobachtetes Verhalten und Tuning-Pfad in 4.9 dokumentiert
- **Erfolgskriterium erfüllt: "Du kannst per API in Weaviate semantisch nach deutschen Inhalten suchen."**

#### ⏭️ Mikro-Phase 1.5c: Retention-Workflow in n8n – NÄCHSTER SCHRITT (Server-Seite)
- n8n-Workflow, täglich (Schedule-Trigger, z. B. 03:00 UTC)
- Cleanup-Regeln pro Collection als konfigurierbare n8n-Variablen:
  - `Conversations`: Tier 1 → 30 Tage, Tier 2/3 → 12 Monate, `importance: high` → unbegrenzt
  - `WebKnowledge`: domain-spezifische TTL, Default 60 Tage
  - `PrivateKnowledge`, `GeneralKnowledge`: keine automatische Löschung
- Vor dem Löschen: Anzahl der zu löschenden Objekte loggen
- **Erfolg:** Workflow läuft, löscht abgelaufene Einträge, hinterlässt Logs. Regel-Änderung wirkt sofort retrospektiv.

#### Mikro-Phase 1.6: Web-Search-Integration (SearXNG + Firecrawl)
- SearXNG-API testen, Firecrawl lokal aufsetzen, Headscale-Routing, MCP-Wrapper in LiteLLM registrieren
- **Erfolg:** Das LLM kann das Web durchsuchen und Seiten crawlen.

#### ✅ Mikro-Phase 1.7: Voice-Orchestrator-Skelett (Python) – CODE-SEITIG FERTIG (v1.7)
- Python-Projekt aufsetzen (FastAPI + LangGraph)
- Minimaler Flow: Text rein → LLM-Call → Text raus
- Anbindung an LiteLLM (LLM und MCP-Tools)
- RAG-Anbindung an Weaviate mit `query:`/`passage:`-Präfix-Konvention (4.9)
- REST-Endpoints aus `docs/PROTOCOL.md` implementieren (`/v1/health`, `/v1/auth/login`, `/v1/voices`, `/v1/cards/layouts`) sowie den WebSocket `/v1/assistant/stream` zunächst im Text-Modus (`text_input` → `assistant_text` → `done`)
- **Erfolg:** Eigener API-Service liefert LLM-Antworten — **direkt testbar aus der Android-App** (Text-Chat inkl. TTS-Fallback-Vorlesen).
- *Stand v1.7: Umgesetzt und mit gemockten Backends getestet (15 Tests). Weaviate bewusst per GraphQL/REST statt v4-Client (kein gRPC-Port eingerichtet). Offen: Validierung gegen die echte VM-Infra; App-Test wartet auf HTTPS (Zertifikats-Umzug läuft).*

#### ✅ Mikro-Phase 1.7b: Minimal-User-/Charakter-Datenmodell – CODE-SEITIG FERTIG (v1.8)
- Leichtgewichtiges Schema (SQLite, stdlib ohne ORM): `users(id, username, password_hash, display_name, tier, system_prompt_override, default_voice_id)` plus `app_settings`, `voices`, `filler_triggers`, `fillers`, `card_layouts`
- Ersetzt NICHT die volle Phase 4.1 (Geräte-Mapping, Permissions-Feinschliff folgen dort) — liefert nur die Felder, die Login-Flow (`user.tier`, 4.4) und Admin-Frontend (1.7c) jetzt schon brauchen
- `POST /v1/auth/login` liest ab jetzt echte User statt Stub
- **Erfolg erreicht:** Login liefert echten Tier + optionalen Charakter-Override aus der DB; WebSocket-Sessions nutzen Charakter-Override und Standard-Stimme des eingeloggten Users (testabgedeckt). Passwörter mit stdlib-scrypt gehasht.

#### ✅ Mikro-Phase 1.7c: Admin-Frontend-Grundgerüst – CODE-SEITIG FERTIG (v1.8)
- Statisches Vanilla-JS unter `/admin` (Tech-Entscheidung siehe 4.14 — kein Vite/Node-Build mehr)
- Login-Screen (nutzt `/v1/auth/login`, nur Tier 3 kommt rein)
- Views: Modelle (LM-Studio-Hot-Swap), Charakter (global + pro Nutzer im Nutzer-Formular), Nutzer-CRUD, Stimmen (Anlegen + WAV-Sample-Upload ins geteilte XTTS-Volume), Filler & Trigger (1.7d), Karten-Editor (JSON-CRUD mit Auto-Version-Bump)
- **Erfolg erreicht (Sandbox):** Kompletter Panel-Durchlauf headless im Browser verifiziert (Login → alle Views → CRUD → Logout, keine JS-Fehler). Der App-Gegencheck „Karte erscheint nach Version-Bump in der App" steht bis zum VM/HTTPS-Test aus.

#### ✅ Mikro-Phase 1.7d: Filler-Verwaltung & Pre-Generierung (NEU in v1.8) – CODE-SEITIG FERTIG
- Konzept siehe 4.14: admin-definierte Trigger (auch tool-spezifisch per Muster), Filler mit Titel/Text/Trigger, XTTS-Pre-Generierung pro Stimme, Laufzeit-Fallback-Kette Cache → Piper → ohne Filler
- Text-Änderung invalidiert den Audio-Cache des Fillers; Trigger-Löschung räumt Filler samt Cache ab (CASCADE)
- **Erfolg erreicht (Sandbox):** Laufzeit-Tests belegen: gecachter Filler wird Piper vorgezogen, spezifisches Tool-Muster schlägt den generischen Trigger, Piper springt ohne Cache ein. Echte XTTS-Generierung folgt auf der VM.

#### ✅ Mikro-Phase 1.8: STT-Service (Whisper) – CODE-SEITIG FERTIG (v1.7)
- faster-whisper als eigenständigen Service, API: Audio rein → Text raus, Latenz messen
- **Erfolg:** Audiodatei hochladen → Text zurück.
- *Stand v1.7: `stt-service/` — WAV (beliebige Rate/Kanäle → 16k mono) oder Roh-PCM16, Antwort mit Latenz-Metriken. Whisper Medium, Deutsch fest. GPU über cuBLAS/cuDNN-pip-Pakete statt CUDA-Basis-Image (auf VM verifizieren). Offen: echte Inferenz + Latenzmessung auf der VM.*

#### ✅ Mikro-Phase 1.9: TTS-Service Piper (Filler-Engine) – CODE-SEITIG FERTIG (v1.7)
- Piper-Container mit deutschem Voice-Modell, API: Text rein → Audio raus
- **Erfolg:** Text-zu-Sprache (schnell, mittlere Qualität).
- *Stand v1.7: `tts-piper/` — Wrapper um das piper-CLI (stabil über Versionen, gegen piper-tts 1.4.2 verifiziert), `de_DE-thorsten-medium` wird beim ersten Start von HuggingFace ins Volume geladen. Offen: Download+Synthese auf der VM (HuggingFace war aus der Entwicklungs-Sandbox geblockt).*

#### ✅ Mikro-Phase 1.10: TTS-Service XTTS-v2 (Hauptstimme) – CODE-SEITIG FERTIG (v1.7)
- XTTS-v2 mit deutschem Voice-Sample, API mit Streaming-Output
- **Erfolg:** Hochwertige deutsche Sprachausgabe per API.
- *Stand v1.7: `tts-xtts/` — coqui-tts-Fork (idiap, das Original kann kein Python 3.11), Streaming als Roh-PCM16/24k, Latents-Cache pro voice_id, Stimmen = Sample-WAVs im `/voices`-Volume. Offen: deutsche Voice-Samples erstellen (offener Punkt), GPU-Inferenz auf der VM.*

#### ✅ Mikro-Phase 1.11: End-to-End-Voice-Loop – CODE-SEITIG FERTIG (v1.7)
- Orchestrator verbindet STT + LLM + TTS, Filler-Logik (Piper parallel zu LLM-Call)
- Audio-Pfad des WebSocket-Protokolls komplettieren (`audio_chunk` rein/raus, `transcript`-Streaming) — danach funktionieren Push-to-Talk, Realtime Talk und der Wake-Word-Assistant-Modus der App End-to-End
- **Erfolg:** Vollständige Voice-Pipeline — vom Handy aus sprechen, Antwort hören.
- *Stand v1.7: Audio-Pfad komplett (`audio_chunk`/`audio_end` rein und raus, `transcript` final, `interrupt`/Barge-in bricht LLM- und Audio-Stream ab, neuer Input = implizites Barge-in). Filler läuft latenzbasiert: erst wenn das LLM länger als `FILLER_DELAY_MS` braucht, spielt Piper eine 4.3-Phrase, resampled auf die Stream-Rate 24 kHz — für die App ein zusammenhängender Stream. End-to-End mit Fake-Backends validiert (`scripts/dev_fake_services.py`); v1-Einschränkungen: `transcript` nur final (kein Streaming-STT), Erfolgskriterium „vom Handy aus" steht bis HTTPS/VM-Deploy aus.*

#### ✅ Mikro-Phase 1.12: Erstes Tool-Calling im Orchestrator – CODE-SEITIG FERTIG (v1.9)
- LangGraph-Flow um Tool-Routing erweitern, 2-3 Beispiel-Tools (Wetter, Web-Search, Zeit)
- Zusätzlich die Geräte-Tool-Bridge (4.13): `hello`-Manifest als session-gebundene LLM-Tools registrieren, `tool_call`/`tool_result` über den WebSocket routen; erste Karten pushen (`card`-Nachricht gegen die mitgelieferten Templates)
- **Erfolg:** Sprachfrage → AI ruft Tool (Server ODER Gerät) → spricht Antwort, Karte erscheint.
- *Stand v1.9: Agent-Loop in LangGraph (retrieve → agent ⇄ tools, Iterations-Obergrenze mit erzwungener Tools-loser Abschlussrunde). Drei Tool-Quellen: LiteLLM-MCP-Gateway (Streamable HTTP, `x-litellm-api-key`; nicht erreichbar = Turn läuft ohne Server-Tools), Geräte-Tools per `tool_call`/`tool_result`-Roundtrip (60s-Timeout wegen Bestätigungs-Dialogen, 4.4), `show_card`-Builtin (Kartentypen aus der DB in der Tool-Beschreibung). Die Admin-Tool-Trigger (1.7d) feuern jetzt vor Tool-Calls, max. ein Filler pro Turn. Beispiel-Tools: Zeit läuft über das bereits registrierte mcp-time; Wetter/Web-Search folgen mit 1.6/1.13 als MCP-Server — das Routing ist generisch. **Offen:** Frame-Feldnamen (`tool_call`/`tool_result`/`card`) gegen `docs/PROTOCOL.md` im App-Repo verifizieren (aus der Entwicklungsumgebung nicht abrufbar); Live-Test gegen echtes LiteLLM-Gateway auf der VM.*

#### Mikro-Phase 1.13: Erste echte Tool-Anbindungen (MCP-Server selbst schreiben)
- Eigene MCP-Server mit FastMCP für: Nextcloud Kalender (CalDAV), Affine Notes (API), Paperless (REST-API)
- Affine sauber neu aufsetzen; eigene Images über lokale Registry (4.7)
- **Erfolg:** "Was steht morgen im Kalender?" funktioniert per Sprache — mit `calendar`-Karte in der App.

#### Mikro-Phase 1.14: Test-Frontend (Open WebUI / Matrix)
**Stark relativiert seit v1.5:** Die Android-App übernimmt diese Rolle, sobald 1.7 steht; das Admin-Frontend (1.7c) übernimmt die Verwaltungs-Rolle. Nur noch umsetzen, falls ein zweites Test-Frontend gebraucht wird — sonst überspringen.

---

### **PHASE 2 – Eigene Apps (Android + Windows)**

#### ✅ Mikro-Phase 2.1: Tech-Stack-Entscheidung – ERLEDIGT (v1.5)
- Android: **Kotlin + Jetpack Compose** (Begründung: 4.11)
- Windows: separater nativer Client, finale Wahl bei 2.5; Karten-Parität über plattformneutrales Layout-JSON (4.12)
- Wake Word: **Porcupine** (4.11)

#### ✅ Mikro-Phase 2.2: Android-App MVP – ERLEDIGT, App-Seite (v1.5)
- Server-Auswahl (Health-Check) → Login → Startscreen: Neuer Chat / Realtime Talk / vergangene Gespräche / Einstellungen
- Einstellungen: Account (Server, User, Tier, Logout), Design/Farben (5 Farbwelten, System/Hell/Dunkel), AI (Stimmauswahl vom Server, Wake Word inkl. AccessKey + Phrase, Rechte-Modus, TTS-Fallback, Systemfreigaben, Stimmerkennungs-Training als Platzhalter für Phase 5)
- Chat mit Push-to-Talk; Realtime Talk mit Dauer-Mikrofon; Audio-Antwort als Stream; Verlauf lokal in Room
- ⏳ **End-to-End-Test offen**, bis der Orchestrator (1.7/1.11) die Gegenseite liefert

#### ✅ Mikro-Phase 2.3: Wake-Word-Integration Android – ERLEDIGT (v1.5)
- Porcupine hinter `WakeWordEngine`-Abstraktion, Microphone-Foreground-Service, Boot-Receiver
- Wake Word → Assistant-Popup: bevorzugt via VoiceInteraction-Session, sonst Overlay-Activity ("Über anderen Apps anzeigen"), sonst High-Priority-Notification
- Erkennung pausiert automatisch, während das Popup selbst das Mikrofon nutzt

#### ✅ Mikro-Phase 2.4: Modal-Anzeige (Komplexe Inhalte) – ERLEDIGT, App-Seite (v1.5)
- App als System-Assistent registrierbar (`VoiceInteractionService`); Assistant-Modus als transluzentes Popup-Overlay über der laufenden App (wie Google Assistant), Karten als schwebende Popup-Karten, zusätzlich inline in Chat/Talk
- Karten-Push via WebSocket (`card`), Rendering über zentrale Layout-Templates (4.12)
- Geräte-Tool-Bridge implementiert (8 Tools, siehe 4.13) inkl. Tiered-Security-Bestätigung (4.4)
- Vorbereitet: `ActionPlanner`-Interface für ein On-Device-Action-Modell (z. B. Gemma-3-270M-Finetune via MediaPipe LLM Inference) für lokale Gerätesteuerung ohne Server-Roundtrip — Integration als offener Punkt
- ⏳ **End-to-End-Test offen** (wie 2.2)

#### ✅ Mikro-Phase 2.5: Windows-App MVP – CODE-SEITIG FERTIG (v1.11)
- **Umgesetzt als Architektur "voll zentral" (4.15):** Tauri-2-Shell (Repo `Windows-AI-Assistant-App`, Branch `claude/exciting-wozniak-cdz75s`) + zentrale User-Web-UI und Chat-Historie im Orchestrator (Branch `claude/windows-app-central-ui`)
- Tray, Hotkeys, anpinnbare Antwort-Popups, Voice-Indikator, Screenshot (inkl. `image_input`/Geräte-Tool), Wake Word (openWakeWord/ONNX) — alles drin; Umfang "alles komplett" wie entschieden
- Konsumiert dasselbe Protokoll (`docs/PROTOCOL.md` + additive Erweiterungen in `docs/protocol-additions-2.5.md`) und dieselben Karten-Layouts (4.12)
- CI baut den NSIS-Installer als Actions-Artifact (`heimai-windows-installer`)
- ⏳ **End-to-End-Test offen:** Deploy des Orchestrator-Branches + Windows-Installer auf echtem PC (Wake-Word-Modelle nach `%APPDATA%\de.heimai.windows\openwakeword\`)
- **Erfolg:** Wake Word und Modal auch am PC.

---

### **PHASE 3 – Screenshot-Analyse**

#### Mikro-Phase 3.1: VLM-Integration
- LLaVA oder ähnliches Vision-Modell evaluieren
- Modell-Swapping-Logik im Orchestrator (LLM zwischenzeitlich entladen) — kann auf die Modell-Hot-Swap-Fähigkeit aus 4.14 aufbauen
- **Erfolg:** Bild-Input → Beschreibung als Text.

#### Mikro-Phase 3.2: Windows-Screenshot-Capture
- Auf Befehl Screenshot via Windows-API, Upload an Orchestrator
- **Erfolg:** "Was siehst du?" → korrekte Antwort über aktuellen Screen.

#### Mikro-Phase 3.3: Game-Specific Workflow
- Screenshot → VLM-Analyse → Web-Search nach Lösung → Antwort
- **Erfolg:** Konkrete Spielfrage wird beantwortet.

*Kompatibilität mit Phase 2 (v1.5): Das WebSocket-Protokoll wird um eine `image_input`-Nachricht erweitert (Envelope-Design lässt das zu); Android ergänzt dann Kamera/Share-Target.*

---

### **PHASE 4 – Basis-User- & Rechte-System**

#### Mikro-Phase 4.1: User-Datenbank
- Postgres oder SQLite für User-Profile; Schema: User, Geräte, Tier-Level, Permissions
- *Hinweis (v1.6): Minimal-Variante bereits in 1.7b vorgezogen (Login, Tier, Charakter-Override, Stimm-Zuweisung). 4.1 erweitert dieses Schema um Geräte-Mapping und die vollständige Permission-Struktur.*

#### Mikro-Phase 4.2: Geräte-basierte Auth
- App-Login mit Token, Gerät → User-Mapping
- *Hinweis (v1.5): Die App nutzt bereits `POST /v1/auth/login` mit `device_name` und speichert Token + Tier — 4.1/4.2 füllen die Server-Seite, der App-Vertrag bleibt stabil.*

#### Mikro-Phase 4.3: Permission-Middleware im Orchestrator
- Vor jedem Tool-Call: Tier prüfen; LiteLLM Virtual Keys pro Tier nutzen
- **Erfolg:** Gäste können keine privaten Daten abfragen.

---

### **PHASE 5 – Stimmerkennung & komplexes Rechte-Management**

#### Mikro-Phase 5.1: Speaker Embeddings
- pyannote.audio oder SpeechBrain, Trainings-Samples aufnehmen
- *Hinweis (v1.5): Einstiegspunkt "Stimmprofil trainieren" existiert bereits in den App-AI-Einstellungen (deaktiviert); Trainings-Upload wird als REST-Endpoint ergänzt.*
- **Erfolg:** Stimmen werden zu Embeddings vektorisiert.

#### Mikro-Phase 5.2: Speaker-ID im Voice-Flow
- STT + Speaker-ID parallel, Tier-Zuweisung dynamisch
- **Erfolg:** AI erkennt, wer spricht.

#### Mikro-Phase 5.3: Kombi-Auth für kritische Aktionen
- Stimme + App-Bestätigung (der Bestätigungs-Mechanismus der App aus 4.4 wird hierfür wiederverwendet)
- **Erfolg:** Sicherheitskritische Aktionen sind doppelt abgesichert.

---

### **PHASE 6 – Erweiterungen (in offener Reihenfolge)**

- Matrix Synapse Anbindung
- Plane Anbindung
- Discord Voice Bot (finale Ausbaustufe)
- News/Wetter-Routinen
- Netzwerk-/Server-Monitoring (read-only)
- Spotify-Steuerung über Sony AVR
- MOVA Home (zurückgestellt)

---

## 6. Offene Punkte / Recherche-Aufgaben

- [ ] **Finale Modellwahl** (Gemma 4 E4B vs. Qwen 3 vs. Llama 3.1) – Benchmark im Live-Setup nach Mikro-Phase 1.11
- [ ] TTS: Evaluation Kokoro, StyleTTS2, Orpheus TTS
- [x] ~~Wake-Word-Engine: Porcupine vs. openWakeWord~~ → **Porcupine** für Android (4.11); openWakeWord/microWakeWord bleibt Kandidat für ESP32-Satelliten
- [ ] Eigene Wake-Word-Phrase definieren → bei Porcupine: Custom-`.ppn` über die Picovoice-Konsole trainieren und in der App hinterlegen
- [ ] Konkrete Voice-Samples für XTTS-v2 erstellen (deutsch)
- [x] ~~App-Tech-Stack finalisieren~~ → Kotlin + Jetpack Compose (4.11); Windows-Stack bei 2.5
- [~] **Lokales DNS mit Zertifikat** – in Arbeit (v1.9.2): echte Domain (`ai.preuss.app` via Hetzner-DNS) statt `.ai.lab` läuft; für ein browservertrauenswürdiges Zertifikat bei LAN-IP fehlt noch die DNS-01-Challenge im Coolify-Traefik (Anleitung in `orchestrator/README.md`). Cleartext-Port 8000 ist entfernt
- [ ] **Upgrade auf `multilingual-e5-large`** sobald die VM mehr RAM hat (Ziel: 128 GB). Reindexing erforderlich.
- [ ] **Slim-Image für Embedding-Sidecar** evaluieren (~5-6 GB Plattenplatz)
- [ ] **e5-Präfixe (`query:`/`passage:`) im Voice-Orchestrator implementieren** (4.9, Phase 1.7+)
- [ ] **Hybrid Search (Vektor + BM25) evaluieren** – sobald Voice-Orchestrator steht
- [ ] **BM25-Stopwords auf Deutsch konfigurieren** – betrifft nur Hybrid Search
- [ ] **Orchestrator-Mock für App-Tests** – Mini-FastAPI mit `/v1/health` + `/v1/auth/login` + Echo-WebSocket (~100 Zeilen), um die App vor 1.7 auf dem Gerät durchspielen zu können
- [ ] **Release-Signing der APK** – aktuell Debug-Signatur (für Sideload ok); für saubere Update-Pfade eigenen Keystore anlegen und im CI signieren
- [ ] **On-Device-Action-Modell** in den `ActionPlanner` integrieren – Pfad: Gemma-3-270M-Finetune ("Mobile Actions") via MediaPipe LLM Inference; offizielles Modell-Artefakt war zum Zeitpunkt v1.5 nicht verifizierbar → prüfen
- [ ] **Binär-WebSocket-Frames statt Base64** für Audio evaluieren (~33 % Overhead; im LAN unkritisch, daher v1 bewusst simpel und debugbar)
- [x] ~~Admin-Frontend-Tech-Stack finalisieren~~ → **Vanilla-JS ohne Build-Step** (v1.8, Begründung 4.14); revisionierbar bei deutlichem UI-Wachstum
- [ ] **Card-Editor-UX** – reiner JSON-Paste in v1; visueller Editor (Formular statt Rohtext) als späterer Ausbau evaluieren
- [ ] **Persona-Datenmodell erweitern** – ggf. weitere Felder (Begrüßungssatz, Tonalität) nach erster Nutzung in 1.7c evaluieren
- [x] ~~LM-Studio-REST-API auf der VM verifizieren~~ → **entfällt (v1.9.1)**: Modell-Panel läuft jetzt über LiteLLM (`GET /v1/models` + aktives Modell in `app_settings`); LM Studio wird nie direkt angesprochen (4.6-Prinzip wiederhergestellt). Voraussetzung stattdessen: **JIT-Loading in LM Studio aktiviert lassen**, damit ein Modellwechsel beim ersten Request automatisch lädt
- [ ] **NEU (v1.8): Piper perspektivisch entfernen** – sobald das Filler-Pre-Generieren auf der VM rund läuft, ist die Fallback-Kette der einzige Piper-Nutzer (RAM sparen)
- [x] ~~XTTS-Latents-Cache bei Sample-Austausch~~ → gelöst (v1.8): Cache prüft die Datei-mtime des Samples und berechnet Latents bei Austausch automatisch neu

---

## 7. Bekannte Probleme

### LiteLLM `reset_budget_job.py` Fehler
**Symptom:** LiteLLM-Logs zeigen kontinuierlich Prisma-Fehler (`MissingRequiredValueError: where.budget_limits.not`).
**Auswirkung:** Kosmetisch. **Status:** Beobachten, bei nächstem Update prüfen. **Workaround:** Keiner nötig.

### Open WebUI v0.9.5 zeigt MCP-Tool-Ergebnisse als leer an
**Symptom:** Tool-Call korrekt, Ergebnis kommt in der UI als leerer String an.
**Auswirkung:** Mittel — Architektur funktioniert (curl-validiert), nur das Test-Frontend zeigt es nicht.
**Status:** Nicht weiter debuggt; Open WebUI ist Übergangslösung, der Orchestrator implementiert die Tool-Schleife selbst.

### LiteLLM MCP-Gateway ist `_experimental`
**Hinweis:** Liegt unter `proxy/_experimental/mcp_server/`; APIs können sich zwischen Versionen ändern.

### Coolify räumt unbenutzte Custom-Images auf
**Status:** Gelöst durch lokale Docker-Registry (4.7).

### Traefik-Routing bei Coolify: Port 80, nicht Port der Anwendung
**Status:** Verstanden. Externe `*.ai.lab`-Aufrufe IMMER ohne expliziten Port.

### Inference-Image enthält kein `wget`
**Status:** Gelöst. Healthchecks über Python (`urllib.request`).

### e5-base hat Hochbias bei Certainty-Werten
**Status:** Verstanden, dokumentiert in 4.9. Nur relative Reihenfolge nutzen, keine absoluten Schwellwerte.

### Spracherkennung fällt aus, sobald ein lokales LLM geladen ist (NEU in v1.11.1)
**Symptom:** Mit Cloud-LLM (Mistral via LiteLLM) funktioniert die Spracherkennung; nach Wechsel auf ein lokales LLM (LiteLLM → LM Studio) schlägt sie fehl. Text-Chat funktioniert weiter.
**Ursache:** VRAM-Verdrängung auf der 11-GB-Karte (Budget 4.2): Whisper läuft auf CUDA und braucht seine ~1,5–3 GB beim (Nach-)Laden. Ist das lokale LLM (5 GB+) plus ggf. XTTS (~3 GB) resident, scheitert Whispers CUDA-Init mit Out-of-Memory — mit Cloud-LLM ist die GPU frei, daher der scheinbare Zusammenhang mit dem LLM-Anbieter.
**Status:** Behoben (v1.11.1) durch **automatischen CPU-Fallback** im STT-Service: Schlägt CUDA beim Laden ODER bei der Inferenz fehl, wechselt Whisper dauerhaft auf CPU (int8) statt den Turn sterben zu lassen — langsamer, aber funktionsfähig; das genutzte Device steht in der Transcribe-Antwort (`device`) und im Log. STT-Fehler tragen jetzt außerdem Klartext-Details bis ins Orchestrator-Log. **Hinweis:** Whisper *Medium* auf CPU ist träge — wenn der CPU-Fallback zum Dauerzustand wird, `WHISPER_MODEL=small` setzen oder das VRAM-Budget neu verteilen (kleineres LLM-Quant).

### "interner Fehler bei der Antwortgenerierung" nur im Audio-Zweig (NEU in v1.11.3, gelöst in v1.11.4)
**Symptom:** Realtime Talk / Voice Message (App und Web-UI) enden mit „interner Fehler bei der Antwortgenerierung", teils nach einem Filler-Audio; Text-Chat mit demselben (lokalen) LLM funktioniert. VRAM unkritisch (~7/11 GB).
**Diagnose-Verbesserung (v1.11.3):** Der generische Sammel-`except` verschluckte die echte Ursache. Jetzt wird der konkrete Fehler (Exception-Typ + Kurztext) an den Client durchgereicht — im Heim-Setup ist der Nutzer Admin, die Diagnose direkt am Gerät spart den Umweg über `docker logs`. Zusätzlich: der LiteLLM-Client hebt den HTTP-Fehlerbody (LiteLLM/LM-Studio-Antwort, z. B. Chat-Template-/Tool-Fehler lokaler Modelle) in die Exception, und der Filler-Pfad ist vollständig gekapselt (ein Filler-Fehler kann den Turn nie mehr zum „internen Fehler" machen — er ist Komfort).
**Ursache (per v1.11.3-Transparenz gefunden):** `LLM-Fehler 400 … invalid_union_discriminator, options: ["object"]` — **LM Studio validiert eingehende Tool-Definitionen strikt** (Zod-Schema) und verlangt am Root des `parameters`-Schemas zwingend `"type": "object"`. Mindestens ein über das MCP-Gateway aggregiertes Tool lieferte ein Schema ohne Root-`type` (bzw. mit Fremdschlüsseln wie `$schema`) — LM Studio lehnt dann den **gesamten** Request ab. Cloud-Anbieter (Mistral) sind tolerant, daher trat es nur mit lokalem LLM auf. Und nur im Audio-Zweig sichtbar, weil der Sprach-Turn der Haupt-Testpfad mit Tools war; der Fehler betriff jeden Turn mit Tool-Angebot.
**Status:** Gelöst (v1.11.4). Der ToolExecutor normalisiert **jedes** Parameter-Schema vor dem LLM-Call (`_ensure_object_schema`): Root-`type` wird auf `object` gezwungen, `properties` ist garantiert ein Dict, `$schema` fliegt raus — quellenunabhängig (show_card, Geräte-Tools, MCP-Gateway). Merksatz: Schemas aus fremden MCP-Servern nie ungeprüft an strikte lokale Runtimes durchreichen.

### Android: Mic-FGS-Restriktionen ab Android 15
**Symptom:** Wake-Word-Service startet nach einem Reboot nicht automatisch.
**Ursache:** Microphone-Foreground-Services dürfen ab Android 15 nicht mehr aus `BOOT_COMPLETED` heraus starten.
**Workaround:** App nach Reboot einmal öffnen. Zusätzlich die App von der OEM-Akku-Optimierung ausnehmen (Samsung/Xiaomi etc. beenden den Service sonst).

### Android: Ein Mikrofon-Konsument zur Zeit
**Symptom:** Wake-Word-Erkennung und Assistant-Aufnahme können nicht gleichzeitig laufen.
**Status:** Gelöst per Design — das Assistant-Popup pausiert die Wake-Word-Engine beim Öffnen und reaktiviert sie beim Schließen.

### Android: Dauerhafter Mikrofon-Indikator
**Symptom:** Bei aktivem Wake Word zeigt Android permanent den grünen Mikrofon-Indikator und eine Foreground-Notification.
**Status:** Erwartetes Plattform-Verhalten (Privacy-Feature), kein Bug. Akku-Last durch Porcupine selbst ist gering; dominanter Faktor ist das offene Mikrofon.

### Coolify: Compose-Pfade sind Repo-Root-relativ (NEU in v1.7)
**Symptom:** `failed to read dockerfile: open Dockerfile: no such file or directory` beim Deploy, obwohl Base Directory gesetzt ist.
**Ursache:** Coolify führt `docker compose` mit `--project-directory <Repo-Root>` aus — das Base Directory steuert nur, wo die Compose-Datei *gesucht* wird, nicht wie Pfade darin aufgelöst werden.
**Status:** Gelöst. Alle `build.context`-Pfade in `orchestrator/docker-compose.yml` sind Repo-Root-relativ (`./orchestrator`, `./stt-service`, …); `env_file` ist optional markiert, weil `.env` gitignored ist.

### Browser zeigt {"detail":"Not Found"} auf der Orchestrator-Domain (NEU in v1.7)
**Symptom:** Aufruf der nackten Domain sieht nach kaputtem Deploy aus.
**Ursache:** Kein Fehler — das ist FastAPIs Standard-404: Der Service lief, es gab nur keine Route auf `/`.
**Status:** Gelöst. `GET /` liefert jetzt Service-Info mit Verweis auf `/docs` und `/v1/health`.

### Browser stuft HTTPS-Domain als "Nicht sicher" ein (NEU in v1.9.2)
**Symptom:** Seite lädt über `https://`, wird aber als unsicher markiert.
**Ursache:** Traefik liefert sein Self-Signed-Fallback-Zertifikat ("TRAEFIK DEFAULT CERT"), weil die HTTP-01-Challenge bei einer Domain, die auf eine private LAN-IP zeigt, zwangsläufig scheitert.
**Lösung:** DNS-01-Challenge über die Hetzner-DNS-API (Coolify-Doku "Switch Traefik to DNS Challenge"); Schritte in `orchestrator/README.md`. Zusätzlich ist der direkte Klartext-Port 8000 aus der Compose entfernt — Zugriff nur noch über die HTTPS-Domain.

### XTTS-Stimmgenerierung bricht mit "incomplete chunked read" ab (NEU in v1.9.2, Ursache gefunden in v1.9.3)
**Symptom:** Filler-Generierung meldet `peer closed connection without sending complete message body`.
**Diagnose-Weg:** Weil die Stream-Header schon gesendet waren, wurde jeder Fehler zum kryptischen Verbindungsabriss. Seit dem Stream-Priming (v1.9.2, erster Audio-Chunk vor der Antwort) kommen Fehler als Klartext-500 an — und genau das legte die echte Ursache frei:
**Ursache:** `No module named 'torch'` — das PyPI-Paket `coqui-tts` (idiap-Fork, 0.27.5) deklariert **torch nicht als Abhängigkeit**, sondern erwartet ein vorinstalliertes PyTorch. Das Docker-Image hatte daher alles außer torch.
**Status:** Gelöst (v1.9.3): `torch`/`torchaudio` explizit im `engine`-Extra von `tts-xtts`. Achtung beim Redeploy: Der Image-Build lädt jetzt die vollen PyTorch-CUDA-Wheels (~2,5 GB) — dauert einmalig entsprechend.
**Nachschlag (v1.9.4):** Danach zweiter Versions-Konflikt derselben Sorte — `coqui-tts` deklariert auch für `transformers` keine Obergrenze, nutzt aber 4.x-Interna (`isin_mps_friendly`, in transformers 5 entfernt). Gelöst durch Pin `transformers>=4.43,<5` (Lock: 4.57.6, Funktion dort verifiziert). Merksatz für 1.13+: Beim idiap-Fork von Coqui die transitiven Versionen im Lock gegenprüfen — er pflegt seine Grenzen nicht sauber.
**Nachschlag (v1.9.5):** Dritte Schicht — ab PyTorch 2.9 verlangt torchaudio für Audio-I/O die separate `torchcodec`-Bibliothek. Gelöst über Coquis eigenes Extra (`coqui-tts[codec]`, Lock: torchcodec 0.14.0) plus `ffmpeg` per apt im Image (torchcodec lädt FFmpeg-Bibliotheken zur Laufzeit, die Wheels bringen sie nicht mit).

---

## 8. Wichtige Constraints & Reminder

- **VRAM ist der Bottleneck** – jede neue Komponente am Modell-Profil prüfen
- **RAM ist aktuell ebenfalls knapp** (16 GB) – Ziel langfristig 128 GB
- **Lernprojekt** – jeder Schritt mit Erklärung des "Warum", nicht nur "Wie" (Ausnahme Phase 2, siehe Abschnitt 1)
- **Eigenständig testbar** – jede Mikro-Phase hat ein konkretes Erfolgskriterium
- **Faktenbasis** – keine spekulativen Aussagen, im Zweifel Recherche/Quelle
- **Security First** – speziell bei Tier-2/3-Aktionen
- **Privacy** – alles soweit möglich lokal, Cloud nur wo unvermeidbar. Embeddings laufen lokal, NICHT über externe APIs. *App: Benachrichtigungsdaten bleiben in-memory auf dem Gerät und verlassen es nur nach explizitem (ggf. bestätigtem) Tool-Aufruf.*
- **MCP-First** – alle Tools als MCP-Server in LiteLLM; **dokumentierte Ausnahme:** session-gebundene Geräte-Tools der Apps (4.6/4.13)
- **Custom-Image-First** – eigene Images über die lokale Registry
- **UTC-First für Timestamps** (4.10)
- **Retention-Regeln zentral in n8n**, nicht im Datenschema
- **Protokoll-Vertrag ist fixiert** – Änderungen an `docs/PROTOCOL.md` müssen App UND Orchestrator berücksichtigen (die App ist bereits gebaut!)
- **Karten-Layouts sind plattformneutral** – keine Android-spezifischen Features in Templates; Windows rendert dasselbe JSON
- **NEU (v1.6): Ein Repo, zwei Codebasen** – Orchestrator und Admin-Frontend werden gemeinsam versioniert und deployed (4.14), um eine dritte Protokoll-Drift-Front (neben App↔Orchestrator) zu vermeiden
- **NEU (v1.6): Skills ≠ Karten** – Skills sind Orchestrator-interne Ablauf-Definitionen, Karten sind nur ihre visuelle Ausgabeschicht; Begriff nicht mehr synonym verwenden (4.14)

---

## 9. Kommunikationsregeln für Claude

- Bei jeder Mikro-Phase **Schritt-für-Schritt-Anleitung** geben, als wäre es das erste Mal
- **Code-Beispiele** vollständig und kommentiert
- Bei Unklarheiten **nachfragen**, nicht raten
- Proaktive Vorschläge bei besseren Alternativen
- Dokument am Ende jeder Phase **aktualisieren** (Versionierung)

---

**Version:** 1.12.4
**Stand:** 2026-07-16
**Changelog:**
- v1.12.4 (2026-07-16): **Karten-Disziplin + verifiziertes iframe-Autosize** (vierter Praxistest: 4× `code-card`-Aufruf für eine Karte, iframe passte sich nicht an, Modell wiederholte den Karteninhalt als Text-Tabelle). (1) Identische `show_card`-Anfragen werden jetzt VOR der Generierung abgefangen (der Agent läuft genau einmal) und Generierungen sind auf 2 pro Turn gedeckelt — darüber hinaus wird aus den Daten eine generic-Karte gebaut. (2) iframe-Autosize umgebaut auf ID-basierte postMessage-Zuordnung + Breiten-Guards (box-sizing, `max-width:100%` für alle Kindelemente, Tabellen auf 100%) und erstmals **real in Chromium verifiziert** (Playwright: Höhe folgt dem Inhalt, kein horizontaler Overflow bei 600px-Tabelle). (3) Das Erfolgs-Ergebnis von `show_card` weist das Modell jetzt explizit an, den Karteninhalt nicht in der Text-Antwort zu wiederholen. 85 Tests grün.
- v1.12.3 (2026-07-16): **Karten-Feinschliff nach drittem Praxistest** (Screenshot: eine gerenderte Karte mit weißem Leerraum, dazu leere Karten bei `generic` mit eigenen Keys und 3× dieselbe Karte). Delegation an `code-card` greift jetzt auch bei `generic` ohne headline/body; ohne Agent werden data-Keys zu headline/body aufbereitet (nie mehr leere Karten); identische Karten werden pro Turn nur einmal gepusht. Web-UI: iframe-Höhe folgt dem Inhalt (postMessage aus der Sandbox), transparenter Hintergrund + App-Textfarbe; code-card-Auftrag: transparenter Hintergrund, Titel nicht wiederholen. 82 Tests grün.
- v1.12.2 (2026-07-16): **Karten-Agent `code-card`** (Nutzer-Report: Karte kam trotz v1.12.1 leer an — kleine lokale Modelle packen kein HTML in Tool-Argumente; Nutzer-Entscheidung: Karten-HTML schreibt der Agent `code-card`, nicht der generische Coding-Agent). `show_card` delegiert das Layout jetzt **automatisch serverseitig** an den reservierten Agenten `code-card`, wenn kein fertiges HTML mitkommt (bei `card_type html` immer; bei unbekannten Typen, wenn auch headline/body fehlen). Haupt-LLM liefert nur Titel + Daten; Delegation sichtbar als `tool_activity`-Chip `agent-code-card`; Agent-Ausfall wird zum korrigierbaren Tool-Fehler. Tool-Beschreibung passt sich an (mit Agent: „liefere nur Daten", ohne: wie bisher selbst schreiben). 79 Tests grün.
- v1.12.1 (2026-07-16): **HTML-Karten-Robustheit + Tool-Aktivitäts-Anzeige** (Nutzer-Report: Karte kam leer an, HTML stand als roher Code im Antworttext). `show_card` sammelt HTML jetzt tolerant ein (`html`-Feld, `data.html`, `data` als String) und gibt bei `card_type html` ohne Inhalt ein korrigierbares Fehler-Ergebnis ans LLM statt eine leere Karte zu pushen; Tool-Beschreibung verbietet HTML im Antworttext ausdrücklich. Neu: additives Frame `tool_activity` (running/done/error) + `tools`-Liste an der Assistant-Message — Chat- und Voice-Verlauf zeigen als Chips, welche Tools/Agenten die KI aufgerufen hat (Web-UI live und in der Historie; Android ignoriert das Frame). 75 Tests grün.
- v1.12 (2026-07-15): **Agenten-System (neue Sektion 4.16) + HTML-Karten (4.12 erweitert).** Agenten = admin-definierbare, spezialisierte LLM-Läufe (Name, ID/Slug, Beschreibung, System-Prompt, eigenes LiteLLM-Modell) — Lastverteilung: unpersönliche Aufgaben (Websuche, Coding) an Cloud-Modelle delegieren, Heim-KI bleibt lokal. Jeder aktive Agent erscheint dem Haupt-LLM als Tool `agent-<slug>`; Ausführung als eigener LiteLLM-Lauf mit MCP-Server-Tools (keine Geräte-Tools/Karten — session-gebunden). Admin-Panel-View „Agenten" + `/v1/admin/agents`-CRUD. Karten: neben Layout-JSON jetzt **HTML-Layouts** (Editor mit Format-Auswahl, `{{data.*}}`-Bindings, sandboxed iframe in der Web-UI, generic-Fallback für Alt-Clients) und **KI-geschriebene Ad-hoc-HTML-Karten** über das neue `show_card`-Feld `html` (kein passendes Layout → LLM schreibt selbst eins, z. B. via Coding-Agent).
- v1.11.4 (2026-07-09): **Audio-Zweig-Fehler gelöst** — die v1.11.3-Transparenz lieferte die echte Ursache: LM Studio validiert Tool-Definitionen strikt (Zod) und verlangt `"type": "object"` am Root des Parameter-Schemas; ein MCP-Gateway-Tool ohne Root-type ließ LM Studio den gesamten Request mit 400 `invalid_union_discriminator` ablehnen (Cloud-Modelle tolerant, daher nur lokal sichtbar). Fix: ToolExecutor normalisiert jedes Tool-Schema vor dem LLM-Call (Root-type object, properties-Dict, `$schema` entfernt), quellenunabhängig. 56 Tests grün.
- v1.11.3 (2026-07-09): **Fehlerdiagnose im Audio-Zweig** (Nutzer-Report: "interner Fehler bei der Antwortgenerierung" nur bei Sprache, Text funktioniert). Der Sammel-except reicht jetzt Exception-Typ + Kurztext an den Client durch; LiteLLM-Client hebt HTTP-Fehlerbody in die Exception (Chat-Template-/Tool-Fehler lokaler Modelle werden sichtbar); Filler-Pfad vollstaendig gekapselt (Komfort-Feature kann den Turn nicht mehr abbrechen). 55 Tests gruen. Eigentliche Ursache wird aus der jetzt sichtbaren Meldung beim naechsten Test bestimmt.
- v1.11.2 (2026-07-09): **Per-Filler-Delay** (Nutzer-Feedback: Filler blockierten schnelle Antworten): jeder Filler traegt ein eigenes `delay_ms` (DB-Migration additiv, Admin-Panel mit Delay-Spalte, Formularfeld und neuer Bearbeiten-Funktion). Abspiel-Logik als Rennen Delay vs. LLM-Antwort/Tool - schnelle Arbeit gewinnt, Filler entfaellt; 0 = sofort. Gilt fuer thinking- UND tool-Trigger (Tool laeuft dafuer als Task). 53 Tests gruen.
- v1.11.1 (2026-07-08): **Spracherkennungs-Ausfall bei lokalem LLM behoben** (VRAM-Verdrängung, siehe Bekannte Probleme): STT-Service mit automatischem CPU-Fallback bei CUDA-Fehlern (Laden UND Inferenz), genutztes Device in der Antwort, Klartext-Fehler bis ins Orchestrator-Log.
- v1.11 (2026-07-08): **Mikro-Phase 2.5 (Windows-App) code-seitig umgesetzt — Architektur-Entscheidung "voll zentral" (neue Sektion 4.15):** Der Orchestrator liefert die User-Web-UI selbst aus (`/app`, Vite + Svelte 5 in `frontend/`, Multi-Stage-Dockerfile) und besitzt die **zentrale Chat-Historie** (`conversations`/`messages` in SQLite, REST `GET/DELETE /v1/conversations[/{id}]`, neues WebSocket-Frame `conversation`, `hello.conversation_id` zum Fortsetzen — alle Geräte sehen dieselben Gespräche). Neues `image_input`-Frame + Bild-Konvention der Geräte-Tool-Bridge (`{image_b64, mime}` → multimodale user-Message; zieht 3.2 client-seitig vor, Gemma 4 E4B ist multimodal). Windows-Repo enthält nur noch die **Tauri-2-Shell**: Tray/Autostart, globale Hotkeys, anpinnbare Antwort-Popups + Voice-Indikator als Topmost-Fenster (rendern die zentrale Karten-UI unter `#/popup`/`#/indicator`), Screenshot (xcap), Wake Word als openWakeWord-ONNX-Port der Android-Engine (cpal + ort), Bootstrap-Seite mit `shell_api_version`-Guard. CORS-Middleware für die Bootstrap-Origin. Alle Protokoll-Erweiterungen additiv und abwärtskompatibel zur Android-App (verifiziert: unbekannte Frames werden ignoriert), dokumentiert in `docs/protocol-additions-2.5.md`. 51 Orchestrator-Tests grün; Shell per cargo check validiert, Windows-Installer via GitHub Actions. **Offen:** E2E-Test auf echtem PC, Einpflegen der Erweiterungen in `docs/PROTOCOL.md` (App-Repo), Android-Umstellung auf zentrale Historie (additiv, später).
- v1.10 (2026-07-04): **Protokoll-Abgleich mit der echten App** (`docs/PROTOCOL.md` war nun einsehbar): Audio-Frames nutzen `data`, Tool-Frames `call_id`/`ok`/`result`, Geräte-Manifest im hello-Feld `tools`, neues `session`-Frame nach Connect, WebSocket-Auth per `Authorization`-Header, `/v1/health`/`/v1/voices`/`/v1/cards/layouts` in den App-Formaten. **Neue Antwort-Modalitäten:** Text rein → Text raus; Audio rein → Sprachantwort + Details im Chat, mit LLM-Kurzfassung für die Sprachausgabe bei langen Antworten (4.13). Beides live gegen Fake-Backends validiert; 42 Tests grün.
- v1.9.5 (2026-07-03): Dritter XTTS-Abhängigkeits-Fix: `coqui-tts[codec]` (torchcodec — ab PyTorch 2.9 Pflicht für torchaudio-Audio-I/O) + `ffmpeg` im Image.
- v1.9.4 (2026-07-03): Zweiter XTTS-Abhängigkeits-Fix: `transformers<5` gepinnt (coqui-tts nutzt `isin_mps_friendly` aus der 4.x-API, in v5 entfernt; Lock jetzt 4.57.6, Vorhandensein der Funktion im Wheel verifiziert).
- v1.9.3 (2026-07-03): **XTTS-Stimmgenerierung repariert:** Ursache des Generierungs-Fehlers war ein fehlendes PyTorch im XTTS-Image (`coqui-tts` deklariert torch nicht als Abhängigkeit) — sichtbar geworden durch das Stream-Priming aus v1.9.2. `torch`/`torchaudio` jetzt explizit im engine-Extra. Erstes Let's-Encrypt-Zertifikat für `ai.preuss.app` ist ausgestellt (Viewer bestätigt LE/YR2); verbleibende "Nicht sicher"-Anzeige wird über Browser-Neustart/DevTools-Security-Tab bzw. Ketten-Check diagnostiziert (Android braucht die volle Zertifikatskette — Hinweise in README).
- v1.9.2 (2026-07-03): **HTTPS-Härtung + Stimmgenerierungs-Diagnose.** Orchestrator läuft hinter Traefik mit `--proxy-headers`; direkter Klartext-Port 8000 aus der Compose entfernt (Zugriff nur noch über die HTTPS-Domain, README erklärt die "Nicht sicher"-Diagnose inkl. DNS-01-Challenge via Hetzner für LAN-IPs). XTTS-Service: Stream-Priming — erster Audio-Chunk wird vor der Response erzeugt, damit Fehler (Modell-Laden, Latents, kaputte Samples, CUDA-OOM) als Klartext-500 ankommen statt als "incomplete chunked read"; Mid-Stream-Abrisse werden mit Traceback geloggt. Orchestrator übersetzt Stream-Abrisse in eine Admin-taugliche Checkliste (docker logs / dmesg / nvidia-smi). Zwei neue Bekannte-Probleme-Einträge.
- v1.9.1 (2026-07-03): **Architektur-Korrektur Modell-Panel:** lief fälschlich direkt gegen LM Studios REST-API und verletzte damit das 4.6-Prinzip (LiteLLM als einziger LLM-Zugang). Jetzt: Modell-Liste aus LiteLLM (`/v1/models`), „Aktivieren" setzt `app_settings.active_model` (greift sofort für alle LLM-Calls, Fallback `.env`), physisches Laden/Entladen via LM Studios JIT/Idle-TTL. `LMSTUDIO_BASE_URL` und der LM-Studio-Client sind entfernt. 4.14 entsprechend umgeschrieben.
- v1.9 (2026-07-03): **Mikro-Phase 1.12 code-seitig umgesetzt** — Tool-Calling als LangGraph-Agent-Loop mit drei Quellen: LiteLLM-MCP-Gateway (4.6, offizielle mcp-SDK über Streamable HTTP, degradiert sauber bei Nichterreichbarkeit), session-gebundene Geräte-Tools aus dem `hello`-Manifest (4.13, `tool_call`/`tool_result` über den WebSocket mit Timeout), `show_card`-Builtin für Karten-Push (4.12). Tool-Trigger aus 1.7d sind damit aktiv (Filler vor Tool-Calls, spezifische Muster vor generischen, max. einer pro Turn). 38 Tests grün; Tool-Loop inkl. Karten-Push zusätzlich live über den WebSocket gegen Fake-Backends validiert. **Offen:** Frame-Feldnamen gegen `docs/PROTOCOL.md` (App-Repo) prüfen, VM-Test gegen echtes Gateway.
- v1.8 (2026-07-03): **Mikro-Phasen 1.7b/1.7c/1.7d code-seitig umgesetzt.** SQLite-Persistenz (stdlib, ohne ORM) für User/Charakter/Stimmen/Trigger/Filler/Karten; Login liest echte User (scrypt-Hashing), WebSocket-Sessions nutzen Charakter-Override + Standard-Stimme des Users. **Admin-Panel unter `/admin`** (Entscheidung: Vanilla-JS ohne Build-Step statt Vite, 4.14) mit Modelle/Charakter/Nutzer/Stimmen/Filler&Trigger/Karten — headless im Browser durchgetestet. **Neues Filler-Konzept (1.7d):** Filler werden per XTTS pro Stimme vorgeneriert statt live von Piper gesprochen (kein Stimmbruch, niedrigere Latenz); Trigger sind admin-definierbar inkl. tool-spezifischer Muster (`Calendar-*`), Fallback-Kette Cache → Piper → ohne Filler. Stimmen-Sample-Upload über das Panel ins geteilte XTTS-Volume (kein `docker cp`). Karten-CRUD mit automatischem Version-Bump. XTTS-Latents-Cache invalidiert jetzt per Sample-mtime. nvidia-container-toolkit auf der VM installiert (GPU-Deploy funktioniert). 32 Tests grün; VM-Validierung (echte XTTS-Generierung, LM-Studio-API-Format) und App-E2E (wartet auf HTTPS) offen.
- v1.7 (2026-07-03): **Mikro-Phasen 1.7–1.11 code-seitig umgesetzt** (Repo `Local-AI-Voice-Orchastrator`, Monorepo mit `orchestrator/`, `stt-service/`, `tts-piper/`, `tts-xtts/`). Orchestrator: Protokoll-Endpoints + WebSocket mit komplettem Audio-Pfad, latenzbasierter Filler-Logik (Piper → 24k resampled), Barge-in, RAG mit e5-Präfixen und Relativ-Filter (4.9), Tier-1-Ausschluss von PrivateKnowledge (4.4). Services jeweils mit lazy geladener Engine (Tests ohne GPU/Modelle), eigenem Dockerfile und Modell-Download in Volumes beim ersten Start. Gemeinsames Coolify-Deploy über eine Compose-Datei (GPU-Reservierung für STT/XTTS — nvidia-container-toolkit auf der VM nötig). End-to-End mit Fake-Backends validiert; **offen: Validierung auf der echten VM** (GPU-Inferenz, Modell-Downloads, deutsche XTTS-Voice-Samples) und App-Test nach HTTPS-Umstellung (Zertifikate via Hetzner in Arbeit). Coolify-Pfad-Eigenheit dokumentiert (--project-directory = Repo-Root). Bekanntes-Problem-Eintrag: FastAPI-404 auf `/` war kein Deploy-Fehler → Root-Route ergänzt.
- v1.6 (2026-07-01): Vorbereitung der Mikro-Phasen 1.7–1.11 (Voice-Orchestrator). Neue Sektion **4.14 Admin-Frontend & Charakter-/Rechte-Verwaltung** — zentrale Verwaltungsoberfläche für Modelle, Charaktere/System-Prompts, Stimmen und Karten-Layouts, entschieden als Monorepo mit dem Voice-Orchestrator (kein eigenes Repo), um Protokoll-Drift wie zwischen App- und Orchestrator-Repo zu vermeiden. Neue Mikro-Phasen **1.7b** (Minimal-User-/Charakter-Datenmodell, Vorzug aus 4.1) und **1.7c** (Admin-Frontend-Grundgerüst). Begriffsklärung „Skills" (Ablauf-Definitionen im Orchestrator, referenzieren MCP-Tools + Karten; MCP-Verwaltung bleibt bei LiteLLM, Admin-Frontend verwaltet nur die Karten-Seite). Modell-Hot-Swap-Fähigkeit von LM Studio recherchiert und dokumentiert (native REST-API mit JIT/TTL, Quellen in 4.14). Neuer Eintrag in Abschnitt 3 (Codebasen). Offener Punkt ergänzt: Admin-Frontend-Tech-Stack (SPA vs. htmx) noch nicht final.
- v1.5 (2026-06-12): **Phase 2 (Android, Mikro-Phasen 2.1–2.4) app-seitig komplett umgesetzt** — Repo `Android-AI-Assistant-App`, CI-Build grün, Debug-APK als Actions-Artifact. Neue Architektur-Sektionen: 4.11 App-Tech-Stack (Kotlin+Compose statt Flutter, Porcupine als Wake-Word-Engine), 4.12 Karten-System & Card-Layout-Server (zentrale Verwaltung, Layout-Updates ohne App-Update, plattformneutral für Windows), 4.13 App↔Orchestrator-Protokoll inkl. Geräte-Tool-Bridge und TTS-Fallback-Regel. Mikro-Phasen 1.7/1.11/1.12 um Protokoll-Implementierung erweitert, 1.14 relativiert. Tiered-Security-Umsetzung in der App dokumentiert (4.4); Geräte-Tools als dokumentierte MCP-Ausnahme (4.6). Drei neue bekannte Probleme (Android-15-FGS, Mikrofon-Exklusivität, Mikrofon-Indikator). Offene Punkte: Wake-Word-Engine und App-Stack entschieden; neu: Orchestrator-Mock, Release-Signing, On-Device-Action-Modell, Binär-Frames.
- v1.4 (2026-06-04): Mikro-Phase 1.5b abgeschlossen (4 Collections, Test-Daten, semantische Suche validiert). Neue Mikro-Phase 1.5c (Retention-Workflow). Neue Sektionen 4.9 (RAG-Tuning) und 4.10 (UTC-First). e5-Hochbias dokumentiert.
- v1.3 (2026-05-29): 1.5 in 1.5a/1.5b aufgeteilt. Neue Sektionen 4.7 (Registry) und 4.8 (Embedding-Pipeline).
- v1.2 (2026-05-11): Mikro-Phase 1.4 abgeschlossen. LiteLLM als zentrales MCP-Gateway. mcp-time deployed.
- v1.1 (2026-04-29): Headscale als "noch nicht installiert" markiert.

**Nächster Schritt:** Branch `claude/windows-app-central-ui` mergen/deployen (Redeploy baut die `/app`-UI mit), dann der große Praxis-Test: Admin-Panel, Voice-Loop, erster Tool-Call — und neu: **Windows-Installer aus den Actions-Artifacts installieren, Server verbinden, Chat/PTT/Realtime/Popups/Hotkeys/Screenshot/Wake Word durchspielen** (Wake-Word-Modelle nach `%APPDATA%\de.heimai.windows\openwakeword\`). Die zentrale Historie auch aus dem Browser gegenprüfen (`https://<server>/app`). Danach: Protokoll-Erweiterungen in `docs/PROTOCOL.md` (App-Repo) einpflegen; code-seitig 1.5c (Retention), 1.6 (Web-Search), 1.13 (eigene MCP-Server).
