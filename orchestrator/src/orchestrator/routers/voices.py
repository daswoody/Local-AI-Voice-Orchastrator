from fastapi import APIRouter

from .. import repos
from ..schemas import Voice

router = APIRouter()


@router.get("/v1/voices", response_model=list[Voice])
def list_voices() -> list[Voice]:
    """Stimmen aus der DB (1.7b) - gepflegt ueber das Admin-Panel."""
    return [Voice(**voice) for voice in repos.list_voices()]
