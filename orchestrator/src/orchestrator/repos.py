"""CRUD-Funktionen ueber der SQLite-DB (1.7b/1.7d).

Eine Funktion = eine Query; Rueckgaben sind dicts (JSON-nah), damit die
Router sie direkt ausliefern koennen."""

import datetime as dt
import json
import sqlite3
import uuid
from typing import Any

from .db import db_session


def _utc_now() -> str:
    # UTC-First (4.10): ISO 8601 mit Z-Suffix, sekundengenau reicht.
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# ---- Users -------------------------------------------------------------------


def _user_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "tier": row["tier"],
        "system_prompt_override": row["system_prompt_override"],
        "default_voice_id": row["default_voice_id"],
    }


def list_users() -> list[dict]:
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [_user_row_to_dict(r) for r in rows]


def get_user_by_username(username: str) -> dict | None:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return _user_row_to_dict(row) if row else None


def get_user_password_hash(username: str) -> str | None:
    with db_session() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()
        return row["password_hash"] if row else None


def create_user(username: str, password_hash: str, display_name: str, tier: int,
                system_prompt_override: str | None, default_voice_id: str | None) -> dict:
    with db_session() as conn:
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, display_name, tier,"
            " system_prompt_override, default_voice_id) VALUES (?, ?, ?, ?, ?, ?)",
            (username, password_hash, display_name, tier, system_prompt_override, default_voice_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _user_row_to_dict(row)


def update_user(user_id: int, fields: dict[str, Any]) -> dict | None:
    allowed = {"display_name", "tier", "system_prompt_override", "default_voice_id", "password_hash"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    with db_session() as conn:
        if updates:
            assignments = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE users SET {assignments} WHERE id = ?", (*updates.values(), user_id))
            conn.commit()
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _user_row_to_dict(row) if row else None


def delete_user(user_id: int) -> bool:
    with db_session() as conn:
        # Den letzten Tier-3-User nicht loeschen lassen - sonst sperrt man
        # sich selbst aus dem Admin-Panel aus.
        row = conn.execute("SELECT tier FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            return False
        if row["tier"] >= 3:
            admins = conn.execute("SELECT COUNT(*) FROM users WHERE tier >= 3").fetchone()[0]
            if admins <= 1:
                raise ValueError("letzter Admin kann nicht geloescht werden")
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        return True


# ---- Charakter / App-Settings --------------------------------------------------


def get_setting(key: str) -> str | None:
    with db_session() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with db_session() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def effective_system_prompt(username: str | None) -> str:
    """Charakter-Logik aus 4.14: globaler Prompt, pro User ueberschreibbar."""
    if username:
        user = get_user_by_username(username)
        if user and user["system_prompt_override"]:
            return user["system_prompt_override"]
    return get_setting("character_prompt") or "Du bist eine hilfreiche, deutschsprachige Heim-Assistenz."


def voice_summary_prompt() -> str:
    """System-Prompt fuer die Sprach-Kurzfassung (Admin > Charakter)."""
    from .db import DEFAULT_VOICE_SUMMARY_PROMPT

    return get_setting("voice_summary_prompt") or DEFAULT_VOICE_SUMMARY_PROMPT


# ---- Voices --------------------------------------------------------------------


def list_voices() -> list[dict]:
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM voices ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def get_voice(voice_id: str) -> dict | None:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM voices WHERE id = ?", (voice_id,)).fetchone()
        return dict(row) if row else None


def create_voice(voice_id: str, name: str, language: str) -> dict:
    with db_session() as conn:
        conn.execute(
            "INSERT INTO voices (id, name, language) VALUES (?, ?, ?)", (voice_id, name, language)
        )
        conn.commit()
    return {"id": voice_id, "name": name, "language": language}


def delete_voice(voice_id: str) -> bool:
    with db_session() as conn:
        cursor = conn.execute("DELETE FROM voices WHERE id = ?", (voice_id,))
        conn.commit()
        return cursor.rowcount > 0


# ---- Filler-Trigger -------------------------------------------------------------


def list_triggers() -> list[dict]:
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM filler_triggers ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def create_trigger(name: str, kind: str, tool_pattern: str | None) -> dict:
    with db_session() as conn:
        cursor = conn.execute(
            "INSERT INTO filler_triggers (name, kind, tool_pattern) VALUES (?, ?, ?)",
            (name, kind, tool_pattern),
        )
        conn.commit()
        return {"id": cursor.lastrowid, "name": name, "kind": kind, "tool_pattern": tool_pattern}


def update_trigger(trigger_id: int, name: str, kind: str, tool_pattern: str | None) -> dict | None:
    with db_session() as conn:
        cursor = conn.execute(
            "UPDATE filler_triggers SET name = ?, kind = ?, tool_pattern = ? WHERE id = ?",
            (name, kind, tool_pattern, trigger_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
        return {"id": trigger_id, "name": name, "kind": kind, "tool_pattern": tool_pattern}


def delete_trigger(trigger_id: int) -> bool:
    with db_session() as conn:
        # ON DELETE CASCADE raeumt die zugehoerigen Filler mit ab.
        cursor = conn.execute("DELETE FROM filler_triggers WHERE id = ?", (trigger_id,))
        conn.commit()
        return cursor.rowcount > 0


# ---- Fillers --------------------------------------------------------------------


def list_fillers() -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT f.*, t.name AS trigger_name, t.kind AS trigger_kind, t.tool_pattern"
            " FROM fillers f JOIN filler_triggers t ON t.id = f.trigger_id ORDER BY f.id"
        ).fetchall()
        return [dict(r) for r in rows]


def get_filler(filler_id: int) -> dict | None:
    with db_session() as conn:
        row = conn.execute(
            "SELECT f.*, t.kind AS trigger_kind, t.tool_pattern FROM fillers f"
            " JOIN filler_triggers t ON t.id = f.trigger_id WHERE f.id = ?",
            (filler_id,),
        ).fetchone()
        return dict(row) if row else None


def fillers_for_kind(kind: str) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT f.*, t.kind AS trigger_kind, t.tool_pattern FROM fillers f"
            " JOIN filler_triggers t ON t.id = f.trigger_id"
            " WHERE f.enabled = 1 AND t.kind = ?",
            (kind,),
        ).fetchall()
        return [dict(r) for r in rows]


def create_filler(title: str, text: str, trigger_id: int, enabled: bool,
                  delay_ms: int = 1200) -> dict:
    with db_session() as conn:
        cursor = conn.execute(
            "INSERT INTO fillers (title, text, trigger_id, enabled, delay_ms)"
            " VALUES (?, ?, ?, ?, ?)",
            (title, text, trigger_id, int(enabled), delay_ms),
        )
        conn.commit()
        return get_filler(cursor.lastrowid)


def update_filler(filler_id: int, title: str, text: str, trigger_id: int, enabled: bool,
                  delay_ms: int = 1200) -> dict | None:
    with db_session() as conn:
        cursor = conn.execute(
            "UPDATE fillers SET title = ?, text = ?, trigger_id = ?, enabled = ?,"
            " delay_ms = ? WHERE id = ?",
            (title, text, trigger_id, int(enabled), delay_ms, filler_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
    return get_filler(filler_id)


def delete_filler(filler_id: int) -> bool:
    with db_session() as conn:
        cursor = conn.execute("DELETE FROM fillers WHERE id = ?", (filler_id,))
        conn.commit()
        return cursor.rowcount > 0


# ---- Conversations (zentrale Chat-Historie, Phase 2.5) ---------------------------


def create_conversation(username: str | None, device_name: str, title: str) -> dict:
    now = _utc_now()
    conversation = {
        "id": uuid.uuid4().hex,
        "username": username,
        "device_name": device_name,
        "title": title[:80],
        "created_at": now,
        "updated_at": now,
    }
    with db_session() as conn:
        conn.execute(
            "INSERT INTO conversations (id, username, device_name, title, created_at, updated_at)"
            " VALUES (:id, :username, :device_name, :title, :created_at, :updated_at)",
            conversation,
        )
        conn.commit()
    return conversation


def get_conversation(conversation_id: str) -> dict | None:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        return dict(row) if row else None


def list_conversations(username: str) -> list[dict]:
    """Nur eigene Gespraeche: Historie ist privat (4.4), auch Tier 3 sieht
    hier nicht die Gespraeche anderer User."""
    with db_session() as conn:
        rows = conn.execute(
            "SELECT c.*, COUNT(m.id) AS message_count FROM conversations c"
            " LEFT JOIN messages m ON m.conversation_id = c.id"
            " WHERE c.username = ? GROUP BY c.id ORDER BY c.updated_at DESC",
            (username,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_conversation(conversation_id: str) -> bool:
    with db_session() as conn:
        # ON DELETE CASCADE raeumt die messages mit ab.
        cursor = conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        conn.commit()
        return cursor.rowcount > 0


def append_message(
    conversation_id: str,
    role: str,
    content: str,
    cards: list[dict] | None = None,
    has_image: bool = False,
) -> dict:
    now = _utc_now()
    with db_session() as conn:
        cursor = conn.execute(
            "INSERT INTO messages (conversation_id, role, content, cards_json, has_image, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                role,
                content,
                json.dumps(cards, ensure_ascii=False) if cards else None,
                int(has_image),
                now,
            ),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
        )
        conn.commit()
        message_id = cursor.lastrowid
    return {"id": message_id, "role": role, "content": content,
            "cards": cards or [], "has_image": has_image, "created_at": now}


def list_messages(conversation_id: str) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conversation_id,)
        ).fetchall()
        return [
            {
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "cards": json.loads(r["cards_json"]) if r["cards_json"] else [],
                "has_image": bool(r["has_image"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]


# ---- Card-Layouts ----------------------------------------------------------------


def cards_current_version() -> int:
    with db_session() as conn:
        row = conn.execute("SELECT MAX(layout_version) AS v FROM card_layouts").fetchone()
        return row["v"] or 0


def list_card_layouts(since_version: int = 0) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM card_layouts WHERE layout_version > ? ORDER BY card_type",
            (since_version,),
        ).fetchall()
        return [
            {"card_type": r["card_type"], "layout_version": r["layout_version"],
             "root": json.loads(r["root_json"])}
            for r in rows
        ]


def upsert_card_layout(card_type: str, root: dict) -> dict:
    with db_session() as conn:
        # Globale Versionsnummer (4.12): jede Aenderung zaehlt hoch und
        # stempelt das Template - Grundlage fuer den since_version-Poll.
        version = (conn.execute("SELECT MAX(layout_version) AS v FROM card_layouts").fetchone()["v"] or 0) + 1
        conn.execute(
            "INSERT INTO card_layouts (card_type, layout_version, root_json) VALUES (?, ?, ?)"
            " ON CONFLICT(card_type) DO UPDATE SET layout_version = excluded.layout_version,"
            " root_json = excluded.root_json",
            (card_type, version, json.dumps(root)),
        )
        conn.commit()
        return {"card_type": card_type, "layout_version": version, "root": root}


def delete_card_layout(card_type: str) -> bool:
    with db_session() as conn:
        cursor = conn.execute("DELETE FROM card_layouts WHERE card_type = ?", (card_type,))
        conn.commit()
        return cursor.rowcount > 0
