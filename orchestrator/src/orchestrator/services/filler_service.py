"""Filler-Verwaltung mit XTTS-Pre-Generierung (Mikro-Phase 1.7d).

Konzept (siehe 4.14): Filler werden im Admin-Panel angelegt (Titel, Text,
Trigger) und per XTTS **vorab** in der jeweiligen Stimme generiert. Zur
Laufzeit wird nur noch eine fertige WAV-Datei abgespielt - schneller als
jede Live-Synthese und ohne den hoerbaren Stimmbruch zwischen Piper-Filler
und XTTS-Hauptantwort. Piper bleibt Fallback fuer Filler, die (noch) nicht
generiert sind."""

import fnmatch
import io
import logging
import random
import wave
from pathlib import Path

import httpx

from .. import repos
from ..config import settings
from .tts_client import xtts_client

logger = logging.getLogger(__name__)


def audio_path(filler_id: int, voice_id: str) -> Path:
    return Path(settings.filler_cache_dir) / f"{filler_id}_{voice_id}.wav"


def has_audio(filler_id: int, voice_id: str) -> bool:
    return audio_path(filler_id, voice_id).exists()


def select_filler(kind: str, voice_id: str, tool_name: str | None = None) -> dict | None:
    """Waehlt einen Filler fuer den Trigger-Zeitpunkt.

    Der Orchestrator kennt den Grund fuers Warten selbst (er entscheidet ja,
    was er tut) - `kind` ist also kein Raten, sondern sein eigener Zustand:
    thinking (LLM langsam), search (RAG/Web), tool (vor einem Tool-Call).

    Bei kind='tool' gewinnen Trigger mit spezifischem tool_pattern
    (fnmatch, z. B. "Calendar-*") gegen den generischen "*"-Trigger.
    Innerhalb der Kandidaten entscheidet Zufall, damit nicht immer derselbe
    Satz kommt. Rueckgabe enthaelt 'path', wenn vorgeneriertes Audio fuer
    die Stimme existiert - sonst faellt der Aufrufer auf Piper zurueck."""
    candidates = repos.fillers_for_kind(kind)

    if kind == "tool" and tool_name is not None:
        specific = [
            f for f in candidates
            if f["tool_pattern"] and f["tool_pattern"] != "*"
            and fnmatch.fnmatch(tool_name, f["tool_pattern"])
        ]
        candidates = specific or [f for f in candidates if f["tool_pattern"] in ("*", None)]

    if not candidates:
        return None

    # Vorgenerierte Filler bevorzugen - der Sinn der ganzen Uebung.
    with_audio = [f for f in candidates if has_audio(f["id"], voice_id)]
    chosen = random.choice(with_audio or candidates)
    chosen["path"] = audio_path(chosen["id"], voice_id) if has_audio(chosen["id"], voice_id) else None
    return chosen


def delete_audio(filler_id: int) -> None:
    """Cache-Dateien eines Fillers entfernen (bei Text-Aenderung/Loeschung)."""
    cache_dir = Path(settings.filler_cache_dir)
    if cache_dir.exists():
        for path in cache_dir.glob(f"{filler_id}_*.wav"):
            path.unlink(missing_ok=True)


async def generate_audio(filler_id: int) -> list[dict]:
    """Generiert den Filler per XTTS fuer alle Stimmen, die ein Sample haben.

    Synchron im Request (Admin klickt und wartet ein paar Sekunden) - bei
    einer Handvoll Stimmen ist eine Job-Queue Overkill."""
    filler = repos.get_filler(filler_id)
    if filler is None:
        raise ValueError("Filler existiert nicht")

    cache_dir = Path(settings.filler_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for voice in repos.list_voices():
        voice_id = voice["id"]
        sample = Path(settings.voices_dir) / f"{voice_id}.wav"
        if not sample.exists():
            results.append({"voice_id": voice_id, "ok": False,
                            "error": "kein Voice-Sample hochgeladen"})
            continue
        try:
            pcm = bytearray()
            rate = 24000
            async for chunk_rate, chunk in xtts_client.stream(filler["text"], voice_id):
                rate = chunk_rate
                pcm.extend(chunk)
            _write_wav(audio_path(filler_id, voice_id), bytes(pcm), rate)
            results.append({"voice_id": voice_id, "ok": True})
        except httpx.RemoteProtocolError:
            # Verbindung mitten im Stream weg = der XTTS-Container ist
            # waehrend der Generierung gestorben (haeufigste Ursachen:
            # RAM-/VRAM-Knappheit, OOM-Kill). Dem Admin sagen, wo er
            # nachsehen muss, statt nur den httpx-Wortlaut zu zeigen.
            logger.exception("XTTS-Stream fuer Stimme %s abgerissen", voice_id)
            results.append({
                "voice_id": voice_id, "ok": False,
                "error": "XTTS-Service waehrend der Generierung abgestuerzt. "
                         "Auf der VM pruefen: 'docker logs heimai-tts-xtts' "
                         "(Fehlertext/Traceback) und 'dmesg | grep -i oom' "
                         "(RAM-Knappheit) sowie nvidia-smi (VRAM).",
            })
        except Exception as exc:
            logger.exception("Filler-Generierung fuer Stimme %s fehlgeschlagen", voice_id)
            results.append({"voice_id": voice_id, "ok": False, "error": str(exc)[:350]})
    return results


def load_audio(path: Path) -> tuple[bytes, int]:
    """Fertigen Filler laden -> (PCM16 mono, Samplerate)."""
    with wave.open(io.BytesIO(path.read_bytes()), "rb") as wav:
        return wav.readframes(wav.getnframes()), wav.getframerate()


def _write_wav(path: Path, pcm: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
