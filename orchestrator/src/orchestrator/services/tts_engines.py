"""TTS-Engines und Wahl der Hauptstimme (v1.17).

Eine Engine ist ein Dienst, der Text in PCM16 verwandelt. Alle Clients
sprechen dieselbe Schnittstelle - stream(text, voice_id) liefert
(Samplerate, PCM16-Chunk)-Tupel -, damit Antwort-Pipeline,
Filler-Generierung und Probehoeren die Engine nur ueber ihre ID kennen.
Eine weitere Engine = ein Client mit stream() + ein Eintrag in ENGINES.

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
from .tts_client import breeze_base_url, breeze_client, piper_client, xtts_client

logger = logging.getLogger(__name__)

ACTIVE_ENGINE_SETTING = "tts_engine"
# Bewaehrte Hauptstimme und Rueckfallebene, wenn eine andere Engine ausfaellt.
DEFAULT_ENGINE = "xtts"

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
        "base_url": breeze_base_url,
        # Offizieller Server: /health antwortet 503, solange das Modell laedt.
        "health_path": "/health",
        "description": "Open-Weight-Modell: klont die Stimme aus Sample + exaktem "
                       "Transkript, sonst eingebaute Stimme. Offiziell nur "
                       "Englisch/Chinesisch. Eigener Server: PyTorch (~7,7 GB VRAM) "
                       "oder Breeze-TTS-2.cpp (~4 GB), Adresse unter 'Breeze-Server'.",
    },
}


def engine_ids() -> list[str]:
    return list(ENGINES)


def get_engine(engine_id: str) -> dict:
    return ENGINES[engine_id]


def public_engines() -> list[dict]:
    """Registry ohne Client-Objekte - so geht sie als JSON ans Panel."""
    return [
        {
            "id": engine_id,
            "label": spec["label"],
            "description": spec["description"],
            "per_voice": spec["per_voice"],
        }
        for engine_id, spec in ENGINES.items()
    ]


def active_engine() -> str:
    """Engine der Hauptantwort: Admin-Wahl, sonst .env. Unbekanntes (z. B.
    eine inzwischen entfernte Engine) faellt auf XTTS zurueck."""
    engine_id = repos.get_setting(ACTIVE_ENGINE_SETTING) or settings.tts_engine
    return engine_id if engine_id in ENGINES else DEFAULT_ENGINE


def set_active_engine(engine_id: str) -> None:
    if engine_id not in ENGINES:
        raise ValueError(f"Unbekannte TTS-Engine '{engine_id}'")
    repos.set_setting(ACTIVE_ENGINE_SETTING, engine_id)


async def stream_main(text: str, voice_id: str) -> AsyncIterator[tuple[int, bytes]]:
    """Hauptantwort in der aktiven Engine. Scheitert eine andere Engine als
    XTTS, BEVOR Audio geflossen ist (Container gestoppt, Modell laedt noch,
    belegt), spricht XTTS - eine ausgefallene Test-Engine soll die
    Assistenz nicht stumm machen. Nach dem ersten Chunk ist kein Wechsel
    mehr moeglich (sonst doppeltes Audio): der Fehler geht an den Aufrufer."""
    engine_id = active_engine()
    started = False
    try:
        async for item in ENGINES[engine_id]["client"].stream(text, voice_id):
            started = True
            yield item
        return
    except Exception:
        if started or engine_id == DEFAULT_ENGINE:
            raise
        logger.exception(
            "TTS-Engine '%s' fehlgeschlagen - Hauptantwort kommt von '%s'", engine_id, DEFAULT_ENGINE
        )
    async for item in ENGINES[DEFAULT_ENGINE]["client"].stream(text, voice_id):
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
    started = time.perf_counter()
    first_chunk_ms = 0.0
    pcm = bytearray()
    rate = settings.target_sample_rate
    async for chunk_rate, chunk in ENGINES[engine_id]["client"].stream(text, voice_id):
        if not pcm:
            first_chunk_ms = (time.perf_counter() - started) * 1000
        rate = chunk_rate
        pcm.extend(chunk)
    return Synthesis(bytes(pcm), rate, first_chunk_ms, (time.perf_counter() - started) * 1000)


async def engine_status(engine_id: str) -> dict:
    """Erreichbarkeit fuers Panel: ok | loading | error | unreachable."""
    spec = ENGINES[engine_id]
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
    """Alle Engines mit Live-Status (parallel geprueft)."""
    statuses = await asyncio.gather(*(engine_status(engine_id) for engine_id in ENGINES))
    return [
        {**entry, "status": status}
        for entry, status in zip(public_engines(), statuses)
    ]
