# tts-engine-kit - TTS-Engine-Vertrag v1 (Heim-AI v1.20)

Gemeinsame Grundlage fuer Sprachausgaben (TTS-Engines) als eigene Container.
Eine Engine, die diesen Vertrag spricht, braucht im Orchestrator **keinen
Code**: Im Admin-Panel unter *Sprachausgabe -> Engines nach dem
Engine-Vertrag* nur ID und Adresse eintragen. Name, Sprachen und
Faehigkeiten meldet sie selbst, Laden/Entladen und Kartenwahl steuert das
Panel (GPUs).

Erste Engine: [`tts-qwen3/`](../tts-qwen3) (Qwen3-TTS).

## Vertrag (HTTP)

| Endpunkt | Zweck |
|---|---|
| `GET /v1/health` | Container lebt: `{"status": "ok", "state": ...}` |
| `GET /v1/info` | Steckbrief: `name`, `languages` (ISO-Codes), `sample_rate`, `needs_sample`, `uses_transcript`, `instructions`, `streaming` (`sentence`/`chunk`), `vram_mb`, `description`, `license`, `contract` |
| `GET /v1/device` | `{assigned, effective, loaded, state, detail, model}` |
| `POST /v1/device` | `{"device": "off" \| "cpu" \| "cuda" \| "cuda:N"}` - laden bzw. entladen |
| `POST /v1/synthesize` | Multipart: `text`, `language`, `voice_id`, `ref_audio` (WAV), `ref_text`, `instruction`, `load_timeout_s` -> PCM16 mono als Stream, Header `X-Sample-Rate` |

Zustaende (`state`): `preparing` (Gewichte werden geladen, `Backend.prepare`),
`idle` (bereit, Modell nicht geladen), `loading`, `ready`, `off`, `error`
(`detail` sagt warum).

Fehler vor dem ersten Audio kommen als HTTP-Code statt als abgerissener
Stream: 400 ungueltige Anfrage, 409 belegt, 503 aus/laedt/Ladefehler,
500 Engine-Fehler (jeweils `{"detail": ...}`).

`load_timeout_s`: Ist das Modell nicht geladen, wird es geladen; der
Request wartet bis zu so vielen Sekunden darauf. 0 = sofort 503 (so macht es
der Orchestrator im Live-Gespraech - dann spricht seine Rueckfallebene),
beim Probehoeren und Filler-Erzeugen wartet er.

## Was das Kit mitbringt

- **Modell im Unterprozess:** "Aus" beendet den Prozess - das VRAM ist danach
  komplett frei, auch der CUDA-Kontext. Stuerzt der Prozess ab (Segfault,
  OOM-Kill), meldet das Kit das mit Exit-Code, der naechste Aufruf laedt neu.
- **Eine Synthese zur Zeit**, weitere warten (`ENGINE_BUSY_WAIT_S`), dann 409.
- **Referenz-Cache** pro Sample + Transkript (z. B. Klon-Prompt), solange das
  Modell geladen ist.
- **Abbruch**: Trennt der Client die Verbindung, stoppt die Synthese nach dem
  laufenden Chunk.
- **Satz-Aufteilung** (`split_sentences`) fuer Modelle ohne Streaming: das
  erste Audio kommt nach dem ersten Satz.

## Neue Engine einbinden

1. Ordner `tts-<name>/` mit einer Backend-Klasse:

   ```python
   from tts_engine_kit import Backend, EngineInfo, Voice, split_sentences
   from tts_engine_kit.audio import float_to_pcm16, wav_to_float

   class MeinBackend(Backend):
       @classmethod
       def info(cls):
           return EngineInfo(name="Mein TTS", languages=("de", "en"), sample_rate=24000)

       @classmethod
       def prepare(cls):          # Hauptprozess, beim Start: Gewichte laden
           return {"model_dir": "/models/..."}   # -> Konstruktor im Unterprozess

       def __init__(self, model_dir): ...
       def load(self, device):    # torch & Co. erst hier importieren
           ...
           return device
       def prepare_voice(self, voice: Voice):   # optional, wird gecacht
           ...
       def synthesize(self, text, language, voice, instruction):
           for sentence in split_sentences(text):
               ...
               yield 24000, float_to_pcm16(audio)
   ```

   `info()` und `prepare()` laufen im Hauptprozess (dort keine schweren
   Imports), alles andere im Modell-Unterprozess. `ValueError` in
   `synthesize` = ungueltige Anfrage (400).

2. `main.py`: `app = create_app("tts_<name>.backend:MeinBackend")`, gestartet
   mit uvicorn.
3. Dockerfile nach dem Muster von `tts-qwen3/Dockerfile` (Build-Context =
   Repo-Root, damit das Kit mit ins Image kommt) und eine Compose-Datei wie
   `orchestrator/docker-compose.qwen3.yml` (ai-lab-Netzwerk, GPU-Freigabe
   `count: all`, Volume fuer die Gewichte).
4. In Coolify als eigene Resource deployen (Base Directory `/`), im Panel
   ID + Adresse eintragen - fertig.

## Einstellungen (Umgebung)

| Variable | Default | Bedeutung |
|---|---|---|
| `ENGINE_DEVICE` | `cuda:0` | Karte beim Start (`off`, `cpu`, `cuda`, `cuda:N`); zur Laufzeit waehlt das Panel |
| `ENGINE_PRELOAD` | `false` | Modell schon beim Start laden |
| `ENGINE_BUSY_WAIT_S` | `60` | so lange wartet ein Request auf eine laufende Synthese |
| `ENGINE_LOAD_WAIT_S` | `150` | so lange wartet `POST /v1/device` aufs Laden |
| `ENGINE_CHUNK_TIMEOUT_S` | `300` | laenger ohne Chunk = Modell haengt, Prozess wird beendet |
| `ENGINE_VOICE_CACHE` | `8` | vorbereitete Referenzen im Cache |
| `ENGINE_LOAD_RETRY_S` | `60` | Sperrfrist nach Lade-/Downloadfehlern (Geraetewechsel versucht sofort) |

## Tests

```bash
uv sync --extra dev
.venv/bin/python -m pytest -q
```

Die Tests starten ein Fake-Backend in echten Unterprozessen (Laden,
Entladen, Absturz, Cache, Warteschlange) und pruefen den Abbruch gegen einen
echten uvicorn-Server.
