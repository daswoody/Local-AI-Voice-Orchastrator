import threading
import time

import numpy as np

from .config import settings


class WhisperEngine:
    """Kapselt faster-whisper hinter einer schmalen Schnittstelle.

    Der Import und das Laden des Modells passieren lazy beim ersten Aufruf
    (bzw. per preload beim Start): So laufen die Tests ohne installiertes
    faster-whisper, und der Container startet schnell, waehrend das Modell
    im Hintergrund des ersten Requests geladen wird."""

    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                settings.whisper_model,
                device=settings.whisper_device,
                compute_type=settings.whisper_compute_type,
                download_root=settings.models_dir,
            )

    def transcribe(self, pcm16: bytes) -> dict:
        """Erwartet PCM16 mono mit 16 kHz (Whisper-Eingabeformat laut 4.1)."""
        self.load()
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0

        started = time.monotonic()
        segments, info = self._model.transcribe(
            audio,
            language=settings.language,
            beam_size=5,
            vad_filter=True,
        )
        text = "".join(segment.text for segment in segments).strip()
        processing_ms = int((time.monotonic() - started) * 1000)

        return {
            "text": text,
            "language": info.language,
            "audio_duration_ms": int(info.duration * 1000),
            "processing_ms": processing_ms,
        }


engine = WhisperEngine()
