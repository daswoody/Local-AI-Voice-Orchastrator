from fastapi import APIRouter

from ..schemas import Voice

router = APIRouter()

# Platzhalter bis die Stimmen-Verwaltung (1.7c) und XTTS-v2 (1.10) stehen.
_STUB_VOICES = [
    Voice(id="default-de-female", name="Standard (weiblich, DE)"),
    Voice(id="default-de-male", name="Standard (maennlich, DE)"),
]


@router.get("/v1/voices", response_model=list[Voice])
def list_voices() -> list[Voice]:
    return _STUB_VOICES
