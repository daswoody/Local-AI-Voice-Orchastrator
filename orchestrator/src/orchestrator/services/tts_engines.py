"""TTS-Engines und Wahl der Hauptstimme (v1.17, Engine-Vertrag v1.20).

Eine Engine ist ein Dienst, der Text in PCM16 verwandelt. Alle Clients
sprechen dieselbe Schnittstelle - stream(text, voice_id) liefert
(Samplerate, PCM16-Chunk)-Tupel -, damit Antwort-Pipeline,
Filler-Generierung und Probehoeren die Engine nur ueber ihre ID kennen.
Fest eingebaut sind XTTS und Piper (ENGINES). Dazu kommt jedes TTS-Modell
eines audio.cpp-Servers als Engine "audiocpp:<Modell-ID>" (v1.21,
angebunden wie LiteLLM, siehe audiocpp) und jede Engine nach dem
Engine-Vertrag (tts-engine-kit), die im Admin-Panel nur mit ID + Adresse
eingetragen wird - Client-Code braucht keine davon (registry()). Breeze
TTS 2 und Qwen3-TTS als eigene Container sind seit v1.22 ausgebaut, beide
laufen ueber audio.cpp.

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
from . import audiocpp, gpu_manager
from .tts_client import ContractClient, piper_client, xtts_client

logger = logging.getLogger(__name__)

ACTIVE_ENGINE_SETTING = "tts_engine"
# Bewaehrte Hauptstimme und Rueckfallebene, wenn eine andere Engine ausfaellt.
DEFAULT_ENGINE = "xtts"
# Rueckfallebene, solange XTTS im Panel ausgeschaltet ist (v1.19): Piper
# laeuft auf der CPU und ist immer da.
OFF_FALLBACK_ENGINE = "piper"


class EngineOff(RuntimeError):
    """Engine ist im Admin-Panel ausgeschaltet (GPUs -> Aus, v1.19)."""


class EngineUnreachable(RuntimeError):
    """Server der Engine nicht erreichbar - der Text sagt, was zu tun ist."""


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
    """Alle Engines: fest eingebaute + Vertrags-Engines aus der Datenbank +
    die TTS-Modelle von audio.cpp (zuletzt gesehene Liste, v1.21)."""
    stored = repos.get_setting(ACTIVE_ENGINE_SETTING) or settings.tts_engine
    engines = dict(ENGINES)
    for row in repos.list_contract_engines():
        engines[row["id"]] = _contract_spec(row)
    models = _audiocpp_models(stored)
    choices = audiocpp.voice_choices() if models else {}
    for model in models:
        engines[audiocpp.engine_id(model["id"])] = _audiocpp_spec(model, choices.get(model["id"]))
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


# ---- audio.cpp (v1.21) ------------------------------------------------------------------
#
# Jedes TTS-Modell des audio.cpp-Servers ist eine Engine "audiocpp:<Modell-ID>".
# Die Liste kommt aus GET /v1/models (bei jedem Oeffnen des Panels frisch) und
# wird gemerkt, damit die Registry ohne Netzwerk auskommt.

AUDIOCPP_DEPLOY_HINT = ("Laeuft der audio.cpp-Server? Die Adresse unter Sprachausgabe -> audio.cpp "
                        "pruefen (Containername:Port im Netzwerk ai-lab, Standard-Port 8080).")
AUDIOCPP_LOGS_HINT = "Logs des audio.cpp-Containers pruefen (Coolify bzw. 'docker logs <Name>')"
# Aktivieren wartet so lange aufs Laden, danach laedt audio.cpp im Hintergrund weiter.
AUDIOCPP_LOAD_WAIT_S = 150.0
_AUDIOCPP_SERVER = {
    "name": "audio.cpp",
    "container": "audio.cpp",
    "base_url": audiocpp.base_url,
    "deploy_hint": AUDIOCPP_DEPLOY_HINT,
    "logs_hint": AUDIOCPP_LOGS_HINT,
}


def _audiocpp_models(stored_active: str) -> list[dict]:
    """Gemerkte TTS-Modelle - plus die aktive Hauptstimme, falls audio.cpp
    sie gerade nicht mehr listet: Die Wahl soll nicht verloren gehen, nur
    weil der Server kurz weg ist (dann spricht die Rueckfallebene)."""
    models = audiocpp.cached_models() if audiocpp.base_url() else []
    active_model = audiocpp.model_of(stored_active)
    if active_model and all(m["id"] != active_model for m in models):
        models.append({"id": active_model, "family": "", "mode": "", "missing": True})
    return models


def _audiocpp_spec(model: dict, builtin: str | None) -> dict:
    """builtin: im Panel gewaehlte eingebaute Stimme (None = klonen)."""
    model_id = model["id"]
    family = model.get("family") or ""
    if model.get("missing"):
        description = "Steht gerade nicht in der Modell-Liste von audio.cpp."
    else:
        description = (f"Modell auf dem audio.cpp-Server (Familie {family or '?'}"
                       f"{', Streaming' if model.get('mode') == 'streaming' else ''}). ")
        description += (f"Spricht mit der eingebauten Stimme '{builtin}'." if builtin else
                        "Klont aus dem Voice-Sample der Stimme (mit Transkript am genauesten), "
                        "sofern das Modell klonen kann.")
    return {
        "label": f"audio.cpp · {model_id}",
        "name": f"audio.cpp/{model_id}",
        "client": audiocpp.AudioCppClient(model_id),
        "per_voice": True,
        # Ob ein Modell ein Sample braucht, verraet audio.cpp nicht - ohne
        # Sample spricht es mit seiner Standardstimme oder meldet sich.
        "needs_sample": False,
        "container": "audio.cpp",
        "audiocpp": True,
        "model": model_id,
        "family": family,
        "deploy_hint": AUDIOCPP_DEPLOY_HINT,
        "logs_hint": AUDIOCPP_LOGS_HINT,
        "base_url": audiocpp.base_url,
        "health_path": "/health",
        # Probehoeren und Filler warten aufs Laden, der Live-Turn nie.
        "preview_kwargs": {"wait_for_load": True},
        "description": description,
    }


async def audiocpp_server() -> dict:
    """Zustand des audio.cpp-Servers fuers Panel: erreichbar?, Backend,
    alle Modelle (auch ASR & Co.). Aktualisiert dabei die gemerkte Liste."""
    url = audiocpp.base_url()
    result = {"configured": bool(url), "url": url, "reachable": False, "detail": "",
              "health": {}, "models": []}
    if not url:
        result["detail"] = "Keine Adresse eingetragen."
        return result
    try:
        data = await audiocpp.fetch_server()
    except httpx.HTTPStatusError as exc:
        result["detail"] = (f"Unter {url} antwortet kein audio.cpp-Server (HTTP "
                            f"{exc.response.status_code} auf /v1/models) - Adresse und Port pruefen.")
        return result
    except ValueError:
        result["detail"] = (f"Unter {url} antwortet kein audio.cpp-Server (keine Modell-Liste) - "
                            "Adresse und Port pruefen.")
        return result
    except Exception as exc:
        result["detail"] = _unreachable_detail(exc, _AUDIOCPP_SERVER)
        return result
    result.update(reachable=True, health=data["health"], models=data["models"])
    return result


def _audiocpp_status(spec: dict, server: dict) -> dict:
    if not server["configured"]:
        return {"status": "unreachable",
                "detail": "Keine audio.cpp-Adresse eingetragen (Sprachausgabe -> audio.cpp)."}
    if not server["reachable"]:
        return {"status": "unreachable", "detail": server["detail"]}
    model_id = spec["model"]
    entry = next((m for m in server["models"] if m.get("id") == model_id), None)
    if entry is None:
        return {"status": "error",
                "detail": ("Steht nicht (mehr) in der Modell-Liste von audio.cpp - dort laden bzw. "
                           "in die server.json eintragen oder eine andere Engine aktivieren.")}
    if entry.get("task") != "tts":
        return {"status": "error", "detail": f"Ist auf audio.cpp als '{entry.get('task')}' eingetragen, nicht als tts."}
    if entry.get("loaded"):
        return {"status": "ok", "detail": "geladen"}
    if audiocpp.warming_up(model_id):
        return {"status": "loading", "detail": "Modell wird geladen"}
    error = audiocpp.warm_up_error(model_id)
    if error:
        return {"status": "error", "detail": f"Laden fehlgeschlagen: {error}"}
    return {"status": "idle", "detail": "nicht geladen - laedt beim Aktivieren bzw. beim ersten Probehoeren"}


async def audiocpp_panel(server: dict) -> dict:
    """audio.cpp-Bereich im Panel: Adresse, Karte, Serverzustand und alle
    Modelle - TTS-Modelle mit ihren eingebauten Stimmen zur Auswahl."""
    choices = audiocpp.voice_choices()
    tts = [m for m in server["models"] if m.get("task") == "tts"]

    async def voices_of(model: str) -> list[str]:
        try:
            return await audiocpp.list_voices(model)
        except Exception:
            return []

    voices = await asyncio.gather(*(voices_of(m["id"]) for m in tts)) if server["reachable"] else []
    by_model = dict(zip((m["id"] for m in tts), voices))
    health = server["health"]
    return {
        "url": repos.get_setting(audiocpp.URL_SETTING) or "",
        "url_default": settings.audiocpp_base_url.rstrip("/"),
        "url_effective": audiocpp.base_url(),
        "device": audiocpp.device(),
        "configured": server["configured"],
        "reachable": server["reachable"],
        "detail": server["detail"],
        "backend": health.get("backend", ""),
        "ui_management": bool(health.get("ui_management")),
        "models": [
            {
                "id": m["id"],
                "engine_id": audiocpp.engine_id(m["id"]) if m.get("task") == "tts" else None,
                "family": m.get("family", ""),
                "task": m.get("task", ""),
                "mode": m.get("mode", ""),
                "loaded": bool(m.get("loaded")),
                "voice": choices.get(m["id"], ""),
                "voices": by_model.get(m["id"], []),
            }
            for m in server["models"]
        ],
    }


async def audiocpp_loaded(server: dict | None = None) -> list[str]:
    """IDs der TTS-Modelle, die audio.cpp gerade im Speicher hat."""
    server = server if server is not None else await audiocpp_server()
    return [m["id"] for m in server["models"] if m.get("task") == "tts" and m.get("loaded")]


async def _pause_audiocpp(models: list[str], reason: str) -> list[str]:
    """audio.cpp-Modelle entladen ("pausieren") - best effort, mit Hinweis."""
    if not models:
        return []
    try:
        unloaded = await audiocpp.unload(models)
    except Exception as exc:
        return [f"audio.cpp: {', '.join(models)} liess sich nicht entladen: {str(exc)[:200]}"]
    return [f"audio.cpp/{model} entladen ({reason})." for model in unloaded]


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
            "audiocpp": spec.get("audiocpp", False),
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
    Karte bleiben, wie sie sind. audio.cpp (v1.21): Ein Modell dort wird
    geladen, andere audio.cpp-Stimmen entladen; wechselt die Hauptstimme weg
    von audio.cpp, wird die bisherige dort pausiert. Rueckgabe: Hinweise
    fuers Panel."""
    engines = registry()
    spec = engines[engine_id]
    previous = active_engine()
    notes: list[str] = []
    if spec.get("audiocpp"):
        notes += await _activate_audiocpp(engine_id, spec, engines)
    else:
        # Bisherige audio.cpp-Hauptstimme pausieren - vor dem Laden, damit
        # ihr VRAM frei ist, falls sie sich die Karte teilen.
        previous_model = audiocpp.model_of(previous)
        service = spec.get("gpu_service")
        target = await _target_device(service) if service else None
        pause: list[str] = []
        if audiocpp.base_url() and (previous_model or (target and _audiocpp_on(target))):
            loaded = await audiocpp_loaded()
            pause = [m for m in loaded if m == previous_model or (target and _audiocpp_on(target))]
        if pause:
            reason = f"gleiche Karte, {target}" if target and _audiocpp_on(target) else "Hauptstimme gewechselt"
            notes += await _pause_audiocpp(pause, reason)
        if service:
            gpu_manager.store_assignment(service, target)
            if target.startswith("cuda"):
                notes += await _unload_same_card(target, engines, exclude=engine_id)
            status = await gpu_manager.service_status(service)
            if status.get("reachable") and not (status.get("assigned") == target and status.get("loaded")):
                try:
                    result = await gpu_manager.apply_device(service, target)
                    notes.append(_load_note(spec["name"], result, target))
                except Exception as exc:
                    notes.append(f"{spec['name']} konnte nicht auf {target} laden: {str(exc)[:200]}")
    set_active_engine(engine_id)
    return notes


def _audiocpp_on(target: str) -> bool:
    """Rechnet audio.cpp (laut Angabe im Panel) auf dieser Karte?"""
    card = audiocpp.device()
    return card.startswith("cuda") and _same_card(card, target)


async def _unload_same_card(target: str, engines: dict[str, dict], exclude: str) -> list[str]:
    """Andere ausschaltbare Sprachausgaben, die auf `target` geladen sind,
    entladen - und als "automatisch aus" merken: Faellt die neue
    Hauptstimme spaeter ganz aus, kommt XTTS von selbst zurueck."""
    notes: list[str] = []
    for other_id, other in engines.items():
        other_service = other.get("gpu_service")
        if other_id == exclude or not other_service or gpu_manager.is_off(other_service):
            continue
        status = await gpu_manager.service_status(other_service)
        if not (status.get("reachable") and status.get("loaded")
                and _same_card(status.get("effective"), target)):
            continue
        try:
            await gpu_manager.apply_device(other_service, gpu_manager.OFF)
            gpu_manager.mark_auto_off(other_service, exclude)
            notes.append(f"{other['name']} entladen (gleiche Karte, {target}).")
        except Exception as exc:
            notes.append(f"{other['name']} liess sich nicht entladen: {str(exc)[:200]}")
    return notes


async def _activate_audiocpp(engine_id: str, spec: dict, engines: dict[str, dict]) -> list[str]:
    """Modell auf audio.cpp zur Hauptstimme machen: andere audio.cpp-Stimmen
    entladen, auf der Karte von audio.cpp (falls angegeben) XTTS & Co.
    entladen, dann das Modell laden. Ist der Server nicht erreichbar, wird
    nichts entladen - XTTS spricht weiter, bis audio.cpp laeuft."""
    name = spec["name"]
    server = await audiocpp_server()
    if not server["reachable"]:
        fallback = get_engine(fallback_engine())["name"]
        return [f"audio.cpp ist nicht erreichbar: {server['detail']} Bis der Server laeuft, spricht {fallback}."]
    model = spec["model"]
    entry = next((m for m in server["models"] if m.get("id") == model), None)
    if entry is None:
        return [f"{name} steht nicht (mehr) in der Modell-Liste von audio.cpp."]
    notes = await _pause_audiocpp([m for m in await audiocpp_loaded(server) if m != model],
                                  "Platz fuer die neue Hauptstimme")
    card = audiocpp.device()
    if card.startswith("cuda"):
        notes += await _unload_same_card(card, engines, exclude=engine_id)
    was_loaded = bool(entry.get("loaded"))
    try:
        # Auch ein schon geladenes Modell spricht einen Testsatz: Das deckt
        # einen kaputten Zustand in audio.cpp auf - und der Client heilt ihn
        # dabei (entladen, frisch laden).
        done = await audiocpp.ensure_loaded(model, audiocpp.warm_up_voice(), AUDIOCPP_LOAD_WAIT_S)
    except Exception as exc:
        # Scheitert nur der Testsatz (z. B. Stimme ohne Sample), ist das
        # Modell trotzdem geladen - beides sagen.
        if model in await audiocpp_loaded():
            notes.append(f"{name} geladen - der Testsatz scheiterte aber: {str(exc)[:300]}")
        else:
            notes.append(f"{name}: Laden fehlgeschlagen - {str(exc)[:300]}")
        return notes
    if done:
        notes.append(f"{name} ist geladen." if was_loaded else f"{name} geladen.")
    else:
        fallback = get_engine(fallback_engine())["name"]
        notes.append(f"{name} laedt noch - bis es fertig ist, spricht {fallback}.")
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
    Ist XTTS im Panel ausgeschaltet, springt Piper ein (v1.19). Hatte erst
    das Aktivieren der ausgefallenen Engine XTTS entladen, wird es wieder
    geladen (v1.21) - ab dann spricht wieder XTTS."""
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
    except Exception as exc:
        if started or engine_id == fallback:
            raise
        gone = engine_gone(exc)
        if isinstance(exc, audiocpp.AudioCppNotReady) or gone:
            # Erwartbar (Modell laedt gerade, Server gestoppt) - eine Zeile
            # statt eines Stacktraces pro Turn.
            logger.warning("TTS-Engine '%s': %s - Hauptantwort kommt von '%s'",
                           engine_id, str(exc)[:200] or type(exc).__name__, fallback)
        else:
            logger.exception(
                "TTS-Engine '%s' fehlgeschlagen - Hauptantwort kommt von '%s'", engine_id, fallback
            )
        if gone:
            revive_fallback()
    async for item in get_engine(fallback)["client"].stream(text, voice_id):
        yield item


def engine_gone(exc: BaseException) -> bool:
    """Ist der Server der Engine weg (Container gestoppt, abgestuerzt) bzw.
    kennt audio.cpp das Modell nicht mehr? Dann belegt sie kein VRAM."""
    if _caused_by(exc, httpx.ConnectError) or _caused_by(exc, httpx.ConnectTimeout):
        return True
    return isinstance(exc, audiocpp.AudioCppError) and exc.status_code == 404


_revival: asyncio.Task | None = None
# Scheitert das Wiederladen (z. B. XTTS-Container auch weg), nicht bei jedem
# Turn neu versuchen.
REVIVE_RETRY_S = 60.0
_revival_failed_at = 0.0


def revive_fallback() -> None:
    """XTTS wieder laden, wenn es nur fuer die jetzt ausgefallene Engine
    entladen wurde ("automatisch aus") - im Hintergrund, bis dahin spricht
    Piper. Von Hand ausgeschaltet bleibt es aus."""
    global _revival
    service = ENGINES[DEFAULT_ENGINE]["gpu_service"]
    if not gpu_manager.auto_off(service) or (_revival is not None and not _revival.done()):
        return
    if time.monotonic() - _revival_failed_at < REVIVE_RETRY_S:
        return
    _revival = asyncio.create_task(_revive(service))


async def _revive(service: str) -> None:
    global _revival_failed_at
    target = gpu_manager.last_device(service) or "cuda:0"
    try:
        await gpu_manager.apply_device(service, target)
        logger.warning("Aktive Sprachausgabe nicht erreichbar - %s wieder auf %s geladen", service, target)
    except Exception as exc:
        _revival_failed_at = time.monotonic()
        logger.warning("%s liess sich nicht wieder laden: %s", service, str(exc)[:200])


@dataclass
class Synthesis:
    pcm: bytes
    rate: int
    # Zeit bis zum ersten Chunk (bei XTTS ~1 s Audio) und fuer die
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
    # Vertrags-Engines und audio.cpp: aufs Laden des Modells warten statt
    # sofort aufzugeben.
    stream = spec["client"].stream(text, voice_id, **spec.get("preview_kwargs", {}))
    try:
        async for chunk_rate, chunk in stream:
            if not pcm:
                first_chunk_ms = (time.perf_counter() - started) * 1000
            rate = chunk_rate
            pcm.extend(chunk)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # "[Errno -2] Name or service not known" -> was zu tun ist.
        raise EngineUnreachable(_unreachable_detail(exc, spec)) from exc
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
    if spec.get("audiocpp"):
        return _audiocpp_status(spec, await audiocpp_server())
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
    return (f"{spec['name']}-Server hat die Verbindung mitten in der Synthese abgebrochen - "
            f"er ist dabei vermutlich abgestuerzt. {logs} (Fehlertext), dazu nvidia-smi (VRAM).")


def _caused_by(exc: BaseException, kind: type) -> bool:
    """httpx verpackt die eigentliche Ursache (socket-Fehler) in eine Kette."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, kind):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


async def overview(server: dict | None = None) -> list[dict]:
    """Alle Engines mit Live-Status (parallel geprueft); Vertrags-Engines
    liefern dabei auch ihren Steckbrief neu, audio.cpp seine Modell-Liste
    (server: schon geholter Zustand, spart die zweite Abfrage)."""
    contract = {eid: spec["url"] for eid, spec in registry().items() if spec.get("contract")}
    if server is None:
        server, *_ = await asyncio.gather(
            audiocpp_server(), *(refresh_info(eid, url) for eid, url in contract.items()))
    else:
        await asyncio.gather(*(refresh_info(eid, url) for eid, url in contract.items()))
    engines = registry()

    async def status_of(engine_id: str, spec: dict) -> dict:
        if spec.get("audiocpp"):
            return _audiocpp_status(spec, server)
        return await engine_status(engine_id)

    statuses = await asyncio.gather(*(status_of(eid, spec) for eid, spec in engines.items()))
    return [
        {**entry, "status": status}
        for entry, status in zip(public_engines(engines), statuses)
    ]
