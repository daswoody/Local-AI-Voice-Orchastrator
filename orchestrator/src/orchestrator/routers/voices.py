from fastapi import APIRouter

from .. import repos
from ..schemas import Voice, VoicesResponse

router = APIRouter()


@router.get("/v1/voices", response_model=VoicesResponse)
def list_voices() -> VoicesResponse:
    """Stimmen aus der DB (1.7b), Response-Form laut docs/PROTOCOL.md:
    {"voices": [...]} statt nacktem Array."""
    return VoicesResponse(voices=[Voice(**voice) for voice in repos.list_voices()])
