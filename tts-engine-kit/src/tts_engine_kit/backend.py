"""Was eine Engine liefern muss (Engine-Vertrag v1, Heim-AI v1.20).

Eine neue Sprachausgabe = eine Backend-Klasse + ein Dockerfile. HTTP-API,
Laden/Entladen, Warteschlange und Referenz-Cache liefert das Kit
(tts_engine_kit.app). Das Modell selbst laeuft in einem Unterprozess, den
das Kit zum Entladen einfach beendet - so ist das VRAM danach komplett frei,
auch der CUDA-Kontext, den ein Python-Prozess sonst bis zum Ende behaelt."""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EngineInfo:
    """Steckbrief fuer den Orchestrator (GET /v1/info)."""

    name: str
    # ISO-639-1-Codes, z. B. ("de", "en") - die Sprache kommt pro Request mit.
    languages: tuple[str, ...]
    sample_rate: int
    # Ohne Voice-Sample keine Stimme (reine Klon-Modelle)?
    needs_sample: bool = True
    # Verbessert ein exaktes Transkript des Samples den Klon?
    uses_transcript: bool = False
    # Versteht eine Sprechanweisung ("instruction")?
    instructions: bool = False
    # "sentence" = Audio kommt Satz fuer Satz, "chunk" = echtes Streaming
    streaming: str = "sentence"
    vram_mb: int | None = None
    description: str = ""
    license: str = ""


@dataclass(frozen=True)
class Voice:
    """Referenz fuers Voice-Cloning, wie sie im Request ankam."""

    # Gleich fuer dasselbe Sample + Transkript: Schluessel des Referenz-Caches.
    key: str
    # WAV-Bytes (der Orchestrator schickt mono PCM16 mit 24 kHz)
    wav: bytes
    transcript: str | None


class Backend(ABC):
    """info() und prepare() ruft der Hauptprozess auf - dort also keine
    schweren Imports (torch & Co. erst in load()). Alles andere laeuft im
    Modell-Unterprozess, immer nur ein Aufruf zur Zeit."""

    @classmethod
    @abstractmethod
    def info(cls) -> EngineInfo:
        """Steckbrief der Engine."""

    @classmethod
    def prepare(cls) -> dict:
        """Einmal beim Containerstart, im Hauptprozess: z. B. Gewichte ins
        Volume laden. Die Rueckgabe geht als Keyword-Argumente an den
        Konstruktor im Unterprozess."""
        return {}

    @abstractmethod
    def load(self, device: str) -> str:
        """Modell auf `device` laden ("cpu", "cuda", "cuda:N"). Rueckgabe:
        das tatsaechlich genutzte Device."""

    def prepare_voice(self, voice: Voice) -> Any:
        """Referenz vorberechnen (z. B. Klon-Prompt). Das Kit cacht das
        Ergebnis pro voice.key, solange das Modell geladen ist."""
        return voice

    @abstractmethod
    def synthesize(
        self, text: str, language: str | None, voice: Any | None, instruction: str | None
    ) -> Iterator[tuple[int, bytes]]:
        """Text -> (Samplerate, PCM16 mono)-Chunks. `voice` ist das Ergebnis
        von prepare_voice (oder None ohne Sample). ValueError = ungueltige
        Anfrage (HTTP 400), jede andere Ausnahme = Fehler der Engine (500)."""
