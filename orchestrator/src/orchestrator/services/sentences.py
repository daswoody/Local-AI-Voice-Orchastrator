"""Text in Saetze teilen - fuer Engines, die nur ganze Aeusserungen erzeugen
(audio.cpp, v1.21). Gleiche Regeln wie im Engine-Kit (tts-engine-kit/text.py).

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


# ---- Saetze aus dem LLM-Stream (v1.26) ----------------------------------------
#
# Das LLM liefert die Antwort Token fuer Token. Fuer die Sprachausgabe sind
# einzelne Tokens zu wenig (Betonung, Overhead je Aufruf), also wird
# gepuffert und an sinnvollen Grenzen geschnitten: am Satzende, an einem
# Zeilenumbruch und - nur fuer das ERSTE Stueck - schon am ersten Komma
# nach ein paar Woertern, damit der erste Ton frueh kommt. Ein Satzende
# zaehlt erst, wenn danach Leerraum folgt ("3.5" ist keins) und das Wort
# davor keine Zahl ("am 3. Oktober") und keine Abkuerzung ("z. B.", "Dr.")
# ist.

_STREAM_BREAK = re.compile(r"[.!?…]+[\"'»«“”„)]*(?=\s)|\n")
_STREAM_SOFT = re.compile(r"[,;:](?=\s)")
_ABBREVIATIONS = {
    "bzw", "ca", "usw", "etc", "evtl", "ggf", "inkl", "exkl", "nr", "dr", "prof", "hr", "fr",
    "str", "vgl", "sog", "max", "min", "mio", "mrd", "tel", "jh", "jhd", "bspw", "zzgl",
    "abs", "ziff", "st", "mr", "mrs", "vs", "u", "o", "a", "z", "d", "s",
}
_THINK = re.compile(r"<think>.*?</think>", re.S)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BULLET = re.compile(r"^\s*(?:[-*•+]|\d+[.)])\s+", re.M)
_EMPHASIS = re.compile(r"[*_`~]+")
_MARKUP = re.compile(r"[#>|]+")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")


def clean_for_speech(text: str) -> str:
    """Was das LLM schreibt, aber nicht vorgelesen werden soll: Markdown,
    Aufzaehlungszeichen, Link-Ziele, Emojis, Denk-Bloecke."""
    text = _THINK.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _BULLET.sub("", text)
    text = _EMPHASIS.sub("", text)
    text = _MARKUP.sub(" ", text)
    text = _EMOJI.sub("", text)
    return " ".join(text.split())


class SentenceStream:
    """Deltas rein (feed), fertige Sprech-Stuecke raus. flush() am Ende
    gibt den Rest. Stuecke unter min_chars wandern zum naechsten Satz
    ("Ja. Das mache ich." ist EIN Aufruf), ueber max_chars wird an Komma
    oder Leerzeichen geteilt."""

    def __init__(self, first_min_words: int = 6, min_chars: int = 20, max_chars: int = 250) -> None:
        self.first_min_words = first_min_words
        self.min_chars = min_chars
        self.max_chars = max_chars
        self._buffer = ""
        self._first = True

    def feed(self, text: str) -> list[str]:
        self._buffer += text
        pieces: list[str] = []
        while (cut := self._find_cut()) is not None:
            piece = clean_for_speech(self._buffer[:cut])
            self._buffer = self._buffer[cut:]
            if piece:
                pieces.append(piece)
                self._first = False
        return pieces

    def flush(self) -> list[str]:
        """Rest am Ende einer LLM-Runde (auch ohne Satzzeichen)."""
        rest = self._buffer
        self._buffer = ""
        if "<think>" in rest and "</think>" not in rest:
            # Abgebrochener Denk-Block - nichts davon vorlesen.
            rest = rest.split("<think>", 1)[0]
        piece = clean_for_speech(rest)
        if not piece:
            return []
        self._first = False
        return _limit(piece, self.max_chars)

    def _find_cut(self) -> int | None:
        buffer = _THINK.sub(" ", self._buffer)
        if buffer != self._buffer:
            self._buffer = buffer
        if "<think>" in buffer:
            # Offener Denk-Block: warten, bis er zu ist (flush verwirft ihn).
            buffer = buffer.split("<think>", 1)[0]
            if not buffer.strip():
                return None
        cut = self._sentence_cut(buffer)
        if self._first:
            soft = self._early_cut(buffer)
            if soft is not None and (cut is None or soft < cut):
                cut = soft
        if cut is None and len(buffer) > self.max_chars:
            cut = self._overflow_cut(buffer)
        return cut

    def _sentence_cut(self, buffer: str) -> int | None:
        for match in _STREAM_BREAK.finditer(buffer):
            end = match.end()
            if match.group() == "\n":
                if clean_for_speech(buffer[:end]):
                    return end
                continue
            if match.group().startswith(".") and _no_sentence_end(buffer[: match.start()]):
                continue
            if len(clean_for_speech(buffer[:end])) >= self.min_chars:
                return end
        return None

    def _early_cut(self, buffer: str) -> int | None:
        for match in _STREAM_SOFT.finditer(buffer):
            if len(buffer[: match.start()].split()) >= self.first_min_words:
                return match.end()
        return None

    def _overflow_cut(self, buffer: str) -> int:
        head = buffer[: self.max_chars]
        soft = [m.end() for m in _STREAM_SOFT.finditer(head)]
        if soft:
            return soft[-1]
        space = head.rfind(" ")
        return space if space > 0 else self.max_chars


def _no_sentence_end(before: str) -> bool:
    """Ist der Punkt am Ende von `before` KEIN Satzende? Ordinalzahl
    ("3."), Einzelbuchstabe ("z.", "B."), Abkuerzung ("Dr.", "bzw.") oder
    Punkt im Wort ("z.B.", "u.a.")."""
    words = before.split()
    if not words:
        return True
    word = words[-1].lstrip("(\"'„“»«")
    return word.isdigit() or len(word) == 1 or "." in word or word.lower() in _ABBREVIATIONS
