"""Admin-API (Mikro-Phase 1.7c, Konzept 4.14).

Alle Routen erfordern Tier 3 via require_admin (4.4). Das zugehoerige
Web-Frontend liegt unter /admin (statisches Vanilla-JS, siehe
static/admin/)."""

import logging
import sqlite3
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field, field_validator

from .. import repos
from ..audio import pcm16_to_wav, wav_to_pcm16
from ..config import settings
from ..schemas import CardLayout
from ..security import hash_password, require_admin
from ..services import filler_service, gpu_manager, tts_engines
from ..services.litellm_client import litellm_client
from ..services.stt_client import stt_client
from ..services.tts_client import (
    BREEZE_INSTRUCTION_SETTING,
    BREEZE_URL_SETTING,
    breeze_base_url,
    normalize_base_url,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/admin", dependencies=[Depends(require_admin)])


# ---- Users ---------------------------------------------------------------------


class UserCreate(BaseModel):
    username: str
    password: str
    display_name: str = ""
    tier: int = 1
    system_prompt_override: str | None = None
    default_voice_id: str | None = None


class UserUpdate(BaseModel):
    password: str | None = None
    display_name: str | None = None
    tier: int | None = None
    system_prompt_override: str | None = None
    default_voice_id: str | None = None


@router.get("/users")
def list_users() -> list[dict]:
    return repos.list_users()


@router.post("/users", status_code=201)
def create_user(payload: UserCreate) -> dict:
    if not payload.username.strip() or not payload.password:
        raise HTTPException(status_code=400, detail="username und password sind Pflicht")
    try:
        return repos.create_user(
            username=payload.username.strip(),
            password_hash=hash_password(payload.password),
            display_name=payload.display_name,
            tier=payload.tier,
            system_prompt_override=payload.system_prompt_override,
            default_voice_id=payload.default_voice_id,
        )
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="username existiert bereits")


@router.put("/users/{user_id}")
def update_user(user_id: int, payload: UserUpdate) -> dict:
    fields: dict[str, Any] = payload.model_dump(exclude_unset=True)
    password_changed = False
    if "password" in fields:
        password = fields.pop("password")
        if password:
            fields["password_hash"] = hash_password(password)
            password_changed = True
    user = repos.update_user(user_id, fields)
    if user is None:
        raise HTTPException(status_code=404, detail="User nicht gefunden")
    if password_changed:
        # Security First (Abschnitt 8): Passwort-Reset macht alle
        # Geraete-Tokens des Users ungueltig - die Geraete melden sich mit
        # dem neuen Passwort neu an.
        repos.delete_device_tokens_for_user(user["username"])
    return user


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int) -> None:
    try:
        if not repos.delete_user(user_id):
            raise HTTPException(status_code=404, detail="User nicht gefunden")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ---- Geraete (v1.13.1) -----------------------------------------------------------
#
# Jeder Login erzeugt ein langlebiges Geraete-Token; hier sieht der Admin
# alle angemeldeten Geraete (wer, welches Geraet, zuletzt gesehen) und kann
# einzelne abmelden (Token-Widerruf, z. B. Handy verloren).


@router.get("/devices")
def list_devices() -> list[dict]:
    return repos.list_device_tokens()


@router.delete("/devices/{token_id}", status_code=204)
def revoke_device(token_id: int) -> None:
    if not repos.delete_device_token(token_id):
        raise HTTPException(status_code=404, detail="Geraet nicht gefunden")


# ---- Charakter -----------------------------------------------------------------


class CharacterPayload(BaseModel):
    prompt: str
    # Prompt fuer die Sprach-Kurzfassung (Audio-Zusammenfassung); leer ->
    # eingebauter Default greift wieder.
    voice_summary_prompt: str = ""


@router.get("/character")
def get_character() -> dict:
    return {
        "prompt": repos.get_setting("character_prompt") or "",
        "voice_summary_prompt": repos.get_setting("voice_summary_prompt") or "",
    }


@router.put("/character")
def set_character(payload: CharacterPayload) -> dict:
    repos.set_setting("character_prompt", payload.prompt)
    repos.set_setting("voice_summary_prompt", payload.voice_summary_prompt)
    return {
        "prompt": payload.prompt,
        "voice_summary_prompt": payload.voice_summary_prompt,
    }


# ---- Stimmen --------------------------------------------------------------------


class VoiceCreate(BaseModel):
    id: str
    name: str
    language: str = "de"


class VoiceUpdate(BaseModel):
    name: str | None = None
    language: str | None = None
    # Exaktes Transkript des Samples (v1.17) - braucht Breeze TTS 2 fuers
    # Voice-Cloning. Leer = keins.
    sample_text: str | None = None


def _sample_path(voice_id: str) -> Path:
    return Path(settings.voices_dir) / f"{voice_id}.wav"


async def _transcribe_sample(wav: bytes) -> tuple[str | None, str | None]:
    """Transkript-Vorschlag per Whisper -> (Text, Fehler). Best effort: Das
    Sample ist auch ohne Transkript gespeichert, der Admin kann es im Panel
    selbst eintragen."""
    try:
        pcm, rate = wav_to_pcm16(wav)
        text = (await stt_client.transcribe(pcm, rate)).strip()
    except Exception as exc:
        logger.warning("Transkription des Voice-Samples fehlgeschlagen: %s", str(exc)[:200])
        return None, str(exc)[:300] or type(exc).__name__
    if not text:
        return None, "Whisper hat im Sample keine Sprache erkannt"
    return text, None


@router.get("/voices")
def list_voices() -> list[dict]:
    return [
        {**voice, "has_sample": _sample_path(voice["id"]).exists()}
        for voice in repos.list_voices()
    ]


@router.post("/voices", status_code=201)
def create_voice(payload: VoiceCreate) -> dict:
    voice_id = payload.id.strip()
    if not voice_id or "/" in voice_id or "\\" in voice_id or ".." in voice_id:
        raise HTTPException(status_code=400, detail="ungueltige voice_id")
    try:
        return repos.create_voice(voice_id, payload.name, payload.language)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="voice_id existiert bereits")


@router.put("/voices/{voice_id}")
def update_voice(voice_id: str, payload: VoiceUpdate) -> dict:
    fields: dict[str, Any] = payload.model_dump(exclude_unset=True)
    if "sample_text" in fields:
        fields["sample_text"] = (fields["sample_text"] or "").strip() or None
    voice = repos.update_voice(voice_id, fields)
    if voice is None:
        raise HTTPException(status_code=404, detail="Stimme nicht gefunden")
    return {**voice, "has_sample": _sample_path(voice_id).exists()}


@router.delete("/voices/{voice_id}", status_code=204)
def delete_voice(voice_id: str) -> None:
    if not repos.delete_voice(voice_id):
        raise HTTPException(status_code=404, detail="Stimme nicht gefunden")
    _sample_path(voice_id).unlink(missing_ok=True)


@router.post("/voices/{voice_id}/sample")
async def upload_voice_sample(voice_id: str, file: UploadFile) -> dict:
    """Sample-Upload (WAV, ~6-30s sauberes Sprechmaterial) - landet im
    geteilten Voices-Volume, aus dem XTTS das Voice-Cloning speist. Damit
    entfaellt das manuelle docker cp."""
    if repos.get_voice(voice_id) is None:
        raise HTTPException(status_code=404, detail="Stimme nicht gefunden")

    content = await file.read()
    if content[:4] != b"RIFF":
        raise HTTPException(status_code=400, detail="nur WAV-Dateien (RIFF-Header) erlaubt")

    path = _sample_path(voice_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    # Achtung: Bereits vorgenerierte Filler dieser Stimme klingen noch nach
    # dem alten Sample - im Panel neu generieren.

    # Transkript fuer Breeze (v1.17) gleich mit vorschlagen. Ein altes
    # Transkript passt zum neuen Sample nicht mehr und wird so oder so
    # ersetzt - ein falsches Transkript verdirbt das Voice-Cloning.
    transcript, error = await _transcribe_sample(content)
    repos.update_voice(voice_id, {"sample_text": transcript})
    result = {"voice_id": voice_id, "bytes": len(content), "sample_text": transcript}
    if error:
        result["transcript_error"] = error
    return result


@router.post("/voices/{voice_id}/transcribe")
async def transcribe_voice_sample(voice_id: str) -> dict:
    """Transkript fuer ein vorhandenes Sample (z. B. vor v1.17 hochgeladen)
    per Whisper neu vorschlagen lassen."""
    if repos.get_voice(voice_id) is None:
        raise HTTPException(status_code=404, detail="Stimme nicht gefunden")
    path = _sample_path(voice_id)
    if not path.exists():
        raise HTTPException(status_code=400, detail="Fuer diese Stimme ist kein Sample hochgeladen")
    transcript, error = await _transcribe_sample(path.read_bytes())
    if transcript is None:
        raise HTTPException(status_code=502, detail=f"Transkription fehlgeschlagen: {error}")
    voice = repos.update_voice(voice_id, {"sample_text": transcript})
    return {**voice, "has_sample": True}


# ---- Filler-Trigger --------------------------------------------------------------


class TriggerPayload(BaseModel):
    name: str
    kind: Literal["thinking", "search", "tool"]
    tool_pattern: str | None = None


@router.get("/triggers")
def list_triggers() -> list[dict]:
    return repos.list_triggers()


@router.post("/triggers", status_code=201)
def create_trigger(payload: TriggerPayload) -> dict:
    try:
        return repos.create_trigger(payload.name, payload.kind, payload.tool_pattern)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Trigger-Name existiert bereits")


@router.put("/triggers/{trigger_id}")
def update_trigger(trigger_id: int, payload: TriggerPayload) -> dict:
    trigger = repos.update_trigger(trigger_id, payload.name, payload.kind, payload.tool_pattern)
    if trigger is None:
        raise HTTPException(status_code=404, detail="Trigger nicht gefunden")
    return trigger


@router.delete("/triggers/{trigger_id}", status_code=204)
def delete_trigger(trigger_id: int) -> None:
    # Zugehoerige Filler fallen per ON DELETE CASCADE mit weg -> deren
    # Audio-Cache vorher aufraeumen.
    for filler in repos.list_fillers():
        if filler["trigger_id"] == trigger_id:
            filler_service.delete_audio(filler["id"])
    if not repos.delete_trigger(trigger_id):
        raise HTTPException(status_code=404, detail="Trigger nicht gefunden")


# ---- Filler ----------------------------------------------------------------------


class FillerPayload(BaseModel):
    title: str
    text: str
    trigger_id: int
    enabled: bool = True
    # Wartezeit, bevor dieser Filler spielen darf (0 = sofort): Ist die
    # Antwort bzw. das Tool vorher fertig, entfaellt der Filler.
    delay_ms: int = Field(default=1200, ge=0, le=60_000)
    # Womit das Audio vorgeneriert wird (v1.15); erlaubt ist jede Engine
    # der Registry (v1.17).
    engine: str = "xtts"

    @field_validator("engine")
    @classmethod
    def _known_engine(cls, value: str) -> str:
        if value not in tts_engines.engine_ids():
            raise ValueError(f"unbekannte TTS-Engine '{value}'")
        return value


@router.get("/tts-engines")
def list_tts_engines() -> list[dict]:
    """Engines, die Filler-Audio erzeugen koennen - Grundlage fuer das
    Dropdown im Filler-Formular."""
    return tts_engines.public_engines()


@router.get("/fillers")
def list_fillers() -> list[dict]:
    voices = [v["id"] for v in repos.list_voices()]
    return [
        {
            **filler,
            "audio_status": {
                voice_id: filler_service.has_audio(filler["id"], voice_id) for voice_id in voices
            },
        }
        for filler in repos.list_fillers()
    ]


@router.post("/fillers", status_code=201)
def create_filler(payload: FillerPayload) -> dict:
    try:
        return repos.create_filler(payload.title, payload.text, payload.trigger_id,
                                   payload.enabled, payload.delay_ms, payload.engine)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="trigger_id existiert nicht")


@router.put("/fillers/{filler_id}")
def update_filler(filler_id: int, payload: FillerPayload) -> dict:
    old = repos.get_filler(filler_id)
    if old is None:
        raise HTTPException(status_code=404, detail="Filler nicht gefunden")
    updated = repos.update_filler(filler_id, payload.title, payload.text, payload.trigger_id,
                                  payload.enabled, payload.delay_ms, payload.engine)
    if old["text"] != payload.text or (old.get("engine") or "xtts") != payload.engine:
        # Text ODER Engine geaendert -> gecachtes Audio passt nicht mehr
        # (es waere sonst noch mit der alten Engine/dem alten Text erzeugt).
        filler_service.delete_audio(filler_id)
    return updated


@router.get("/fillers/{filler_id}/audio")
def get_filler_audio(filler_id: int, voice_id: str) -> Response:
    """Vorgeneriertes Filler-Audio zum Anhoeren im Panel (v1.15).

    Damit faellt eine misslungene Generierung beim Anlegen auf - statt
    erst, wenn der Filler zufaellig im Realtime-Talk gespielt wird."""
    path = filler_service.audio_path(filler_id, voice_id)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Fuer Stimme '{voice_id}' ist noch kein Audio generiert",
        )
    return Response(
        content=path.read_bytes(),
        media_type="audio/wav",
        # Nach einem Neu-Generieren soll der Browser das neue Audio holen.
        headers={"Cache-Control": "no-store"},
    )


@router.delete("/fillers/{filler_id}", status_code=204)
def delete_filler(filler_id: int) -> None:
    if not repos.delete_filler(filler_id):
        raise HTTPException(status_code=404, detail="Filler nicht gefunden")
    filler_service.delete_audio(filler_id)


@router.post("/fillers/{filler_id}/generate")
async def generate_filler(filler_id: int, voice_id: str | None = None) -> dict:
    """Erzeugt das Filler-Audio mit der Engine des Fillers fuer alle Stimmen
    (1.7d/v1.15) - oder mit ?voice_id=... nur fuer diese eine (v1.16): Eine
    gelungene Stimme bleibt so erhalten, wenn nur eine andere neu gewuerfelt
    wird."""
    try:
        results = await filler_service.generate_audio(filler_id, voice_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"results": results}


# ---- Sprachausgabe / TTS-Engine (v1.17) ------------------------------------------
#
# Wie beim LLM (Modelle): Der Admin waehlt server-weit, welche Engine die
# gesprochenen Antworten erzeugt. Probehoeren rendert einen Testsatz mit
# einer beliebigen Engine - ohne Fallback und mit Zeitmessung, damit sich
# Engines auf der echten Hardware vergleichen lassen.


class TtsActivatePayload(BaseModel):
    engine: str


class TtsPreviewPayload(BaseModel):
    engine: str
    voice_id: str
    text: str = Field(min_length=1, max_length=500)


class TtsSettingsPayload(BaseModel):
    # Nur mitgeschickte Felder werden geaendert.
    # Optionale Sprechanweisung fuer Breeze ("Voice Direction"), z. B.
    # "Speak in a warm, calm tone." - leer = keine.
    breeze_instruction: str | None = None
    # Adresse des Breeze-Servers (v1.18): Container-Name, IP:Port oder
    # Domain - leer = BREEZE_BASE_URL aus der .env.
    breeze_url: str | None = None


@router.get("/tts")
async def tts_overview() -> dict:
    return {
        "active_engine": tts_engines.active_engine(),
        "engines": await tts_engines.overview(),
        "breeze_instruction": repos.get_setting(BREEZE_INSTRUCTION_SETTING) or "",
        "breeze_url": repos.get_setting(BREEZE_URL_SETTING) or "",
        "breeze_url_default": settings.breeze_base_url.rstrip("/"),
        "breeze_url_effective": breeze_base_url(),
    }


@router.post("/tts/activate")
async def activate_tts_engine(payload: TtsActivatePayload) -> dict:
    if payload.engine not in tts_engines.engine_ids():
        raise HTTPException(status_code=404, detail=f"Unbekannte TTS-Engine '{payload.engine}'")
    tts_engines.set_active_engine(payload.engine)
    status = await tts_engines.engine_status(payload.engine)
    result: dict[str, Any] = {"active_engine": payload.engine, "status": status}
    if status["status"] != "ok":
        # Nicht blockieren (man darf eine Engine vorab waehlen), aber klar
        # sagen, was bis dahin passiert.
        name = tts_engines.get_engine(payload.engine)["name"]
        meanwhile = (
            "Bis der Dienst laeuft, spricht XTTS die Antworten."
            if payload.engine != tts_engines.DEFAULT_ENGINE
            else "Bis dahin liest die App die Antworten selbst vor (TTS-Fallback)."
        )
        result["warning"] = (
            f"{name} ist aktiviert, antwortet aber gerade nicht: {status['detail']} {meanwhile}"
        )
    return result


@router.post("/tts/preview")
async def preview_tts(payload: TtsPreviewPayload) -> Response:
    if payload.engine not in tts_engines.engine_ids():
        raise HTTPException(status_code=404, detail=f"Unbekannte TTS-Engine '{payload.engine}'")
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text fuer das Probehoeren fehlt")
    name = tts_engines.get_engine(payload.engine)["name"]
    try:
        result = await tts_engines.synthesize(payload.engine, text, payload.voice_id)
    except Exception as exc:
        logger.exception("Probehoeren mit %s fehlgeschlagen", payload.engine)
        raise HTTPException(status_code=502, detail=f"{name}: {str(exc)[:400] or type(exc).__name__}")
    if not result.pcm:
        raise HTTPException(status_code=502, detail=f"{name} hat keine Audio-Daten geliefert")
    return Response(
        content=pcm16_to_wav(result.pcm, result.rate),
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-store",
            "X-TTS-First-Chunk-Ms": str(round(result.first_chunk_ms)),
            "X-TTS-Total-Ms": str(round(result.total_ms)),
            "X-Audio-Ms": str(round(result.audio_ms)),
        },
    )


@router.put("/tts/settings")
def save_tts_settings(payload: TtsSettingsPayload) -> dict:
    if payload.breeze_url is not None:
        try:
            url = normalize_base_url(payload.breeze_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Breeze-Adresse ungueltig: {exc}")
        repos.set_setting(BREEZE_URL_SETTING, url)
    if payload.breeze_instruction is not None:
        repos.set_setting(BREEZE_INSTRUCTION_SETTING, payload.breeze_instruction.strip())
    return {
        "breeze_instruction": repos.get_setting(BREEZE_INSTRUCTION_SETTING) or "",
        "breeze_url": repos.get_setting(BREEZE_URL_SETTING) or "",
        "breeze_url_effective": breeze_base_url(),
    }


# ---- Karten-Layouts ---------------------------------------------------------------


class CardPayload(BaseModel):
    card_type: str
    # format 'json': root ist das Layout-JSON. format 'html' (4.12 v1.12):
    # html ist das Fragment mit {{data.*}}-Bindings, root wird serverseitig
    # durch das generic-Fallback ersetzt (Alt-Clients).
    root: dict = {}
    format: Literal["json", "html"] = "json"
    html: str | None = None


def _validate_card_payload(payload: CardPayload) -> None:
    if payload.format == "html":
        if not (payload.html or "").strip():
            raise HTTPException(status_code=400, detail="HTML-Layout ohne html-Inhalt")
    elif not payload.root:
        raise HTTPException(status_code=400, detail="JSON-Layout ohne root-Objekt")


@router.get("/cards")
def list_cards() -> list[CardLayout]:
    return [CardLayout(**entry) for entry in repos.list_card_layouts(0)]


@router.post("/cards", status_code=201)
def create_card(payload: CardPayload) -> CardLayout:
    _validate_card_payload(payload)
    return CardLayout(**repos.upsert_card_layout(
        payload.card_type.strip(), payload.root, payload.format, payload.html))


@router.put("/cards/{card_type}")
def update_card(card_type: str, payload: CardPayload) -> CardLayout:
    _validate_card_payload(payload)
    return CardLayout(**repos.upsert_card_layout(
        card_type, payload.root, payload.format, payload.html))


@router.delete("/cards/{card_type}", status_code=204)
def delete_card(card_type: str) -> None:
    if not repos.delete_card_layout(card_type):
        raise HTTPException(status_code=404, detail="Kartentyp nicht gefunden")


# ---- Agenten (4.16) ----------------------------------------------------------------
#
# Spezialisierte LLM-Laeufe mit eigenem Modell (aus LiteLLM) und Prompt.
# Jeder aktive Agent erscheint dem Haupt-LLM als Tool "agent-<slug>";
# die Beschreibung entscheidet, wann das LLM ihn auswaehlt.


class AgentPayload(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9_-]{1,40}$")
    name: str
    description: str
    system_prompt: str = ""
    model: str
    enabled: bool = True


@router.get("/agents")
def list_agents() -> list[dict]:
    return repos.list_agents()


@router.post("/agents", status_code=201)
def create_agent(payload: AgentPayload) -> dict:
    if not payload.name.strip() or not payload.description.strip() or not payload.model.strip():
        raise HTTPException(status_code=400, detail="name, description und model sind Pflicht")
    try:
        return repos.create_agent(payload.slug, payload.name, payload.description,
                                  payload.system_prompt, payload.model, payload.enabled)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Agent-ID (slug) existiert bereits")


@router.put("/agents/{slug}")
def update_agent(slug: str, payload: AgentPayload) -> dict:
    agent = repos.update_agent(slug, payload.name, payload.description,
                               payload.system_prompt, payload.model, payload.enabled)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent nicht gefunden")
    return agent


@router.delete("/agents/{slug}", status_code=204)
def delete_agent(slug: str) -> None:
    if not repos.delete_agent(slug):
        raise HTTPException(status_code=404, detail="Agent nicht gefunden")


# ---- GPU-Verteilung (4.2, v1.14) ----------------------------------------------------
#
# Zwei Karten, vier Dienste: Hier waehlt der Admin pro Dienst die Karte (oder
# CPU). Umgesetzt auf Anwendungsebene - die Container sehen alle Karten und
# laden ihr Modell dort neu, wo sie sollen (Begruendung siehe gpu_manager).


class DeviceAssignPayload(BaseModel):
    # "cpu", "cuda:0", "cuda:1", ...
    device: str = Field(pattern=r"^(cpu|cuda(:\d+)?)$")


@router.get("/gpus")
async def gpu_overview() -> dict:
    # Selbstheilung: Wurde ein Dienst zwischendurch neu gestartet, steht er
    # wieder auf seinem Compose-Default - beim Oeffnen der Seite ziehen wir
    # die gespeicherte Zuweisung nach.
    try:
        await gpu_manager.restore_assignments()
    except Exception:
        logger.exception("GPU-Zuweisungen konnten nicht geprueft werden")
    return await gpu_manager.overview()


@router.put("/gpus/{service}")
async def assign_device(service: str, payload: DeviceAssignPayload) -> dict:
    spec = gpu_manager.SERVICES.get(service)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Unbekannter Dienst '{service}'")
    if not spec["controllable"]:
        raise HTTPException(
            status_code=400,
            detail=f"{spec['label']} ist nicht umschaltbar: {spec.get('note', '')}",
        )
    try:
        return await gpu_manager.apply_device(service, payload.device)
    except Exception as exc:
        # Haeufigster Fall: Karte ist voll. Der Dienst sagt selbst, woran es
        # lag - das gehoert unveraendert ins Panel.
        raise HTTPException(status_code=502, detail=str(exc)[:400])


# ---- Modelle (ueber LiteLLM, 4.6/4.14) ----------------------------------------------
#
# Der Orchestrator spricht NIE direkt mit LM Studio: Das Panel listet die in
# LiteLLM registrierten Modelle und setzt das aktive Modell fuer alle
# LLM-Calls. Das physische Laden erledigt LM Studio per JIT beim ersten
# Request, das Entladen seine Idle-TTL/Auto-Evict.


class ModelActivatePayload(BaseModel):
    model: str


@router.get("/models")
async def list_models() -> dict:
    try:
        models = await litellm_client.list_models()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"LiteLLM nicht erreichbar: {exc}")
    return {"models": models, "active_model": litellm_client.active_model()}


@router.post("/models/activate")
async def activate_model(payload: ModelActivatePayload) -> dict:
    try:
        known = {model.get("id") for model in await litellm_client.list_models()}
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"LiteLLM nicht erreichbar: {exc}")
    if payload.model not in known:
        raise HTTPException(status_code=404, detail=f"Modell '{payload.model}' ist in LiteLLM nicht registriert")
    repos.set_setting("active_model", payload.model)
    return {"active_model": payload.model}
