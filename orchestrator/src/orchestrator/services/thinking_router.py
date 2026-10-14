"""Denk-Router (v1.27): entscheidet pro Turn, ob das LLM nachdenken darf.

Gemma 4 denkt in LM Studio standardmaessig vor jeder Antwort - selbst
"Was ist 17*23?" kostet dann ~300 Denk-Token und ~10 s, ohne Denken
~0,5 s. Fuer Sprache ist das zu langsam, fuer schwierige Fragen ist
Denken aber deutlich besser. Der Router laesst das Modell deshalb nur
denken, wenn die Frage danach aussieht; alles andere geht mit
`think=False` (= reasoning_effort "none", siehe LiteLLMClient) raus.

Bewusst lokale Regeln statt eines externen Klassifikators (z. B. JEV):
kein Netz-Umweg, keine Nutzerfragen an Dritte, ~0 ms. Die Entscheidung
wird pro Turn geloggt, damit sich die Treffsicherheit beurteilen laesst.
"""

import re
from dataclasses import dataclass

from .. import repos
from ..config import settings

# auto = Router entscheidet, on = immer denken (Modell-Default),
# off = nie denken.
MODES = ("auto", "on", "off")
MODE_SETTING = "thinking_mode"

# Ab so vielen Woertern gilt eine Anfrage als komplex genug zum Nachdenken.
LONG_REQUEST_WORDS = 35

# Nutzer verlangt es ausdruecklich ("denk mal genau nach", "ueberleg gut").
_EXPLICIT = re.compile(
    r"\bdenk\w*\s+(?:\w+\s+){0,3}?nach\b|\b(?:über|ueber)leg|\bgr(?:ü|ue)ndlich"
)

# Stichworte fuer Fragen, bei denen Denken die Antwort spuerbar verbessert:
# Begruendungen, Vergleiche, Planung, Rechnen, Code, Abwaegungen.
_REASONING = re.compile(
    r"\b(?:"
    r"warum|wieso|weshalb|"
    r"erkl(?:ä|ae)r\w*|begr(?:ü|ue)nd\w*|herleit\w*|beweis\w*|"
    r"vergleich\w*|unterschied\w*|vor-? und nachteil\w*|vorteile|nachteile|"
    r"analysier\w*|bewert\w*|abw(?:ä|ae)g\w*|"
    r"plan(?:e|en|ung)|optimier\w*|strategie\w*|"
    r"berechn\w*|ausrechn\w*|rechne\w*|"
    r"schritt f(?:ü|ue)r schritt|was w(?:ä|ae)re,? wenn|"
    r"l(?:ö|oe)s(?:e|en|ung)|r(?:ä|ae)tsel\w*|logik\w*|"
    r"programmier\w*|code|skript\w*|script\w*|debug\w*|"
    r"empf(?:ie|eh)l\w*|soll ich"
    r")\b"
)

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_OPERATOR = re.compile(r"[+*/×÷^]|\b(?:mal|geteilt|plus|minus|hoch)\b")
_HARD_MATH = re.compile(r"prozent|wurzel|\bquadrat|\bzins")


@dataclass(frozen=True)
class ThinkingDecision:
    think: bool
    reason: str


def current_mode() -> str:
    """Im Admin-Panel gewaehlter Modus, Fallback auf die .env. Pro Turn
    aufgeloest, damit ein Wechsel sofort greift."""
    try:
        stored = repos.get_setting(MODE_SETTING)
    except Exception:
        # DB (noch) nicht initialisiert -> .env-Default statt Crash.
        stored = None
    mode = stored or settings.thinking_mode
    return mode if mode in MODES else "auto"


def decide(text: str, has_image: bool = False, mode: str | None = None) -> ThinkingDecision:
    mode = mode or current_mode()
    if mode == "on":
        return ThinkingDecision(True, "Admin: immer an")
    if mode == "off":
        return ThinkingDecision(False, "Admin: immer aus")
    return classify(text, has_image)


def classify(text: str, has_image: bool = False) -> ThinkingDecision:
    lowered = text.lower()
    if _EXPLICIT.search(lowered):
        return ThinkingDecision(True, "ausdruecklich verlangt")
    if has_image:
        return ThinkingDecision(True, "Bild-Eingabe")
    keyword = _REASONING.search(lowered)
    if keyword:
        return ThinkingDecision(True, f"Stichwort '{keyword.group(0)}'")
    if _is_hard_math(lowered):
        return ThinkingDecision(True, "Rechenaufgabe")
    words = len(lowered.split())
    if words >= LONG_REQUEST_WORDS:
        return ThinkingDecision(True, f"lange Anfrage ({words} Woerter)")
    return ThinkingDecision(False, "kurze/alltaegliche Anfrage")


def _is_hard_math(text: str) -> bool:
    """Einfaches Kopfrechnen ("17 mal 23") schafft das Modell auch ohne
    Denken in ~0,5 s - erst mehrere Rechenschritte, grosse Zahlen oder
    Prozent/Wurzel lohnen die Denkzeit."""
    numbers = _NUMBER.findall(text)
    if not numbers:
        return False
    if _HARD_MATH.search(text):
        return True
    operators = len(_OPERATOR.findall(text))
    if operators == 0:
        return False
    digits = max(len(re.sub(r"\D", "", number)) for number in numbers)
    return operators >= 2 or digits >= 3
