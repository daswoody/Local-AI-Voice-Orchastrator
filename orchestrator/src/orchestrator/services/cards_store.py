import json
import threading
from pathlib import Path

from ..schemas import CardLayout

_DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "default_card_layouts.json"


class CardLayoutStore:
    """Haelt die Karten-Layouts aus 4.12 im Speicher. Jede Aenderung zaehlt die
    globale Versionsnummer hoch und stempelt das betroffene Template damit -
    das ist die Grundlage fuer den `since_version`-Poll-Mechanismus der App."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._templates: dict[str, CardLayout] = {}
        self._version = 0
        self._load_defaults()

    def _load_defaults(self) -> None:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
        for entry in raw["templates"]:
            template = CardLayout.model_validate(entry)
            self._templates[template.card_type] = template
            self._version = max(self._version, template.layout_version)

    def current_version(self) -> int:
        return self._version

    def list_since(self, since_version: int) -> list[CardLayout]:
        return [t for t in self._templates.values() if t.layout_version > since_version]

    def list_all(self) -> list[CardLayout]:
        return list(self._templates.values())

    def upsert(self, card_type: str, root: dict) -> CardLayout:
        with self._lock:
            self._version += 1
            template = CardLayout(card_type=card_type, layout_version=self._version, root=root)
            self._templates[card_type] = template
            return template

    def delete(self, card_type: str) -> bool:
        with self._lock:
            if card_type not in self._templates:
                return False
            del self._templates[card_type]
            self._version += 1
            return True


card_layout_store = CardLayoutStore()
