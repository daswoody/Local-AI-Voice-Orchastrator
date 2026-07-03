"""Admin-API (Mikro-Phase 1.7c, Konzept 4.14).

Alle Routen erfordern Tier 3 via require_admin (4.4). Das zugehoerige
Web-Frontend liegt unter /admin (statisches Vanilla-JS, siehe
static/admin/)."""

import logging
import sqlite3
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel

from .. import repos
from ..config import settings
from ..schemas import CardLayout
from ..security import hash_password, require_admin
from ..services import filler_service
from ..services.litellm_client import litellm_client

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
    if "password" in fields:
        password = fields.pop("password")
        if password:
            fields["password_hash"] = hash_password(password)
    user = repos.update_user(user_id, fields)
    if user is None:
        raise HTTPException(status_code=404, detail="User nicht gefunden")
    return user


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int) -> None:
    try:
        if not repos.delete_user(user_id):
            raise HTTPException(status_code=404, detail="User nicht gefunden")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ---- Charakter -----------------------------------------------------------------


class CharacterPayload(BaseModel):
    prompt: str


@router.get("/character")
def get_character() -> dict:
    return {"prompt": repos.get_setting("character_prompt") or ""}


@router.put("/character")
def set_character(payload: CharacterPayload) -> dict:
    repos.set_setting("character_prompt", payload.prompt)
    return {"prompt": payload.prompt}


# ---- Stimmen --------------------------------------------------------------------


class VoiceCreate(BaseModel):
    id: str
    name: str
    language: str = "de"


def _sample_path(voice_id: str) -> Path:
    return Path(settings.voices_dir) / f"{voice_id}.wav"


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
    return {"voice_id": voice_id, "bytes": len(content)}


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
        return repos.create_filler(payload.title, payload.text, payload.trigger_id, payload.enabled)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="trigger_id existiert nicht")


@router.put("/fillers/{filler_id}")
def update_filler(filler_id: int, payload: FillerPayload) -> dict:
    old = repos.get_filler(filler_id)
    if old is None:
        raise HTTPException(status_code=404, detail="Filler nicht gefunden")
    updated = repos.update_filler(filler_id, payload.title, payload.text, payload.trigger_id, payload.enabled)
    if old["text"] != payload.text:
        # Text geaendert -> gecachtes Audio passt nicht mehr.
        filler_service.delete_audio(filler_id)
    return updated


@router.delete("/fillers/{filler_id}", status_code=204)
def delete_filler(filler_id: int) -> None:
    if not repos.delete_filler(filler_id):
        raise HTTPException(status_code=404, detail="Filler nicht gefunden")
    filler_service.delete_audio(filler_id)


@router.post("/fillers/{filler_id}/generate")
async def generate_filler(filler_id: int) -> dict:
    """Erzeugt das Filler-Audio per XTTS fuer alle Stimmen mit Sample (1.7d)."""
    try:
        results = await filler_service.generate_audio(filler_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"results": results}


# ---- Karten-Layouts ---------------------------------------------------------------


class CardPayload(BaseModel):
    card_type: str
    root: dict


@router.get("/cards")
def list_cards() -> list[CardLayout]:
    return [CardLayout(**entry) for entry in repos.list_card_layouts(0)]


@router.post("/cards", status_code=201)
def create_card(payload: CardPayload) -> CardLayout:
    return CardLayout(**repos.upsert_card_layout(payload.card_type.strip(), payload.root))


@router.put("/cards/{card_type}")
def update_card(card_type: str, payload: CardPayload) -> CardLayout:
    return CardLayout(**repos.upsert_card_layout(card_type, payload.root))


@router.delete("/cards/{card_type}", status_code=204)
def delete_card(card_type: str) -> None:
    if not repos.delete_card_layout(card_type):
        raise HTTPException(status_code=404, detail="Kartentyp nicht gefunden")


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
