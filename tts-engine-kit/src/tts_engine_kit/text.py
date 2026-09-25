"""Text in Saetze teilen - fuer Engines, die nur ganze Aeusserungen erzeugen.

Satz fuer Satz statt am Stueck: Das erste Audio kommt nach dem ersten Satz,
nicht erst nach der ganzen Antwort. Zu kurze Stuecke ("z.", "3.") werden an
den naechsten Satz gehaengt, damit Abkuerzungen und Ordinalzahlen den Satz
nicht zerreissen; zu lange an Komma/Semikolon oder Leerzeichen geteilt."""

import re

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_SOFT_BREAK = re.compile(r"(?<=[,;:])\s+")


def split_sentences(text: str, max_chars: int = 250, min_chars: int = 20) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []
    merged: list[str] = []
    buffer = ""
    for part in _SENTENCE_END.split(text):
        buffer = f"{buffer} {part}".strip()
        if len(buffer) >= min_chars:
            merged.append(buffer)
            buffer = ""
    if buffer:
        if merged and len(buffer) < min_chars:
            merged[-1] = f"{merged[-1]} {buffer}"
        else:
            merged.append(buffer)

    result: list[str] = []
    for sentence in merged:
        result.extend(_limit(sentence, max_chars))
    return result


def _limit(sentence: str, max_chars: int) -> list[str]:
    if len(sentence) <= max_chars:
        return [sentence]
    pieces: list[str] = []
    current = ""
    for part in _SOFT_BREAK.split(sentence):
        candidate = f"{current} {part}".strip()
        if current and len(candidate) > max_chars:
            pieces.append(current)
            current = part
        else:
            current = candidate
    if current:
        pieces.append(current)
    # Immer noch zu lang (kein Komma weit und breit): an Wortgrenzen teilen.
    result: list[str] = []
    for piece in pieces:
        while len(piece) > max_chars:
            cut = piece.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            result.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            result.append(piece)
    return result
