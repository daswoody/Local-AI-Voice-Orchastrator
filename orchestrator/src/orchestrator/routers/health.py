from fastapi import APIRouter

router = APIRouter()


@router.get("/v1/health")
def health() -> dict[str, str]:
    # Form laut docs/PROTOCOL.md - der Server-Auswahl-Screen der App prueft
    # diesen Endpoint.
    return {"status": "ok", "name": "heim-ai-orchestrator", "version": "0.1.0"}
