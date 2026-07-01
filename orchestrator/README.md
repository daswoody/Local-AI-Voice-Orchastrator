# Voice-Orchestrator (Mikro-Phase 1.7)

Siehe `../docs/heim-ai-projektspezifikation.md` fuer den Gesamtkontext.

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

## Bekannte Einschraenkungen dieser Mikro-Phase

- Login ist ein Stub (ein Admin-User aus der Config) bis 1.7b.
- `/v1/voices` liefert zwei feste Platzhalter-Stimmen bis 1.7c/1.10.
- Tool-Calling (MCP) kommt erst in 1.12 - der LLM-Call ist reines
  Text-Frage/Antwort ohne Tools.
- WebSocket kennt nur `text_input`; Audio folgt in 1.11.
