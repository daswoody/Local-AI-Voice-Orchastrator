"""Filler-Verwaltung mit XTTS-Pre-Generierung (Mikro-Phase 1.7d).

Konzept (siehe 4.14): Filler werden im Admin-Panel angelegt (Titel, Text,
Trigger) und per XTTS **vorab** in der jeweiligen Stimme generiert. Zur
Laufzeit wird nur noch eine fertige WAV-Datei abgespielt - schneller als
jede Live-Synthese und ohne den hoerbaren Stimmbruch zwischen Piper-Filler
und XTTS-Hauptantwort. Piper bleibt Fallback fuer Filler, die (noch) nicht
generiert sind.

Saubere Vorgenerierung (v1.16) - Nutzer-Report: Fetzen am Ende, Zeitlupe:
- Immer nur EINE Generierung gleichzeitig (Hauptursache: parallele
  XTTS-Synthesen verfaelschen sich gegenseitig, siehe tts-xtts/engine.py).
- XTTS-Qualitaetspfad statt Streaming, Text mit sauberem Satzende.
- Stille vorn/hinten weg, Sprechdauer auf Plausibilitaet pruefen, bei
  Ausreissern bis zu zweimal vorsichtiger neu wuerfeln.
- Einzelne Stimmen neu generierbar; neues Audio ersetzt das alte erst,
  wenn es komplett geschrieben ist."""

import asyncio
import fnmatch
import io
import logging
import os
import random
import re
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx

from .. import repos
from ..audio import speech_segments, trim_silence
from ..config import settings
from . import tts_engines
from .tts_client import piper_client, xtts_client

logger = logging.getLogger(__name__)

# Nur EINE Filler-Generierung gleichzeitig. Der XTTS-Service serialisiert
# zwar selbst, aber ohne diese Sperre stauten sich bei mehreren "Audio
# generieren"-Klicks viele Synthesen vor dem Modell - und ein Realtime-Turn
# muesste hinter allen warten.
_generation_lock = asyncio.Lock()

# Erster Versuch mit dem XTTS-Default, Neuversuche zunehmend vorsichtiger
# gesampelt: weniger Ausreisser, dafuer etwas gleichfoermigere Betonung -
# fuer einen kurzen Filler kein Verlust.
XTTS_TEMPERATURES: tuple[float | None, ...] = (None, 0.6, 0.45)

# Plausible Sprechdauer: XTTS spricht Deutsch mit grob 12-16 Zeichen/s.
# Deutlich laenger heisst Zeitlupe oder angehaengte Laute, deutlich kuerzer
# abgeschnitten. Bewusst grosszuegig - die Pruefung soll grobe Ausreisser
# fangen, keine gute Aufnahme verwerfen.
_LONGEST = (0.4, 9.0)    # Sekunden Sockel, Zeichen pro Sekunde
_SHORTEST = (0.1, 40.0)

# Welche Engines es gibt, steht seit v1.17 in tts_engines.ENGINES - dieselbe
# Registry wie fuer Hauptstimme und Probehoeren.


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


async def generate_audio(filler_id: int, voice_id: str | None = None) -> list[dict]:
    """Generiert das Filler-Audio mit der am Filler gewaehlten Engine (v1.15)
    fuer alle Stimmen - oder nur fuer `voice_id` (v1.16), damit eine
    gelungene Stimme erhalten bleibt, wenn eine andere neu gewuerfelt wird.

    Synchron im Request (Admin klickt und wartet ein paar Sekunden) - bei
    einer Handvoll Stimmen ist eine Job-Queue Overkill."""
    voice_ids = [voice["id"] for voice in repos.list_voices()]
    if voice_id is not None:
        if voice_id not in voice_ids:
            raise ValueError(f"Stimme '{voice_id}' existiert nicht")
        voice_ids = [voice_id]

    async with _generation_lock:
        # Erst IN der Sperre lesen: Wer hier gewartet hat, soll den Text
        # sprechen, der jetzt gilt - nicht den von vor dem Warten.
        filler = repos.get_filler(filler_id)
        if filler is None:
            raise ValueError("Filler existiert nicht")
        Path(settings.filler_cache_dir).mkdir(parents=True, exist_ok=True)
        engine_id = filler.get("engine") or "xtts"
        if engine_id == "piper":
            return await _generate_with_piper(filler, voice_ids)
        if engine_id == "xtts":
            return await _generate_with_xtts(filler, voice_ids)
        if engine_id in tts_engines.ENGINES:
            return await _generate_with_engine(filler, voice_ids, engine_id)
        error = f"Engine '{engine_id}' gibt es nicht mehr - Filler auf eine andere Engine umstellen"
        return [{"voice_id": v, "ok": False, "error": error} for v in voice_ids]


def prepare_text(text: str) -> str:
    """Filler-Text fuer XTTS aufbereiten (v1.16).

    Bei kurzen Saetzen ohne Satzzeichen am Ende findet XTTS schlecht ein
    Ende und haengt Laute an; Auslassungspunkte verleiten es zu Atmern und
    Fetzen. Deshalb: "..." im Satz -> Komma, am Ende -> Punkt, fehlendes
    Satzende ergaenzen. Der gespeicherte Filler-Text bleibt unveraendert."""
    text = " ".join(text.replace("\u2026", "...").split())
    text = re.sub(r"\s*\.{2,}\s*$", ".", text)
    text = re.sub(r"\s*\.{2,}\s*", ", ", text)
    text = text.rstrip(",;:-\u2013 ")
    if text and text[-1] not in ".!?":
        text += "."
    return text


@dataclass
class Take:
    """Ein XTTS-Ergebnis samt Urteil der Plausibilitaetspruefung."""

    pcm: bytes
    rate: int
    problem: str | None  # None = plausibel
    deviation: float     # wie weit ausserhalb des plausiblen Bereichs (0 = drin)


def assess_take(text: str, pcm: bytes, rate: int) -> Take:
    """Grobe Plausibilitaetspruefung: passt die Sprechdauer (erster bis
    letzter hoerbarer Laut) zur Textlaenge?"""
    segments = speech_segments(pcm, rate)
    if not segments:
        return Take(pcm, rate, "ohne hoerbares Sprachsignal", float("inf"))
    spoken = segments[-1][1] - segments[0][0]
    chars = len(text)
    longest = _LONGEST[0] + chars / _LONGEST[1]
    shortest = _SHORTEST[0] + chars / _SHORTEST[1]
    if spoken > longest:
        return Take(pcm, rate, f"auffaellig lang ({spoken:.1f}s fuer {chars} Zeichen, "
                               "Zeitlupe oder angehaengte Laute?)", spoken / longest - 1)
    if spoken < shortest:
        return Take(pcm, rate, f"auffaellig kurz ({spoken:.1f}s fuer {chars} Zeichen, "
                               "abgeschnitten?)", shortest / spoken - 1)
    return Take(pcm, rate, None, 0.0)


async def _best_xtts_take(text: str, voice_id: str) -> tuple[Take, int]:
    """Bis zu len(XTTS_TEMPERATURES) Versuche; der erste plausible gewinnt,
    sonst der am wenigsten auffaellige. -> (Take, Anzahl Versuche)"""
    best: Take | None = None
    attempts = 0
    for temperature in XTTS_TEMPERATURES:
        try:
            pcm, rate = await xtts_client.synthesize(text, voice_id, temperature=temperature)
        except Exception:
            if best is None:
                raise  # schon der erste Versuch scheitert -> echter Fehler
            logger.exception("XTTS-Neuversuch fuer Stimme %s fehlgeschlagen", voice_id)
            break
        attempts += 1
        take = assess_take(text, pcm, rate)
        if take.problem is None:
            return take, attempts
        logger.warning("Filler-Audio fuer Stimme %s, Versuch %d %s", voice_id, attempts, take.problem)
        if best is None or take.deviation < best.deviation:
            best = take
    return best, attempts


async def _generate_with_xtts(filler: dict, voice_ids: list[str]) -> list[dict]:
    text = prepare_text(filler["text"])
    results = []
    for voice_id in voice_ids:
        sample = Path(settings.voices_dir) / f"{voice_id}.wav"
        if not sample.exists():
            results.append({"voice_id": voice_id, "ok": False,
                            "error": "kein Voice-Sample hochgeladen"})
            continue
        try:
            take, attempts = await _best_xtts_take(text, voice_id)
            result = _store(filler, voice_id, trim_silence(take.pcm, take.rate), take.rate)
            result["attempts"] = attempts
            if result["ok"] and take.problem:
                result["warning"] = (f"nach {attempts} Versuchen weiter {take.problem} - bitte "
                                     "probehoeren, ggf. nur diese Stimme neu generieren")
            results.append(result)
        except httpx.RemoteProtocolError:
            # Verbindung ohne Antwort weg = der XTTS-Container ist waehrend
            # der Generierung gestorben (haeufigste Ursachen: RAM-/VRAM-
            # Knappheit, OOM-Kill). Dem Admin sagen, wo er nachsehen muss,
            # statt nur den httpx-Wortlaut zu zeigen.
            logger.exception("XTTS-Verbindung fuer Stimme %s abgerissen", voice_id)
            results.append({
                "voice_id": voice_id, "ok": False,
                "error": "XTTS-Service waehrend der Generierung abgestuerzt. "
                         "Auf der VM pruefen: 'docker logs heimai-tts-xtts' "
                         "(Fehlertext/Traceback) und 'dmesg | grep -i oom' "
                         "(RAM-Knappheit) sowie nvidia-smi (VRAM). Alternativ "
                         "diesen Filler auf die Engine 'Piper' umstellen.",
            })
        except Exception as exc:
            logger.exception("Filler-Generierung fuer Stimme %s fehlgeschlagen", voice_id)
            results.append({"voice_id": voice_id, "ok": False, "error": str(exc)[:350]})
    return results


async def _generate_with_engine(filler: dict, voice_ids: list[str], engine_id: str) -> list[dict]:
    """Weitere Engines aus der Registry (z. B. Breeze TTS 2, v1.17) ueber
    ihren Stream: pro Stimme ein Versuch mit derselben Textaufbereitung,
    Trimmung und Plausibilitaetspruefung wie bei XTTS - nur ohne dessen
    Temperatur-Neuversuche, die kennt die gemeinsame Schnittstelle nicht."""
    spec = tts_engines.get_engine(engine_id)
    text = prepare_text(filler["text"])
    results = []
    for voice_id in voice_ids:
        sample = Path(settings.voices_dir) / f"{voice_id}.wav"
        if spec["needs_sample"] and not sample.exists():
            results.append({"voice_id": voice_id, "ok": False,
                            "error": "kein Voice-Sample hochgeladen"})
            continue
        try:
            synthesis = await tts_engines.synthesize(engine_id, text, voice_id)
            if not synthesis.pcm:
                raise RuntimeError(f"{spec['name']} hat keine Audio-Daten geliefert")
            take = assess_take(text, synthesis.pcm, synthesis.rate)
            result = _store(filler, voice_id, trim_silence(take.pcm, take.rate), take.rate)
            if result["ok"] and take.problem:
                result["warning"] = (f"{take.problem} - bitte probehoeren, ggf. nur diese "
                                     "Stimme neu generieren")
            results.append(result)
        except httpx.RemoteProtocolError:
            # Stream mitten in der Generierung abgerissen = Container
            # gestorben (RAM-/VRAM-Knappheit) - wie bei XTTS sagen, wo man
            # nachsehen muss.
            logger.exception("%s-Stream fuer Stimme %s abgerissen", spec["name"], voice_id)
            logs = spec.get("logs_hint") or f"Logs pruefen: 'docker logs {spec['container']}'"
            results.append({
                "voice_id": voice_id, "ok": False,
                "error": f"{spec['name']}-Service waehrend der Generierung abgestuerzt. "
                         f"{logs} (Fehlertext/Traceback), dazu nvidia-smi (VRAM). "
                         "Alternativ diesen Filler auf die Engine 'Piper' umstellen.",
            })
        except Exception as exc:
            logger.exception("Filler-Generierung (%s) fuer Stimme %s fehlgeschlagen",
                             spec["name"], voice_id)
            results.append({"voice_id": voice_id, "ok": False, "error": str(exc)[:350]})
    return results


async def _generate_with_piper(filler: dict, voice_ids: list[str]) -> list[dict]:
    """Piper hat genau EINE Stimme - dasselbe Audio wird fuer alle Stimmen
    abgelegt. So bleibt der Abspielpfad (Cache-Matrix Filler x Stimme)
    unveraendert: select_filler muss die Engine gar nicht kennen."""
    try:
        pcm, rate = await piper_client.synthesize(filler["text"])
    except Exception as exc:
        logger.exception("Piper-Generierung fuer Filler %s fehlgeschlagen", filler["id"])
        error = str(exc)[:350]
        return [{"voice_id": voice_id, "ok": False, "error": error} for voice_id in voice_ids]

    pcm = trim_silence(pcm, rate)
    return [_store(filler, voice_id, pcm, rate) for voice_id in voice_ids]


def _store(filler: dict, voice_id: str, pcm: bytes, rate: int) -> dict:
    """Ergebnis ablegen - aber nur, wenn der Filler noch so aussieht wie beim
    Start: Wurde er waehrenddessen umgetextet, auf eine andere Engine
    gestellt oder geloescht, gehoert dieses Audio nicht mehr zu ihm."""
    current = repos.get_filler(filler["id"])
    if current is None or current["text"] != filler["text"] or current.get("engine") != filler.get("engine"):
        return {"voice_id": voice_id, "ok": False,
                "error": "Filler wurde waehrend der Generierung geaendert - bitte neu generieren"}
    try:
        _write_wav(audio_path(filler["id"], voice_id), pcm, rate)
    except Exception as exc:
        logger.exception("Filler-Audio fuer Stimme %s nicht speicherbar", voice_id)
        return {"voice_id": voice_id, "ok": False, "error": str(exc)[:350]}
    return {"voice_id": voice_id, "ok": True}


def load_audio(path: Path) -> tuple[bytes, int]:
    """Fertigen Filler laden -> (PCM16 mono, Samplerate)."""
    with wave.open(io.BytesIO(path.read_bytes()), "rb") as wav:
        return wav.readframes(wav.getnframes()), wav.getframerate()


def _write_wav(path: Path, pcm: bytes, rate: int) -> None:
    """Atomar: erst eine Temp-Datei, dann umbenennen. Ein laufender Turn liest
    so nie ein halbes WAV, und scheitert das Schreiben, bleibt die bisherige
    (vielleicht gute) Aufnahme erhalten. Die Temp-Datei endet nicht auf
    .wav und faellt deshalb nie unter die Cache-Globs."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle, wave.open(handle, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(pcm)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
