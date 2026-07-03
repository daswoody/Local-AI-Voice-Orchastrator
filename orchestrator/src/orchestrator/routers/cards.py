from fastapi import APIRouter

from .. import repos
from ..schemas import CardLayout, CardLayoutsResponse

router = APIRouter()


@router.get("/v1/cards/layouts", response_model=CardLayoutsResponse)
def get_layouts(since_version: int = 0) -> CardLayoutsResponse:
    """Layouts jetzt DB-gestuetzt (1.7b) statt in-memory - Aenderungen aus
    dem Admin-Panel ueberleben damit Container-Neustarts."""
    templates = [CardLayout(**entry) for entry in repos.list_card_layouts(since_version)]
    return CardLayoutsResponse(layout_version=repos.cards_current_version(), templates=templates)
