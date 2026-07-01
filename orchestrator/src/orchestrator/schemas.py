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
    layout_version: int
    templates: list[CardLayout]


class DeviceTool(BaseModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any] = {}


class HelloFrame(BaseModel):
    type: Literal["hello"] = "hello"
    mode: Literal["chat", "talk", "assist"] = "chat"
    voice_id: str | None = None
    device_tools: list[DeviceTool] = []
