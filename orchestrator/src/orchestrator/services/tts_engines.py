"""TTS-Engines und Wahl der Hauptstimme (v1.17, Engine-Vertrag v1.20).

Eine Engine ist ein Dienst, der Text in PCM16 verwandelt. Alle Clients
sprechen dieselbe Schnittstelle - stream(text, voice_id) liefert
(Samplerate, PCM16-Chunk)-Tupel -, damit Antwort-Pipeline,
Filler-Generierung und Probehoeren die Engine nur ueber ihre ID kennen.
Fest eingebaut sind XTTS, Piper und Breeze (ENGINES); jede weitere Engine
folgt dem Engine-Vertrag (tts-engine-kit) und wird im Admin-Panel nur mit
ID + Adresse eingetragen - Client-Code braucht sie keinen (registry()).

Welche Engine die Hauptantwort spricht, waehlt der Admin im Panel
(app_settings, Fallback .env TTS_ENGINE) - server-weit wie das aktive LLM
(4.14), weil das VRAM keine zwei grossen Stimmen-Modelle nebeneinander
hergibt. Filler behalten ihre eigene Engine (v1.15)."""

import asyncio
import logging
import socket
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .. import repos
from ..config import settings
from . import gpu_manager
from .tts_client import ContractClient, breeze_base_url, breeze_client, piper_client, xtts_client

logger = logging.getLogger(__name__)

ACTIVE_ENGINE_SETTING = "tts_engine"
# Bewaehrte Hauptstimme und Rueckfallebene, wenn eine andere Engine ausfaellt.
DEFAULT_ENGINE = "xtts"
# Rueckfallebene, solange XTTS im Panel ausgeschaltet ist (v1.19): Piper
# laeuft auf der CPU und ist immer da.
OFF_FALLBACK_ENGINE = "piper"


class EngineOff(RuntimeError):
    """Engine ist im Admin-Panel ausgeschaltet (GPUs -> Aus, v1.19)."""


# Fest eingebaute Engines; Vertrags-Engines kommen per registry() dazu.
ENGINES: dict[str, dict] = {
    "xtts": {
        "label": "XTTS-v2 (Stimme des Nutzers)",
        "name": "XTTS",
        "client": xtts_client,
        # per_voice: spricht in der jeweiligen Nutzerstimme (Filler werden
        # pro Stimme erzeugt); needs_sample: ohne Voice-Sample geht nichts.
        "per_voice": True,
        "needs_sample": True,
        "container": "heimai-tts-xtts",
        # Im GPU-Panel ausschaltbar (v1.19) - dann weder Hauptstimme noch
        # Rueckfallebene.
        "gpu_service": "tts-xtts",
        "deploy_hint": "Laeuft der Voice-Stack (docker-compose.yml) in Coolify?",
        "base_url": lambda: settings.xtts_base_url,
        "health_path": "/v1/health",
        "description": "Klont die Nutzerstimme aus dem Voice-Sample, spricht "
                       "Deutsch. ~3 GB VRAM, streamt die Antwort.",
    },
    "piper": {
        "label": "Piper (feste Stimme, robust)",
        "name": "Piper",
        "client": piper_client,
        "per_voice": False,
        "needs_sample": False,
        "container": "heimai-tts-piper",
        "deploy_hint": "Laeuft der Voice-Stack (docker-compose.yml) in Coolify?",
        "base_url": lambda: settings.piper_base_url,
        "health_path": "/v1/health",
        "description": "Schnell und ohne GPU, eine feste deutsche Stimme - "
                       "klingt nicht nach der Nutzerstimme. Guter Ausweg, "
                       "wenn eine GPU-Engine an einem Text scheitert.",
    },
    "breeze": {
        "label": "Breeze TTS 2 (Test)",
        "name": "Breeze",
        "client": breeze_client,
        "per_voice": True,
        "needs_sample": False,
        "container": "heimai-tts-breeze",
        "deploy_hint": "Breeze laeuft separat: als Container (docker-compose.breeze.yml "
                       "oder docker-compose.breeze-cpp.yml, in Coolify anlegen bzw. starten - "
                       "der erste Start dauert) oder als breeze-server auf einem Rechner im "
                       "Netz. Laeuft er, die Adresse unter 'Breeze-Server' pruefen.",
        "logs_hint": "Logs pruefen: 'docker logs heimai-tts-breeze' bzw. "
                     "'heimai-tts-breeze-cpp', nativ die Konsole von breeze-server",
        # Breeze-TTS-2.cpp schickt die Antwort-Header schon vor dem Kodieren
        # der Referenz - ein CUDA-Fehler danach beendet den Prozess mitten
        # im Stream.
        "crash_hint": "Haeufigste Ursachen: VRAM der Karte voll (Log: 'out of memory' - "
                      "andere Dienste von der Karte nehmen oder ein kuerzeres Sample) "
                      "oder BREEZE_CUDA_ARCHS deckt die Karte nicht ab (Log: 'no kernel "
                      "image is available').",
        "base_url": breeze_base_url,
        # Offizieller Server: /health antwortet 503, solange das Modell laedt.
        "health_path": "/health",
        "description": "Open-Weight-Modell: klont die Stimme aus Sample + exaktem "
                       "Transkript, sonst eingebaute Stimme. Offiziell nur "
                       "Englisch/Chinesisch. Eigener Server: PyTorch (~7,7 GB VRAM) "
                       "oder Breeze-TTS-2.cpp (~4 GB), Adresse unter 'Breeze-Server'.",
    },
}


# ---- Vertrags-Engines (v1.20) ----------------------------------------------------------
#
# Engines nach dem Engine-Vertrag stehen nicht im Code, sondern in der
# Datenbank (ID + Adresse, Admin-Panel -> Sprachausgabe). Was sie koennen,
# melden sie per /v1/info selbst - zwischengespeichert, damit die Registry
# ohne Netzwerk auskommt.

# Beim Probehoeren und Filler-Erzeugen aufs Laden warten (das erste Laden
# dauert ~1 Minute); im Live-Turn nie - dann spricht die Rueckfallebene.
PREVIEW_LOAD_TIMEOUT_S = 240.0
_INFO: dict[str, dict] = {}


def registry() -> dict[str, dict]:
    """Alle Engines: fest eingebaute + Vertrags-Engines aus der Datenbank."""
    engines = dict(ENGINES)
    for row in repos.list_contract_engines():
        engines[row["id"]] = _contract_spec(row)
    return engines


def _contract_spec(row: dict) -> dict:
    engine_id, url = row["id"], row["url"].rstrip("/")
    info = _INFO.get(engine_id, {})
    name = row.get("label") or info.get("name") or engine_id
    description = info.get("description") or "Engine nach dem Engine-Vertrag."
    if info.get("languages"):
        description += f" Sprachen: {', '.join(info['languages'])}."
    return {
        "label": name,
        "name": name,
        "client": ContractClient(lambda: url),
        "per_voice": True,
        "needs_sample": bool(info.get("needs_sample", True)),
        "container": f"heimai-tts-{engine_id}",
        "gpu_service": gpu_manager.contract_service(engine_id),
        "contract": True,
        "url": url,
        "deploy_hint": (f"Laeuft der Container der Engine (eigener Coolify-Deploy)? Die Adresse "
                        f"{url} unter Sprachausgabe pruefen."),
        "logs_hint": (f"Logs pruefen: 'docker logs heimai-tts-{engine_id}' "
                      "(bzw. der Name des Engine-Containers)"),
        "base_url": lambda: url,
        "health_path": "/v1/health",
        "preview_kwargs": {"load_timeout_s": PREVIEW_LOAD_TIMEOUT_S},
        "description": f"{description} Adresse: {url}",
    }


async def refresh_info(engine_id: str, url: str) -> None:
    """Steckbrief einer Vertrags-Engine holen - best effort; fehlt er,
    gelten vorsichtige Annahmen (braucht ein Sample)."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{url}/v1/info")
        if response.status_code == 200:
            _INFO[engine_id] = response.json()
    except Exception:
        pass


def engine_ids() -> list[str]:
    return list(registry())


def get_engine(engine_id: str) -> dict:
    return registry()[engine_id]


def public_engines(engines: dict[str, dict] | None = None) -> list[dict]:
    """Registry ohne Client-Objekte - so geht sie als JSON ans Panel."""
    engines = engines if engines is not None else registry()
    return [
        {
            "id": engine_id,
            "label": spec["label"],
            "description": spec["description"],
            "per_voice": spec["per_voice"],
            "contract": spec.get("contract", False),
        }
        for engine_id, spec in engines.items()
    ]


def active_engine() -> str:
    """Engine der Hauptantwort: Admin-Wahl, sonst .env. Unbekanntes (z. B.
    eine inzwischen entfernte Engine) faellt auf XTTS zurueck."""
    engine_id = repos.get_setting(ACTIVE_ENGINE_SETTING) or settings.tts_engine
    return engine_id if engine_id in registry() else DEFAULT_ENGINE


def set_active_engine(engine_id: str) -> None:
    if engine_id not in registry():
        raise ValueError(f"Unbekannte TTS-Engine '{engine_id}'")
    if not engine_enabled(engine_id):
        raise EngineOff(off_detail(engine_id))
    repos.set_setting(ACTIVE_ENGINE_SETTING, engine_id)


def engine_enabled(engine_id: str) -> bool:
    service = get_engine(engine_id).get("gpu_service")
    return not (service and gpu_manager.is_off(service))


def off_detail(engine_id: str) -> str:
    name = get_engine(engine_id)["name"]
    return (f"{name} ist unter GPUs ausgeschaltet - unter Sprachausgabe aktivieren oder unter "
            "GPUs eine Karte zuweisen.")


def fallback_engine() -> str:
    """Wer einspringt, wenn die aktive Engine vor dem ersten Audio scheitert:
    XTTS, solange es an ist, sonst Piper."""
    return DEFAULT_ENGINE if engine_enabled(DEFAULT_ENGINE) else OFF_FALLBACK_ENGINE


def disable_blocker(service: str) -> str | None:
    """Grund, warum sich ein GPU-Dienst gerade nicht ausschalten laesst:
    Er spricht die aktive Hauptstimme. Sonst None."""
    spec = get_engine(active_engine())
    if spec.get("gpu_service") != service:
        return None
    return (f"{spec['name']} ist die aktive Hauptstimme - erst unter Sprachausgabe eine andere "
            "Engine aktivieren, dann ausschalten.")


async def activate(engine_id: str) -> list[str]:
    """Engine zur Hauptstimme machen - und dafuer sorgen, dass sie auch
    spricht (v1.20): Ausgeschaltet -> wieder an (auf ihrer letzten Karte);
    andere ausschaltbare Engines, die auf DIESER Karte geladen sind, werden
    entladen (VRAM); dann wird sie selbst geladen. Engines auf der anderen
    Karte bleiben, wie sie sind. Rueckgabe: Hinweise fuers Panel."""
    engines = registry()
    spec = engines[engine_id]
    notes: list[str] = []
    service = spec.get("gpu_service")
    if service:
        target = await _target_device(service)
        gpu_manager.store_assignment(service, target)
        if target.startswith("cuda"):
            for other_id, other in engines.items():
                other_service = other.get("gpu_service")
                if other_id == engine_id or not other_service or gpu_manager.is_off(other_service):
                    continue
                status = await gpu_manager.service_status(other_service)
                if not (status.get("reachable") and status.get("loaded")
                        and _same_card(status.get("effective"), target)):
                    continue
                try:
                    await gpu_manager.apply_device(other_service, gpu_manager.OFF)
                    notes.append(f"{other['name']} entladen (gleiche Karte, {target}).")
                except Exception as exc:
                    notes.append(f"{other['name']} liess sich nicht entladen: {str(exc)[:200]}")
        status = await gpu_manager.service_status(service)
        if status.get("reachable") and not (status.get("assigned") == target and status.get("loaded")):
            try:
                result = await gpu_manager.apply_device(service, target)
                notes.append(_load_note(spec["name"], result, target))
            except Exception as exc:
                notes.append(f"{spec['name']} konnte nicht auf {target} laden: {str(exc)[:200]}")
    set_active_engine(engine_id)
    return notes


def _load_note(name: str, result: dict, target: str) -> str:
    if result.get("loaded"):
        return f"{name} geladen auf {result.get('effective') or target}."
    if result.get("state") == "error":
        return f"{name}: {result.get('detail') or 'Laden fehlgeschlagen'}"
    fallback = get_engine(fallback_engine())["name"]
    return f"{name} laedt noch - bis es fertig ist, spricht {fallback}."


async def _target_device(service: str) -> str:
    """Karte fuers Aktivieren: die zugewiesene, sonst die vor dem
    Ausschalten, sonst die, die der Dienst selbst meldet, sonst GPU 0."""
    stored = gpu_manager.assigned_device(service)
    if stored and stored != gpu_manager.OFF:
        return stored
    last = gpu_manager.last_device(service)
    if last:
        return last
    status = await gpu_manager.service_status(service)
    live = status.get("assigned") if status.get("reachable") else None
    return live if live and live != gpu_manager.OFF else "cuda:0"


def _same_card(device: str | None, target: str) -> bool:
    def normalized(value: str) -> str:
        return "cuda:0" if value == "cuda" else value

    return device is not None and normalized(device) == normalized(target)


async def stream_main(text: str, voice_id: str) -> AsyncIterator[tuple[int, bytes]]:
    """Hauptantwort in der aktiven Engine. Scheitert eine andere Engine als
    XTTS, BEVOR Audio geflossen ist (Container gestoppt, Modell laedt noch,
    belegt), spricht XTTS - eine ausgefallene Test-Engine soll die
    Assistenz nicht stumm machen. Nach dem ersten Chunk ist kein Wechsel
    mehr moeglich (sonst doppeltes Audio): der Fehler geht an den Aufrufer.
    Ist XTTS im Panel ausgeschaltet, springt Piper ein (v1.19)."""
    engine_id = active_engine()
    fallback = fallback_engine()
    if not engine_enabled(engine_id):
        # Sollte das Panel verhindern (aktive Engine ist nicht ausschaltbar),
        # z. B. aber TTS_ENGINE=xtts aus der .env ohne Admin-Wahl.
        engine_id = fallback
    started = False
    try:
        async for item in get_engine(engine_id)["client"].stream(text, voice_id):
            started = True
            yield item
        return
    except Exception:
        if started or engine_id == fallback:
            raise
        logger.exception(
            "TTS-Engine '%s' fehlgeschlagen - Hauptantwort kommt von '%s'", engine_id, fallback
        )
    async for item in get_engine(fallback)["client"].stream(text, voice_id):
        yield item


@dataclass
class Synthesis:
    pcm: bytes
    rate: int
    # Zeit bis zum ersten Chunk (bei XTTS/Breeze ~1 s Audio) und fuer die
    # komplette Synthese - Grundlage fuer den Engine-Vergleich im Panel.
    first_chunk_ms: float
    total_ms: float

    @property
    def audio_ms(self) -> float:
        return len(self.pcm) / 2 / self.rate * 1000 if self.rate else 0.0


async def synthesize(engine_id: str, text: str, voice_id: str | None) -> Synthesis:
    """Komplette Synthese mit GENAU dieser Engine - ohne Fallback, denn
    Filler-Generierung und Probehoeren sollen die gewaehlte Engine hoeren
    lassen, nicht die Rueckfallebene."""
    spec = get_engine(engine_id)
    if not engine_enabled(engine_id):
        raise EngineOff(off_detail(engine_id))
    started = time.perf_counter()
    first_chunk_ms = 0.0
    pcm = bytearray()
    rate = settings.target_sample_rate
    # Vertrags-Engines: aufs Laden des Modells warten statt sofort 503.
    stream = spec["client"].stream(text, voice_id, **spec.get("preview_kwargs", {}))
    async for chunk_rate, chunk in stream:
        if not pcm:
            first_chunk_ms = (time.perf_counter() - started) * 1000
        rate = chunk_rate
        pcm.extend(chunk)
    return Synthesis(bytes(pcm), rate, first_chunk_ms, (time.perf_counter() - started) * 1000)


# Zustaende einer Vertrags-Engine (GET /v1/device) -> Panel-Status
_CONTRACT_STATES = {
    "ready": ("ok", ""),
    "loading": ("loading", "Modell wird geladen"),
    "preparing": ("loading", "laedt die Gewichte herunter (erster Start, mehrere GB)"),
    "idle": ("idle", "nicht geladen - laedt beim Aktivieren bzw. beim ersten Probehoeren"),
    "off": ("off", "Modell entladen"),
}


async def engine_status(engine_id: str) -> dict:
    """Erreichbarkeit fuers Panel: ok | loading | idle | error | unreachable | off."""
    if not engine_enabled(engine_id):
        return {"status": "off", "detail": "Modell entladen"}
    spec = get_engine(engine_id)
    if spec.get("contract"):
        return await _contract_status(spec)
    url = spec["base_url"]().rstrip("/") + spec["health_path"]
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(url)
    except Exception as exc:
        return {"status": "unreachable", "detail": _unreachable_detail(exc, spec)}
    if response.status_code == 200:
        return {"status": "ok", "detail": ""}
    if response.status_code == 503:
        return {"status": "loading", "detail": "Modell wird geladen"}
    return {"status": "error", "detail": f"HTTP {response.status_code}"}


async def _contract_status(spec: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(spec["base_url"]() + "/v1/device")
    except Exception as exc:
        return {"status": "unreachable", "detail": _unreachable_detail(exc, spec)}
    if response.status_code != 200:
        return {"status": "error", "detail": f"HTTP {response.status_code}"}
    device = response.json()
    state = device.get("state", "")
    if state == "error":
        return {"status": "error", "detail": device.get("detail") or "Laden fehlgeschlagen"}
    status, detail = _CONTRACT_STATES.get(state, ("error", f"unbekannter Zustand '{state}'"))
    if status == "ok" and device.get("effective"):
        detail = f"geladen auf {device['effective']}"
    return {"status": status, "detail": detail}


def _unreachable_detail(exc: Exception, spec: dict) -> str:
    """Technische Ursache -> was zu tun ist. Ein nackter DNS-Fehler wie
    "[Errno -2] Name or service not known" sieht im Panel sonst wie ein
    Programmfehler aus, heisst aber schlicht: der Container laeuft nicht."""
    host = urlsplit(spec["base_url"]()).hostname or "?"
    if _caused_by(exc, socket.gaierror):
        return (f"Server nicht gefunden - den Namen '{host}' kennt das Netzwerk nicht. "
                f"{spec['deploy_hint']}")
    if _caused_by(exc, ConnectionRefusedError):
        logs = spec.get("logs_hint") or f"'docker logs {spec['container']}'"
        return (f"'{host}' ist da, nimmt aber keine Verbindungen an - der Dienst startet "
                f"noch oder ist abgestuerzt ({logs}).")
    if isinstance(exc, httpx.TimeoutException):
        return f"'{host}' antwortet nicht innerhalb von 3 s."
    return str(exc)[:200] or type(exc).__name__


# Verbindung mitten in der Synthese weg ("incomplete chunked read",
# "Server disconnected", Reset): Der Dienst ist dabei so gut wie immer
# abgestuerzt - Fehler innerhalb der Synthese melden alle Server sauber.
STREAM_ABORTED = (httpx.RemoteProtocolError, httpx.ReadError)


def stream_abort_detail(engine_id: str) -> str:
    """Was der Admin nach einem abgerissenen Stream tun kann - statt des
    httpx-Wortlauts, der wie ein Fehler des Orchestrators aussieht."""
    spec = get_engine(engine_id)
    logs = spec.get("logs_hint") or f"Logs pruefen: 'docker logs {spec['container']}'"
    detail = (f"{spec['name']}-Server hat die Verbindung mitten in der Synthese abgebrochen - "
              f"er ist dabei vermutlich abgestuerzt. {logs} (Fehlertext), dazu nvidia-smi (VRAM).")
    if spec.get("crash_hint"):
        detail += " " + spec["crash_hint"]
    return detail


def _caused_by(exc: BaseException, kind: type) -> bool:
    """httpx verpackt die eigentliche Ursache (socket-Fehler) in eine Kette."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, kind):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


async def overview() -> list[dict]:
    """Alle Engines mit Live-Status (parallel geprueft); Vertrags-Engines
    liefern dabei auch ihren Steckbrief neu."""
    contract = {eid: spec["url"] for eid, spec in registry().items() if spec.get("contract")}
    await asyncio.gather(*(refresh_info(eid, url) for eid, url in contract.items()))
    engines = registry()
    statuses = await asyncio.gather(*(engine_status(engine_id) for engine_id in engines))
    return [
        {**entry, "status": status}
        for entry, status in zip(public_engines(engines), statuses)
    ]
