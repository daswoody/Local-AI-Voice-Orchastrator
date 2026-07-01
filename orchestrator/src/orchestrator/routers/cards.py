from fastapi import APIRouter

from ..schemas import CardLayoutsResponse
from ..services.cards_store import card_layout_store

router = APIRouter()


@router.get("/v1/cards/layouts", response_model=CardLayoutsResponse)
def get_layouts(since_version: int = 0) -> CardLayoutsResponse:
    templates = card_layout_store.list_since(since_version)
    return CardLayoutsResponse(layout_version=card_layout_store.current_version(), templates=templates)
