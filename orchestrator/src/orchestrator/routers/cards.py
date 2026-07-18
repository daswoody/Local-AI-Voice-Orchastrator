from fastapi import APIRouter

from .. import repos
from ..schemas import CardLayout, CardLayoutsResponse

router = APIRouter()


@router.get("/v1/cards/layouts", response_model=CardLayoutsResponse)
def get_layouts(since_version: int = 0) -> CardLayoutsResponse:
    """Layouts DB-gestuetzt (1.7b); Response-Feldnamen laut
    docs/PROTOCOL.md: version + layouts."""
    layouts = [CardLayout(**entry) for entry in repos.list_card_layouts(since_version)]
    return CardLayoutsResponse(version=repos.cards_current_version(), layouts=layouts)
