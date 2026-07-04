from typing import Any, Literal

from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str
    device_name: str


class UserInfo(BaseModel):
    name: str
    tier: int


class LoginResponse(BaseModel):
    token: str
    user: UserInfo


class Voice(BaseModel):
    id: str
    name: str
    language: str = "de"


class CardLayout(BaseModel):
    card_type: str
    layout_version: int
    root: dict[str, Any]


class CardLayoutsResponse(BaseModel):
    # Feldnamen laut docs/PROTOCOL.md der App: version + layouts.
    version: int
    layouts: list[CardLayout]


class VoicesResponse(BaseModel):
    voices: list[Voice]


class DeviceTool(BaseModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any] = {}
    # 4.4: sensible Geraete-Tools erfordern App-seitige Bestaetigung; der
    # Orchestrator reicht das Flag nur durch (App setzt es im Manifest).
    sensitive: bool = False


class HelloFrame(BaseModel):
    type: Literal["hello"] = "hello"
    mode: Literal["chat", "talk", "assist"] = "chat"
    voice_id: str | None = None
    device_tools: list[DeviceTool] = []
