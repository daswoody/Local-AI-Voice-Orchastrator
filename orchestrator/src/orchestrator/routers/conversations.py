"""Zentrale Chat-Historie (Phase 2.5).

Der Server besitzt die Gespraeche, Clients sind Ansichten: Windows-App,
Android-App und Browser sehen ueber diese Endpoints dieselbe Historie.
Gespeichert wird im Stream-Router (jeder Turn), gelesen wird hier.

Sichtbarkeit: strikt nur eigene Gespraeche (username aus dem Token).
Gast-Sessions ohne Login werden zwar persistiert (Retention/RAG,
Satelliten-Szenario), sind hier aber bewusst nicht abrufbar.
"""

from fastapi import APIRouter, Depends, HTTPException

from .. import repos
from ..security import require_user

router = APIRouter()


@router.get("/v1/conversations")
def list_conversations(user: dict = Depends(require_user)) -> dict:
    conversations = repos.list_conversations(user["sub"])
    return {
        "conversations": [
            {
                "id": c["id"],
                "title": c["title"],
                "device_name": c["device_name"],
                "message_count": c["message_count"],
                "created_at": c["created_at"],
                "updated_at": c["updated_at"],
            }
            for c in conversations
        ]
    }


def _owned_conversation(conversation_id: str, user: dict) -> dict:
    conversation = repos.get_conversation(conversation_id)
    # 404 statt 403 fuer fremde IDs: verraet nicht, welche IDs existieren.
    if conversation is None or conversation["username"] != user["sub"]:
        raise HTTPException(status_code=404, detail="Gespraech nicht gefunden")
    return conversation


@router.get("/v1/conversations/{conversation_id}")
def get_conversation(conversation_id: str, user: dict = Depends(require_user)) -> dict:
    conversation = _owned_conversation(conversation_id, user)
    return {
        "id": conversation["id"],
        "title": conversation["title"],
        "device_name": conversation["device_name"],
        "created_at": conversation["created_at"],
        "updated_at": conversation["updated_at"],
        "messages": repos.list_messages(conversation_id),
    }


@router.delete("/v1/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, user: dict = Depends(require_user)) -> dict:
    _owned_conversation(conversation_id, user)
    repos.delete_conversation(conversation_id)
    return {"ok": True}
