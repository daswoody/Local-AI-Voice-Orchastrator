import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..graph import orchestrator_graph
from ..security import decode_access_token

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/v1/assistant/stream")
async def assistant_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    tier = _resolve_tier(websocket)
    try:
        while True:
            frame = await websocket.receive_json()
            frame_type = frame.get("type")

            if frame_type == "hello":
                # Mode/voice_id/Geraete-Tool-Manifest werden erst ab 1.11/1.12
                # ausgewertet - hier reicht die Verbindungsbestaetigung.
                continue

            if frame_type == "text_input":
                await _handle_text_input(websocket, frame.get("text", ""), tier)
                continue

            if frame_type == "interrupt":
                # Barge-in: ohne laufenden Audio-Stream (erst ab 1.11) gibt es
                # hier noch nichts abzubrechen.
                continue

            await websocket.send_json({"type": "error", "message": f"unbekannter frame type: {frame_type}"})
    except WebSocketDisconnect:
        logger.info("Client hat die WebSocket-Verbindung getrennt")


def _resolve_tier(websocket: WebSocket) -> int:
    # Tier kommt ueber das Login-Token (4.4); ohne/mit ungueltigem Token bis
    # 1.7b bewusst Fallback auf Tier 1 (Gast) statt eines harten Fehlers.
    token = websocket.query_params.get("token")
    if not token:
        return 1
    try:
        payload = decode_access_token(token)
        return int(payload.get("tier", 1))
    except Exception:
        return 1


async def _handle_text_input(websocket: WebSocket, text: str, tier: int) -> None:
    try:
        result = await orchestrator_graph.ainvoke(
            {"text": text, "tier": tier, "context_chunks": [], "response": ""}
        )
    except Exception:
        logger.exception("Orchestrator-Graph fehlgeschlagen")
        await websocket.send_json({"type": "error", "message": "interner Fehler bei der Antwortgenerierung"})
        return
    await websocket.send_json({"type": "assistant_text", "text": result["response"], "final": True})
    await websocket.send_json({"type": "done"})
