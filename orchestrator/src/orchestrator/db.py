"""SQLite-Anbindung (Mikro-Phase 1.7b).

Bewusst stdlib-sqlite3 statt ORM: Die Datenmengen sind winzig (Handvoll
User/Stimmen/Filler), und ohne ORM bleibt jede Query im Lernprojekt
nachvollziehbar. Verbindung pro Zugriff (open/close) reicht bei dieser
Last voellig."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    tier INTEGER NOT NULL DEFAULT 1,
    system_prompt_override TEXT,
    default_voice_id TEXT
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS voices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'de'
);

CREATE TABLE IF NOT EXISTS filler_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    -- thinking = LLM braucht lange; search = RAG/Web-Suche laeuft;
    -- tool = Tool-Aufruf, optional eingeschraenkt per tool_pattern
    -- (fnmatch, z. B. "Calendar-*") - Grundlage fuer 1.12.
    kind TEXT NOT NULL CHECK (kind IN ('thinking', 'search', 'tool')),
    tool_pattern TEXT
);

CREATE TABLE IF NOT EXISTS fillers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    trigger_id INTEGER NOT NULL REFERENCES filler_triggers(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS card_layouts (
    card_type TEXT PRIMARY KEY,
    layout_version INTEGER NOT NULL,
    root_json TEXT NOT NULL
);

-- Zentrale Chat-Historie (Phase 2.5): der Server besitzt die Gespraeche,
-- alle Clients (Windows, Android, Browser, spaeter Satelliten) sind nur
-- Ansichten darauf. username NULL = Gast-/Geraete-Session ohne Login
-- (Satellit); solche Gespraeche sind ueber die REST-Liste nicht abrufbar,
-- werden aber fuer Retention/RAG serverseitig festgehalten.
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    username TEXT,
    device_name TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    -- Karten des Turns als JSON-Liste von CardEnvelopes (4.12); die
    -- Historie kann sie damit identisch zur Live-Ansicht rendern.
    cards_json TEXT,
    -- Bild-Eingaben (Screenshots) werden aus Platzgruenden NICHT in der
    -- Historie gespeichert, nur markiert.
    has_image INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);
"""

_DEFAULT_CHARACTER_PROMPT = "Du bist eine hilfreiche, deutschsprachige Heim-Assistenz."

# Default fuer den Sprach-Kurzfassungs-Prompt (Admin > Charakter, editierbar).
# Wichtig: XTTS liest Text WOERTLICH vor - Emotions-Tags wie [froehlich],
# Emojis oder Markdown wuerden mitgesprochen, daher verbietet der Default sie.
DEFAULT_VOICE_SUMMARY_PROMPT = (
    "Fasse die folgende Assistenz-Antwort fuer eine Sprachausgabe zusammen: "
    "maximal zwei kurze, natuerlich gesprochene Saetze auf Deutsch. "
    "Keine Aufzaehlungen, keine Formatierung, keine Emojis und keine "
    "Regieanweisungen oder Emotions-Tags wie [froehlich] - der Text wird "
    "woertlich vorgelesen. Verweise bei Bedarf darauf, dass die Details im "
    "Chat stehen."
)

_CARDS_SEED_FILE = Path(__file__).resolve().parent / "data" / "default_card_layouts.json"


def connect() -> sqlite3.Connection:
    path = Path(settings.database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_session():
    """Verbindung pro Zugriff, garantiert geschlossen. Commits macht der
    Aufrufer explizit - sqlite3-Connections als with-Block wuerden zwar
    committen, aber NICHT schliessen (haeufiger Stolperstein)."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Schema anlegen und leere Tabellen mit Startdaten fuellen (idempotent)."""
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        _seed_admin_user(conn)
        _seed_character(conn)
        _seed_voices(conn)
        _seed_triggers_and_fillers(conn)
        _seed_card_layouts(conn)
        conn.commit()
    finally:
        conn.close()


def _seed_admin_user(conn: sqlite3.Connection) -> None:
    # Erst-Einrichtung: der Admin aus der .env, damit man sich ins
    # Admin-Panel einloggen und weitere User anlegen kann (4.4: Tier 3).
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return
    from .security import hash_password

    conn.execute(
        "INSERT INTO users (username, password_hash, display_name, tier) VALUES (?, ?, ?, 3)",
        (settings.admin_username, hash_password(settings.admin_password), "Admin"),
    )


def _seed_character(conn: sqlite3.Connection) -> None:
    # INSERT OR IGNORE laeuft bei jedem Start - so bekommen auch bestehende
    # Datenbanken neue Settings-Keys, ohne Admin-Aenderungen zu ueberschreiben.
    conn.execute(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('character_prompt', ?)",
        (_DEFAULT_CHARACTER_PROMPT,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('voice_summary_prompt', ?)",
        (DEFAULT_VOICE_SUMMARY_PROMPT,),
    )


def _seed_voices(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM voices").fetchone()[0]:
        return
    conn.executemany(
        "INSERT INTO voices (id, name, language) VALUES (?, ?, 'de')",
        [
            ("default-de-female", "Standard (weiblich, DE)"),
            ("default-de-male", "Standard (maennlich, DE)"),
        ],
    )


def _seed_triggers_and_fillers(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM filler_triggers").fetchone()[0]:
        return
    # Die zwei Kategorien aus 4.3 plus ein generischer Tool-Trigger;
    # weitere legt der Admin im Panel an (z. B. Kalender-spezifisch).
    triggers = [
        ("Nachdenken", "thinking", None),
        ("Suche/RAG", "search", None),
        ("Tool-Aufruf (allgemein)", "tool", "*"),
    ]
    fillers = {
        "Nachdenken": [("Kurz nachdenken", "Lass mich kurz nachdenken."),
                       ("Augenblick", "Hm, einen Augenblick bitte.")],
        "Suche/RAG": [("Suche laeuft", "Ich schaue kurz nach.")],
        "Tool-Aufruf (allgemein)": [("Ich kuemmere mich", "Moment, ich kuemmere mich darum.")],
    }
    for name, kind, pattern in triggers:
        cursor = conn.execute(
            "INSERT INTO filler_triggers (name, kind, tool_pattern) VALUES (?, ?, ?)",
            (name, kind, pattern),
        )
        for title, text in fillers.get(name, []):
            conn.execute(
                "INSERT INTO fillers (title, text, trigger_id) VALUES (?, ?, ?)",
                (title, text, cursor.lastrowid),
            )


def _seed_card_layouts(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM card_layouts").fetchone()[0]:
        return
    raw = json.loads(_CARDS_SEED_FILE.read_text(encoding="utf-8"))
    conn.executemany(
        "INSERT INTO card_layouts (card_type, layout_version, root_json) VALUES (?, ?, ?)",
        [
            (entry["card_type"], entry["layout_version"], json.dumps(entry["root"]))
            for entry in raw["templates"]
        ],
    )
